"""Finnhub, free tier with an api key (DESK_FINNHUB_API_KEY): fundamental metrics and the earnings
calendar. Sixty calls a minute are allowed; the desk makes about sixty a day."""

from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request
from datetime import date
from typing import Any

BASE = "https://finnhub.io/api/v1"

METRICS = {
    "peTTM": "P/E",
    "pbAnnual": "price to book",
    "psTTM": "price to sales",
    "revenueGrowthTTMYoy": "revenue growth (yoy)",
    "epsGrowthTTMYoy": "EPS growth (yoy)",
    "netProfitMarginTTM": "net margin",
    "roeTTM": "return on equity",
    "52WeekHigh": "52-week high",
    "52WeekLow": "52-week low",
    "beta": "beta",
    "dividendYieldIndicatedAnnual": "dividend yield",
}


def parse_metrics(data: dict[str, Any]) -> dict[str, float | None]:
    raw = data.get("metric") or {}
    out: dict[str, float | None] = {}
    for key in METRICS:
        v = raw.get(key)
        try:
            out[key] = float(v) if v is not None else None
        except (TypeError, ValueError):
            out[key] = None
    return out


def parse_earnings(data: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for e in data.get("earningsCalendar") or []:
        try:
            out.append(
                {
                    "symbol": str(e.get("symbol", "")).upper(),
                    "date": date.fromisoformat(e["date"]),
                    "hour": e.get("hour") or "",
                    "eps_estimate": e.get("epsEstimate"),
                    "revenue_estimate": e.get("revenueEstimate"),
                }
            )
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(out, key=lambda x: x["date"])


class Finnhub:
    def __init__(self, api_key: str, *, timeout: float = 20.0) -> None:
        self.api_key = api_key
        self.timeout = timeout

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        query = urllib.parse.urlencode({**params, "token": self.api_key})
        with urllib.request.urlopen(f"{BASE}/{path}?{query}", timeout=self.timeout) as resp:
            return json.load(resp)

    async def metrics(self, symbol: str) -> dict[str, float | None]:
        return parse_metrics(
            await asyncio.to_thread(self._get, "stock/metric", symbol=symbol, metric="all")
        )

    async def earnings(self, symbol: str, start: date, end: date) -> list[dict[str, Any]]:
        data = await asyncio.to_thread(
            self._get,
            "calendar/earnings",
            symbol=symbol,
            **{"from": start.isoformat(), "to": end.isoformat()},
        )
        return parse_earnings(data)
