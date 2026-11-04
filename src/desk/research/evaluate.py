"""The evaluation harness: every stance an analyst took, and every buy the head trader planned, is
scored against the forward return of the name. Negative results are reported as plainly as positive
ones; an analyst that does no better than chance is still useful evidence of that."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from desk.core import calendar
from desk.core.events import Event, Topics
from desk.storage.db import Database

HORIZONS = (1, 5)


@dataclass
class Call:
    who: str  # analyst, or head_trader / skeptic
    kind: str
    symbol: str
    at: datetime
    stance: str  # bullish | bearish
    conviction: float
    event_id: str
    forward: dict[int, float | None] = field(default_factory=dict)  # sessions -> return

    def hit(self, h: int) -> bool | None:
        r = self.forward.get(h)
        if r is None:
            return None
        return r > 0 if self.stance == "bullish" else r < 0


@dataclass
class Score:
    who: str
    kind: str
    n: int = 0
    hits: dict[int, int] = field(default_factory=lambda: {h: 0 for h in HORIZONS})
    counted: dict[int, int] = field(default_factory=lambda: {h: 0 for h in HORIZONS})
    sum_return: dict[int, float] = field(default_factory=lambda: {h: 0.0 for h in HORIZONS})

    def add(self, call: Call) -> None:
        self.n += 1
        for h in HORIZONS:
            hit = call.hit(h)
            if hit is None:
                continue
            self.counted[h] += 1
            self.hits[h] += int(hit)
            r = call.forward[h] or 0.0
            self.sum_return[h] += r if call.stance == "bullish" else -r

    def hit_rate(self, h: int) -> float | None:
        return self.hits[h] / self.counted[h] if self.counted[h] else None

    def mean_signed_return(self, h: int) -> float | None:
        """Average return in the direction of the call: positive means the calls made money."""
        return self.sum_return[h] / self.counted[h] if self.counted[h] else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "who": self.who,
            "kind": self.kind,
            "n": self.n,
            "hit_rate": {str(h): self.hit_rate(h) for h in HORIZONS},
            "mean_signed_return": {str(h): self.mean_signed_return(h) for h in HORIZONS},
            "counted": {str(h): self.counted[h] for h in HORIZONS},
        }


def forward_return(
    closes: list[tuple[datetime, Decimal]], at: datetime, sessions: int
) -> float | None:
    """Return from the last close at or before `at` to the close `sessions` sessions later."""
    before = [(ts, c) for ts, c in closes if ts <= at]
    if not before:
        return None
    base_ts, base = before[-1]
    after = [(ts, c) for ts, c in closes if ts > base_ts]
    if len(after) < sessions:
        return None
    later = after[sessions - 1][1]
    return float(later) / float(base) - 1.0 if base else None


def calls_from_events(events: list[Event]) -> list[Call]:
    calls: list[Call] = []
    for e in events:
        p = e.payload
        if e.type == Topics.RESEARCH_REPORT:
            stance = p.get("stance")
            if p.get("symbol") and stance in ("bullish", "bearish"):
                calls.append(
                    Call(
                        who=str(p.get("analyst")),
                        kind=str(p.get("kind")),
                        symbol=str(p["symbol"]).upper(),
                        at=e.occurred_at,
                        stance=stance,
                        conviction=float(p.get("conviction") or 0),
                        event_id=e.event_id,
                    )
                )
        elif e.type == Topics.CYCLE_PLAN and not p.get("dry_run"):
            for d in (p.get("plan") or {}).get("decisions") or []:
                if d.get("action") == "buy":
                    calls.append(
                        Call(
                            who="head_trader",
                            kind="buy",
                            symbol=str(d.get("symbol", "")).upper(),
                            at=e.occurred_at,
                            stance="bullish",
                            conviction=float(d.get("conviction") or 0),
                            event_id=e.event_id,
                        )
                    )
            for o in (p.get("challenge") or {}).get("objections") or []:
                if o.get("severity") in ("blocking", "concern"):
                    calls.append(
                        Call(
                            who="skeptic",
                            kind=str(o.get("severity")),
                            symbol=str(o.get("symbol", "")).upper(),
                            at=e.occurred_at,
                            stance="bearish",  # an objection says: this will not pay
                            conviction=1.0 if o.get("severity") == "blocking" else 0.5,
                            event_id=e.event_id,
                        )
                    )
    return calls


async def evaluate(db: Database, now: datetime, days: int = 30) -> dict[str, Any]:
    since = now - timedelta(days=days)
    events = await db.recent_events(
        [Topics.RESEARCH_REPORT, Topics.CYCLE_PLAN], since, limit=20000, until=now
    )
    calls = calls_from_events(events)
    closes: dict[str, list[tuple[datetime, Decimal]]] = {}
    for symbol in {c.symbol for c in calls}:
        rows = await db.recent_bars(symbol, "1d", limit=days + 30, before=now)
        closes[symbol] = [(r["ts"], Decimal(str(r["close"]))) for r in rows]
    for c in calls:
        for h in HORIZONS:
            c.forward[h] = forward_return(closes.get(c.symbol, []), c.at, h)
    scores: dict[tuple[str, str], Score] = {}
    for c in calls:
        scores.setdefault((c.who, c.kind), Score(c.who, c.kind)).add(c)
    return {
        "since": since,
        "until": now,
        "days": days,
        "calls": len(calls),
        "scores": [s.as_dict() for s in sorted(scores.values(), key=lambda s: (s.who, s.kind))],
        "sessions_covered": calendar.sessions_between(since.date(), now.date()),
    }


def render(result: dict[str, Any]) -> str:
    lines = [
        f"EVALUATION {result['since']:%Y-%m-%d} to {result['until']:%Y-%m-%d}: "
        f"{result['calls']} calls over {result['sessions_covered']} sessions"
    ]
    if not result["scores"]:
        lines.append(
            "no stances to score yet: the harness needs reports and the sessions after them"
        )
        return "\n".join(lines)
    lines.append(
        f"{'who':<14}{'kind':<11}{'n':>4}  {'hit 1d':>7} {'hit 5d':>7}  {'ret 1d':>8} {'ret 5d':>8}"
    )
    for s in result["scores"]:

        def f(v: float | None, pct: bool = False) -> str:
            if v is None:
                return "   n/a"
            return f"{v * 100:+6.2f}%" if pct else f"{v * 100:5.0f}%"

        lines.append(
            f"{s['who']:<14}{s['kind']:<11}{s['n']:>4}  {f(s['hit_rate']['1']):>7} {f(s['hit_rate']['5']):>7}  "
            f"{f(s['mean_signed_return']['1'], True):>8} {f(s['mean_signed_return']['5'], True):>8}"
        )
    lines.append(
        "hit = share of calls whose direction matched the next 1 or 5 sessions; ret = average return in the call's direction"
    )
    return "\n".join(lines)
