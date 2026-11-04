"""FRED, the St. Louis Fed's data service: free with an api key (DESK_FRED_API_KEY). Daily series for
the macro strategist and the calendar of upcoming releases."""

from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request
from datetime import date, datetime
from typing import Any

BASE = "https://api.stlouisfed.org/fred"

SERIES = {
    "DGS10": "10-year Treasury yield",
    "DGS2": "2-year Treasury yield",
    "T10Y2Y": "10-year minus 2-year spread",
    "DFF": "effective fed funds rate",
    "VIXCLS": "VIX",
    "DTWEXBGS": "trade-weighted dollar",
    "DCOILWTICO": "WTI crude",
    "UNRATE": "unemployment rate",
    "CPIAUCSL": "CPI (index)",
}


def parse_observations(data: dict[str, Any]) -> list[tuple[date, float]]:
    out = []
    for o in data.get("observations", []):
        v = o.get("value")
        if v in (None, ".", ""):
            continue
        try:
            out.append((date.fromisoformat(o["date"]), float(v)))
        except (KeyError, ValueError):
            continue
    return out


def parse_release_dates(data: dict[str, Any]) -> list[tuple[date, str]]:
    out = []
    for r in data.get("release_dates", []):
        try:
            out.append((date.fromisoformat(r["date"]), str(r.get("release_name", ""))))
        except (KeyError, ValueError):
            continue
    return out


class Fred:
    def __init__(self, api_key: str, *, timeout: float = 20.0) -> None:
        self.api_key = api_key
        self.timeout = timeout

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        query = urllib.parse.urlencode({**params, "api_key": self.api_key, "file_type": "json"})
        with urllib.request.urlopen(f"{BASE}/{path}?{query}", timeout=self.timeout) as resp:
            return json.load(resp)

    async def observations(self, series_id: str, start: date) -> list[tuple[date, float]]:
        data = await asyncio.to_thread(
            self._get,
            "series/observations",
            series_id=series_id,
            observation_start=start.isoformat(),
        )
        return parse_observations(data)

    async def release_dates(self, start: date, end: date) -> list[tuple[date, str]]:
        data = await asyncio.to_thread(
            self._get,
            "releases/dates",
            realtime_start=start.isoformat(),
            realtime_end=end.isoformat(),
            include_release_dates_with_no_data="true",
            sort_order="asc",
            limit=200,
        )
        return [(d, name) for d, name in parse_release_dates(data) if start <= d <= end]


def when(d: date, now: datetime) -> str:
    days = (d - now.date()).days
    return "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
