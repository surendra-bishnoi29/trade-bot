"""Price history for the markets the desk watches but does not trade yet: oil and gold through
their exchange-traded funds, and crypto. Read-only REST calls; minute bars for the funds come from
the free real-time IEX feed, crypto bars from Alpaca's crypto data, which needs no subscription."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timedelta

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from desk.core.payloads import MarketBar
from desk.data.alpaca import bar_to_model

MINUTE = TimeFrame(1, TimeFrameUnit.Minute)

log = logging.getLogger(__name__)


class WatchData:
    def __init__(self, key: str, secret: str) -> None:
        self._stocks = StockHistoricalDataClient(key, secret)
        self._crypto = CryptoHistoricalDataClient(key, secret)

    async def bars(
        self, symbols: Iterable[str], interval: str, start: datetime, end: datetime | None = None
    ) -> list[MarketBar]:
        """Minute ("1m") or daily ("1d") bars for a mixed list of funds and crypto pairs."""
        symbols = list(symbols)
        crypto = [s for s in symbols if "/" in s]
        funds = [s for s in symbols if "/" not in s]
        frame = MINUTE if interval == "1m" else TimeFrame.Day
        out: list[MarketBar] = []
        if funds:
            request = StockBarsRequest(
                symbol_or_symbols=funds,
                timeframe=frame,
                # recent minutes are only on the IEX feed without a paid plan; finished days are on SIP
                feed=DataFeed.IEX,
                start=start,
                end=end,
            )
            result = await asyncio.to_thread(self._stocks.get_stock_bars, request)
            for s, rows in result.data.items():
                out += [bar_to_model(b, interval, s) for b in rows]
        if crypto:
            request = CryptoBarsRequest(
                symbol_or_symbols=crypto, timeframe=frame, start=start, end=end
            )
            result = await asyncio.to_thread(self._crypto.get_crypto_bars, request)
            for s, rows in result.data.items():
                out += [bar_to_model(b, interval, s) for b in rows]
        return out


class PolledBars:
    """Live minute bars without the websocket: the same interface as AlpacaLiveBars, fed by a REST
    poll. Alpaca's free plan allows one live stream per login, so a second desk on the same login
    (the lab beside the main desk) must not open one: its retries could take the stream from the
    desk that owns it. Each completed bar is delivered once, in time order, at most `every_s` late."""

    def __init__(
        self,
        data: WatchData,
        clock: object,
        *,
        every_s: float = 20.0,
        is_open: Callable[[datetime], bool] | None = None,
    ) -> None:
        self.data = data
        self.clock = clock
        self.every_s = every_s
        self.is_open = is_open or (lambda at: True)
        self.seen: dict[str, datetime] = {}
        self.running = True

    async def poll(self, symbols: list[str]) -> list[MarketBar]:
        """The completed minute bars not yet delivered."""
        now = self.clock.now()  # type: ignore[attr-defined]
        if not self.is_open(now):
            return []
        oldest = min((self.seen[s] for s in symbols if s in self.seen), default=None)
        start = (oldest or now - timedelta(minutes=30)) - timedelta(minutes=2)
        start = max(start, now - timedelta(minutes=90))  # never replay hours after a long outage
        fresh: list[MarketBar] = []
        for b in await self.data.bars(symbols, "1m", start):
            if b.closes_at > now:
                continue  # still forming
            if b.symbol in self.seen and b.ts <= self.seen[b.symbol]:
                continue
            fresh.append(b)
        fresh.sort(key=lambda b: (b.ts, b.symbol))
        for b in fresh:
            self.seen[b.symbol] = b.ts
        return fresh

    async def run(
        self, symbols: Iterable[str], on_bar: Callable[[MarketBar], Awaitable[None]]
    ) -> None:
        symbols = list(symbols)
        while self.running:
            try:
                for b in await self.poll(symbols):
                    await on_bar(b)
            except Exception as exc:  # noqa: BLE001 - a failed poll must not end the feed
                log.warning("polled bars failed: %s", exc)
            await self.clock.sleep(self.every_s)  # type: ignore[attr-defined]

    async def stop(self) -> None:
        self.running = False
