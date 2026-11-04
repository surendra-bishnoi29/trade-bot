"""The macro strategist: rates, the curve, volatility, the dollar, oil, the labour market and the
release calendar, read once a day before the open from FRED and published as a report. Deterministic.
Without a FRED key the regime strategist's price-based read stands in."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core import calendar
from desk.core.clock import Clock
from desk.core.events import Topics
from desk.data.fred import SERIES, Fred, when
from desk.storage.db import Database

log = logging.getLogger(__name__)

RUN_AT_UTC = (10, 45)  # before the first pre-market screen


def _last(series: list[tuple[date, float]]) -> float | None:
    return series[-1][1] if series else None


def _change(series: list[tuple[date, float]], back: int) -> float | None:
    if len(series) <= back:
        return None
    return series[-1][1] - series[-1 - back][1]


def macro_report(
    data: dict[str, list[tuple[date, float]]], releases: list[tuple[date, str]], now: datetime
) -> dict[str, Any]:
    ten = data.get("DGS10", [])
    two = data.get("DGS2", [])
    spread = data.get("T10Y2Y", [])
    vix = data.get("VIXCLS", [])
    dollar = data.get("DTWEXBGS", [])
    oil = data.get("DCOILWTICO", [])
    unrate = data.get("UNRATE", [])
    cpi = data.get("CPIAUCSL", [])
    fed = data.get("DFF", [])
    vix_now = _last(vix)
    vix_label = (
        "unknown"
        if vix_now is None
        else "calm"
        if vix_now < 15
        else "stressed"
        if vix_now > 25
        else "normal"
    )
    curve = _last(spread)
    ten_change = _change(ten, 20)
    parts: list[str] = []
    if _last(ten) is not None:
        parts.append(
            f"10-year {_last(ten):.2f}%"
            + (f" ({ten_change:+.2f} over 20 sessions)" if ten_change is not None else "")
        )
    if _last(two) is not None:
        parts.append(f"2-year {_last(two):.2f}%")
    if curve is not None:
        parts.append(f"curve {'inverted' if curve < 0 else 'positive'} at {curve:+.2f}")
    if _last(fed) is not None:
        parts.append(f"fed funds {_last(fed):.2f}%")
    if vix_now is not None:
        parts.append(f"VIX {vix_now:.1f} ({vix_label})")
    d20 = _change(dollar, 20)
    if _last(dollar) is not None and d20 is not None:
        parts.append(f"dollar {d20:+.1f} over 20 sessions")
    o20 = _change(oil, 20)
    if _last(oil) is not None:
        parts.append(
            f"WTI ${_last(oil):.0f}"
            + (f" ({o20:+.0f} over 20 sessions)" if o20 is not None else "")
        )
    if _last(unrate) is not None:
        parts.append(f"unemployment {_last(unrate):.1f}%")
    cpi_yoy = None
    if len(cpi) >= 13:
        cpi_yoy = cpi[-1][1] / cpi[-13][1] - 1
        parts.append(f"CPI {cpi_yoy * 100:.1f}% yoy")
    soon = [(d, name) for d, name in releases if 0 <= (d - now.date()).days <= 3]
    if soon:
        parts.append("ahead: " + ", ".join(f"{name} {when(d, now)}" for d, name in soon[:6]))
    if vix_now is not None and vix_now > 25:
        stance, conviction = "bearish", 0.6
    elif vix_now is not None and vix_now < 15 and (ten_change is None or ten_change <= 0):
        stance, conviction = "bullish", 0.4
    else:
        stance, conviction = "neutral", 0.2
    return {
        "analyst": "macro",
        "kind": "macro",
        "symbol": None,
        "stance": stance,
        "conviction": conviction,
        "horizon": "weeks",
        "summary": ("; ".join(parts) + ".") if parts else "No macro data returned.",
        "data": {
            "ten_year": _last(ten),
            "two_year": _last(two),
            "curve": curve,
            "fed_funds": _last(fed),
            "vix": vix_now,
            "vix_label": vix_label,
            "dollar_change_20": d20,
            "oil": _last(oil),
            "unemployment": _last(unrate),
            "cpi_yoy": cpi_yoy,
            "releases": [{"date": d.isoformat(), "name": name} for d, name in soon],
        },
    }


class MacroStrategist(BaseAgent):
    name = "macro"
    topics = ()

    def __init__(self, bus: Bus, clock: Clock, db: Database | None, fred: Fred) -> None:
        super().__init__(bus, clock)
        self.db = db
        self.fred = fred
        self.written_for: date | None = None

    async def exists_today(self) -> bool:
        if self.db is None:
            return False
        now = self.clock.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        for e in await self.db.recent_events([Topics.RESEARCH_REPORT], start, limit=100, until=now):
            if e.payload.get("kind") == "macro":
                return True
        return False

    async def run_once(self) -> dict[str, Any]:
        now = self.clock.now()
        start = now.date() - timedelta(days=420)
        data: dict[str, list[tuple[date, float]]] = {}
        for series_id in SERIES:
            try:
                data[series_id] = await self.fred.observations(series_id, start)
            except Exception as exc:  # noqa: BLE001 - one series missing must not stop the read
                log.warning("fred %s: %s", series_id, exc)
        try:
            releases = await self.fred.release_dates(now.date(), now.date() + timedelta(days=3))
        except Exception as exc:  # noqa: BLE001
            log.warning("fred releases: %s", exc)
            releases = []
        report = macro_report(data, releases, now)
        await self.emit(Topics.RESEARCH_REPORT, report, book="swing")
        self.written_for = now.date()
        log.info("macro: %s", report["summary"][:160])
        return report

    async def run_forever(self) -> None:
        now = self.clock.now()
        if calendar.is_session(now.date()) and not await self.exists_today():
            await self.run_once()
        while True:
            now = self.clock.now()
            target = now.replace(hour=RUN_AT_UTC[0], minute=RUN_AT_UTC[1], second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            while self.clock.now() < target:
                await self.clock.sleep(
                    min(60.0, max(1.0, (target - self.clock.now()).total_seconds()))
                )
            if calendar.is_session(self.clock.now().date()):
                try:
                    await self.run_once()
                except Exception as exc:  # noqa: BLE001
                    log.error("macro read failed: %s", exc)
