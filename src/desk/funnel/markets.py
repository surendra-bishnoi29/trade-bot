"""The markets the funnel can draw. A market is a list of instruments, a benchmark and a session
shape; everything else in the funnel is the same code. Stocks are traded by the desk; oil and gold
(through their ETFs) and crypto are watched until the desk's trading agents are wired to them."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from desk.core import calendar
from desk.core.sectors import REFERENCES, SECTORS


@dataclass(frozen=True)
class Market:
    key: str
    label: str
    symbols: tuple[str, ...]
    benchmark: str
    session: Literal["nyse", "utc_day"]
    traded: bool
    note: str = ""

    @property
    def all_symbols(self) -> tuple[str, ...]:
        return self.symbols if self.benchmark in self.symbols else (*self.symbols, self.benchmark)


COMMODITIES = ("USO", "BNO", "UNG", "XLE", "XOP", "GLD", "IAU", "SLV", "GDX", "CPER")
CRYPTO = (
    "BTC/USD",
    "ETH/USD",
    "SOL/USD",
    "XRP/USD",
    "DOGE/USD",
    "AVAX/USD",
    "LINK/USD",
    "LTC/USD",
    "BCH/USD",
    "UNI/USD",
    "AAVE/USD",
    "DOT/USD",
)

NAMES: dict[str, str] = {
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "Nvidia", "AVGO": "Broadcom", "GOOGL": "Alphabet",
    "META": "Meta", "NFLX": "Netflix", "AMZN": "Amazon", "TSLA": "Tesla", "HD": "Home Depot",
    "PG": "Procter & Gamble", "COST": "Costco", "KO": "Coca-Cola", "LLY": "Eli Lilly",
    "UNH": "UnitedHealth", "JNJ": "Johnson & Johnson", "JPM": "JPMorgan", "V": "Visa",
    "BRK.B": "Berkshire Hathaway", "GS": "Goldman Sachs", "CAT": "Caterpillar", "GE": "GE Aerospace",
    "UNP": "Union Pacific", "XOM": "Exxon Mobil", "CVX": "Chevron", "LIN": "Linde",
    "NEE": "NextEra Energy", "PLD": "Prologis", "SPY": "S&P 500 ETF", "QQQ": "Nasdaq 100 ETF",
    "USO": "US Oil Fund", "BNO": "Brent Oil Fund", "UNG": "US Natural Gas", "XLE": "Energy Sector",
    "XOP": "Oil & Gas Explorers", "GLD": "Gold Trust", "IAU": "iShares Gold", "SLV": "Silver Trust",
    "GDX": "Gold Miners", "CPER": "Copper Fund", "BTC/USD": "Bitcoin", "ETH/USD": "Ethereum",
    "SOL/USD": "Solana", "XRP/USD": "XRP", "DOGE/USD": "Dogecoin", "AVAX/USD": "Avalanche",
    "LINK/USD": "Chainlink", "LTC/USD": "Litecoin", "BCH/USD": "Bitcoin Cash", "UNI/USD": "Uniswap",
    "AAVE/USD": "Aave", "DOT/USD": "Polkadot",
}  # fmt: skip

GROUPS: dict[str, str] = {
    **SECTORS,
    "USO": "Oil", "BNO": "Oil", "UNG": "Gas", "XLE": "Energy equities", "XOP": "Energy equities",
    "GLD": "Gold", "IAU": "Gold", "SLV": "Silver", "GDX": "Gold miners", "CPER": "Copper",
}  # fmt: skip


def markets(universe: list[str]) -> dict[str, Market]:
    stocks = tuple(s for s in universe if s not in REFERENCES)
    return {
        "stocks": Market("stocks", "US stocks", stocks, "SPY", "nyse", True),
        "commodities": Market(
            "commodities",
            "Oil and gold",
            COMMODITIES,
            "GLD",
            "nyse",
            False,
            "Watched through their exchange-traded funds. The desk does not trade this market yet.",
        ),
        "crypto": Market(
            "crypto",
            "Crypto",
            CRYPTO,
            "BTC/USD",
            "utc_day",
            False,
            "Trades around the clock; the day runs midnight to midnight UTC. The desk does not trade this market yet.",
        ),
    }


def group_of(symbol: str) -> str:
    if "/" in symbol:
        return "Crypto"
    return GROUPS.get(symbol, "Other")


def session_window(market: Market, at: datetime) -> tuple[date, datetime, datetime]:
    """(day, open, close) of the session the moment belongs to. Before today's open on the NYSE
    calendar that is the previous session, so the funnel always has a day to show."""
    if market.session == "utc_day":
        start = at.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return start.date(), start, start + timedelta(days=1)
    day = calendar.session_of(at)
    open_at, close_at = calendar.session_bounds(day)
    return day, open_at, close_at
