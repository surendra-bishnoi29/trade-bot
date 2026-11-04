"""The quant screen: the deterministic scorecard, rebuilt on a schedule and published as a research
report so the desk is visibly working before the open, during the session and after the close. Once a
day, after the first pre-market screen, the head trader writes the morning note from it."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from desk.agents.base import BaseAgent
from desk.agents.scorecard import build_scorecard, render_scorecard
from desk.bus.base import Bus
from desk.core import calendar
from desk.core.clock import Clock
from desk.core.events import Topics
from desk.storage.db import Database

log = logging.getLogger(__name__)

PRE_MARKET_HOURS = 3.5
PRE_MARKET_EVERY = timedelta(minutes=15)
SESSION_EVERY = timedelta(minutes=30)
POST_CLOSE_AFTER = timedelta(minutes=5)


def slots_for(day: date) -> list[tuple[datetime, str]]:
    """Every screen of one session day: pre-market every 15 minutes, in session every 30, once after."""
    open_at, close_at = calendar.session_bounds(day)
    out: list[tuple[datetime, str]] = []
    t = open_at - timedelta(hours=PRE_MARKET_HOURS)
    while t < open_at:
        out.append((t, "pre_market"))
        t += PRE_MARKET_EVERY
    t = open_at + SESSION_EVERY
    while t < close_at:
        out.append((t, "session"))
        t += SESSION_EVERY
    out.append((close_at + POST_CLOSE_AFTER, "post_close"))
    return out


def next_slot(now: datetime) -> tuple[datetime, str]:
    days = [
        d for d in calendar.last_sessions(now.date() + timedelta(days=10), 10) if d >= now.date()
    ]
    for day in sorted(days):
        for at, kind in slots_for(day):
            if at > now:
                return at, kind
    raise RuntimeError("no session in the next ten days")


class QuantScreen(BaseAgent):
    name = "quant_screen"
    topics = ()

    def __init__(
        self,
        bus: Bus,
        clock: Clock,
        db: Database,
        universe: list[str],
        back_office: Any,
        *,
        head: Any = None,
        cycle: Any = None,
        theses: Any = None,
    ) -> None:
        super().__init__(bus, clock)
        self.db = db
        self.universe = universe
        self.back_office = back_office
        self.head = head
        self.cycle = cycle
        self.store = theses or db
        self.note_written_for: date | None = None
        self.last_rows: list = []

    async def screen(self, kind: str) -> list:
        now = self.clock.now()
        snap = self.back_office.snapshot
        positions = {s: (p.qty, p.avg_entry) for s, p in snap.positions.items()}
        rows = await build_scorecard(self.db, self.universe, positions, now)
        self.last_rows = rows
        await self.emit(
            Topics.RESEARCH_REPORT,
            {
                "analyst": "quant_screen",
                "kind": "scorecard",
                "when": kind,
                "table": render_scorecard(rows, top_n=10),
                "top": [r.symbol for r in rows[:10]],
                "ranked": [
                    {
                        "symbol": r.symbol,
                        "rank": r.rank,
                        "score": r.score,
                        "last_close": float(r.last_close) if r.last_close is not None else None,
                        "ret_1d": r.ret_1d,
                        "ret_5d": r.ret_5d,
                        "ret_20d": r.ret_20d,
                        "news_5d": r.news_5d,
                        "news_count_5d": r.news_count_5d,
                        "alerts_24h": r.alerts_24h,
                        "held": float(r.position_qty) if r.position_qty else 0.0,
                    }
                    for r in rows
                ],
            },
            book="swing",
        )
        log.info("quant screen (%s): top %s", kind, ", ".join(r.symbol for r in rows[:5]))
        return rows

    async def morning_note(self, rows: list) -> None:
        if self.head is None or self.cycle is None:
            return
        open_theses = await self.store.open_theses()
        material = await self.cycle._material(rows, open_theses)
        note = await self.head.morning_note(material)
        await self.emit(
            Topics.RESEARCH_REPORT,
            {
                "analyst": "head_trader",
                "kind": "morning_note",
                "day": self.clock.now().date().isoformat(),
                "note": note.model_dump(mode="json"),
            },
            book="swing",
        )
        self.note_written_for = self.clock.now().date()
        log.info("morning note: %s", note.headline)

    async def note_exists_today(self) -> bool:
        now = self.clock.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        for e in await self.db.recent_events([Topics.RESEARCH_REPORT], start, limit=50, until=now):
            if e.payload.get("kind") == "morning_note":
                return True
        return False

    async def run_once(self, kind: str) -> None:
        try:
            rows = await self.screen(kind)
        except Exception as exc:  # noqa: BLE001 - a failed screen must not stop the schedule
            log.error("quant screen failed: %s", exc)
            return
        now = self.clock.now()
        if (
            kind == "pre_market"
            and calendar.is_session(now.date())
            and self.note_written_for != now.date()
            and not await self.note_exists_today()
        ):
            try:
                await self.morning_note(rows)
            except Exception as exc:  # noqa: BLE001
                log.error("morning note failed: %s", exc)

    async def run_forever(self) -> None:
        """A screen right away if the desk starts inside a window, then on the schedule, in slices."""
        now = self.clock.now()
        if calendar.is_session(now.date()):
            open_at, close_at = calendar.session_bounds(now.date())
            if open_at - timedelta(hours=PRE_MARKET_HOURS) <= now < close_at:
                await self.run_once("pre_market" if now < open_at else "session")
        while True:
            at, kind = next_slot(self.clock.now())
            while self.clock.now() < at:
                remaining = (at - self.clock.now()).total_seconds()
                await self.clock.sleep(min(60.0, max(1.0, remaining)))
            await self.run_once(kind)
