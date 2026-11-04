"""The universe's sectors (decision A3: thirty large caps across the eleven GICS sectors, plus two
index references). Static on purpose; reviewed quarterly with the universe."""

from __future__ import annotations

SECTORS: dict[str, str] = {
    "AAPL": "Information Technology",
    "MSFT": "Information Technology",
    "NVDA": "Information Technology",
    "AVGO": "Information Technology",
    "GOOGL": "Communication Services",
    "META": "Communication Services",
    "NFLX": "Communication Services",
    "AMZN": "Consumer Discretionary",
    "TSLA": "Consumer Discretionary",
    "HD": "Consumer Discretionary",
    "PG": "Consumer Staples",
    "COST": "Consumer Staples",
    "KO": "Consumer Staples",
    "LLY": "Health Care",
    "UNH": "Health Care",
    "JNJ": "Health Care",
    "JPM": "Financials",
    "V": "Financials",
    "BRK.B": "Financials",
    "GS": "Financials",
    "CAT": "Industrials",
    "GE": "Industrials",
    "UNP": "Industrials",
    "XOM": "Energy",
    "CVX": "Energy",
    "LIN": "Materials",
    "NEE": "Utilities",
    "PLD": "Real Estate",
    "SPY": "Index",
    "QQQ": "Index",
}

REFERENCES = ("SPY", "QQQ")


def sector_of(symbol: str) -> str:
    return SECTORS.get(symbol.upper(), "Other")
