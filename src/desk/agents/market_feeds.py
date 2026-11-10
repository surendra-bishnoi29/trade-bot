"""Market feeds: keeps prices fresh for the markets the desk watches but does not trade (oil and
gold through their funds, crypto). It only writes bars to the store, so the funnel can draw them;
it emits no market events, so no trading agent ever acts on these instruments."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core.clock import Clock
from desk.core.events import Topics
from desk.funnel.markets import Market, session_window
from desk.storage.db import Database

log = logging.getLogger(__name__)

DAILY_EVERY = timedelta(hours=6)
DAILY_HISTORY = timedelta(days=50)


class MarketFeeds(BaseAgent):
    name = "market_feeds"
    topics = ()

    def __init__(
        self,
        bus: Bus,
        clock: Clock,
        db: Database,
        data: Any,
        markets: list[Market],
        *,
        every_s: float = 60.0,
        emit_bars: bool = False,
    ) -> None:
        super().__init__(bus, clock)
        self.db = db
        self.data = data
        self.markets = markets
        self.every_s = every_s
        self.emit_bars = emit_bars  # the lab desk trades these markets, so its agents need the bars
        self.daily_at: dict[str, datetime] = {}
        self.last_minute: dict[str, datetime] = {}
        self.counts: dict[str, int] = {}

    async def refresh(self, market: Market) -> int:
        now = self.clock.now()
        symbols = list(market.all_symbols)
        written = 0
        due = self.daily_at.get(market.key)
        if due is None or now - due >= DAILY_EVERY:
            daily = await self.data.bars(symbols, "1d", now - DAILY_HISTORY)
            written += await self.db.insert_bars(daily, now)
            self.daily_at[market.key] = now
        _, open_at, close_at = session_window(market, now)
        start = max(open_at, self.last_minute.get(market.key, open_at) - timedelta(minutes=3))
        if now >= open_at and start < close_at + timedelta(minutes=5):
            minute = await self.data.bars(symbols, "1m", start)
            written += await self.db.insert_bars(minute, now)
            if minute:
                newest = max(b.ts for b in minute)
                if self.emit_bars:
                    seen = self.last_minute.get(market.key)
                    for b in sorted(minute, key=lambda b: b.ts):
                        if seen is None or b.ts > seen:
                            await self.emit(
                                Topics.MARKET_BAR_1M, b, symbol=b.symbol, occurred_at=b.closes_at
                            )
                self.last_minute[market.key] = newest
        self.counts[market.key] = self.counts.get(market.key, 0) + written
        return written

    async def run_once(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for m in self.markets:
            try:
                out[m.key] = await self.refresh(m)
            except Exception as exc:  # noqa: BLE001 - a feed hiccup must not stop the loop
                log.warning("market feed %s failed: %s", m.key, exc)
                out[m.key] = -1
        return out

    async def run_forever(self) -> None:
        first = True
        while True:
            result = await self.run_once()
            if first:
                log.info("market feeds: first pass wrote %s", result)
                first = False
            await self.clock.sleep(self.every_s)
