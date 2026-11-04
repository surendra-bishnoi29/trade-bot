"""The research team: independent analysts that each work on their own inputs and publish
research.report events the head trader reads at every cycle and in every answer.

All of them are deterministic (no model, no cost). The quant analyst reads the scorecard, the regime
strategist and the sector specialist react to it, the news analyst aggregates the scored articles.
Every report carries a stance, a conviction and a plain-words summary, and its event id is the
evidence the head trader can cite.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from pydantic import ConfigDict, Field

from desk.agents.base import BaseAgent
from desk.agents.scorecard import ScoreRow
from desk.bus.base import Bus
from desk.core.clock import Clock
from desk.core.events import Event, Topics, payload_for
from desk.core.payloads import Payload
from desk.core.sectors import REFERENCES, sector_of
from desk.storage.db import Database

log = logging.getLogger(__name__)

IMPACT_WEIGHT = {"low": 0.5, "medium": 1.0, "high": 1.5}


@payload_for(Topics.RESEARCH_REPORT)
class ResearchReport(Payload):
    """What every analyst publishes. Extra fields are allowed: each kind carries its own data."""

    model_config = ConfigDict(extra="allow")

    analyst: str
    kind: str = "report"
    symbol: str | None = None
    stance: str = "none"  # bullish | bearish | neutral | none
    conviction: float = 0.0
    horizon: str = ""
    summary: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


def _pct(v: float | None) -> str:
    """Returns and distances arrive as fractions (0.087 is 8.7%)."""
    return "n/a" if v is None else f"{v * 100:+.1f}%"


def vol_label(vol: float | None) -> str:
    if vol is None:
        return "unknown"
    return "low" if vol < 0.15 else "high" if vol > 0.25 else "normal"


# ---- the quant analyst: a technical read per name ----


def technical_report(r: ScoreRow, n_rows: int) -> dict[str, Any]:
    sma = r.sma20_rel or 0.0
    r20 = r.ret_20d or 0.0
    if sma > 0 and r20 > 0:
        trend, stance = "uptrend", "bullish"
    elif sma < 0 and r20 < 0:
        trend, stance = "downtrend", "bearish"
    else:
        trend, stance = "sideways", "neutral"
    conviction = round(0.3 + 0.6 * (1 - (r.rank - 1) / max(1, n_rows - 1)), 2) if r.rank else 0.3
    summary = (
        f"{trend}: {_pct(r.ret_20d)} over 20 days, {_pct(r.ret_5d)} over 5, {_pct(r.sma20_rel)} vs the "
        f"20-day average, {_pct(r.dist_high_20d)} from the 20-day high, {_pct(r.dist_low_20d)} from the "
        f"20-day low; volatility {f'{r.vol_20d:.0%}' if r.vol_20d is not None else 'n/a'} "
        f"({vol_label(r.vol_20d)}); relative volume {f'{r.rvol:.1f}x' if r.rvol is not None else 'n/a'}; "
        f"scorecard rank {r.rank}."
    )
    return {
        "analyst": "quant_analyst",
        "kind": "technical",
        "symbol": r.symbol,
        "stance": stance,
        "conviction": conviction,
        "horizon": "days",
        "summary": summary,
        "data": {
            "rank": r.rank,
            "score": r.score,
            "last_close": float(r.last_close) if r.last_close is not None else None,
            "ret_1d": r.ret_1d,
            "ret_5d": r.ret_5d,
            "ret_20d": r.ret_20d,
            "sma20_rel": r.sma20_rel,
            "sma50_rel": r.sma50_rel,
            "vol_20d": r.vol_20d,
            "rvol": r.rvol,
            "dist_high_20d": r.dist_high_20d,
            "dist_low_20d": r.dist_low_20d,
        },
    }


# ---- the regime strategist ----


def regime_report(ranked: list[dict[str, Any]]) -> dict[str, Any]:
    by = {r["symbol"]: r for r in ranked}
    names = [r for r in ranked if r["symbol"] not in REFERENCES]
    with_sma = [r for r in names if r.get("sma20_rel") is not None]
    breadth = sum(1 for r in with_sma if r["sma20_rel"] > 0) / len(with_sma) if with_sma else None
    with_5d = [r for r in names if r.get("ret_5d") is not None]
    up_week = sum(1 for r in with_5d if r["ret_5d"] > 0) / len(with_5d) if with_5d else None
    spy, qqq = by.get("SPY", {}), by.get("QQQ", {})
    spy_trend = spy.get("sma20_rel")
    vol = spy.get("vol_20d")
    if spy_trend is not None and breadth is not None:
        if spy_trend > 0 and breadth >= 0.6:
            label, stance = "risk-on", "bullish"
        elif spy_trend < 0 and breadth <= 0.4:
            label, stance = "risk-off", "bearish"
        else:
            label, stance = "mixed", "neutral"
    else:
        label, stance = "unknown", "none"
    parts = [f"{label} tape"]
    if spy:
        parts.append(
            f"SPY {_pct(spy.get('ret_20d'))} over 20 days and {_pct(spy_trend)} vs its 20-day average"
        )
    if qqq:
        parts.append(f"QQQ {_pct(qqq.get('ret_20d'))} over 20 days")
    if breadth is not None:
        parts.append(f"{breadth:.0%} of the universe above its 20-day average")
    if up_week is not None:
        parts.append(f"{up_week:.0%} up on the week")
    if vol is not None:
        parts.append(f"SPY realized volatility {vol:.0%} ({vol_label(vol)})")
    conviction = 0.0
    if breadth is not None:
        conviction = round(min(1.0, abs(breadth - 0.5) * 2), 2)
    return {
        "analyst": "regime",
        "kind": "regime",
        "symbol": None,
        "stance": stance,
        "conviction": conviction,
        "horizon": "days",
        "summary": "; ".join(parts) + ".",
        "data": {
            "label": label,
            "breadth_above_sma20": breadth,
            "share_up_week": up_week,
            "spy_sma20_rel": spy_trend,
            "spy_ret_20d": spy.get("ret_20d"),
            "qqq_ret_20d": qqq.get("ret_20d"),
            "spy_vol_20d": vol,
            "vol_label": vol_label(vol),
        },
    }


# ---- the sector specialist ----


def sector_report(ranked: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in ranked:
        if r["symbol"] in REFERENCES:
            continue
        groups.setdefault(sector_of(r["symbol"]), []).append(r)

    def avg(rows: list[dict[str, Any]], key: str) -> float | None:
        vals = [r[key] for r in rows if r.get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    table = []
    for sector, rows in groups.items():
        table.append(
            {
                "sector": sector,
                "names": [r["symbol"] for r in rows],
                "ret_5d": avg(rows, "ret_5d"),
                "ret_20d": avg(rows, "ret_20d"),
                "above_sma20": (
                    sum(1 for r in rows if (r.get("sma20_rel") or 0) > 0) / len(rows)
                    if rows
                    else None
                ),
            }
        )
    ranked_sectors = sorted(
        [t for t in table if t["ret_5d"] is not None], key=lambda t: t["ret_5d"], reverse=True
    )
    leaders = ranked_sectors[:3]
    laggards = ranked_sectors[-3:][::-1] if len(ranked_sectors) > 3 else []
    fmt = lambda t: (
        f"{t['sector']} ({_pct(t['ret_5d'])} on the week, {_pct(t['ret_20d'])} over 20 days)"
    )
    summary = ""
    if leaders:
        summary += "Leading: " + ", ".join(fmt(t) for t in leaders) + ". "
    if laggards:
        summary += "Lagging: " + ", ".join(fmt(t) for t in laggards) + "."
    spread = (leaders[0]["ret_5d"] - laggards[0]["ret_5d"]) if leaders and laggards else None
    return {
        "analyst": "sector",
        "kind": "sector",
        "symbol": None,
        "stance": "none",
        "conviction": round(min(1.0, abs(spread) * 100 / 5), 2) if spread is not None else 0.0,
        "horizon": "days",
        "summary": summary.strip() or "No sector data yet.",
        "data": {
            "sectors": table,
            "leaders": [t["sector"] for t in leaders],
            "laggards": [t["sector"] for t in laggards],
        },
    }


# ---- the news analyst ----


def news_state(symbol: str, items: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """items: scored articles with at, polarity, relevance, impact, confidence, event_type, title, source."""
    recent = [i for i in items if now - i["at"] <= timedelta(hours=48)]
    day = [i for i in recent if now - i["at"] <= timedelta(hours=24)]
    weights = [
        float(i.get("relevance", 0))
        * IMPACT_WEIGHT.get(i.get("impact", "low"), 0.5)
        * float(i.get("confidence", 0.5))
        for i in recent
    ]
    total = sum(weights)
    tone = (
        sum(float(i.get("polarity", 0)) * w for i, w in zip(recent, weights, strict=True)) / total
        if total
        else 0.0
    )
    impact_rank = {"low": 0, "medium": 1, "high": 2}
    impact_max = max(
        (i.get("impact", "low") for i in recent),
        key=lambda x: impact_rank.get(x, 0),
        default="none",
    )
    themes = [t for t, _ in Counter(i.get("event_type", "other") for i in recent).most_common(3)]
    top = sorted(zip(recent, weights, strict=True), key=lambda x: x[1], reverse=True)[:3]
    headlines = [
        {
            "title": i.get("title") or i.get("article_id"),
            "source": i.get("source"),
            "polarity": i.get("polarity"),
            "impact": i.get("impact"),
            "article_id": i.get("article_id"),
        }
        for i, _ in top
    ]
    if len(recent) >= 2 and tone > 0.2:
        stance = "bullish"
    elif len(recent) >= 2 and tone < -0.2:
        stance = "bearish"
    else:
        stance = "neutral"
    conviction = round(min(1.0, total / 5) * min(1.0, abs(tone) * 2), 2) if recent else 0.0
    if not recent:
        summary = "No news in the last 48 hours."
    else:
        summary = (
            f"{len(day)} articles in 24h ({len(recent)} in 48h), tone {tone:+.2f}, strongest impact "
            f"{impact_max}; themes: {', '.join(themes) or 'none'}. "
            + " ".join(
                f"'{h['title']}' ({h['source']}, tone {float(h['polarity'] or 0):+.1f}, {h['impact']})."
                for h in headlines[:2]
            )
        )
    return {
        "analyst": "news_analyst",
        "kind": "news",
        "symbol": symbol,
        "stance": stance,
        "conviction": conviction,
        "horizon": "days",
        "summary": summary,
        "evidence_ids": [str(h["article_id"]) for h in headlines if h.get("article_id")],
        "data": {
            "tone_48h": round(tone, 3),
            "count_24h": len(day),
            "count_48h": len(recent),
            "impact_max": impact_max,
            "themes": themes,
            "headlines": headlines,
        },
    }


class RegimeStrategist(BaseAgent):
    """Reads the universe as a whole: trend of the references, breadth, volatility."""

    name = "regime"
    topics = (Topics.RESEARCH_REPORT,)

    async def handle(self, event: Event) -> None:
        if event.payload.get("kind") != "scorecard" or not event.payload.get("ranked"):
            return
        report = regime_report(event.payload["ranked"])
        await self.emit(Topics.RESEARCH_REPORT, report, book="swing", caused_by=event)


class SectorSpecialist(BaseAgent):
    name = "sector"
    topics = (Topics.RESEARCH_REPORT,)

    async def handle(self, event: Event) -> None:
        if event.payload.get("kind") != "scorecard" or not event.payload.get("ranked"):
            return
        report = sector_report(event.payload["ranked"])
        await self.emit(Topics.RESEARCH_REPORT, report, book="swing", caused_by=event)


class NewsAnalyst(BaseAgent):
    """Aggregates the scored articles per name into a current news state, at most once per name every
    few minutes, and publishes it as a report."""

    name = "news_analyst"
    topics = (Topics.NEWS_ARTICLE_RAW, Topics.NEWS_ARTICLE_SCORED)

    def __init__(
        self,
        bus: Bus,
        clock: Clock,
        db: Database | None,
        *,
        debounce: timedelta = timedelta(minutes=5),
    ) -> None:
        super().__init__(bus, clock)
        self.db = db
        self.debounce = debounce
        self.titles: dict[str, dict[str, Any]] = {}
        self.items: dict[str, list[dict[str, Any]]] = {}
        self.last_report: dict[str, datetime] = {}
        self.pending: set[str] = set()

    async def warm(self, hours: int = 48) -> int:
        """Rebuild the state from the journal after a start, without publishing anything."""
        if self.db is None:
            return 0
        since = self.clock.now() - timedelta(hours=hours)
        for e in await self.db.recent_events([Topics.NEWS_ARTICLE_RAW], since, limit=2000):
            self._remember_title(e)
        n = 0
        for e in await self.db.recent_events([Topics.NEWS_ARTICLE_SCORED], since, limit=2000):
            if self._remember_score(e):
                n += 1
        return n

    def _remember_title(self, event: Event) -> None:
        p = event.payload
        self.titles[str(p.get("article_id"))] = {
            "title": p.get("title"),
            "source": p.get("source"),
            "published_at": p.get("published_at"),
        }

    def _remember_score(self, event: Event) -> bool:
        if not event.symbol:
            return False
        p = event.payload
        aid = str(p.get("article_id"))
        rows = self.items.setdefault(event.symbol, [])
        if any(i["article_id"] == aid for i in rows):
            return False
        title = self.titles.get(aid, {})
        rows.append(
            {
                "article_id": aid,
                "at": event.occurred_at,
                "polarity": p.get("polarity", 0.0),
                "relevance": p.get("relevance", 0.0),
                "impact": p.get("impact", "low"),
                "confidence": p.get("confidence", 0.5),
                "event_type": p.get("event_type", "other"),
                "title": title.get("title"),
                "source": title.get("source"),
            }
        )
        cutoff = self.clock.now() - timedelta(hours=72)
        self.items[event.symbol] = [i for i in rows if i["at"] >= cutoff]
        return True

    def state_for(self, symbol: str) -> dict[str, Any]:
        return news_state(symbol, self.items.get(symbol, []), self.clock.now())

    async def handle(self, event: Event) -> None:
        if event.type == Topics.NEWS_ARTICLE_RAW:
            self._remember_title(event)
            return
        if not self._remember_score(event):
            return
        symbol = event.symbol or ""
        now = self.clock.now()
        last = self.last_report.get(symbol)
        if last is not None and now - last < self.debounce:
            self.pending.add(symbol)
            return
        await self._publish(symbol, event)

    async def _publish(self, symbol: str, caused_by: Event | None) -> None:
        report = self.state_for(symbol)
        self.last_report[symbol] = self.clock.now()
        self.pending.discard(symbol)
        await self.emit(
            Topics.NEWS_STATE,
            report["data"] | {"symbol": symbol},
            symbol=symbol,
            book="swing",
            caused_by=caused_by,
        )
        await self.emit(
            Topics.RESEARCH_REPORT, report, symbol=symbol, book="swing", caused_by=caused_by
        )

    async def flush_forever(self, every_s: float = 60.0) -> None:
        """Names that were debounced get their report once the quiet period has passed."""
        while True:
            await self.clock.sleep(every_s)
            now = self.clock.now()
            for symbol in list(self.pending):
                last = self.last_report.get(symbol)
                if last is None or now - last >= self.debounce:
                    await self._publish(symbol, None)


# ---- what the head trader reads ----


async def research_digest(
    db: Database, now: datetime, symbols: list[str], *, hours: int = 24, limit: int = 1500
) -> str:
    """The latest report of each analyst, for the desk and for the given names, with event ids."""
    events = await db.recent_events(
        [Topics.RESEARCH_REPORT], now - timedelta(hours=hours), limit=limit, until=now
    )
    latest: dict[tuple[str, str, str | None], Event] = {}
    for e in events:  # newest first
        p = e.payload
        key = (p.get("analyst", ""), p.get("kind", ""), p.get("symbol"))
        latest.setdefault(key, e)

    def line(e: Event) -> str:
        p = e.payload
        who = f"{p.get('analyst')} {p.get('kind')}" + (
            f" {p.get('symbol')}" if p.get("symbol") else ""
        )
        stance = (
            f"{p.get('stance')} ({float(p.get('conviction', 0)):.2f})"
            if p.get("stance") not in (None, "none")
            else ""
        )
        return f"[{e.event_id}] {e.occurred_at:%H:%M} UTC {who}: {stance} {str(p.get('summary', ''))[:400]}".replace(
            "  ", " "
        )

    lines: list[str] = []
    for key in (
        ("macro", "macro", None),
        ("regime", "regime", None),
        ("sector", "sector", None),
        ("intraday", "playbook", None),
    ):
        if key in latest:
            lines.append(line(latest[key]))
    note = next((e for (a, k, _), e in latest.items() if k == "morning_note"), None)
    if note is not None:
        n = note.payload.get("note") or {}
        lines.append(f"[{note.event_id}] morning note: {n.get('headline', '')}")
    wanted = {s.upper() for s in symbols}
    for (analyst, kind, symbol), e in sorted(
        latest.items(), key=lambda kv: (kv[0][2] or "", kv[0][0])
    ):
        if (
            symbol
            and symbol.upper() in wanted
            and kind in ("technical", "forecast", "news", "filing", "fundamental", "event")
        ):
            lines.append(line(e))
    return "\n".join(lines) if lines else "no research reports yet"
