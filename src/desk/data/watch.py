"""Price history for the markets the desk watches but does not trade yet: oil and gold through
their exchange-traded funds, and crypto. Read-only REST calls; minute bars for the funds come from
the free real-time IEX feed, crypto bars from Alpaca's crypto data, which needs no subscription."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from desk.core.payloads import MarketBar
from desk.data.alpaca import bar_to_model

MINUTE = TimeFrame(1, TimeFrameUnit.Minute)


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
