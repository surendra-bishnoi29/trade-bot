"""Alpaca adapters: market data (historical and live), news, and paper or live trading.

alpaca-py is synchronous for REST, so calls run in a worker thread. Nothing here knows about the
bus or the clock; agents own those.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.live import StockDataStream
from alpaca.data.requests import NewsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    MarketOrderRequest,
    ReplaceOrderRequest,
    StopLossRequest,
)

from desk.core.payloads import MarketBar, NewsArticleRaw

log = logging.getLogger(__name__)


def _dec(v: Any) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def bar_to_model(bar: Any, interval: str, symbol: str | None = None) -> MarketBar:
    return MarketBar(
        symbol=symbol or bar.symbol,
        interval=interval,  # type: ignore[arg-type]
        ts=bar.timestamp,
        open=_dec(bar.open),
        high=_dec(bar.high),
        low=_dec(bar.low),
        close=_dec(bar.close),
        volume=int(bar.volume),
        vwap=_dec(getattr(bar, "vwap", None)),
        trade_count=int(bar.trade_count) if getattr(bar, "trade_count", None) is not None else None,
        source="alpaca",
    )


def news_to_model(item: Any) -> NewsArticleRaw:
    return NewsArticleRaw(
        article_id=str(item.id),
        published_at=item.created_at,
        source=str(item.source),
        url=item.url,
        title=item.headline,
        summary=item.summary or None,
        symbols=list(item.symbols or []),
    )


class AlpacaMarketData:
    def __init__(self, key: str, secret: str) -> None:
        self._data = StockHistoricalDataClient(key, secret)
        self._news = NewsClient(key, secret)

    async def daily_bars(
        self, symbols: Iterable[str], start: datetime, end: datetime
    ) -> dict[str, list[MarketBar]]:
        request = StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=TimeFrame.Day,
            feed=DataFeed.SIP,
            start=start,
            end=end,
        )
        result = await asyncio.to_thread(self._data.get_stock_bars, request)
        return {s: [bar_to_model(b, "1d", s) for b in bars] for s, bars in result.data.items()}

    async def minute_bars(
        self, symbols: Iterable[str], start: datetime, end: datetime
    ) -> dict[str, list[MarketBar]]:
        request = StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=TimeFrame(1, TimeFrameUnit.Minute),
            feed=DataFeed.SIP,
            start=start,
            end=end,
        )
        result = await asyncio.to_thread(self._data.get_stock_bars, request)
        return {s: [bar_to_model(b, "1m", s) for b in bars] for s, bars in result.data.items()}

    async def news(
        self, symbols: Iterable[str], start: datetime | None, limit: int = 50
    ) -> list[NewsArticleRaw]:
        request = NewsRequest(
            symbols=",".join(symbols), start=start, limit=limit, include_content=False
        )
        result = await asyncio.to_thread(self._news.get_news, request)
        items = getattr(result, "news", None)
        if items is None and hasattr(result, "data"):
            items = result.data.get("news", [])
        return [news_to_model(i) for i in (items or [])]


class AlpacaLiveBars:
    """Minute bars over the free IEX websocket (up to 30 symbols)."""

    def __init__(self, key: str, secret: str, feed: DataFeed = DataFeed.IEX) -> None:
        self._stream = StockDataStream(key, secret, feed=feed)

    async def run(
        self, symbols: Iterable[str], on_bar: Callable[[MarketBar], Awaitable[None]]
    ) -> None:
        async def handler(bar: Any) -> None:
            await on_bar(bar_to_model(bar, "1m"))

        self._stream.subscribe_bars(handler, *symbols)
        await self._stream._run_forever()

    async def stop(self) -> None:
        self._stream.stop()
        try:
            await self._stream.close()
        except Exception as exc:  # noqa: BLE001 - closing a dead socket is fine
            log.debug("stream close: %s", exc)


@dataclass(frozen=True)
class AccountInfo:
    status: str
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    pattern_day_trader: bool
    daytrade_count: int
    last_equity: Decimal | None = None  # the previous close's equity: the day's P&L baseline


def canon_symbol(symbol: str, asset_class: str = "") -> str:
    """Alpaca reports crypto positions as BTCUSD; the desk names them BTC/USD everywhere."""
    if "/" in symbol:
        return symbol
    if "crypto" in asset_class.lower() and symbol.endswith("USD"):
        return f"{symbol[:-3]}/USD"
    return symbol


@dataclass(frozen=True)
class PositionInfo:
    symbol: str
    qty: Decimal
    avg_entry: Decimal | None
    market_value: Decimal | None
    unrealized_pl: Decimal | None


@dataclass(frozen=True)
class BrokerOrder:
    broker_order_id: str
    status: str
    filled_qty: Decimal
    filled_avg_price: Decimal | None
    client_order_id: str = ""
    symbol: str = ""
    side: str = ""


class AlpacaTrading:
    def __init__(self, key: str, secret: str, paper: bool = True) -> None:
        self.paper = paper
        self._client = TradingClient(key, secret, paper=paper)

    async def account(self) -> AccountInfo:
        a = await asyncio.to_thread(self._client.get_account)
        return AccountInfo(
            status=str(getattr(a.status, "value", a.status)),
            equity=_dec(a.equity) or Decimal(0),
            cash=_dec(a.cash) or Decimal(0),
            buying_power=_dec(a.buying_power) or Decimal(0),
            pattern_day_trader=bool(a.pattern_day_trader),
            daytrade_count=int(a.daytrade_count or 0),
            last_equity=_dec(getattr(a, "last_equity", None)),
        )

    async def positions(self) -> list[PositionInfo]:
        rows = await asyncio.to_thread(self._client.get_all_positions)
        return [
            PositionInfo(
                symbol=canon_symbol(str(r.symbol), str(getattr(r, "asset_class", "") or "")),
                qty=_dec(r.qty) or Decimal(0),
                avg_entry=_dec(r.avg_entry_price),
                market_value=_dec(r.market_value),
                unrealized_pl=_dec(r.unrealized_pl),
            )
            for r in rows
        ]

    @staticmethod
    def _order(o: Any) -> BrokerOrder:
        return BrokerOrder(
            broker_order_id=str(o.id),
            status=str(getattr(o.status, "value", o.status)),
            filled_qty=_dec(o.filled_qty) or Decimal(0),
            filled_avg_price=_dec(o.filled_avg_price),
            client_order_id=str(getattr(o, "client_order_id", "") or ""),
            symbol=str(getattr(o, "symbol", "") or ""),
            side=str(getattr(getattr(o, "side", None), "value", getattr(o, "side", "")) or ""),
        )

    async def submit_market_order(
        self,
        symbol: str,
        qty: Decimal,
        side: str,
        client_order_id: str,
        *,
        stop_price: Decimal | None = None,
    ) -> BrokerOrder:
        """A market order; with `stop_price` a stop-loss leg rests at the broker once it fills
        (one-triggers-other), so the position is protected even if the desk is down. Crypto takes
        no legs and only GTC, so its lines live in the desk."""
        crypto = "/" in symbol
        request = MarketOrderRequest(
            symbol=symbol,
            qty=float(qty),
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.GTC if crypto else TimeInForce.DAY,
            client_order_id=client_order_id,
            order_class=OrderClass.OTO if stop_price is not None and not crypto else None,
            stop_loss=StopLossRequest(stop_price=float(stop_price))
            if stop_price is not None and not crypto
            else None,
        )
        return self._order(await asyncio.to_thread(self._client.submit_order, request))

    async def stop_leg(self, parent_order_id: str) -> BrokerOrder | None:
        """The resting stop that belongs to an entry, if the broker holds one."""
        o = await asyncio.to_thread(self._client.get_order_by_id, parent_order_id)
        for leg in getattr(o, "legs", None) or []:
            if str(getattr(getattr(leg, "type", None), "value", "")) in ("stop", "stop_limit"):
                return self._order(leg)
        return None

    async def replace_stop(self, order_id: str, stop_price: Decimal) -> BrokerOrder:
        request = ReplaceOrderRequest(stop_price=float(stop_price))
        return self._order(
            await asyncio.to_thread(self._client.replace_order_by_id, order_id, request)
        )

    async def cancel_order(self, order_id: str) -> None:
        await asyncio.to_thread(self._client.cancel_order_by_id, order_id)

    async def get_order(self, broker_order_id: str) -> BrokerOrder:
        return self._order(await asyncio.to_thread(self._client.get_order_by_id, broker_order_id))

    async def open_orders(self, limit: int = 100) -> list[BrokerOrder]:
        """Orders still working at the broker, whoever placed them."""
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=limit)
        rows = await asyncio.to_thread(self._client.get_orders, request)
        return [self._order(o) for o in rows]

    async def recent_orders(self, limit: int = 50) -> list[BrokerOrder]:
        request = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=limit)
        rows = await asyncio.to_thread(self._client.get_orders, request)
        return [self._order(o) for o in rows]
