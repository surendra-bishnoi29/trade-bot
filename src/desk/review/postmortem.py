"""Trade post-mortems and analyst credibility: the deterministic half of the performance reviewer.

A closed thesis is graded from the journal alone: what the head trader expected, what was realized,
how the trade exited, how far it went for and against the position while it was open. The model
only writes the lesson; it never changes a number. Analyst credibility comes from the evaluation
harness: each analyst's calls against what prices did, turned into a weight the head trader reads."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from desk.core import calendar, prompts
from desk.core.payloads import ThesisPayload
from desk.research.evaluate import evaluate as evaluate_calls
from desk.storage.db import Database

log = logging.getLogger(__name__)

MIN_CALLS_FOR_WEIGHT = 10
WEIGHT_FLOOR, WEIGHT_CEILING = 0.25, 1.5


@dataclass
class Grade:
    thesis_id: str
    symbol: str
    direction: str
    conviction: float
    expected_pct: float
    realized_pct: float | None
    realized_pnl: float | None
    exit_kind: str  # target, stop, time, invalidated, reconciled, kill, other
    holding_sessions: int
    mfe_pct: float | None  # best point during the hold, in the trade's favor
    mae_pct: float | None  # worst point during the hold
    entry_price: float | None
    stop_pct: float
    target_pct: float
    horizon_days: int
    opened_at: datetime
    closed_at: datetime
    reason: str
    verdict: str
    lesson: str = ""
    evidence_ids: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["opened_at"] = self.opened_at.isoformat()
        d["closed_at"] = self.closed_at.isoformat()
        return d


def exit_kind(reason: str) -> str:
    r = (reason or "").lower()
    for key, kind in (
        ("target", "target"),
        ("stop", "stop"),
        ("time", "time"),
        ("horizon", "time"),
        ("invalid", "invalidated"),
        ("reconciled", "reconciled"),
        ("gone", "reconciled"),
        ("kill", "kill"),
    ):
        if key in r:
            return kind
    return "other"


def excursions(
    thesis: ThesisPayload, bars: list[dict[str, Any]]
) -> tuple[float | None, float | None]:
    """Best and worst points during the hold as percent of entry, signed for the direction."""
    if not bars or thesis.entry_price is None or thesis.entry_price <= 0:
        return None, None
    entry = float(thesis.entry_price)
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]
    up, down = max(highs) / entry - 1, min(lows) / entry - 1
    if thesis.direction == "short":
        return -down * 100, -up * 100
    return up * 100, down * 100


def verdict_for(g: Grade) -> str:
    exp = f"{g.expected_pct:+.1f}% expected"
    real = f"{g.realized_pct:+.1f}%" if g.realized_pct is not None else "unknown"
    best = f"{g.mfe_pct:+.1f}%" if g.mfe_pct is not None else "n/a"
    worst = f"{g.mae_pct:+.1f}%" if g.mae_pct is not None else "n/a"
    hold = f"{g.holding_sessions} session{'s' if g.holding_sessions != 1 else ''}"
    if g.exit_kind == "target":
        text = f"Target hit after {hold}: {real} realized vs {exp}."
    elif g.exit_kind == "stop":
        text = f"Stopped out after {hold} at {real} ({exp}); worst point {worst}, best {best}."
        if g.mfe_pct is not None and g.target_pct and g.mfe_pct >= g.target_pct / 2:
            text += " It had more than half the target in hand before reversing: managed loosely."
    elif g.exit_kind == "time":
        text = f"Time exit after {hold} at {real} vs {exp}; best point {best}, worst {worst}."
    elif g.exit_kind == "invalidated":
        text = f"Invalidated after {hold} at {real} vs {exp}: the thesis stopped being true."
    elif g.exit_kind == "reconciled":
        text = f"Position disappeared at the broker after {hold}; recorded {real}."
    elif g.exit_kind == "kill":
        text = f"Closed by the kill switch after {hold} at {real}."
    else:
        text = f"Closed after {hold} at {real} vs {exp}: {g.reason[:120]}"
    if g.realized_pct is not None and g.expected_pct:
        ratio = g.realized_pct / g.expected_pct
        if g.realized_pct > 0 and ratio >= 1:
            text += " Outcome met the plan."
        elif g.realized_pct > 0:
            text += f" Captured {ratio:.0%} of the expected move."
    return text


def grade(thesis: ThesisPayload, bars: list[dict[str, Any]], now: datetime) -> Grade:
    closed_at = thesis.closed_at or thesis.updated_at or now
    sessions = max(0, calendar.sessions_between(thesis.created_at.date(), closed_at.date()))
    realized_pct = realized_pnl = None
    if thesis.realized_pnl is not None:
        realized_pnl = float(thesis.realized_pnl)
        if thesis.entry_price and thesis.quantity:
            basis = float(thesis.entry_price) * float(thesis.quantity)
            realized_pct = realized_pnl / basis * 100 if basis else None
    mfe, mae = excursions(thesis, bars)
    g = Grade(
        thesis_id=thesis.thesis_id,
        symbol=thesis.symbol,
        direction=thesis.direction,
        conviction=float(thesis.conviction),
        expected_pct=float(thesis.expected_return_pct),
        realized_pct=realized_pct,
        realized_pnl=realized_pnl,
        exit_kind=exit_kind(thesis.reason),
        holding_sessions=sessions,
        mfe_pct=mfe,
        mae_pct=mae,
        entry_price=float(thesis.entry_price) if thesis.entry_price is not None else None,
        stop_pct=float(thesis.stop_pct),
        target_pct=float(thesis.target_pct),
        horizon_days=int(thesis.horizon_days),
        opened_at=thesis.created_at,
        closed_at=closed_at,
        reason=thesis.reason or "",
        verdict="",
        evidence_ids=list(thesis.evidence_ids or [])[:6],
    )
    g.verdict = verdict_for(g)
    return g


async def bars_for(
    db: Database, thesis: ThesisPayload, closed_at: datetime
) -> list[dict[str, Any]]:
    """Daily bars across the hold when it spans sessions, minute bars inside one session."""
    start = thesis.created_at
    end = closed_at + timedelta(minutes=1)
    if (closed_at - start) >= timedelta(hours=20):
        day_start = datetime.combine(start.date(), datetime.min.time(), tzinfo=start.tzinfo)
        return await db.bars_between([thesis.symbol], "1d", day_start, end)
    return await db.bars_between([thesis.symbol], "1m", start, end)


async def grade_closed(
    db: Database, since: datetime, until: datetime, *, skip: set[str] | None = None
) -> list[Grade]:
    """Grades for every thesis closed in the window, oldest first."""
    theses = await db.closed_theses(since, until)
    out: list[Grade] = []
    for t in theses:
        if skip and t.thesis_id in skip:
            continue
        closed_at = t.closed_at or t.updated_at
        bars = await bars_for(db, t, closed_at)
        out.append(grade(t, bars, until))
    return out


# ---- the narrative: one model call for the day's closed theses ----


class Lesson(BaseModel):
    thesis_id: str
    lesson: str = Field(description="at most 60 words: what the trade taught, and the one change")
    process_sound: bool = Field(
        description="was the decision sound given what was knowable at entry"
    )


class Lessons(BaseModel):
    lessons: list[Lesson]


def narrative_material(grades: list[Grade]) -> str:
    rows = []
    for g in grades:
        rows.append(
            json.dumps(
                {
                    "thesis_id": g.thesis_id,
                    "symbol": g.symbol,
                    "direction": g.direction,
                    "conviction": g.conviction,
                    "expected_pct": g.expected_pct,
                    "realized_pct": g.realized_pct,
                    "exit": g.exit_kind,
                    "holding_sessions": g.holding_sessions,
                    "best_point_pct": g.mfe_pct,
                    "worst_point_pct": g.mae_pct,
                    "stop_pct": g.stop_pct,
                    "target_pct": g.target_pct,
                    "horizon_days": g.horizon_days,
                    "reason": g.reason[:200],
                    "verdict": g.verdict,
                },
                default=str,
            )
        )
    return "\n".join(rows)


async def write_lessons(head: Any, grades: list[Grade]) -> dict[str, Lesson]:
    """One structured call for all of the day's post-mortems, cached by content."""
    if not grades or head is None:
        return {}
    system = prompts.load("postmortem")
    material = narrative_material(grades)
    key = hashlib.sha256(f"postmortem|{system}|{material}".encode()).hexdigest()
    cache = getattr(head, "cache", None)
    if cache is not None:
        hit = await cache.get(key)
        if hit is not None:
            return {x.thesis_id: x for x in Lessons.model_validate(hit).lessons}
    response = await head.client.messages.parse(
        model=head.model,
        max_tokens=2000,
        system=[{"type": "text", "text": system}],
        messages=[
            {
                "role": "user",
                "content": f"Closed theses with their grades, one JSON object per line:\n{material}\n\nWrite the post-mortems.",
            }
        ],
        output_format=Lessons,
        output_config={"effort": "medium"},
    )
    usage = getattr(head, "_usage", None)
    if usage is not None:
        usage(response)
    if response.stop_reason == "refusal" or response.parsed_output is None:
        return {}
    parsed = response.parsed_output
    if cache is not None:
        await cache.put(key, "postmortem", head.model, parsed.model_dump(mode="json"))
    return {x.thesis_id: x for x in parsed.lessons}


