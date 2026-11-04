"""The scorecard: deterministic triage of the universe from stored bars, news scores and alerts.

Not a forecast by itself. It ranks names so the head trader looks at the right ten. When a tested
model (phase 2) has a champion, its expected return is one of the inputs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import mean, pstdev

from desk.core import calendar
from desk.core.events import Topics
from desk.storage.db import Database

IMPACT_WEIGHT = {"low": 0.5, "medium": 1.0, "high": 2.0}


@dataclass
class ScoreRow:
    symbol: str
    last_close: Decimal | None = None
    ret_1d: float | None = None
    ret_5d: float | None = None
    ret_20d: float | None = None
    vol_20d: float | None = None
    rvol: float | None = None
    dist_high_20d: float | None = None
    dist_low_20d: float | None = None
    sma20_rel: float | None = None
    sma50_rel: float | None = None
    news_1d: float = 0.0
    news_5d: float = 0.0
    news_count_5d: int = 0
    news_max_impact_5d: str = "none"
    alerts_24h: int = 0
    position_qty: Decimal = Decimal(0)
    position_pnl_pct: float | None = None
    forecast: float | None = None  # the champion model's expected return, when one exists
    forecast_horizon: int = 5
    score: float = 0.0
    rank: int = 0


def _pct(a: Decimal | float, b: Decimal | float) -> float:
    return float(a) / float(b) - 1.0 if float(b) else 0.0


def _news_blend(items: list[dict], since: datetime) -> tuple[float, int, str]:
    weights, total, count, top = 0.0, 0.0, 0, "none"
    order = {"none": 0, "low": 1, "medium": 2, "high": 3}
    for it in items:
        if it["occurred_at"] < since:
            continue
        w = IMPACT_WEIGHT.get(it.get("impact", "low"), 0.5) * float(it.get("confidence", 0.5))
        weights += w
        total += float(it.get("polarity", 0)) * float(it.get("relevance", 0)) * w
        count += 1
        if order.get(it.get("impact", "low"), 0) > order[top]:
            top = it.get("impact", "low")
    return (total / weights if weights else 0.0), count, top


def _rank_scores(values: dict[str, float | None]) -> dict[str, float]:
    """0 to 1 by rank among the symbols that have a value; missing values score 0.5."""
    present = sorted((v, s) for s, v in values.items() if v is not None)
    n = len(present)
    ranked = {s: (i / (n - 1) if n > 1 else 0.5) for i, (_, s) in enumerate(present)}
    return {s: ranked.get(s, 0.5) for s in values}


def compute_scorecard(
    bars: dict[str, list[dict]],
    news: dict[str, list[dict]],
    alerts: dict[str, int],
    positions: dict[str, tuple[Decimal, Decimal | None]],
    now: datetime,
    forecasts: dict[str, float] | None = None,
    forecast_horizon: int = 5,
) -> list[ScoreRow]:
    rows: dict[str, ScoreRow] = {}
    for symbol, series in bars.items():
        row = ScoreRow(symbol=symbol)
        closes = [Decimal(b["close"]) for b in series]
        volumes = [int(b["volume"]) for b in series]
        highs = [Decimal(b["high"]) for b in series]
        lows = [Decimal(b["low"]) for b in series]
        if closes:
            row.last_close = closes[-1]
        if len(closes) >= 2:
            row.ret_1d = _pct(closes[-1], closes[-2])
        if len(closes) >= 6:
            row.ret_5d = _pct(closes[-1], closes[-6])
        if len(closes) >= 21:
            row.ret_20d = _pct(closes[-1], closes[-21])
            logs = [math.log(float(closes[i]) / float(closes[i - 1])) for i in range(-20, 0)]
            row.vol_20d = pstdev(logs) * math.sqrt(252)
            row.rvol = volumes[-1] / mean(volumes[-21:-1]) if mean(volumes[-21:-1]) else None
            row.dist_high_20d = _pct(closes[-1], max(highs[-20:]))
            row.dist_low_20d = _pct(closes[-1], min(lows[-20:]))
            row.sma20_rel = _pct(closes[-1], mean(closes[-20:]))
        if len(closes) >= 50:
            row.sma50_rel = _pct(closes[-1], mean(closes[-50:]))
        items = news.get(symbol, [])
        row.news_1d, _, _ = _news_blend(items, now - timedelta(days=1))
        row.news_5d, row.news_count_5d, row.news_max_impact_5d = _news_blend(
            items, now - timedelta(days=5)
        )
        row.alerts_24h = alerts.get(symbol, 0)
        if forecasts and symbol in forecasts:
            row.forecast = forecasts[symbol]
            row.forecast_horizon = forecast_horizon
        if symbol in positions:
            qty, avg_entry = positions[symbol]
            row.position_qty = qty
            if avg_entry and row.last_close:
                row.position_pnl_pct = _pct(row.last_close, avg_entry)
        rows[symbol] = row

    r20 = _rank_scores({s: r.ret_20d for s, r in rows.items()})
    r5 = _rank_scores({s: r.ret_5d for s, r in rows.items()})
    rnews = _rank_scores({s: (r.news_5d if r.news_count_5d else None) for s, r in rows.items()})
    rvol = _rank_scores(
        {s: (-r.vol_20d if r.vol_20d is not None else None) for s, r in rows.items()}
    )
    with_forecast = bool(forecasts) and any(r.forecast is not None for r in rows.values())
    rfc = _rank_scores({s: r.forecast for s, r in rows.items()}) if with_forecast else {}
    for s, r in rows.items():
        if with_forecast:
            # the tested model takes a third of the score; the triage inputs share the rest
            r.score = round(
                0.3 * rfc[s] + 0.3 * r20[s] + 0.15 * r5[s] + 0.2 * rnews[s] + 0.05 * rvol[s], 4
            )
        else:
            r.score = round(0.4 * r20[s] + 0.2 * r5[s] + 0.3 * rnews[s] + 0.1 * rvol[s], 4)
    ordered = sorted(rows.values(), key=lambda r: r.score, reverse=True)
    for i, r in enumerate(ordered, 1):
        r.rank = i
    return ordered


def _f(v: float | None, pct: bool = True) -> str:
    if v is None:
        return "n/a"
    return f"{v * 100:+.1f}%" if pct else f"{v:.2f}"


def render_scorecard(rows: list[ScoreRow], top_n: int = 10) -> str:
    chosen = [r for r in rows if r.rank <= top_n or r.position_qty]
    with_forecast = any(r.forecast is not None for r in rows)
    horizon = next((r.forecast_horizon for r in rows if r.forecast is not None), 5)
    lines = [
        "rank symbol close ret1d ret5d ret20d vol20 rvol dHi20 dLo20 sma20 sma50 news1d news5d(n,max) alerts24h position"
        + (f" fcst{horizon}d" if with_forecast else "")
    ]
    for r in chosen:
        pos = f"{r.position_qty} ({_f(r.position_pnl_pct)})" if r.position_qty else "-"
        lines.append(
            f"{r.rank:>2} {r.symbol:<6} {r.last_close} {_f(r.ret_1d)} {_f(r.ret_5d)} {_f(r.ret_20d)} {_f(r.vol_20d)} "
            f"{_f(r.rvol, pct=False)} {_f(r.dist_high_20d)} {_f(r.dist_low_20d)} {_f(r.sma20_rel)} {_f(r.sma50_rel)} "
            f"{r.news_1d:+.2f} {r.news_5d:+.2f}({r.news_count_5d},{r.news_max_impact_5d}) {r.alerts_24h} {pos}"
            + (f" {_f(r.forecast)}" if with_forecast else "")
        )
    if with_forecast:
        lines.append(
            f"fcst{horizon}d: the champion model's expected {horizon}-session return (a third of the score when present)"
        )
    return "\n".join(lines)


async def build_scorecard(
    db: Database,
    universe: list[str],
    positions: dict[str, tuple[Decimal, Decimal | None]],
    now: datetime,
    forecasts: dict[str, float] | None = None,
) -> list[ScoreRow]:
    bars: dict[str, list[dict]] = {}
    news: dict[str, list[dict]] = {}
    alerts: dict[str, int] = {}
    in_session = calendar.is_session(now.date()) and now < calendar.session_bounds(now.date())[1]
    for symbol in universe:
        daily = await db.recent_bars(symbol, "1d", limit=61, before=now)
        if in_session:
            # today's daily bar is only trusted after the close; during the session the latest minute
            # bar stands in for it, so the scorecard never reads a close that has not happened yet
            daily = [b for b in daily if b["ts"].date() != now.date()]
            latest = await db.recent_bars(symbol, "1m", limit=1, before=now)
            if latest and latest[0]["ts"].date() == now.date():
                b = latest[0]
                daily = daily + [
                    {
                        "ts": b["ts"],
                        "open": b["close"],
                        "high": b["close"],
                        "low": b["close"],
                        "close": b["close"],
                        "volume": b["volume"] or 0,
                    }
                ]
        bars[symbol] = daily[-60:]
        scored = await db.recent_events(
            [Topics.NEWS_ARTICLE_SCORED],
            now - timedelta(days=5),
            symbol=symbol,
            limit=200,
            until=now,
        )
        news[symbol] = [{**e.payload, "occurred_at": e.occurred_at} for e in scored]
    for e in await db.recent_events(
        [Topics.WATCHER_ALERT], now - timedelta(hours=24), limit=500, until=now
    ):
        if e.symbol:
            alerts[e.symbol] = alerts.get(e.symbol, 0) + 1
    horizon = 5
    if forecasts is None:
        forecasts = {}
        for e in await db.recent_events(
            [Topics.RESEARCH_REPORT], now - timedelta(hours=24), limit=400, until=now
        ):
            p = e.payload
            if p.get("kind") == "forecast" and p.get("symbol") and p["symbol"] not in forecasts:
                data = p.get("data") or {}
                try:
                    forecasts[p["symbol"]] = float(data.get("exp_ret"))
                    horizon = int(data.get("horizon_sessions") or horizon)
                except (TypeError, ValueError):
                    continue
    return compute_scorecard(bars, news, alerts, positions, now, forecasts or None, horizon)
