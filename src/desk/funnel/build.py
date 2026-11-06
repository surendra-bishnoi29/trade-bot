"""The funnel: every watched instrument's day as one line, then the ranking that narrows them to
the names the desk acts on, with the evidence for each step.

One build is one moment in time. Prices come from the minute bars, attention from the watcher's
alerts, tone from the news analyst, and the last two checks from the desk's own journal: what the
head trader proposed, what the skeptic said, what the risk officer approved and what was filled.
The interface draws this; the head trader reads the same thing as text (`digest`). Deterministic,
no model calls."""

from __future__ import annotations

import itertools
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from desk.core.events import Topics
from desk.funnel.markets import NAMES, Market, group_of, session_window
from desk.storage.db import Database

STEP_MINUTES = 5
CHECKS = ("watcher", "quant", "news", "head", "risk")
WEIGHTS = {"price": 0.40, "trend": 0.20, "news": 0.15, "attention": 0.15, "market": 0.10}


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or not b:
        return None
    return (a / b - 1) * 100


def _f(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _verdict(x: float | None, eps: float = 0.0) -> str:
    if x is None:
        return "neutral"
    return "bullish" if x > eps else "bearish" if x < -eps else "neutral"


def first_sentence(text: str, limit: int = 180) -> str:
    text = (text or "").strip().replace("\n", " ")
    cut = text.find(". ")
    out = text if cut < 0 else text[: cut + 1]
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


AGENT_LABEL = {
    "watcher": "Watcher",
    "news": "News agent",
    "news_analyst": "News analyst",
    "quant_analyst": "Quant analyst",
    "quant_screen": "Quant analyst",
    "regime": "Regime strategist",
    "sector": "Sector specialist",
    "cycle": "Head trader",
    "head_trader": "Head trader",
    "risk": "Risk officer",
    "execution": "Execution",
    "swing_strategy": "Thesis monitor",
    "back_office": "Back office",
    "filings": "Filing analyst",
    "macro": "Macro strategist",
    "fundamentals": "Fundamentals",
    "reviewer": "Reviewer",
    "intraday": "Intraday book",
    "forecaster": "Forecaster",
}


def describe(e: Any) -> str:
    """One plain line for an event on the wire or in an instrument's trail."""
    from desk.core.notices import summarize

    p = e.payload
    if e.type == Topics.WATCHER_ALERT:
        d = p.get("details") or {}
        if p.get("kind") == "volume_spike":
            return f"{e.symbol} volume spike: {d.get('volume')} shares in the minute, {d.get('multiple')} times its usual"
        if p.get("kind") == "abnormal_move":
            pc = _f(d.get("pct_change"))
            return (
                f"{e.symbol} sharp move: {pc:+.2f}% in {d.get('bars')} minutes, to {d.get('close')}"
                if pc is not None
                else f"{e.symbol} sharp move"
            )
        return f"{e.symbol} {str(p.get('kind', 'alert')).replace('_', ' ')}"
    if e.type == Topics.NEWS_ARTICLE_RAW:
        return f"{e.symbol or ''} headline: {p.get('title', '')}".strip()
    if e.type == Topics.ORDER_FILLED:
        verb = {"buy": "Bought", "sell": "Sold"}.get(str(p.get("side")), "Filled")
        return f"{verb} {p.get('filled_quantity')} {e.symbol} at {p.get('avg_price')}"
    if e.type == Topics.CYCLE_PLAN:
        plan = p.get("plan") or {}
        acts = [d for d in plan.get("decisions") or [] if d.get("action") in ("buy", "sell")]
        said = ", ".join(
            f"{d['action']} {d.get('symbol')} at conviction {float(d.get('conviction') or 0):.2f}"
            for d in acts
        )
        return f"Review ({p.get('trigger', '')}): " + (said or "no action")
    if e.type == Topics.THESIS_CHALLENGE:
        obj = (p.get("challenge") or p).get("objections") or []
        if obj:
            return "Skeptic: " + "; ".join(
                f"{o.get('severity')} on {o.get('symbol')}" for o in obj[:3]
            )
        return "Skeptic: no objection"
    text = summarize(e)
    for mark in ("✅ ", "⛔ ", "🧭 ", "🏁 ", "🛑 ", "🟢 ", "⚠️ ", "🧠 ", "🧐 "):
        text = text.replace(mark, "")
    return text


WIRE_TYPES = [
    Topics.WATCHER_ALERT,
    Topics.NEWS_ARTICLE_RAW,
    Topics.CYCLE_PLAN,
    Topics.RISK_APPROVED,
    Topics.RISK_REJECTED,
    Topics.ORDER_FILLED,
    Topics.THESIS_CLOSED,
    Topics.RISK_KILL_SWITCH,
]


# ---- one instrument's day from its bars ----


def day_shape(
    minute: list[dict[str, Any]],
    daily: list[dict[str, Any]],
    grid: list[datetime],
) -> dict[str, Any] | None:
    """Path, levels and volume profile for one instrument. `daily` holds bars before the session."""
    if not minute:
        return None
    closes = [float(b["close"]) for b in minute]
    highs = [float(b["high"]) for b in minute]
    lows = [float(b["low"]) for b in minute]
    vols = [float(b["volume"] or 0) for b in minute]
    open_px, last = float(minute[0]["open"]), closes[-1]
    prior = float(daily[-1]["close"]) if daily else open_px
    d_closes = [float(b["close"]) for b in daily[-20:]]
    avg20 = sum(d_closes) / len(d_closes) if d_closes else None
    hi20 = max((float(b["high"]) for b in daily[-20:]), default=None)
    lo20 = min((float(b["low"]) for b in daily[-20:]), default=None)
    ret20 = _pct(prior, float(daily[-21]["close"])) if len(daily) >= 21 else None
    # the line: percent against the prior close on a fixed time grid, forward-filled
    path: list[float | None] = []
    i, cur = 0, None
    for t in grid:
        while i < len(minute) and minute[i]["ts"] <= t:
            cur = closes[i]
            i += 1
        path.append(round((cur / prior - 1) * 100, 3) if cur is not None and prior else None)
    notional = sum(c * v for c, v in zip(closes, vols, strict=True))
    volume = sum(vols)
    vwap = notional / volume if volume else last
    s_hi, s_lo = max(highs), min(lows)
    # volume by price, twelve rows from the session low to its high
    rows = 12
    profile = [0.0] * rows
    span = (s_hi - s_lo) or 1.0
    for c, v in zip(closes, vols, strict=True):
        profile[min(rows - 1, int((c - s_lo) / span * rows))] += v
    top = max(profile) or 1.0
    return {
        "last": last,
        "open": open_px,
        "prior_close": prior,
        "day_pct": _pct(last, prior),
        "open_pct": _pct(last, open_px),
        "vwap": vwap,
        "vs_vwap_pct": _pct(last, vwap),
        "session_high": s_hi,
        "session_low": s_lo,
        "avg20": avg20,
        "vs_avg20_pct": _pct(last, avg20),
        "hi20": hi20,
        "lo20": lo20,
        "ret20_pct": ret20,
        "path": path,
        "profile": [round(v / top, 3) for v in reversed(profile)],  # highest price first
        "bars": len(minute),
        "last_at": minute[-1]["ts"],
    }


# ---- the score, its evidence, the checks, the book ----


def score(
    shape: dict[str, Any],
    alerts: dict[str, int],
    news: dict[str, Any] | None,
    market_day_pct: float | None,
    benchmark_label: str,
) -> tuple[float, list[dict[str, Any]]]:
    """A sentiment score from −100 to +100 and the readings behind it, each with its source."""
    day, vs_vwap, vs_avg = shape["day_pct"], shape["vs_vwap_pct"], shape["vs_avg20_pct"]
    price = 0.6 * _clamp((day or 0) / 3) + 0.4 * _clamp((vs_vwap or 0) / 1)
    trend = _clamp((vs_avg or 0) / 5)
    tone = _f((news or {}).get("tone_48h"))
    count = int((news or {}).get("count_24h") or 0)
    news_part = _clamp((tone or 0) / 0.5) if news and (count or tone) else 0.0
    n_alerts = sum(alerts.values())
    attention = (1 if price > 0 else -1 if price < 0 else 0) * min(1.0, n_alerts / 5)
    market = _clamp((market_day_pct or 0) / 2)
    total = 100 * (
        WEIGHTS["price"] * price
        + WEIGHTS["trend"] * trend
        + WEIGHTS["news"] * news_part
        + WEIGHTS["attention"] * attention
        + WEIGHTS["market"] * market
    )
    ev = [
        {
            "label": "Price on the day",
            "value": f"{day:+.2f}%" if day is not None else "n/a",
            "verdict": _verdict(day, 0.15),
            "source": "Price feed",
        },
        {
            "label": "Against today's average price (VWAP)",
            "value": f"{vs_vwap:+.2f}%" if vs_vwap is not None else "n/a",
            "verdict": _verdict(vs_vwap, 0.1),
            "source": "Quant analyst",
        },
        {
            "label": "Against its 20-day average",
            "value": f"{vs_avg:+.1f}%" if vs_avg is not None else "n/a",
            "verdict": _verdict(vs_avg, 1.0),
            "source": "Quant analyst",
        },
        {
            "label": f"News tone, {count} article{'s' if count != 1 else ''} in 24 hours",
            "value": f"{tone:+.2f}" if tone is not None else "none",
            "verdict": _verdict(tone, 0.1) if news else "neutral",
            "source": "News analyst",
        },
        {
            "label": "Unusual moves and volume flagged today",
            "value": str(n_alerts),
            "verdict": _verdict(attention),
            "source": "Watcher",
        },
        {
            "label": f"Market: {benchmark_label} on the day",
            "value": f"{market_day_pct:+.2f}%" if market_day_pct is not None else "n/a",
            "verdict": _verdict(market_day_pct, 0.3),
            "source": "Regime strategist",
        },
    ]
    return round(total, 1), ev


def setup_tag(shape: dict[str, Any], side: str, desk: dict[str, Any]) -> str:
    if desk.get("filled_today"):
        return "BOUGHT TODAY" if desk["filled_today"] == "buy" else "SOLD TODAY"
    if desk.get("held"):
        return "HELD"
    if desk.get("blocked"):
        return "BLOCKED BY SKEPTIC"
    if desk.get("proposed"):
        return "PROPOSED"
    last, day, vs_avg = shape["last"], shape["day_pct"] or 0, shape["vs_avg20_pct"] or 0
    if side == "bull":
        if shape["hi20"] and last >= shape["hi20"]:
            return "NEW 20-DAY HIGH"
        if day >= 1 and last >= shape["session_high"] * 0.998:
            return "BREAKOUT"
        if vs_avg < -1:
            return "BOUNCE"
        return "MOMENTUM" if vs_avg >= 1 else "DRIFT UP"
    if shape["lo20"] and last <= shape["lo20"]:
        return "NEW 20-DAY LOW"
    if day <= -2:
        return "SELL-OFF"
    if vs_avg > 1:
        return "PULLBACK"
    return "DOWNTREND" if vs_avg <= -1 else "DRIFT DOWN"


def route_book(
    shape: dict[str, Any],
    alerts: dict[str, int],
    news: dict[str, Any] | None,
    desk: dict[str, Any],
    market: Market,
) -> dict[str, Any]:
    """Which book the name belongs to and the tests that decided it."""
    day, vs_avg, ret20 = shape["day_pct"] or 0, shape["vs_avg20_pct"] or 0, shape["ret20_pct"]
    n_alerts = sum(alerts.values())
    count = int((news or {}).get("count_24h") or 0)
    tone = _f((news or {}).get("tone_48h")) or 0.0
    thesis = desk.get("thesis")
    if thesis:
        return {
            "tag": "SWING",
            "reasons": [
                f"Held in the swing book: {first_sentence(thesis.get('narrative', ''))}",
                (
                    f"The plan is to hold up to {thesis.get('horizon_days')} sessions, stop {thesis.get('stop')}, "
                    f"target {thesis.get('target')}. No same-day exit is planned."
                ),
            ],
        }
    if not market.traded:
        return {"tag": "WATCH ONLY", "reasons": [market.note]}
    same_way = (vs_avg > 0) == (day > 0)
    multi_day = abs(vs_avg) >= 2 and same_way
    burst = n_alerts >= 3 or abs(day) >= 2
    catalyst = count > 0 and abs(tone) >= 0.15
    if desk.get("proposed") or desk.get("blocked"):
        return {
            "tag": "SWING",
            "reasons": [
                "The head trader proposed it for the swing book today: "
                + first_sentence((desk.get("decision") or {}).get("thesis", ""))
            ],
        }
    if multi_day and (catalyst or not burst):
        why = [
            f"A multi-day trend is behind today's move: {vs_avg:+.1f}% against its 20-day average"
            + (f", {ret20:+.1f}% over 20 days." if ret20 is not None else ".")
        ]
        if catalyst:
            why.append(f"News that plays out over days: {count} articles, tone {tone:+.2f}.")
        return {"tag": "SWING CANDIDATE", "reasons": why}
    if burst:
        return {
            "tag": "INTRADAY CANDIDATE",
            "reasons": [
                f"Today's move is the story: {day:+.2f}% with {n_alerts} unusual-activity flags, "
                + (
                    "against its multi-day trend."
                    if not same_way
                    else "without a strong multi-day trend."
                ),
                "The intraday book is off until a strategy beats costs in testing, so the desk only watches.",
            ],
        }
    return {
        "tag": "WATCH",
        "reasons": [
            "No multi-day trend, no unusual activity and no fresh news: nothing to act on."
        ],
    }


def levels(shape: dict[str, Any], side: str, desk: dict[str, Any]) -> dict[str, Any]:
    last = shape["last"]
    zone = sorted([shape["vwap"], shape["prior_close"]])
    out: dict[str, Any] = {
        "last": last,
        "s1": shape["session_low"],
        "s2": shape["lo20"],
        "r1": shape["session_high"],
        "r2": shape["hi20"],
        "zone": [round(zone[0], 4), round(zone[1], 4)],
        "vwap": shape["vwap"],
        "prior_close": shape["prior_close"],
    }
    thesis = desk.get("thesis")
    if thesis and thesis.get("stop") and thesis.get("target"):
        stop, target = float(thesis["stop"]), float(thesis["target"])
        out.update(entry=_f(thesis.get("entry")), stop=stop, target=target)
        risk, reward = last - stop, target - last
    elif side == "bull":
        risk = last - shape["session_low"]
        reward = (shape["hi20"] or shape["session_high"]) - last
    else:
        risk = shape["session_high"] - last
        reward = last - (shape["lo20"] or shape["session_low"])
    out["rr"] = round(reward / risk, 2) if risk and risk > 0 and reward > 0 else None
    return out


# ---- the desk's own state from the journal ----


async def desk_state(
    db: Database, open_at: datetime, asof: datetime, symbols: set[str]
) -> dict[str, dict[str, Any]]:
    """Per symbol: thesis and position, the head trader's latest proposal, the skeptic's objection,
    the risk officer's decision and any fill, all as known at `asof`."""
    since = open_at - timedelta(hours=4)
    state: dict[str, dict[str, Any]] = defaultdict(dict)
    for t in await db.recent_theses(limit=300):
        if t.symbol not in symbols or t.created_at > asof:
            continue
        closed = t.closed_at is not None and t.closed_at <= asof
        if t.status in ("rejected",) or closed:
            continue
        state[t.symbol]["thesis"] = {
            "thesis_id": t.thesis_id,
            "status": t.status,
            "narrative": t.narrative,
            "conviction": t.conviction,
            "horizon_days": t.horizon_days,
            "quantity": float(t.quantity),
            "entry": _f(t.entry_price),
            "stop": _f(t.stop_price),
            "target": _f(t.target_price),
            "created_at": t.created_at.isoformat(),
        }
        state[t.symbol]["held"] = t.status in ("active", "closing")
    snaps = await db.recent_events(
        [Topics.PORTFOLIO_RECONCILED], asof - timedelta(days=4), limit=1, until=asof
    )
    for p in (snaps[0].payload.get("positions") if snaps else []) or []:
        if p.get("symbol") in symbols and _f(p.get("qty")):
            state[p["symbol"]]["held"] = True
            state[p["symbol"]]["position"] = {
                "qty": _f(p.get("qty")),
                "avg_entry": _f(p.get("avg_entry")),
                "unrealized_pl": _f(p.get("unrealized_pl")),
            }
    plans = await db.recent_events([Topics.CYCLE_PLAN], since, limit=60, until=asof)
    for e in reversed(plans):  # oldest first, so the latest wins
        if e.payload.get("dry_run"):
            continue
        objections = {
            o.get("symbol"): o for o in (e.payload.get("challenge") or {}).get("objections") or []
        }
        for d in (e.payload.get("plan") or {}).get("decisions") or []:
            sym = d.get("symbol")
            if sym not in symbols or d.get("action") not in ("buy", "sell"):
                continue
            obj = objections.get(sym)
            s = state[sym]
            s["decision"] = {
                "action": d["action"],
                "conviction": d.get("conviction"),
                "thesis": d.get("thesis", ""),
                "stop_pct": d.get("stop_pct"),
                "target_pct": d.get("target_pct"),
                "at": e.occurred_at.isoformat(),
                "trigger": e.payload.get("trigger"),
                "objection": {"severity": obj.get("severity"), "argument": obj.get("argument")}
                if obj
                else None,
            }
            s["proposed"] = True
            s["blocked"] = bool(obj and obj.get("severity") == "blocking")
    risk = await db.recent_events(
        [Topics.RISK_APPROVED, Topics.RISK_REJECTED], since, limit=200, until=asof
    )
    for e in reversed(risk):
        if e.symbol in symbols:
            rules = e.payload.get("rules") or []
            state[e.symbol]["risk"] = {
                "decision": "approved" if e.type == Topics.RISK_APPROVED else "rejected",
                "reason": e.payload.get("reason", ""),
                "passed": sum(1 for r in rules if r.get("passed")),
                "rules": len(rules),
                "at": e.occurred_at.isoformat(),
            }
    fills = await db.recent_events([Topics.ORDER_FILLED], open_at, limit=200, until=asof)
    intents = await db.recent_events([Topics.ORDER_INTENT], since, limit=400, until=asof)
    side_of = {i.payload.get("intent_id"): i.payload.get("side") for i in intents}
    for e in reversed(fills):
        if e.symbol in symbols:
            side = e.payload.get("side") or side_of.get(e.payload.get("intent_id")) or "buy"
            state[e.symbol]["filled_today"] = side
            state[e.symbol]["fill"] = {
                "side": side,
                "qty": _f(e.payload.get("filled_quantity")),
                "price": _f(e.payload.get("avg_price")),
                "at": e.occurred_at.isoformat(),
            }
    return state


# ---- the build ----


def breadth(day_pcts: list[float]) -> dict[str, Any]:
    edges = [-100, -4, -3, -2, -1.5, -1, -0.5, 0, 0.5, 1, 1.5, 2, 3, 4, 100]
    bins = [
        {"lo": lo, "hi": hi, "n": sum(1 for x in day_pcts if lo <= x < hi)}
        for lo, hi in itertools.pairwise(edges)
    ]
    return {
        "bull": sum(1 for x in day_pcts if x > 0.1),
        "bear": sum(1 for x in day_pcts if x < -0.1),
        "flat": sum(1 for x in day_pcts if -0.1 <= x <= 0.1),
        "bins": bins,
    }


def market_sentiment(
    bench: dict[str, Any] | None, rows: list[dict[str, Any]], market: Market
) -> dict[str, Any]:
    days = [r["day_pct"] for r in rows if r["day_pct"] is not None]
    above = [r for r in rows if (r["vs_avg20_pct"] or 0) > 0]
    alerts = sum(r["alerts_total"] for r in rows)
    up_share = sum(1 for x in days if x > 0) / len(days) if days else 0.5
    above_share = len(above) / len(rows) if rows else 0.5
    b_day = bench["day_pct"] if bench else None
    b_avg = bench["vs_avg20_pct"] if bench else None
    total = 100 * (
        0.40 * _clamp((b_day or 0) / 2)
        + 0.20 * _clamp((b_avg or 0) / 4)
        + 0.25 * _clamp((up_share - 0.5) * 2.5)
        + 0.15 * _clamp((above_share - 0.5) * 2.5)
    )
    name = NAMES.get(market.benchmark, market.benchmark)
    ev = [
        {
            "label": f"{name} on the day",
            "value": f"{b_day:+.2f}%" if b_day is not None else "n/a",
            "verdict": _verdict(b_day, 0.3),
            "source": "Price feed",
        },
        {
            "label": f"{name} against its 20-day average",
            "value": f"{b_avg:+.1f}%" if b_avg is not None else "n/a",
            "verdict": _verdict(b_avg, 1.0),
            "source": "Regime strategist",
        },
        {
            "label": "Watched names up on the day",
            "value": f"{up_share:.0%}",
            "verdict": _verdict(up_share - 0.5, 0.1),
            "source": "Regime strategist",
        },
        {
            "label": "Watched names above their 20-day average",
            "value": f"{above_share:.0%}",
            "verdict": _verdict(above_share - 0.5, 0.1),
            "source": "Regime strategist",
        },
        {
            "label": "Unusual moves and volume flagged today",
            "value": str(alerts),
            "verdict": "neutral",
            "source": "Watcher",
        },
    ]
    verdict = "BULLISH" if total >= 25 else "BEARISH" if total <= -25 else "MIXED"
    return {"score": round(total, 1), "verdict": verdict, "evidence": ev}


async def wire_events(
    db: Database, open_at: datetime, asof: datetime, symbols: set[str] | None = None
) -> list[Any]:
    """The latest events between agents: decisions first, and only the newest few watcher alerts,
    which would otherwise drown everything else."""
    since = open_at - timedelta(hours=2)
    quiet = [t for t in WIRE_TYPES if t != Topics.WATCHER_ALERT]
    events = await db.recent_events(quiet, since, limit=40, until=asof)
    events += await db.recent_events([Topics.WATCHER_ALERT], since, limit=3, until=asof)
    if symbols is not None:  # headlines about names the desk does not watch are noise here
        events = [e for e in events if e.symbol is None or e.symbol in symbols]
    return sorted(events, key=lambda e: e.occurred_at, reverse=True)[:10]


async def build_funnel(
    db: Database,
    market: Market,
    now: datetime,
    *,
    at: datetime | None = None,
    step_minutes: int = STEP_MINUTES,
) -> dict[str, Any]:
    moment = at or now
    day, open_at, close_at = session_window(market, moment)
    asof = min(moment, close_at)
    is_live = at is None and open_at <= now < close_at
    symbols = list(market.all_symbols)
    minute_rows = await db.bars_between(symbols, "1m", open_at, asof + timedelta(seconds=1))
    daily_rows = await db.bars_between(symbols, "1d", open_at - timedelta(days=45), open_at)
    minute: dict[str, list[dict[str, Any]]] = defaultdict(list)
    daily: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in minute_rows:
        minute[r["symbol"]].append(r)
    for r in daily_rows:
        if r["ts"].date() < day:
            daily[r["symbol"]].append(r)
    last_bar = max((rows[-1]["ts"] for rows in minute.values()), default=None)
    end = last_bar if last_bar is not None else open_at
    grid = [open_at - timedelta(minutes=step_minutes)]
    t = open_at
    while t <= end:
        grid.append(t)
        t += timedelta(minutes=step_minutes)
    if grid[-1] < end:
        grid.append(end)

    alerts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for e in await db.recent_events([Topics.WATCHER_ALERT], open_at, limit=5000, until=asof):
        if e.symbol:
            alerts[e.symbol][str(e.payload.get("kind"))] += 1
    news: dict[str, dict[str, Any]] = {}
    for e in await db.recent_events(
        [Topics.NEWS_STATE], asof - timedelta(hours=48), limit=2000, until=asof
    ):
        if e.symbol and e.symbol not in news:
            news[e.symbol] = e.payload
    desk = await desk_state(db, open_at, asof, set(symbols)) if market.traded else defaultdict(dict)

    shapes = {s: day_shape(minute.get(s, []), daily.get(s, []), grid) for s in symbols}
    for sh in shapes.values():
        if sh is not None:
            sh["path"][0] = 0.0  # every line leaves from the same point: the prior close
    bench = shapes.get(market.benchmark)
    bench_label = NAMES.get(market.benchmark, market.benchmark)
    rows: list[dict[str, Any]] = []
    for s in market.symbols:
        sh = shapes.get(s)
        if sh is None:
            continue
        a = dict(alerts.get(s, {}))
        d = dict(desk.get(s, {}))
        sc, ev = score(sh, a, news.get(s), bench["day_pct"] if bench else None, bench_label)
        # the side is what the day shows (the fan draws it); the score ranks names within a side
        side = "bull" if (sh["day_pct"] or 0) >= 0 else "bear"
        sign = 1 if side == "bull" else -1
        tone = _f((news.get(s) or {}).get("tone_48h"))
        checks = {
            "watcher": sum(a.values()) > 0 or abs(sh["day_pct"] or 0) >= 1.0,
            "quant": (sh["vs_avg20_pct"] or 0) * sign > 0.5,
            "news": tone is not None and tone * sign > 0.1,
            "head": bool(d.get("proposed") or d.get("held")),
            "risk": bool((d.get("risk") or {}).get("decision") == "approved" or d.get("held"))
            and not d.get("blocked"),
        }
        passed = sum(1 for c in CHECKS if checks[c])
        rows.append(
            {
                "symbol": s,
                "name": NAMES.get(s, s),
                "group": group_of(s),
                "side": side,
                "score": sc,
                "strength": round(max(0.0, sign * sc), 1),
                "rank_value": round(sign * sc + 12 * passed, 1),
                "day_pct": sh["day_pct"],
                "open_pct": sh["open_pct"],
                "vs_avg20_pct": sh["vs_avg20_pct"],
                "last": sh["last"],
                "last_at": sh["last_at"].isoformat(),
                "path": sh["path"],
                "profile": sh["profile"],
                "checks": checks,
                "checks_passed": passed,
                "setup": setup_tag(sh, side, d),
                "book": route_book(sh, a, news.get(s), d, market),
                "held": bool(d.get("held")),
                "desk": {
                    k: d[k] for k in ("thesis", "position", "decision", "risk", "fill") if k in d
                },
                "levels": levels(sh, side, d),
                "evidence": ev,
                "alerts": a,
                "alerts_total": sum(a.values()),
            }
        )
    for side in ("bull", "bear"):
        ranked = sorted((r for r in rows if r["side"] == side), key=lambda r: -r["rank_value"])
        for i, r in enumerate(ranked):
            r["rank"] = i + 1
    return {
        "market": market.key,
        "label": market.label,
        "traded": market.traded,
        "note": market.note,
        "session": {
            "day": day.isoformat(),
            "open": open_at.isoformat(),
            "close": close_at.isoformat(),
            "as_of": (last_bar or asof).isoformat(),
            "is_live": is_live,
            "step_minutes": step_minutes,
        },
        "times": [t.isoformat() for t in grid],
        "benchmark": {
            "symbol": market.benchmark,
            "name": bench_label,
            "day_pct": bench["day_pct"] if bench else None,
            "last": bench["last"] if bench else None,
            "path": bench["path"] if bench else [],
        },
        "sentiment": market_sentiment(bench, rows, market),
        "wire": [
            {
                "at": e.occurred_at.isoformat(),
                "agent": AGENT_LABEL.get(e.source.split("@")[0], e.source.split("@")[0]),
                "symbol": e.symbol,
                "text": describe(e).split("\n")[0][:200],
            }
            for e in (await wire_events(db, open_at, asof, set(symbols)) if market.traded else [])
        ],
        "breadth": breadth([r["day_pct"] for r in rows if r["day_pct"] is not None]),
        "symbols": rows,
    }


async def symbol_detail(
    db: Database, market: Market, symbol: str, now: datetime, *, at: datetime | None = None
) -> dict[str, Any]:
    """Five-minute candles for the session and the journal's trail for one instrument."""
    moment = at or now
    _, open_at, close_at = session_window(market, moment)
    asof = min(moment, close_at)
    rows = await db.bars_between([symbol], "1m", open_at, asof + timedelta(seconds=1))
    candles: list[dict[str, Any]] = []
    for r in rows:
        slot = open_at + timedelta(
            minutes=int((r["ts"] - open_at).total_seconds() // 60 // STEP_MINUTES) * STEP_MINUTES
        )
        o, h, lo, c, v = (float(r[k]) for k in ("open", "high", "low", "close", "volume"))
        if candles and candles[-1]["t"] == slot.isoformat():
            k = candles[-1]
            k["h"], k["l"], k["c"], k["v"] = max(k["h"], h), min(k["l"], lo), c, k["v"] + v
        else:
            candles.append({"t": slot.isoformat(), "o": o, "h": h, "l": lo, "c": c, "v": v})
    types = [
        Topics.WATCHER_ALERT,
        Topics.NEWS_ARTICLE_RAW,
        Topics.RESEARCH_REPORT,
        Topics.THESIS_CREATED,
        Topics.THESIS_CLOSED,
        Topics.RISK_APPROVED,
        Topics.RISK_REJECTED,
        Topics.ORDER_FILLED,
    ]
    events = await db.recent_events(
        types, open_at - timedelta(hours=4), symbol=symbol, limit=60, until=asof
    )
    trail = []
    for e in events:
        trail.append(
            {
                "at": e.occurred_at.isoformat(),
                "agent": AGENT_LABEL.get(e.source.split("@")[0], e.source.split("@")[0]),
                "type": e.type,
                "text": describe(e).split("\n")[0][:260],
            }
        )
    return {"symbol": symbol, "candles": candles, "trail": trail}


def digest(funnel: dict[str, Any], top: int = 6) -> str:
    """The funnel as the head trader reads it: the market verdict, then the leaders on each side."""
    s = funnel["sentiment"]
    b = funnel["breadth"]
    lines = [
        f"Market sentiment {s['verdict']} (score {s['score']:+.0f}): "
        + "; ".join(f"{e['label']} {e['value']}" for e in s["evidence"][:4])
        + f". {b['bull']} names up, {b['bear']} down."
    ]
    for side, title in (("bull", "Strongest"), ("bear", "Weakest")):
        ranked = sorted(
            (r for r in funnel["symbols"] if r["side"] == side), key=lambda r: r.get("rank", 99)
        )[:top]
        for r in ranked:
            checks = "".join("x" if r["checks"][c] else "-" for c in CHECKS)
            lines.append(
                f"{title} {r['rank']}: {r['symbol']} score {r['score']:+.0f}, day {r['day_pct']:+.2f}%, "
                f"{r['setup']}, book {r['book']['tag']}, checks watcher/quant/news/head/risk {checks}"
            )
    return "\n".join(lines)