# ---- analyst credibility ----


def credibility_weights(scores: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """A weight per analyst and kind from the five-session hit rate: 50% is 1.0, each point of hit
    rate moves it by two, bounded. Fewer than ten scored calls is unproven and weighs 1.0."""
    out: dict[str, dict[str, Any]] = {}
    for s in scores:
        counted = int((s.get("counted") or {}).get("5") or 0)
        hit = (s.get("hit_rate") or {}).get("5")
        key = f"{s['who']}/{s['kind']}"
        if counted < MIN_CALLS_FOR_WEIGHT or hit is None:
            out[key] = {"weight": 1.0, "hit_5d": hit, "n": counted, "status": "unproven"}
            continue
        weight = min(WEIGHT_CEILING, max(WEIGHT_FLOOR, 1.0 + (float(hit) - 0.5) * 2))
        status = "trusted" if weight >= 1.1 else "doubted" if weight <= 0.9 else "neutral"
        out[key] = {
            "weight": round(weight, 2),
            "hit_5d": float(hit),
            "n": counted,
            "status": status,
        }
    return out


async def score_analysts(db: Database, now: datetime, days: int = 60) -> dict[str, Any]:
    result = await evaluate_calls(db, now, days=days)
    weights = credibility_weights(result["scores"])
    return {
        "since": result["since"],
        "until": result["until"],
        "days": days,
        "calls": result["calls"],
        "scores": result["scores"],
        "weights": weights,
    }


def render_weights(weights: dict[str, dict[str, Any]]) -> str:
    """The line the head trader reads: who has earned trust and who has not."""
    if not weights:
        return "no scored calls yet"
    parts = []
    for key, w in sorted(weights.items()):
        if w["status"] == "unproven":
            parts.append(f"{key}: unproven ({w['n']} scored calls)")
        else:
            parts.append(
                f"{key}: hit {w['hit_5d']:.0%} over 5 sessions, n={w['n']}, weight {w['weight']:.2f} ({w['status']})"
            )
    return "; ".join(parts)


def render_grades(grades: list[Grade]) -> str:
    if not grades:
        return "POST-MORTEMS: no theses closed in the window"
    lines = ["POST-MORTEMS"]
    for g in grades:
        real = f"{g.realized_pct:+.2f}%" if g.realized_pct is not None else "n/a"
        pnl = f"{g.realized_pnl:+,.2f} USD" if g.realized_pnl is not None else "n/a"
        lines.append(
            f"{g.closed_at:%Y-%m-%d} {g.symbol:<6} {g.direction:<5} {g.exit_kind:<11} {real:>8} ({pnl}) "
            f"expected {g.expected_pct:+.1f}% conviction {g.conviction:.2f}: {g.verdict}"
        )
        if g.lesson:
            lines.append(f"    lesson: {g.lesson}")
    return "\n".join(lines)
