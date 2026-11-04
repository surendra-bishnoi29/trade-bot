"""The performance reviewer: grades from the journal, credibility weights, the agent's events, and
the replay comparison's arithmetic."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from desk.agents.reviewer import ReviewerAgent, analyst_record
from desk.bus.memory import InMemoryBus
from desk.core.clock import SimClock
from desk.core.events import Topics
from desk.core.payloads import ThesisPayload
from desk.replay.replayer import ReplayReport
from desk.review.compare import render as render_compare
from desk.review.compare import summarize, totals
from desk.review.postmortem import (
    credibility_weights,
    excursions,
    exit_kind,
    grade,
    render_grades,
    render_weights,
    verdict_for,
)
from tests.conftest import make_event

OPENED = datetime(2026, 10, 26, 14, 35, tzinfo=UTC)
CLOSED = datetime(2026, 10, 30, 18, 0, tzinfo=UTC)


def thesis(**kw) -> ThesisPayload:
    base = {
        "thesis_id": "t1",
        "symbol": "V",
        "book": "swing",
        "direction": "long",
        "status": "closed",
        "cycle_id": "c1",
        "narrative": "breakout",
        "evidence_ids": ["e1", "e2"],
        "expected_return_pct": 4.0,
        "horizon_days": 10,
        "conviction": 0.7,
        "quantity": Decimal(10),
        "entry_price": Decimal(100),
        "stop_price": Decimal(97),
        "target_price": Decimal(105),
        "stop_pct": 3.0,
        "target_pct": 5.0,
        "reason": "target hit at 105.10",
        "created_at": OPENED,
        "updated_at": CLOSED,
        "closed_at": CLOSED,
        "realized_pnl": Decimal("51.00"),
    }
    base.update(kw)
    return ThesisPayload(**base)


def bars(path: list[tuple[float, float]]) -> list[dict]:
    return [
        {"ts": OPENED + timedelta(days=i), "open": hi, "high": hi, "low": lo, "close": hi}
        for i, (hi, lo) in enumerate(path)
    ]


def test_exit_kinds_and_excursions():
    assert exit_kind("target hit at 105") == "target"
    assert exit_kind("stop hit at 96.9") == "stop"
    assert exit_kind("horizon elapsed (time exit) at 101") == "time"
    assert exit_kind("invalidated: earnings miss") == "invalidated"
    assert exit_kind("reconciled: position gone") == "reconciled"
    assert exit_kind("") == "other"
    up, down = excursions(thesis(), bars([(102, 99), (106, 101)]))
    assert up == pytest.approx(6.0) and down == pytest.approx(-1.0)
    up, down = excursions(thesis(direction="short"), bars([(102, 99), (106, 101)]))
    assert up == pytest.approx(1.0) and down == pytest.approx(-6.0)
    assert excursions(thesis(entry_price=None), bars([(1, 1)])) == (None, None)


def test_grade_target_stop_and_time_exits():
    g = grade(thesis(), bars([(102, 99), (106, 101)]), CLOSED)
    assert g.realized_pct == pytest.approx(5.1) and g.exit_kind == "target"
    assert g.holding_sessions == 4 and g.mfe_pct == pytest.approx(6.0)
    assert "Target hit after 4 sessions" in g.verdict and "met the plan" in g.verdict
    stopped = grade(
        thesis(reason="stop hit at 96.9", realized_pnl=Decimal(-31)),
        bars([(103.5, 99), (101, 96.5)]),
        CLOSED,
    )
    assert stopped.exit_kind == "stop" and stopped.realized_pct == pytest.approx(-3.1)
    assert "managed loosely" in stopped.verdict  # it had 3.5% of a 5% target in hand
    timed = grade(
        thesis(reason="time exit at 101", realized_pnl=Decimal(10)), bars([(101, 100)]), CLOSED
    )
    assert timed.exit_kind == "time" and "Captured 25%" in timed.verdict
    unknown = grade(thesis(realized_pnl=None, reason="kill switch flattened"), [], CLOSED)
    assert (
        unknown.realized_pct is None
        and unknown.exit_kind == "kill"
        and "unknown" in unknown.verdict
    )
    text = render_grades([g, stopped])
    assert "POST-MORTEMS" in text and "+5.10%" in text and "-3.10%" in text
    assert render_grades([]).startswith("POST-MORTEMS: no theses")
    assert verdict_for(g).startswith("Target hit")


def test_credibility_weights_from_scores():
    scores = [
        {
            "who": "quant_analyst",
            "kind": "technical",
            "n": 40,
            "hit_rate": {"1": 0.5, "5": 0.6},
            "counted": {"1": 40, "5": 40},
        },
        {
            "who": "news_analyst",
            "kind": "news",
            "n": 30,
            "hit_rate": {"1": 0.5, "5": 0.4},
            "counted": {"1": 30, "5": 30},
        },
        {
            "who": "macro",
            "kind": "macro",
            "n": 4,
            "hit_rate": {"1": 1.0, "5": 1.0},
            "counted": {"1": 4, "5": 4},
        },
        {
            "who": "head_trader",
            "kind": "buy",
            "n": 20,
            "hit_rate": {"1": 0.5, "5": 0.95},
            "counted": {"1": 20, "5": 20},
        },
    ]
    w = credibility_weights(scores)
    assert w["quant_analyst/technical"] == {
        "weight": 1.2,
        "hit_5d": 0.6,
        "n": 40,
        "status": "trusted",
    }
    assert w["news_analyst/news"]["weight"] == 0.8 and w["news_analyst/news"]["status"] == "doubted"
    assert w["macro/macro"]["status"] == "unproven" and w["macro/macro"]["weight"] == 1.0
    assert w["head_trader/buy"]["weight"] == 1.5  # bounded
    text = render_weights(w)
    assert "quant_analyst/technical: hit 60% over 5 sessions, n=40, weight 1.20 (trusted)" in text
    assert "macro/macro: unproven (4 scored calls)" in text
    assert render_weights({}) == "no scored calls yet"


class FakeHead:
    """Stands in for the head trader: no client, so the reviewer keeps the numbers only."""

    cache = None
    client = None
    model = "test"


class FakeDB:
    def __init__(self, theses, events=None):
        self.theses = theses
        self.events = events or {}

    async def closed_theses(self, since, until):
        return [t for t in self.theses if since <= (t.closed_at or t.updated_at) <= until]

    async def recent_events(self, types, since, limit=100, until=None, **kw):
        out = []
        for t in types:
            out.extend(self.events.get(t, []))
        return out

    async def bars_between(self, symbols, interval, start, end):
        return bars([(102, 99), (106, 101)])

    async def recent_bars(self, symbol, interval, limit=30, before=None):
        return []


async def test_reviewer_emits_a_postmortem_per_closed_thesis_once_and_scores_on_friday():
    friday_close = datetime(2026, 10, 30, 21, 30, tzinfo=UTC)
    clock = SimClock(friday_close)
    bus = InMemoryBus(clock)
    db = FakeDB(
        [
            thesis(),
            thesis(thesis_id="t2", symbol="HD", reason="stop hit", realized_pnl=Decimal(-31)),
        ]
    )
    agent = ReviewerAgent(bus, clock, db, FakeHead(), narrative=False)  # type: ignore[arg-type]
    agent.start()
    task = asyncio.create_task(bus.run())
    await bus.publish(
        make_event(
            clock, type=Topics.CYCLE_PLAN, payload={"trigger": "post_close", "dry_run": False}
        )
    )
    await bus.wait_idle()
    posts = [e for e in bus.log if e.type == Topics.REVIEW_POSTMORTEM]
    assert [e.symbol for e in posts] == ["V", "HD"] and posts[0].payload["exit_kind"] == "target"
    assert posts[1].payload["realized_pct"] == pytest.approx(-3.1) and posts[0].book == "swing"
    scores = [e for e in bus.log if e.type == Topics.REVIEW_ANALYST_SCORE]
    assert len(scores) == 1 and scores[0].payload["days"] == 60
    # a second post-close cycle grades nothing twice
    db.events[Topics.REVIEW_POSTMORTEM] = posts
    db.events[Topics.REVIEW_ANALYST_SCORE] = scores
    clock.advance_to(friday_close + timedelta(days=3))
    await bus.publish(
        make_event(
            clock, type=Topics.CYCLE_PLAN, payload={"trigger": "post_close", "dry_run": False}
        )
    )
    await bus.wait_idle()
    assert len([e for e in bus.log if e.type == Topics.REVIEW_POSTMORTEM]) == 2
    assert (
        len([e for e in bus.log if e.type == Topics.REVIEW_ANALYST_SCORE]) == 1
    )  # Monday, scored 3 days ago
    record = await analyst_record(db, clock.now())  # type: ignore[arg-type]
    assert "no scored calls yet" in record or "unproven" in record or "hit" in record
    assert "not scored yet" in await analyst_record(FakeDB([]), clock.now())  # type: ignore[arg-type]
    await bus.stop()
    task.cancel()


def test_compare_summary_and_totals():
    a = ReplayReport(
        day=OPENED.date(),
        equity_start=Decimal(100000),
        realized_pnl=Decimal(250),
        unrealized_pnl=Decimal(-50),
        costs=Decimal(12),
        max_drawdown_pct=-0.4,
    )
    a.fills = [{"x": 1}, {"x": 2}]
    a.llm = {"hits": 3, "misses": 2}
    b = ReplayReport(
        day=CLOSED.date(),
        equity_start=Decimal(100000),
        realized_pnl=Decimal(-100),
        costs=Decimal(5),
        max_drawdown_pct=-0.9,
    )
    sa, sb = summarize(a), summarize(b)
    assert (
        sa["pnl"] == 200
        and sa["return_pct"] == pytest.approx(0.2)
        and sa["fills"] == 2
        and sa["llm_misses"] == 2
    )
    rows = [
        {"day": "2026-10-26", "spy_pct": 0.5, "rules": sa, "head_trader": sb},
        {"day": "2026-10-30", "spy_pct": -0.25, "rules": sb},
    ]
    t = totals(rows)
    assert (
        t["rules"]["pnl"] == 100
        and t["rules"]["days_positive"] == 1
        and t["rules"]["worst_day_drawdown_pct"] == -0.9
    )
    assert t["head_trader"]["fills"] == 0 and t["spy_pct"] == pytest.approx(0.25)
    text = render_compare({"days": 2, "llm": True, "capital": 100000.0, "rows": rows, "totals": t})
    assert (
        "REPLAY COMPARISON over 2 sessions" in text
        and "head trader: P&L -100" in text
        and "SPY open-to-close" in text
    )
