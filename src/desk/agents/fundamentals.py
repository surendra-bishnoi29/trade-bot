"""The fundamental analyst and the events analyst, one agent with two reports per name, read once a day
before the open from Finnhub: valuation, growth, margins and the 52-week range; and the next earnings
date, which the head trader must treat as a binary event inside a thesis horizon."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core import calendar
from desk.core.clock import Clock
from desk.core.events import Topics
from desk.core.sectors import REFERENCES
from desk.data.finnhub import Finnhub
from desk.storage.db import Database

log = logging.getLogger(__name__)

RUN_AT_UTC = (10, 50)


def _f(v: float | None, kind: str = "x") -> str:
    if v is None:
        return "n/a"
    if kind == "pct":
        return f"{v:.1f}%"
    return f"{v:.1f}"


def fundamental_report(
    symbol: str, m: dict[str, float | None], last_close: float | None
) -> dict[str, Any]:
    pe, growth, margin = m.get("peTTM"), m.get("revenueGrowthTTMYoy"), m.get("netProfitMarginTTM")
    eps_g = m.get("epsGrowthTTMYoy")
    hi, lo = m.get("52WeekHigh"), m.get("52WeekLow")
    position = None
    if last_close is not None and hi and lo and hi > lo:
        position = (last_close - lo) / (hi - lo)
    if growth is not None and margin is not None and growth > 10 and margin > 15:
        stance, conviction = "bullish", 0.35
    elif (growth is not None and growth < 0) or (margin is not None and margin < 0):
        stance, conviction = "bearish", 0.35
    else:
        stance, conviction = "neutral", 0.2
    summary = (
        f"P/E {_f(pe)}, revenue growth {_f(growth, 'pct')} yoy, EPS growth {_f(eps_g, 'pct')} yoy, "
        f"net margin {_f(margin, 'pct')}, return on equity {_f(m.get('roeTTM'), 'pct')}"
        + (f"; {position:.0%} of the way up its 52-week range" if position is not None else "")
        + "."
    )
    return {
        "analyst": "fundamentals",
        "kind": "fundamental",
        "symbol": symbol,
        "stance": stance,
        "conviction": conviction,
        "horizon": "weeks",
        "summary": summary,
        "data": {**m, "range_position": position},
    }


def event_report(
    symbol: str, earnings: list[dict[str, Any]], now: datetime
) -> dict[str, Any] | None:
    ahead = [e for e in earnings if e["date"] >= now.date()]
    if not ahead:
        return None
    e = ahead[0]
    sessions = calendar.sessions_between(now.date(), e["date"])
    hour = {"bmo": "before the open", "amc": "after the close", "dmh": "during the session"}.get(
        e.get("hour", ""), ""
    )
    summary = f"Earnings on {e['date'].isoformat()} {hour}".strip() + f" ({sessions} sessions away)"
    if e.get("eps_estimate") is not None:
        summary += f"; EPS estimate {e['eps_estimate']}"
    summary += (
        ". A binary event: a new swing thesis should not straddle it unless it is about the print."
    )
    return {
        "analyst": "fundamentals",
        "kind": "event",
        "symbol": symbol,
        "stance": "none",
        "conviction": 0.0,
        "horizon": "days",
        "summary": summary,
        "data": {
            "earnings_date": e["date"].isoformat(),
            "sessions_away": sessions,
            "hour": e.get("hour"),
            "eps_estimate": e.get("eps_estimate"),
        },
    }


class FundamentalAnalyst(BaseAgent):
    name = "fundamentals"
    topics = ()

    def __init__(
        self, bus: Bus, clock: Clock, db: Database | None, client: Finnhub, universe: list[str]
    ) -> None:
        super().__init__(bus, clock)
        self.db = db
        self.client = client
        self.symbols = [s for s in universe if s not in REFERENCES]
        self.written_for: date | None = None

    async def exists_today(self) -> bool:
        if self.db is None:
            return False
        now = self.clock.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return await self.db.report_exists("fundamental", start, now)

    async def last_close(self, symbol: str) -> float | None:
        if self.db is None:
            return None
        rows = await self.db.recent_bars(symbol, "1d", limit=1, before=self.clock.now())
        return float(rows[-1]["close"]) if rows else None

    async def run_once(self) -> int:
        now = self.clock.now()
        n = 0
        for symbol in self.symbols:
            try:
                metrics = await self.client.metrics(symbol)
                report = fundamental_report(symbol, metrics, await self.last_close(symbol))
                await self.emit(Topics.RESEARCH_REPORT, report, symbol=symbol, book="swing")
                n += 1
                earnings = await self.client.earnings(
                    symbol, now.date(), now.date() + timedelta(days=45)
                )
                event = event_report(symbol, earnings, now)
                if event is not None:
                    await self.emit(Topics.RESEARCH_REPORT, event, symbol=symbol, book="swing")
            except Exception as exc:  # noqa: BLE001 - one name failing must not stop the rest
                log.warning("finnhub %s: %s", symbol, exc)
            await asyncio.sleep(1.1)  # well inside the free tier's sixty calls a minute
        self.written_for = now.date()
        log.info("fundamentals: %d names read", n)
        return n

    async def run_forever(self) -> None:
        now = self.clock.now()
        if calendar.is_session(now.date()) and not await self.exists_today():
            await self.run_once()
        while True:
            now = self.clock.now()
            target = now.replace(hour=RUN_AT_UTC[0], minute=RUN_AT_UTC[1], second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            while self.clock.now() < target:
                await self.clock.sleep(
                    min(60.0, max(1.0, (target - self.clock.now()).total_seconds()))
                )
            if calendar.is_session(self.clock.now().date()):
                try:
                    await self.run_once()
                except Exception as exc:  # noqa: BLE001
                    log.error("fundamentals read failed: %s", exc)
