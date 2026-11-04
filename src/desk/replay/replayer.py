"""The replayer: one historical session fed to the same agents under a driven clock.

Minute bars come from the bars table and are published at their close, as the live watcher does;
the watcher's alert logic runs on them; the scheduled cycles run at their slot times; the simulated
broker fills at the next bar. A feed gap can be injected to prove the kill switch engages."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from desk.agents.base import BaseAgent
from desk.agents.watcher import WatcherConfig, WatcherState
from desk.bus.memory import InMemoryBus
from desk.core import calendar
from desk.core.clock import SimClock
from desk.core.events import Topics
from desk.core.payloads import MarketBar
from desk.replay.broker import SimulatedBroker
from desk.storage.db import Database


@dataclass
class ReplayReport:
    day: date
    bars_fed: int = 0
    minutes_skipped: int = 0
    cycles: list[dict[str, Any]] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    theses: list[dict[str, Any]] = field(default_factory=list)
    kill_switch: list[str] = field(default_factory=list)
    equity_start: Decimal = Decimal(0)
    equity_end: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    unrealized_pnl: Decimal = Decimal(0)
    costs: Decimal = Decimal(0)
    max_drawdown_pct: float = 0.0
    llm: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "bars_fed": self.bars_fed,
            "minutes_skipped": self.minutes_skipped,
            "cycles": self.cycles,
            "fills": self.fills,
            "theses": self.theses,
            "kill_switch": self.kill_switch,
            "equity_start": str(self.equity_start),
            "equity_end": str(self.equity_end),
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(self.unrealized_pnl),
            "costs": str(self.costs),
            "max_drawdown_pct": self.max_drawdown_pct,
            "llm": self.llm,
        }

    def render(self) -> str:
        lines = [
            f"REPLAY {self.day}: {self.bars_fed} bars fed"
            + (
                f", {self.minutes_skipped} minutes withheld (feed gap)"
                if self.minutes_skipped
                else ""
            ),
            (
                f"equity {self.equity_start:,.2f} -> {self.equity_end:,.2f} "
                f"(realized {self.realized_pnl:+,.2f}, unrealized {self.unrealized_pnl:+,.2f}, "
                f"costs {self.costs:,.2f}, max drawdown {self.max_drawdown_pct:.2f}%)"
            ),
        ]
        for c in self.cycles:
            lines.append(f"cycle {c['trigger']} at {c['at']}: {c['summary']}")
            for a in c.get("actions", []):
                if a.get("action") in ("buy", "sell"):
                    lines.append(
                        f"  {a['action']} {a.get('quantity')} {a['symbol']}: {a.get('note')}"
                    )
        for f in self.fills:
            lines.append(
                f"fill {f['at']} {f['side']} {f['qty']} {f['symbol']} at {f['price']} "
                f"(reference {f['reference']}, cost {f['cost']})"
            )
        for t in self.theses:
            lines.append(
                f"thesis {t['symbol']} {t['status']} entry={t.get('entry_price')} stop={t.get('stop_price')} "
                f"target={t.get('target_price')} pnl={t.get('realized_pnl')} {t.get('reason') or ''}"
            )
        for k in self.kill_switch:
            lines.append(f"kill switch: {k}")
        if self.llm:
            lines.append(f"llm cache: {self.llm}")
        return "\n".join(lines)


class Replayer(BaseAgent):
    name = "replayer"
    topics = ()

    def __init__(
        self,
        bus: InMemoryBus,
        clock: SimClock,
        db: Database,
        broker: SimulatedBroker,
        universe: list[str],
        cycle: Any,
        *,
        watcher_config: WatcherConfig | None = None,
        feed_gap: tuple[datetime, datetime] | None = None,
        settle_rounds: int = 4,
    ) -> None:
        super().__init__(bus, clock)
        self.sim = clock
        self.memory_bus = bus
        self.db = db
        self.broker = broker
        self.universe = universe
        self.cycle = cycle
        self.watcher_config = watcher_config or WatcherConfig()
        self.feed_gap = feed_gap
        self.settle_rounds = settle_rounds

    async def _settle(self) -> None:
        """Let every handler and timer that is due run before time moves on."""
        for _ in range(self.settle_rounds):
            await asyncio.sleep(0)
            await self.memory_bus.wait_idle()

    async def run_day(self, day: date, report: ReplayReport | None = None) -> ReplayReport:
        report = report or ReplayReport(day=day)
        open_at, close_at = calendar.session_bounds(day)
        slots = [
            (open_at - timedelta(minutes=60), "pre_market"),
            (open_at + timedelta(hours=3), "mid_session"),
            (close_at + timedelta(minutes=10), "post_close"),
        ]
        rows = await self.db.bars_between(self.universe, "1m", open_at, close_at)
        by_minute: dict[datetime, list[MarketBar]] = {}
        for r in rows:
            by_minute.setdefault(r["ts"], []).append(
                MarketBar(
                    symbol=r["symbol"],
                    interval="1m",
                    ts=r["ts"],
                    open=r["open"],
                    high=r["high"],
                    low=r["low"],
                    close=r["close"],
                    volume=int(r["volume"]),
                    vwap=r.get("vwap"),
                    trade_count=r.get("trade_count"),
                    source="replay",
                )
            )
        timeline: list[tuple[datetime, str, Any]] = [(at, "slot", name) for at, name in slots]
        timeline += [(ts + timedelta(minutes=1), "minute", ts) for ts in by_minute]
        timeline.sort(key=lambda x: x[0])

        state = WatcherState(self.watcher_config)
        state.started_at = open_at
        marks: list[Decimal] = []
        for at, kind, ref in timeline:
            if at < self.sim.now():
                continue
            self.sim.advance_to(at)
            await self._settle()
            if kind == "slot":
                result = await self.cycle.run(ref)
                report.cycles.append(
                    {
                        "trigger": ref,
                        "at": at.strftime("%H:%M"),
                        "summary": result.render().splitlines()[-1],
                        "actions": [a.as_dict() for a in result.actions],
                        "failed": result.failed,
                    }
                )
                await self._settle()
                continue
            ts = ref
            withheld = self.feed_gap is not None and self.feed_gap[0] <= ts < self.feed_gap[1]
            if withheld:
                report.minutes_skipped += 1
            else:
                for bar in by_minute[ts]:
                    for fill in self.broker.on_bar(bar):
                        report.fills.append(
                            {
                                "at": fill.at.strftime("%H:%M"),
                                "symbol": fill.symbol,
                                "side": fill.side,
                                "qty": str(fill.qty),
                                "price": str(fill.price),
                                "reference": str(fill.reference),
                                "cost": str(fill.cost),
                                "realized_pnl": str(fill.realized_pnl),
                            }
                        )
                    await self.emit(
                        Topics.MARKET_BAR_1M,
                        bar,
                        symbol=bar.symbol,
                        book="intraday",
                        occurred_at=at,
                    )
                    report.bars_fed += 1
                    state.last_seen[bar.symbol] = at
                    state.last_any_bar = at
                    for alert in state.on_bar(bar, at):
                        await self.emit(
                            Topics.WATCHER_ALERT, alert, symbol=alert.symbol, book="intraday"
                        )
            for alert in state.stale_checks(self.universe, at, calendar.is_open(at), floor=open_at):
                await self.emit(Topics.WATCHER_ALERT, alert, symbol=alert.symbol, book="intraday")
            marks.append(self.broker.equity())
            await self._settle()
        await self._settle()
        report.kill_switch = [
            f"{e.occurred_at:%H:%M} {'engaged' if e.payload.get('engaged', True) else 'released'}: {e.payload.get('reason', '')}"
            for e in self.memory_bus.log
            if e.type == Topics.RISK_KILL_SWITCH
        ]
        peak = Decimal(0)
        worst = 0.0
        for m in marks:
            peak = max(peak, m)
            if peak:
                worst = min(worst, float((m - peak) / peak * 100))
        report.max_drawdown_pct = round(abs(worst), 3)
        report.equity_end = self.broker.equity()
        report.realized_pnl = self.broker.realized_pnl
        report.unrealized_pnl = self.broker.unrealized_pnl()
        report.costs = self.broker.costs
        return report
