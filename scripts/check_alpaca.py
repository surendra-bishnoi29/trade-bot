"""Confirm the Alpaca paper keys in .env work: the paper account, free-tier history, and news.

Run:  uv run python scripts/check_alpaca.py
Prints account status and a few bars and headlines. Never prints the keys.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from dotenv import load_dotenv


def main() -> int:
    load_dotenv()
    key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        print("ALPACA_API_KEY or ALPACA_SECRET_KEY is missing from .env")
        return 1
    paper = os.environ.get("DESK_ALPACA_PAPER", "true").lower() == "true"

    account = TradingClient(key, secret, paper=paper).get_account()
    print(
        f"account: paper={paper} status={account.status} equity={account.equity} "
        f"buying_power={account.buying_power} pattern_day_trader={account.pattern_day_trader}"
    )

    now = datetime.now(UTC)
    data = StockHistoricalDataClient(key, secret)
    daily = data.get_stock_bars(
        StockBarsRequest(
            symbol_or_symbols="AAPL",
            timeframe=TimeFrame.Day,
            feed=DataFeed.SIP,
            start=now - timedelta(days=12),
            end=now - timedelta(days=1),
            limit=5,
        )
    )
    bars = daily.data.get("AAPL", []) if hasattr(daily, "data") else []
    print(f"daily bars (SIP, free tier): {len(bars)} returned")
    for b in bars[-3:]:
        print(f"  {b.timestamp:%Y-%m-%d}  o={b.open} h={b.high} l={b.low} c={b.close} v={b.volume}")

    minute = data.get_stock_bars(
        StockBarsRequest(
            symbol_or_symbols="AAPL",
            timeframe=TimeFrame(1, TimeFrameUnit.Minute),
            feed=DataFeed.SIP,
            start=now - timedelta(days=4),
            end=now - timedelta(minutes=20),
            limit=3,
        )
    )
    mbars = minute.data.get("AAPL", []) if hasattr(minute, "data") else []
    print(
        f"minute bars (SIP, free tier): {len(mbars)} returned; first at {mbars[0].timestamp if mbars else 'n/a'}"
    )

    news = NewsClient(key, secret).get_news(
        NewsRequest(symbols="AAPL", limit=3, include_content=False)
    )
    items = getattr(news, "news", None)
    if items is None and hasattr(news, "data"):
        items = news.data.get("news", [])
    items = items or []
    print(f"news (Benzinga via Alpaca): {len(items)} returned")
    for n in items:
        print(f"  {n.created_at:%Y-%m-%d %H:%M}Z  {n.source}: {n.headline[:90]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
