"""Last-price cache fed by bar events, so the risk officer can value an order synchronously."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core.clock import Clock
from desk.core.events import Event, Topics
from desk.storage.db import Database


class PriceCache(BaseAgent):
    name = "prices"
    topics = (Topics.MARKET_BAR_1M, Topics.MARKET_BAR_1D)

    def __init__(self, bus: Bus, clock: Clock) -> None:
        super().__init__(bus, clock)
        self.last: dict[str, Decimal] = {}
        self.last_at: dict[str, datetime] = {}

    async def seed(self, db: Database, symbols: list[str], before: datetime | None = None) -> int:
        for symbol in symbols:
            bars = await db.recent_bars(symbol, "1d", limit=1, before=before)
            if bars:
                self.last[symbol] = Decimal(bars[-1]["close"])
                self.last_at[symbol] = bars[-1]["ts"]
        return len(self.last)

    async def handle(self, event: Event) -> None:
        symbol, close = event.payload.get("symbol"), event.payload.get("close")
        if symbol and close is not None:
            self.last[symbol] = Decimal(str(close))
            self.last_at[symbol] = event.occurred_at

    def price_of(self, symbol: str) -> Decimal | None:
        return self.last.get(symbol)

    def age_of(self, symbol: str, now: datetime) -> timedelta | None:
        at = self.last_at.get(symbol)
        return None if at is None else now - at
