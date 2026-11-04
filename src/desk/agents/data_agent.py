"""Data agent: backfills history from Alpaca into the bars table and publishes the latest session."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core import calendar
from desk.core.clock import Clock
from desk.core.events import Topics
from desk.core.payloads import MarketBar, SystemHealth
from desk.data.alpaca import AlpacaMarketData
from desk.storage.db import Database

log = logging.getLogger(__name__)


class DataAgent(BaseAgent):
    name = "data"
    topics = ()

    def __init__(
        self,
        bus: Bus,
        clock: Clock,
        db: Database,
        market: AlpacaMarketData,
        universe: list[str],
        *,
        daily_sessions: int = 250,
        minute_sessions: int = 5,
    ) -> None:
        super().__init__(bus, clock)
        self.db = db
        self.market = market
        self.universe = universe
        self.daily_sessions = daily_sessions
        self.minute_sessions = minute_sessions

    async def backfill(self) -> dict[str, int]:
        now = self.clock.now()
        # the free tier serves everything older than 15 minutes
        end = now - timedelta(minutes=16)
        today = calendar.session_of(end)
        daily_start = datetime.combine(
            calendar.last_sessions(today, self.daily_sessions)[0],
            datetime.min.time(),
            tzinfo=now.tzinfo,
        )
        minute_start = datetime.combine(
            calendar.last_sessions(today, self.minute_sessions)[0],
            datetime.min.time(),
            tzinfo=now.tzinfo,
        )

        latest_daily = await self.db.latest_bar_ts("1d")
        earliest_daily = await self.db.earliest_bar_ts("1d")
        # incremental only when the stored history already reaches back as far as asked
        if latest_daily is not None and (
            earliest_daily is not None and earliest_daily <= daily_start + timedelta(days=7)
        ):
            daily_start = max(daily_start, latest_daily - timedelta(days=3))
        latest_minute = await self.db.latest_bar_ts("1m")
        earliest_minute = await self.db.earliest_bar_ts("1m")
        if latest_minute is not None and (
            earliest_minute is not None and earliest_minute <= minute_start + timedelta(days=7)
        ):
            minute_start = max(minute_start, latest_minute - timedelta(hours=1))

        counts = {"1d": 0, "1m": 0}
        daily = await self.market.daily_bars(self.universe, daily_start, end)
        latest: list[MarketBar] = []
        for symbol, bars in daily.items():
            counts["1d"] += await self.db.insert_bars(bars, now)
            if bars:
                latest.append(bars[-1])
        for bar in latest:
            _, session_close = calendar.session_bounds(bar.ts.date())
            await self.emit(Topics.MARKET_BAR_1D, bar, symbol=bar.symbol, occurred_at=session_close)

        for symbol in self.universe:
            minute = await self.market.minute_bars([symbol], minute_start, end)
            for bars in minute.values():
                counts["1m"] += await self.db.insert_bars(bars, now)

        await self.emit(
            Topics.SYSTEM_HEALTH,
            SystemHealth(
                component=self.name,
                status="ok",
                detail=f"backfill daily={counts['1d']} minute={counts['1m']}",
            ),
        )
        log.info("backfill complete: %s", counts)
        return counts

    async def run_forever(self) -> None:
        await self.backfill()
        while True:
            now = self.clock.now()
            refresh_at = calendar.previous_close(now) + timedelta(minutes=10)
            if refresh_at <= now:
                refresh_at = calendar.next_open(now) + timedelta(hours=6, minutes=40)
            await self.clock.sleep(max(60.0, (refresh_at - now).total_seconds()))
            await self.backfill()
