"""The lab desk, read back from its journal: what the predictor was shown, what it concluded, what
the skeptic and the risk officer said, what the lines did, and how it ended. One story per call.

Every call is graded:
- a call that became a position is graded by what the position really returned, after costs;
- a call that named a direction but was not traded (below the confidence bar, blocked, refused) is
  graded on paper: the same lines are run over the real minute bars that followed, so the desk
  knows whether its caution cost it a winner or saved it a loser;
- a pass ("none") still names the side the predictor leaned to (prompt version 3 onward), and
  that lean is graded on paper the same way; the report also says how far the price then moved
  and flags the passes that were followed by a move the desk could have traded;
- a trigger the predictor never looked at (the hourly budget was spent, or the model declined)
  is counted apart as unanswered: it is not a pass.

No model calls: this is arithmetic over stored events and bars."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from desk.core import calendar
from desk.core.events import Event, Topics
from desk.funnel.markets import COMMODITIES, NAMES, Market, group_of, session_window
from desk.lab.lines import ROUND_TRIP, Line
from desk.storage.db import Database

TILES = (
    ("equities", "US equities"),
    ("oil", "Oil and energy"),
    ("gold", "Gold and metals"),
    ("crypto", "Crypto"),
)
BUCKETS = ((0.0, 0.4), (0.4, 0.5), (0.5, 0.55), (0.55, 0.65), (0.65, 0.8), (0.8, 1.01))
EXIT_WORDS = {
    "line": "came back to the min-profit line",
    "base": "came back to the base after the max-loss line had moved there",
    "stop": "reached the maximum loss line",
    "horizon": "ran out of time",
    "close": "closed before the bell",
    "broker": "the broker's stop fired",
    "unfilled": "the order never filled",
    "risk": "refused by the risk officer",
}


def tile_of(symbol: str) -> str:
    if "/" in symbol:
        return "crypto"
    if symbol in COMMODITIES:
        return "oil" if group_of(symbol) in ("Oil", "Gas", "Energy equities") else "gold"
    return "equities"


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(UTC).isoformat() if t else None


# ---- grading on paper ----


def simulate(
    call: dict[str, Any],
    market: str,
    price: float,
    opened_at: datetime,
    bars: list[dict[str, Any]],
    *,
    flat_at: datetime | None = None,
) -> dict[str, Any]:
    """Run the lines over the bars that followed a call, as if it had been traded at `price`.
    Returns the result, or `pending` when the bars end before the lines or the horizon decide."""
    line = Line(
        call_id="paper",
        symbol="",
        market=market,
        direction=str(call.get("direction")),
        confidence=float(call.get("confidence") or 0),
        quantity=Decimal(0),
        max_loss_pct=float(call.get("max_loss_pct") or 1),
        first_profit_pct=float(call.get("first_profit_pct") or 1),
        step_pct=float(call.get("step_pct") or 0.5),
        horizon_minutes=int(call.get("horizon_minutes") or 240),
    )
    line.open(price, opened_at)
    deadline = opened_at + timedelta(minutes=line.horizon_minutes)
    exit_px: float | None = None
    reason = ""
    last_close = price
    exit_at: datetime | None = None
    for b in bars:
        ts = b["ts"]
        if ts < opened_at:
            continue
        high, low, close = float(b["high"]), float(b["low"]), float(b["close"])
        last_close = close
        if "exit" in line.watch(high, low):
            reason = line.exit_reason
            exit_px = line.profit_line if reason == "line" else line.stop
            exit_at = ts
            break
        if flat_at is not None and ts >= flat_at:
            exit_px, reason, exit_at = close, "close", ts
            break
        if ts >= deadline:
            exit_px, reason, exit_at = close, "horizon", ts
            break
    if exit_px is None:
        mark = (last_close / price - 1) * 100 * line.sign
        return {
            "result": "pending",
            "reason": "",
            "mark_pct": round(mark, 3),
            "steps": line.steps,
            "armed": line.armed,
        }
    realized = (exit_px / price - 1) * 100 * line.sign - ROUND_TRIP[market] * 100
    return {
        "result": "right" if realized > 0 else "wrong",
        "reason": reason,
        "exit": round(exit_px, 6),
        "exit_at": _iso(exit_at),
        "realized_pct": round(realized, 3),
        "steps": line.steps,
        "armed": line.armed,
    }


def moved_after(
    price: float, at: datetime, bars: list[dict[str, Any]], minutes: int = 60
) -> dict[str, Any] | None:
    """How far the price went, up and down, in the hour after a pass."""
    window = [b for b in bars if at <= b["ts"] < at + timedelta(minutes=minutes)]
    if not window or price <= 0:
        return None
    hi = max(float(b["high"]) for b in window)
    lo = min(float(b["low"]) for b in window)
    return {
        "up_pct": round((hi / price - 1) * 100, 3),
        "down_pct": round((lo / price - 1) * 100, 3),
        "end_pct": round((float(window[-1]["close"]) / price - 1) * 100, 3),
        "minutes": minutes,
    }


# ---- the stories ----


async def stories(
    db: Database,
    markets: dict[str, Market],
    since: datetime,
    until: datetime,
    *,
    now: datetime | None = None,
    limit: int = 2000,
    grade: bool = True,
) -> list[dict[str, Any]]:
    """Every predictor call in the window with everything that followed it, newest first."""
    now = now or until
    calls = await db.recent_events([Topics.PREDICTION_CALL], since, limit=limit, until=until)
    if not calls:
        return []
    horizon = until + timedelta(hours=41)  # the longest horizon a call may carry
    later = min(horizon, now)
    intents = await db.recent_events([Topics.ORDER_INTENT], since, limit=4000, until=later)
    risks = await db.recent_events(
        [Topics.RISK_APPROVED, Topics.RISK_REJECTED], since, limit=4000, until=later
    )
    fills = await db.recent_events(
        [Topics.ORDER_FILLED, Topics.ORDER_REJECTED, Topics.ORDER_CANCELED],
        since,
        limit=4000,
        until=later,
    )
    lines = await db.recent_events([Topics.LINES_UPDATED], since, limit=6000, until=later)
    scores = await db.recent_events([Topics.PREDICTION_SCORE], since, limit=4000, until=later)

    entry_of: dict[str, Event] = {}
    for i in reversed(intents):  # oldest first: the first intent of a call is its entry
        cid = i.payload.get("call_id")
        if cid and cid not in entry_of:
            entry_of[cid] = i
    risk_of = {str(r.payload.get("intent_id")): r for r in reversed(risks)}
    order_of = {str(f.payload.get("intent_id")): f for f in reversed(fills)}
    lines_of: dict[str, list[Event]] = defaultdict(list)
    for e in reversed(lines):
        lines_of[str(e.payload.get("call_id"))].append(e)
    score_of = {str(s.payload.get("call_id")): s for s in reversed(scores)}

    out: list[dict[str, Any]] = []
    for e in calls:
        p = e.payload
        cid = str(p.get("call_id"))
        c = p.get("call") or {}
        symbol = str(p.get("symbol") or e.symbol)
        market = str(p.get("market") or "stocks")
        direction = str(c.get("direction") or "none")
        story: dict[str, Any] = {
            "call_id": cid,
            "at": _iso(e.occurred_at),
            "symbol": symbol,
            "name": NAMES.get(symbol, symbol),
            "market": market,
            "tile": tile_of(symbol),
            "trigger": p.get("trigger", ""),
            "price": _f(p.get("price")),
            "call": c,
            "acted": bool(p.get("acted")),
            "skipped_because": p.get("skipped_because", ""),
            "objection": p.get("objection", ""),
            "inputs": p.get("inputs") or {},
            "record": p.get("record") or {},
            "seconds": p.get("seconds"),
            "tokens": p.get("tokens") or {},
            "model": p.get("model", ""),
            "risk": None,
            "fill": None,
            "lines": [],
            "score": None,
            "paper": None,
            "after": None,
        }
        intent = entry_of.get(cid)
        if intent is not None:
            iid = str(intent.payload.get("intent_id"))
            story["order"] = {
                "side": intent.payload.get("side"),
                "quantity": _f(intent.payload.get("quantity")),
                "stop_price": _f(intent.payload.get("stop_price")),
            }
            r = risk_of.get(iid)
            if r is not None:
                rules = r.payload.get("rules") or []
                story["risk"] = {
                    "decision": "approved" if r.type == Topics.RISK_APPROVED else "rejected",
                    "reason": r.payload.get("reason", ""),
                    "passed": sum(1 for x in rules if x.get("passed")),
                    "rules": len(rules),
                    "failed": [
                        f"{x.get('rule')}: {x.get('detail')}" for x in rules if not x.get("passed")
                    ],
                    "at": _iso(r.occurred_at),
                }
            o = order_of.get(iid)
            if o is not None and o.type == Topics.ORDER_FILLED:
                story["fill"] = {
                    "price": _f(o.payload.get("avg_price")),
                    "quantity": _f(o.payload.get("filled_quantity")),
                    "at": _iso(o.occurred_at),
                }
        story["lines"] = [
            {
                "event": x.payload.get("event"),
                "at": _iso(x.occurred_at),
                "base": _f(x.payload.get("base")),
                "stop": _f(x.payload.get("stop")),
                "profit_line": _f(x.payload.get("profit_line")),
                "last": _f(x.payload.get("last")),
                "steps": x.payload.get("steps"),
                "armed": x.payload.get("armed"),
            }
            for x in lines_of.get(cid, [])
        ]
        s = score_of.get(cid)
        if s is not None:
            sp = s.payload
            story["score"] = {
                "hit": sp.get("hit"),
                "realized_pct": _f(sp.get("realized_pct")),
                "reason": sp.get("reason", ""),
                "how": EXIT_WORDS.get(str(sp.get("reason")), str(sp.get("reason") or "")),
                "entry": _f(sp.get("entry")),
                "exit": _f(sp.get("exit")),
                "closed_at": _iso(s.occurred_at),
            }
        # where the call stands
        if direction == "none" and not p.get("model"):
            outcome = "unanswered"  # the budget was spent or the model declined: nobody looked
        elif direction == "none":
            outcome = "passed"
        elif not story["acted"]:
            outcome = "held_back"
        elif story["score"] and story["score"]["hit"] is True:
            outcome = "right"
        elif story["score"] and story["score"]["hit"] is False:
            outcome = "wrong"
        elif story["score"]:
            outcome = "not_opened"  # refused by risk, or the order never filled
        elif story["risk"] and story["risk"]["decision"] == "rejected":
            outcome = "not_opened"
        elif story["fill"] or story["lines"]:
            outcome = "open"
        else:
            outcome = "ordering"
        story["outcome"] = outcome
        out.append(story)

    if grade:
        await _grade_on_paper(db, markets, out, now)
    return out


async def _grade_on_paper(
    db: Database, markets: dict[str, Market], rows: list[dict[str, Any]], now: datetime
) -> None:
    """Fill `paper` for untraded calls (a pass is graded on the side it leaned to) and `after` for
    passes, from the minute bars."""
    need = [r for r in rows if r["outcome"] in ("held_back", "not_opened", "passed") and r["price"]]
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in need:
        by_symbol[r["symbol"]].append(r)
    for symbol, group in by_symbol.items():
        starts = [datetime.fromisoformat(r["at"]) for r in group]
        longest = max(int((r["call"] or {}).get("horizon_minutes") or 60) for r in group)
        bars = await db.bars_between(
            [symbol],
            "1m",
            min(starts) - timedelta(minutes=1),
            min(max(starts) + timedelta(minutes=max(longest, 60) + 5), now + timedelta(minutes=1)),
        )
        for r, at in zip(group, starts, strict=True):
            following = [b for b in bars if b["ts"] >= at]
            call = r["call"] or {}
            if r["outcome"] == "passed":
                r["after"] = moved_after(r["price"], at, following)
                if call.get("lean") not in ("long", "short"):
                    continue  # calls before prompt version 3 named no lean: nothing to grade
                call = {**call, "direction": call["lean"]}
            flat_at = None
            market = markets.get(r["market"])
            if market is not None and market.session == "nyse":
                _, _, close_at = session_window(market, at)
                flat_at = close_at - timedelta(minutes=10)
                if not calendar.is_open(at):
                    continue  # a call outside the session has no bars to be graded on
            r["paper"] = simulate(call, r["market"], r["price"], at, following, flat_at=flat_at)


# ---- the totals ----


def _blank() -> dict[str, Any]:
    return {
        "calls": 0,
        "passed": 0,
        "directional": 0,
        "traded": 0,
        "open": 0,
        "right": 0,
        "wrong": 0,
        "not_opened": 0,
        "sum_pct": 0.0,
        "paper_right": 0,
        "paper_wrong": 0,
        "paper_pending": 0,
        "paper_sum_pct": 0.0,
        "missed_moves": 0,
        "unanswered": 0,
        "lean_right": 0,
        "lean_wrong": 0,
        "lean_pending": 0,
        "lean_sum_pct": 0.0,
    }


def _add(t: dict[str, Any], r: dict[str, Any], missed_at: float) -> None:
    t["calls"] += 1
    o = r["outcome"]
    if o == "unanswered":
        t["unanswered"] += 1
        return
    if o == "passed":
        t["passed"] += 1
        a = r.get("after")
        if a and max(a["up_pct"], -a["down_pct"]) >= missed_at:
            t["missed_moves"] += 1
        p = r.get("paper")
        if p:
            if p["result"] == "pending":
                t["lean_pending"] += 1
            else:
                t["lean_" + p["result"]] += 1
                t["lean_sum_pct"] += float(p.get("realized_pct") or 0)
        return
    t["directional"] += 1
    if o in ("right", "wrong"):
        t["traded"] += 1
        t[o] += 1
        t["sum_pct"] += float((r["score"] or {}).get("realized_pct") or 0)
    elif o in ("open", "ordering"):
        t["traded"] += 1
        t["open"] += 1
    else:
        if o == "not_opened":
            t["not_opened"] += 1
        p = r.get("paper")
        if p:
            if p["result"] == "pending":
                t["paper_pending"] += 1
            else:
                t["paper_" + p["result"]] += 1
                t["paper_sum_pct"] += float(p.get("realized_pct") or 0)


def _finish(t: dict[str, Any]) -> dict[str, Any]:
    done = t["right"] + t["wrong"]
    t["hit_rate"] = round(t["right"] / done, 3) if done else None
    t["avg_pct"] = round(t["sum_pct"] / done, 3) if done else None
    t["sum_pct"] = round(t["sum_pct"], 3)
    paper = t["paper_right"] + t["paper_wrong"]
    t["paper_hit_rate"] = round(t["paper_right"] / paper, 3) if paper else None
    t["paper_avg_pct"] = round(t["paper_sum_pct"] / paper, 3) if paper else None
    t["paper_sum_pct"] = round(t["paper_sum_pct"], 3)
    leans = t["lean_right"] + t["lean_wrong"]
    t["lean_hit_rate"] = round(t["lean_right"] / leans, 3) if leans else None
    t["lean_avg_pct"] = round(t["lean_sum_pct"] / leans, 3) if leans else None
    t["lean_sum_pct"] = round(t["lean_sum_pct"], 3)
    every = done + paper
    t["all_right"] = t["right"] + t["paper_right"]
    t["all_wrong"] = t["wrong"] + t["paper_wrong"]
    t["all_hit_rate"] = round(t["all_right"] / every, 3) if every else None
    return t


def missed_threshold(market: str) -> float:
    """A pass is flagged when the price then moved at least this far: enough to pay for a trade."""
    return max(0.5, 3 * ROUND_TRIP[market] * 100)


def tally(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = _blank()
    tiles = {k: _blank() for k, _ in TILES}
    directions = {"long": _blank(), "short": _blank()}
    exits: dict[str, dict[str, Any]] = defaultdict(lambda: {"n": 0, "right": 0, "sum_pct": 0.0})
    buckets = [
        {"lo": lo, "hi": min(hi, 1.0), "right": 0, "wrong": 0, "traded": 0} for lo, hi in BUCKETS
    ]
    for r in rows:
        missed = missed_threshold(r["market"])
        _add(total, r, missed)
        _add(tiles[r["tile"]], r, missed)
        d = (r["call"] or {}).get("direction")
        if d in directions:
            _add(directions[d], r, missed)
        if r["outcome"] in ("right", "wrong"):
            x = exits[(r["score"] or {}).get("reason") or "other"]
            x["n"] += 1
            x["right"] += int(r["outcome"] == "right")
            x["sum_pct"] += float((r["score"] or {}).get("realized_pct") or 0)
        # calibration: every graded call, traded or on paper, a pass by the side it leaned to
        grade = (
            r["outcome"]
            if r["outcome"] in ("right", "wrong")
            else (r.get("paper") or {}).get("result")
        )
        if grade in ("right", "wrong"):
            conf = float((r["call"] or {}).get("confidence") or 0)
            for b in buckets:
                if b["lo"] <= conf < (b["hi"] if b["hi"] < 1.0 else 1.01):
                    b[grade] += 1
                    b["traded"] += int(r["outcome"] in ("right", "wrong"))
                    break
    for b in buckets:
        n = b["right"] + b["wrong"]
        b["hit_rate"] = round(b["right"] / n, 3) if n else None
    return {
        "total": _finish(total),
        "tiles": [{"key": k, "label": label, **_finish(tiles[k])} for k, label in TILES],
        "directions": {k: _finish(v) for k, v in directions.items()},
        "exits": [
            {
                "reason": k,
                "how": EXIT_WORDS.get(k, k),
                "n": v["n"],
                "right": v["right"],
                "avg_pct": round(v["sum_pct"] / v["n"], 3) if v["n"] else None,
            }
            for k, v in sorted(exits.items(), key=lambda kv: -kv[1]["n"])
        ],
        "confidence": buckets,
    }


def findings(t: dict[str, Any], final: bool) -> list[str]:
    """The day in plain sentences, numbers first. Nothing here that the tally does not show."""
    x = t["total"]
    out: list[str] = []
    if not x["calls"]:
        return ["The predictor made no calls in this period."]
    answered = x["calls"] - x["unanswered"]
    out.append(
        f"The predictor answered {answered} triggers: it named a direction on {x['directional']} "
        f"and passed on {x['passed']}."
    )
    if x["unanswered"]:
        n = x["unanswered"]
        out.append(
            f"{n} more {'trigger was' if n == 1 else 'triggers were'} never looked at: the hourly "
            f"call budget was spent."
        )
    done = x["right"] + x["wrong"]
    if done:
        out.append(
            f"Traded and closed: {done}. Right {x['right']}, wrong {x['wrong']} "
            f"({x['hit_rate'] * 100:.0f}% right), {x['avg_pct']:+.2f}% a trade after costs, "
            f"{x['sum_pct']:+.2f}% in all."
        )
    else:
        out.append(
            "No traded call has closed yet, so there is no right or wrong to count on real trades."
        )
    if x["open"]:
        out.append(f"Still open on lines: {x['open']}.")
    paper = x["paper_right"] + x["paper_wrong"]
    if paper:
        out.append(
            f"Named a direction but not traded: {paper} graded on paper against the real prices. "
            f"Right {x['paper_right']}, wrong {x['paper_wrong']} ({x['paper_hit_rate'] * 100:.0f}% right), "
            f"{x['paper_avg_pct']:+.2f}% each if they had been traded."
        )
    if x["paper_pending"]:
        n = x["paper_pending"]
        out.append(f"{n} untraded {'call is' if n == 1 else 'calls are'} still running on paper.")
    if x["passed"]:
        out.append(
            f"Of the {x['passed']} passes, {x['missed_moves']} were followed within the hour by a "
            f"move large enough to trade."
        )
    leans = x["lean_right"] + x["lean_wrong"]
    if leans:
        out.append(
            f"The side it leaned to on its passes, graded on paper: right {x['lean_right']}, wrong "
            f"{x['lean_wrong']} ({x['lean_hit_rate'] * 100:.0f}% right), {x['lean_avg_pct']:+.2f}% each."
        )
    every = x["all_right"] + x["all_wrong"]
    if every < 60:
        out.append(
            f"{every} graded calls so far. The bar for judging the predictor is 60, and 55% right "
            f"after costs."
        )
    elif x["all_hit_rate"] is not None:
        verdict = "above" if x["all_hit_rate"] > 0.55 else "below"
        out.append(
            f"{every} graded calls, {x['all_hit_rate'] * 100:.0f}% right: {verdict} the 55% bar."
        )
    if not final:
        out.append("The period is still running; these numbers will move.")
    return out


# ---- what each agent is doing ----


def _ago(now: datetime, t: datetime | None) -> int | None:
    return None if t is None else max(0, int((now - t).total_seconds()))


async def agents(db: Database, markets: dict[str, Market], now: datetime) -> list[dict[str, Any]]:
    hour = now - timedelta(hours=1)

    async def recent(types: list[str], limit: int = 400) -> list[Event]:
        return await db.recent_events(types, hour, limit=limit, until=now)

    triggers = await recent([Topics.SENTINEL_TRIGGER])
    calls = await recent([Topics.PREDICTION_CALL])
    risks = await recent([Topics.RISK_APPROVED, Topics.RISK_REJECTED])
    orders = await recent([Topics.ORDER_SUBMITTED, Topics.ORDER_FILLED, Topics.ORDER_REJECTED])
    lines = await recent([Topics.LINES_UPDATED])
    alerts = await recent([Topics.WATCHER_ALERT])
    news = await recent([Topics.NEWS_ARTICLE_SCORED], 600)
    snaps = await db.recent_events([Topics.PORTFOLIO_RECONCILED], now - timedelta(hours=6), limit=1)
    watched = sum(len(m.symbols) for m in markets.values() if m.traded)
    open_now = [m.label for m in markets.values() if m.traded and _session_open(m, now)]

    def row(key: str, name: str, role: str, events: list[Event], line: str) -> dict[str, Any]:
        last = events[0].occurred_at if events else None
        age = _ago(now, last)
        return {
            "key": key,
            "name": name,
            "role": role,
            "count": len(events),
            "last_at": _iso(last),
            "state": "working" if age is not None and age < 180 else "quiet",
            "line": line,
        }

    acted = [c for c in calls if c.payload.get("acted")]
    secs = [float(c.payload["seconds"]) for c in calls if c.payload.get("seconds")]
    objections = [
        str(c.payload.get("objection") or "") for c in calls if c.payload.get("objection")
    ]
    severe = defaultdict(int)
    for o in objections:
        severe[o.split(":", 1)[0].strip() or "note"] += 1
    rejected = [r for r in risks if r.type == Topics.RISK_REJECTED]
    filled = [o for o in orders if o.type == Topics.ORDER_FILLED]
    last_t = triggers[0] if triggers else None
    last_c = calls[0] if calls else None
    last_l = lines[0] if lines else None
    snap = snaps[0].payload if snaps else {}
    return [
        row(
            "sentinel",
            "Sentinel",
            "looks at every instrument every minute; no model",
            triggers,
            (
                f"Watching {watched} instruments ({', '.join(open_now) or 'no market open'}). "
                f"{len(triggers)} triggers in the last hour."
                + (
                    f" Latest: {last_t.symbol}, {last_t.payload.get('reason')} ({last_t.payload.get('detail')})."
                    if last_t
                    else ""
                )
            ),
        ),
        row(
            "predictor",
            "Predictor",
            "answers each trigger with a call",
            calls,
            (
                f"{len(calls)} calls in the last hour, {len(acted)} became orders."
                + (f" About {sum(secs) / len(secs):.0f}s a call." if secs else "")
                + (
                    f" Latest: {last_c.symbol}, {(last_c.payload.get('call') or {}).get('direction')} at "
                    f"{float((last_c.payload.get('call') or {}).get('confidence') or 0):.2f}."
                    if last_c
                    else ""
                )
            ),
        ),
        row(
            "skeptic",
            "Skeptic",
            "argues against every call that would trade",
            [c for c in calls if c.payload.get("objection")],
            (
                f"Challenged {len(objections)} calls in the last hour"
                + (
                    ": " + ", ".join(f"{v} {k}" for k, v in severe.items()) + "."
                    if objections
                    else "."
                )
                + " It is only asked when a call clears the bar."
            ),
        ),
        row(
            "risk",
            "Risk officer",
            "checks every order against every rule",
            risks,
            (
                f"{len(risks) - len(rejected)} approved, {len(rejected)} refused in the last hour."
                + (
                    f" Last refusal: {str(rejected[0].payload.get('reason'))[:120]}."
                    if rejected
                    else ""
                )
            ),
        ),
        row(
            "execution",
            "Execution",
            "sends approved orders to the broker",
            orders,
            (
                f"{len(filled)} fills in the last hour."
                + (
                    f" Latest: {filled[0].symbol} {filled[0].payload.get('filled_quantity')} at "
                    f"{filled[0].payload.get('avg_price')}."
                    if filled
                    else ""
                )
            ),
        ),
        row(
            "lines",
            "Lines engine",
            "holds every position on its lines",
            lines,
            (
                f"{len(lines)} line moves in the last hour."
                + (
                    f" Latest: {last_l.symbol}, {last_l.payload.get('event')}, stop {last_l.payload.get('stop')}, "
                    f"profit line {last_l.payload.get('profit_line')}."
                    if last_l
                    else " No position has needed it yet."
                )
            ),
        ),
        row(
            "watcher",
            "Watcher",
            "flags unusual moves and volume",
            alerts,
            f"{len(alerts)} flags in the last hour."
            + (f" Latest: {alerts[0].symbol}, {alerts[0].payload.get('kind')}." if alerts else ""),
        ),
        row(
            "news",
            "News analyst",
            "scores every headline",
            news,
            f"{len(news)} headlines scored in the last hour.",
        ),
        row(
            "back_office",
            "Back office",
            "reconciles with the broker",
            snaps,
            (
                f"Equity {snap.get('equity')}, cash {snap.get('cash')}, {len(snap.get('positions') or [])} positions."
                if snap
                else "No snapshot yet."
            ),
        ),
    ]


def _session_open(market: Market, now: datetime) -> bool:
    if market.session == "utc_day":
        return True
    return calendar.is_open(now)


# ---- the two views ----


async def open_lines(db: Database, now: datetime) -> list[dict[str, Any]]:
    events = await db.recent_events(
        [Topics.LINES_UPDATED], now - timedelta(days=5), limit=3000, until=now
    )
    seen: set[str] = set()
    out = []
    for e in events:
        cid = str(e.payload.get("call_id"))
        if cid in seen:
            continue
        seen.add(cid)
        if e.payload.get("event") != "exit":
            symbol = str(e.symbol or e.payload.get("symbol"))
            out.append(
                {
                    **e.payload,
                    "symbol": symbol,
                    "name": NAMES.get(symbol, symbol),
                    "tile": tile_of(symbol),
                    "at": _iso(e.occurred_at),
                }
            )
    return out


async def live(db: Database, markets: dict[str, Market], now: datetime, hours: int = 12) -> dict:
    """What the lab is doing now: the agents, the markets, the latest calls with their reasoning."""
    day_start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    since = min(day_start, now - timedelta(hours=hours))
    rows = await stories(db, markets, since, now, now=now, limit=600)
    today = [r for r in rows if r["at"] >= day_start.isoformat()]
    t = tally(today)
    held = await open_lines(db, now)
    last_market: dict[str, dict[str, Any]] = {}
    for r in rows:  # newest first: the first reading seen per market is the latest
        m = (r["inputs"] or {}).get("market")
        if m and m.get("verdict") and r["market"] not in last_market:
            last_market[r["market"]] = {**m, "at": r["at"]}
    tiles = []
    for (key, label), counts in zip(TILES, t["tiles"], strict=True):
        market_key = {"equities": "stocks", "oil": "commodities", "gold": "commodities"}.get(
            key, "crypto"
        )
        market = markets.get(market_key)
        symbols = [s for s in (market.symbols if market else ()) if tile_of(s) == key]
        tiles.append(
            {
                **counts,
                "market": market_key,
                # flags, named apart from the counts `traded` and `open` they sit beside
                "is_traded": bool(market and market.traded),
                "is_open": bool(market and market.traded and _session_open(market, now)),
                "instruments": len(symbols),
                "symbols": symbols,
                "on_lines": [x["symbol"] for x in held if x["tile"] == key],
                "mood": last_market.get(market_key),
                "shorts": key != "crypto",
            }
        )
    return {
        "as_of": _iso(now),
        "tiles": tiles,
        "agents": await agents(db, markets, now),
        "stories": rows[:80],
        "lines": held,
        "tally": t,
        "findings": findings(t, final=False),
        "next_open": _iso(calendar.next_open(now)) if not calendar.is_open(now) else None,
        "nyse_open": calendar.is_open(now),
    }


async def report(db: Database, markets: dict[str, Market], day: date, now: datetime) -> dict:
    """One UTC day of the lab, graded. The New York session (14:30 to 21:00 UTC) lies inside it."""
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    end = start + timedelta(days=1)
    rows = await stories(db, markets, start, min(end, now), now=now, limit=4000)
    t = tally(rows)
    final = now >= end
    nyse_closed = True
    if calendar.is_session(day):
        nyse_closed = now >= calendar.session_bounds(day)[1]
    return {
        "day": day.isoformat(),
        "as_of": _iso(now),
        "final": final,
        "nyse_session": calendar.is_session(day),
        "nyse_closed": nyse_closed,
        "tally": t,
        "findings": findings(t, final),
        "stories": rows,
    }


# ---- one position, bar by bar: the price and where its three lines stood at every minute ----


def _line_rows(events: list[Event]) -> list[dict[str, Any]]:
    return [
        {
            "at": _iso(x.occurred_at),
            "event": x.payload.get("event"),
            "base": _f(x.payload.get("base")),
            "stop": _f(x.payload.get("stop")),
            "profit_line": _f(x.payload.get("profit_line")),
            "last": _f(x.payload.get("last")),
            "steps": int(x.payload.get("steps") or 0),
            "armed": bool(x.payload.get("armed")),
            "quantity": _f(x.payload.get("quantity")),
            "step_pct": _f(x.payload.get("step_pct")),
        }
        for x in events
    ]


async def trades(
    db: Database, now: datetime, days: int = 7, day: str | None = None
) -> list[dict[str, Any]]:
    """Every position the lines engine has held in the last `days`, or every position opened on
    one UTC `day`, newest first: when it opened, when and how it closed, and what it returned.
    The list behind the charts."""
    if day is None:
        since, opened_before = now - timedelta(days=days), now
    else:
        since = datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=UTC)
        opened_before = since + timedelta(days=1)
    lines = await db.recent_events([Topics.LINES_UPDATED], since, limit=8000, until=now)
    scores = await db.recent_events([Topics.PREDICTION_SCORE], since, limit=4000, until=now)
    score_of = {str(s.payload.get("call_id")): s for s in reversed(scores)}
    by_call: dict[str, list[Event]] = defaultdict(list)
    for e in reversed(lines):  # oldest first
        by_call[str(e.payload.get("call_id"))].append(e)
    out = []
    for cid, evs in by_call.items():
        first, last = evs[0], evs[-1]
        if day is not None and (
            first.payload.get("event") != "opened" or first.occurred_at >= opened_before
        ):
            continue  # opened before the day (its opening lies outside the window) or after it
        symbol = str(first.symbol or first.payload.get("symbol"))
        s = score_of.get(cid)
        closed = last.payload.get("event") == "exit"
        qty = _f(first.payload.get("quantity")) or 0.0
        base = _f(first.payload.get("base")) or 0.0
        realized = _f(s.payload.get("realized_pct")) if s else None
        out.append(
            {
                "call_id": cid,
                "symbol": symbol,
                "name": NAMES.get(symbol, symbol),
                "tile": tile_of(symbol),
                "market": first.payload.get("market"),
                "direction": first.payload.get("direction"),
                "opened_at": _iso(first.occurred_at),
                "closed_at": _iso(last.occurred_at) if closed else None,
                "base": _f(first.payload.get("base")),
                "quantity": _f(first.payload.get("quantity")),
                "armed": bool(last.payload.get("armed")),
                "steps": int(last.payload.get("steps") or 0),
                "moves": len(evs),
                "open": not closed,
                "reason": (s.payload.get("reason") if s else None) or ("" if closed else "open"),
                "how": EXIT_WORDS.get(str(s.payload.get("reason")), "") if s else "",
                "realized_pct": realized,
                "hit": s.payload.get("hit") if s else None,
                # the money: what the position was worth at the fill, and what it made after costs
                "notional": round(base * qty, 2),
                "pnl": round(realized / 100 * base * qty, 2) if realized is not None else None,
            }
        )
    out.sort(key=lambda r: r["opened_at"], reverse=True)
    return out


async def trade_chart(db: Database, call_id: str, now: datetime) -> dict[str, Any] | None:
    """One position with the minute bars it lived through and every move of its lines, so the
    screen can replay it: at each minute, the price and where the base, the maximum-loss line and
    the minimum-profit line stood. Bars run from twenty minutes before the fill to twenty after the
    exit, or to now while the position is open."""
    since = now - timedelta(days=14)
    lines = [
        e
        for e in await db.recent_events([Topics.LINES_UPDATED], since, limit=8000, until=now)
        if str(e.payload.get("call_id")) == call_id
    ]
    if not lines:
        return None
    lines.reverse()  # oldest first
    first, last = lines[0], lines[-1]
    symbol = str(first.symbol or first.payload.get("symbol"))
    market = str(first.payload.get("market") or "stocks")
    calls = [
        e
        for e in await db.recent_events(
            [Topics.PREDICTION_CALL],
            first.occurred_at - timedelta(hours=2),
            symbol=symbol,
            limit=200,
            until=first.occurred_at + timedelta(minutes=1),
        )
        if str(e.payload.get("call_id")) == call_id
    ]
    scores = [
        e
        for e in await db.recent_events(
            [Topics.PREDICTION_SCORE], first.occurred_at, symbol=symbol, limit=200, until=now
        )
        if str(e.payload.get("call_id")) == call_id
    ]
    closed = last.payload.get("event") == "exit"
    pad = timedelta(minutes=20)
    start = first.occurred_at - pad
    end = (last.occurred_at + pad) if closed else now + timedelta(minutes=1)
    bars = await db.bars_between([symbol], "1m", start, end)
    call = calls[0].payload if calls else {}
    score = scores[0].payload if scores else None
    qty = _f(first.payload.get("quantity")) or 0.0
    base = _f(first.payload.get("base")) or 0.0
    realized = _f(score.get("realized_pct")) if score else None
    return {
        "call_id": call_id,
        "symbol": symbol,
        "name": NAMES.get(symbol, symbol),
        "tile": tile_of(symbol),
        "market": market,
        "direction": first.payload.get("direction"),
        "quantity": _f(first.payload.get("quantity")),
        "notional": round(base * qty, 2),
        "opened_at": _iso(first.occurred_at),
        "closed_at": _iso(last.occurred_at) if closed else None,
        "open": not closed,
        "as_of": _iso(now),
        "call": call.get("call") or {},
        "trigger": call.get("trigger", ""),
        "confidence": _f((call.get("call") or {}).get("confidence")),
        "lines": _line_rows(lines),
        "score": {
            "hit": score.get("hit"),
            "reason": score.get("reason", ""),
            "how": EXIT_WORDS.get(str(score.get("reason")), str(score.get("reason") or "")),
            "realized_pct": realized,
            "pnl": round(realized / 100 * base * qty, 2) if realized is not None else None,
            "entry": _f(score.get("entry")),
            "exit": _f(score.get("exit")),
        }
        if score
        else None,
        "bars": [
            {
                "ts": _iso(b["ts"]),
                "open": float(b["open"]),
                "high": float(b["high"]),
                "low": float(b["low"]),
                "close": float(b["close"]),
                "volume": float(b.get("volume") or 0),
            }
            for b in bars
        ],
    }
