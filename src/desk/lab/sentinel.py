"""The sentinel: one eye per instrument, open every minute, no model calls. It rebuilds the funnel
for each market and compares every instrument with its last look. When something changes that a
decision could act on, it raises a trigger for the predictor: a flip of side, a surge in strength,
new unusual activity, a change in news tone, a turn of the whole market, or simply a quiet name
that has not been reviewed for a while. Instruments with an open position are the lines engine's
business and are left alone."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core.clock import Clock
from desk.core.events import Topics
from desk.core.payloads import SentinelTrigger
from desk.funnel.build import build_funnel
from desk.funnel.markets import Market, session_window
from desk.storage.db import Database

log = logging.getLogger(__name__)


def changed(
    prev: dict[str, Any] | None, cur: dict[str, Any], market_turned: bool
) -> tuple[str, str] | None:
    """What changed since the last look, as (reason, detail), or None."""
    if prev is None:
        return (
            ("first look", f"{cur['side']} {cur['strength']:.0f}")
            if cur["strength"] >= 25
            else None
        )
    if cur["side"] != prev["side"] and cur["strength"] >= 25:
        return "side flip", f"{prev['side']} to {cur['side']} at {cur['strength']:.0f}"
    if cur["strength"] >= 50 > prev["strength"]:
        return "surge", f"strength {prev['strength']:.0f} to {cur['strength']:.0f}"
    if cur["alerts_total"] >= prev["alerts_total"] + 2:
        return (
            "unusual activity",
            f"{cur['alerts_total'] - prev['alerts_total']} new flags, {cur['setup'].lower()}",
        )
    tone = lambda r: next(
        (e["verdict"] for e in r["evidence"] if e["label"].startswith("News tone")), "neutral"
    )
    if tone(cur) != tone(prev) and tone(cur) != "neutral":
        return "news tone", f"news turned {tone(cur)}"
    if market_turned and cur.get("rank", 99) <= 3:
        return "market turned", f"ranked {cur['rank']} on the {cur['side']} side"
    return None


class Sentinel(BaseAgent):
    name = "sentinel"
    topics = ()

    def __init__(
        self,
        bus: Bus,
        clock: Clock,
        db: Database,
        markets: list[Market],
        *,
        every_s: float = 60.0,
        review_minutes: int = 45,
        quiet_minutes: int = 10,
        has_position: Any = None,
    ) -> None:
        super().__init__(bus, clock)
        self.db = db
        self.markets = markets
        self.every_s = every_s
        self.review = timedelta(minutes=review_minutes)
        self.quiet = timedelta(minutes=quiet_minutes)
        self.has_position = has_position or (lambda symbol: False)
        self.last: dict[str, dict[str, Any]] = {}  # symbol -> last readings
        self.last_trigger: dict[str, datetime] = {}
        self.last_verdict: dict[str, str] = {}
        self.funnels: dict[
            str, dict[str, Any]
        ] = {}  # the latest build per market, for the predictor

    def market_is_open(self, market: Market, now: datetime) -> bool:
        _, open_at, close_at = session_window(market, now)
        return open_at <= now < close_at

    async def look(self, market: Market) -> list[SentinelTrigger]:
        now = self.clock.now()
        if not self.market_is_open(market, now):
            return []
        funnel = await build_funnel(self.db, market, now)
        self.funnels[market.key] = funnel
        verdict = funnel["sentiment"]["verdict"]
        market_turned = self.last_verdict.get(market.key) not in (None, verdict)
        self.last_verdict[market.key] = verdict
        out: list[SentinelTrigger] = []
        for row in funnel["symbols"]:
            symbol = row["symbol"]
            prev = self.last.get(symbol)
            if self.has_position(symbol):
                self.last[symbol] = row
                continue
            since = now - self.last_trigger.get(symbol, now - timedelta(days=1))
            why = changed(prev, row, market_turned)
            if why is None and since >= self.review and row["strength"] >= 25:
                why = (
                    "review",
                    f"{row['side']} {row['strength']:.0f} for {int(since.total_seconds() // 60)} minutes",
                )
            if why is None:
                self.last[symbol] = row  # nothing to say: this look becomes the reference
                continue
            if since < self.quiet:
                continue  # a change inside the quiet period waits, measured from the last reference
            self.last[symbol] = row
            self.last_trigger[symbol] = now
            out.append(
                SentinelTrigger(
                    symbol=symbol,
                    market=market.key,
                    reason=why[0],
                    detail=why[1],
                    score=row["score"],
                    side=row["side"],
                    strength=row["strength"],
                    setup=row["setup"],
                    last=row["last"],
                )
            )
        return out

    async def run_once(self) -> int:
        n = 0
        for market in self.markets:
            try:
                for t in await self.look(market):
                    await self.emit(Topics.SENTINEL_TRIGGER, t, symbol=t.symbol)
                    n += 1
            except Exception as exc:  # noqa: BLE001 - one bad look must not blind the others
                log.warning("sentinel look at %s failed: %s", market.key, exc)
        return n

    async def run_forever(self) -> None:
        while True:
            n = await self.run_once()
            if n:
                log.info("sentinel: %d triggers", n)
            await self.clock.sleep(self.every_s)
