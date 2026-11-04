"""The daily brief: after the post-close cycle the desk writes up its day for the people who own the
money. Facts come from the journal and the broker snapshot; the head trader turns them into plain
words; the result is an event, so Telegram and the interface both carry it."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core import calendar
from desk.core.clock import Clock
from desk.core.events import Event, Topics
from desk.core.notices import summarize
from desk.core.payloads import DailyBriefPayload
from desk.storage.db import Database

log = logging.getLogger(__name__)


def _n(v: Any) -> Any:
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


class DailyBriefAgent(BaseAgent):
    name = "brief"
    topics = (Topics.CYCLE_PLAN,)

    def __init__(
        self, bus: Bus, clock: Clock, db: Database, head: Any, back_office: Any, theses: Any = None
    ) -> None:
        super().__init__(bus, clock)
        self.db = db
        self.head = head
        self.back_office = back_office
        self.store = theses or db
        self.last: DailyBriefPayload | None = None

    async def handle(self, event: Event) -> None:
        p = event.payload
        if p.get("trigger") == "post_close" and not p.get("dry_run"):
            try:
                await self.write(caused_by=event)
            except Exception as exc:  # noqa: BLE001 - a failed brief must not stop anything else
                log.error("daily brief failed: %s", exc)

    def day_of(self, now: datetime) -> date:
        return calendar.session_of(now) if calendar.is_session(now.date()) else now.date()

    async def facts(self, day: date) -> dict[str, Any]:
        now = self.clock.now()
        if calendar.is_session(day):
            open_at, close_at = calendar.session_bounds(day)
        else:
            open_at = datetime.combine(day, datetime.min.time(), tzinfo=now.tzinfo)
            close_at = open_at + timedelta(days=1)
        since = open_at - timedelta(hours=3)

        async def events(types: list[str], limit: int = 200) -> list[Event]:
            rows = await self.db.recent_events(types, since, limit=limit, until=now)
            return list(reversed(rows))  # oldest first

        fills: list[dict[str, Any]] = []
        seen: set[str] = set()
        for e in await events([Topics.ORDER_FILLED]):
            intent_id = e.payload.get("intent_id")
            if intent_id in seen:
                continue
            seen.add(intent_id)
            fills.append(
                {
                    "time": e.occurred_at.strftime("%H:%M UTC"),
                    "symbol": e.symbol,
                    "side": e.payload.get("side"),
                    "quantity": _n(e.payload.get("filled_quantity")),
                    "price": _n(e.payload.get("avg_price")),
                }
            )
        opened = [
            {
                "symbol": e.symbol,
                "quantity": _n(e.payload.get("quantity")),
                "stop_pct": e.payload.get("stop_pct"),
                "target_pct": e.payload.get("target_pct"),
                "horizon_days": e.payload.get("horizon_days"),
                "conviction": e.payload.get("conviction"),
                "why": str(e.payload.get("narrative", ""))[:400],
            }
            for e in await events([Topics.THESIS_CREATED])
        ]
        closed = [
            {
                "symbol": e.symbol,
                "reason": e.payload.get("reason"),
                "realized_pnl": _n(e.payload.get("realized_pnl")),
                "entry": _n(e.payload.get("entry_price")),
            }
            for e in await events([Topics.THESIS_CLOSED])
        ]
        cycles = []
        for e in await events([Topics.CYCLE_PLAN], limit=20):
            p = e.payload
            plan = p.get("plan") or {}
            decisions = plan.get("decisions") or []
            objections = (p.get("challenge") or {}).get("objections") or []
            cycles.append(
                {
                    "time": e.occurred_at.strftime("%H:%M UTC"),
                    "trigger": p.get("trigger"),
                    "dry_run": p.get("dry_run", False),
                    "market_view": plan.get("market_view"),
                    "trades_proposed": [
                        f"{d.get('action')} {d.get('quantity')} {d.get('symbol')} (conviction {d.get('conviction')})"
                        for d in decisions
                        if d.get("action") in ("buy", "sell")
                    ],
                    "holds": [d.get("symbol") for d in decisions if d.get("action") == "hold"],
                    "skeptic": [
                        f"{o.get('severity')} on {o.get('symbol')}: {str(o.get('argument', ''))[:200]}"
                        for o in objections
                    ],
                    "outcome": [
                        f"{a.get('action')} {a.get('quantity') or ''} {a.get('symbol')}: {a.get('note')}"
                        for a in p.get("final_actions") or []
                        if a.get("action") in ("buy", "sell")
                    ],
                }
            )
        incidents = []
        for e in await events(
            [
                Topics.RISK_KILL_SWITCH,
                Topics.WATCHER_ALERT,
                Topics.SYSTEM_RESTARTED,
                Topics.SYSTEM_HEALTH,
                Topics.CYCLE_FAILED,
                Topics.RISK_REJECTED,
                Topics.ORDER_REJECTED,
            ],
            limit=300,
        ):
            if e.type == Topics.WATCHER_ALERT and e.payload.get("kind") not in (
                "feed_stale",
                "feed_recovered",
            ):
                continue
            if e.type == Topics.SYSTEM_HEALTH and e.payload.get("status") == "ok":
                continue
            incidents.append(f"{e.occurred_at:%H:%M} UTC {summarize(e)}")
        snapshots = await events([Topics.PORTFOLIO_RECONCILED], limit=500)
        start_equity = _n(snapshots[0].payload.get("equity")) if snapshots else None
        snap = self.back_office.snapshot
        open_theses = await self.store.open_theses()
        book = {
            "equity_start": start_equity,
            "equity_now": _n(snap.equity),
            "cash": _n(snap.cash),
            "positions": [
                {
                    "symbol": p.symbol,
                    "quantity": _n(p.qty),
                    "avg_entry": _n(p.avg_entry),
                    "market_value": _n(p.market_value),
                    "unrealized_pl": _n(p.unrealized_pl),
                }
                for p in snap.positions.values()
            ],
            "open_theses": [
                {
                    "symbol": t.symbol,
                    "status": t.status,
                    "entry": _n(t.entry_price),
                    "stop": _n(t.stop_price),
                    "target": _n(t.target_price),
                    "horizon_days": t.horizon_days,
                    "since": t.created_at.date().isoformat(),
                    "why": t.narrative[:300],
                }
                for t in open_theses
            ],
        }
        return {
            "day": day.isoformat(),
            "session": {
                "open": open_at.strftime("%H:%M UTC"),
                "close": close_at.strftime("%H:%M UTC"),
            },
            "book": book,
            "fills": fills,
            "theses_opened": opened,
            "theses_closed": closed,
            "cycles": cycles,
            "incidents": incidents,
        }

    @staticmethod
    def material(facts: dict[str, Any]) -> str:
        return "FACTS FROM THE JOURNAL\n" + json.dumps(facts, indent=1, default=str)

    async def write(self, *, caused_by: Event | None = None) -> DailyBriefPayload:
        now = self.clock.now()
        day = self.day_of(now)
        facts = await self.facts(day)
        brief = await self.head.brief(self.material(facts))
        payload = DailyBriefPayload(day=day, generated_at=now, brief=brief, facts=facts)
        await self.emit(Topics.DESK_BRIEF, payload, book="swing", caused_by=caused_by)
        self.last = payload
        log.info("daily brief written: %s", brief.headline)
        return payload

    @staticmethod
    def render(payload: DailyBriefPayload) -> str:
        b = payload.brief
        lines = [f"DAILY BRIEF {payload.day}: {b.headline}", "", b.summary, ""]
        if b.trades:
            lines.append("Trades")
            lines += [
                f"- {t.side.upper()} {t.quantity} {t.symbol} at {t.price:,.2f}: {t.why}"
                for t in b.trades
            ]
            lines.append("")
        lines += [f"Book tonight: {b.book}", f"Incidents: {b.incidents}", f"Tomorrow: {b.tomorrow}"]
        return "\n".join(lines)
