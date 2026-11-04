"""The weekly review and the graduation checklist on a faked journal, and the reviewer's Friday
weekly event."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from desk.agents.reviewer import ReviewerAgent
from desk.bus.memory import InMemoryBus
from desk.core.clock import SimClock
from desk.core.events import Event, Topics
from desk.review.graduation import graduation, render_graduation
from desk.review.weekly import render_weekly, weekly_facts
from tests.test_review import FakeHead, thesis

NOW = datetime(2026, 11, 6, 21, 30, tzinfo=UTC)  # a Friday after the close


def ev(
    type: str, when: datetime, payload: dict, symbol: str | None = None, book: str | None = None
) -> Event:
    return Event(
        type=type,
        occurred_at=when,
        ingested_at=when,
        source="test@0.1",
        symbol=symbol,
        book=book,
        payload=payload,
    )


class FakeJournal:
    """Enough of Database for the weekly review and the graduation checklist."""

    def __init__(
        self,
        *,
        first=NOW - timedelta(days=100),
        equity=None,
        heartbeat_days=65,
        bypasses=0,
        bars_short=(),
        restarts=None,
        kills=None,
        champion=None,
    ):
        self.first = first
        self.equity = equity or [
            (NOW - timedelta(days=6), Decimal(100000)),
            (NOW - timedelta(days=3), Decimal(101500)),
            (NOW, Decimal(100900)),
        ]
        self.heartbeat_days = heartbeat_days
        self.bypasses = bypasses
        self.bars_short = set(bars_short)
        self.restarts = restarts or []
        self.kills = kills or []
        self.champ = champion
        self.theses = [
            thesis(closed_at=NOW - timedelta(days=2), updated_at=NOW - timedelta(days=2))
        ]

    async def equity_series(self, since, until):
        return [(t, e) for t, e in self.equity if since <= t <= until]

    async def recent_events(self, types, since, limit=100, until=None, **kw):
        out = []
        if Topics.ORDER_FILLED in types:
            out += [
                ev(
                    Topics.ORDER_FILLED,
                    NOW - timedelta(days=1),
                    {
                        "intent_id": "i1",
                        "filled_quantity": "4",
                        "avg_price": "250",
                        "status": "filled",
                        "order_id": "o1",
                    },
                    "V",
                )
            ]
        if Topics.RISK_KILL_SWITCH in types:
            out += self.kills
        if Topics.REVIEW_ANALYST_SCORE in types:
            out += [
                ev(
                    Topics.REVIEW_ANALYST_SCORE,
                    NOW - timedelta(days=1),
                    {
                        "weights": {
                            "quant_analyst/technical": {
                                "weight": 1.2,
                                "hit_5d": 0.6,
                                "n": 40,
                                "status": "trusted",
                            }
                        }
                    },
                )
            ]
        return out

    async def closed_theses(self, since, until):
        return [t for t in self.theses if since <= t.closed_at <= until]

    async def bars_between(self, symbols, interval, start, end):
        return [{"ts": start, "open": 100, "high": 104, "low": 99, "close": 103}]

    async def recent_bars(self, *a, **k):
        return []

    async def restart_gaps(self, since, until):
        return self.restarts

    async def count_events_between(self, types, since, until, **kw):
        if Topics.RISK_KILL_SWITCH in types:
            return len([k for k in self.kills if since <= k.occurred_at < until])
        if Topics.CYCLE_PLAN in types:
            return 15
        if Topics.ORDER_FILLED in types and kw.get("book") == "intraday":
            return 0
        return 1

    async def models(self, kind, **kw):
        return [
            {
                "model_id": "swing_5d_x",
                "status": "candidate",
                "horizon": 5,
                "metrics": {"pooled": {"ic": 0.003}},
                "note": "no reliable out-of-sample skill",
            }
        ]

    async def first_event_at(self, type=None):
        return self.first

    async def sessions_seen(self, since, until):
        return [NOW.date() - timedelta(days=i) for i in range(self.heartbeat_days)]

    async def sessions_with_events(self, type, since, until):
        return [NOW.date() - timedelta(days=i) for i in range(70)]

    async def bar_count_per_session(self, symbol, since, until):
        return {
            NOW.date() - timedelta(days=i): (
                100 if (NOW.date() - timedelta(days=i)) in self.bars_short else 390
            )
            for i in range(70)
        }

    async def fills_without_approval(self, since, until):
        return self.bypasses

    async def champion(self, kind, horizon):
        return self.champ


async def test_weekly_review_markdown_carries_every_section():
    facts = await weekly_facts(FakeJournal(), NOW)  # type: ignore[arg-type]
    assert facts["pnl"] == 900 and facts["trades"] == 1 and facts["notional"] == 1000
    assert facts["max_drawdown_pct"] < 0 and len(facts["grades"]) == 1
    assert facts["model_calls_estimate"] == 15 * 2 + 1 + 1
    text = render_weekly(facts)
    for part in (
        "# Weekly review",
        "## Performance",
        "+900 USD",
        "## Post-mortems",
        "**V** long, target",
        "## Analyst scoreboard",
        "quant_analyst/technical: hit 60%",
        "## Incidents",
        "Kill switch events: 0",
        "## Models and prompts",
        "swing_5d_x: candidate",
        "plan_v1@",
    ):
        assert part in text, part


async def test_graduation_checklist_passes_on_a_clean_journal_and_names_what_fails():
    clean = await graduation(FakeJournal(), NOW)  # type: ignore[arg-type]
    assert clean["ready"], [c for c in clean["criteria"] if c["status"] != "pass"]
    assert {c["key"] for c in clean["criteria"]} == {
        "operation",
        "uninterrupted",
        "performance",
        "drawdown",
        "intraday",
        "forecast",
        "journal",
        "risk",
        "incidents",
        "reconciliation",
    }
    text = render_graduation(clean)
    assert text.startswith("GRADUATION") and "READY" in text and "[PASS]" in text
    young = await graduation(FakeJournal(first=NOW - timedelta(days=10), heartbeat_days=8), NOW)  # type: ignore[arg-type]
    assert not young["ready"] and set(young["pending"]) == {"operation", "performance"}
    kills = [
        ev(Topics.RISK_KILL_SWITCH, NOW - timedelta(days=2), {"engaged": True, "reason": "x"}),
        ev(Topics.RISK_KILL_SWITCH, NOW - timedelta(days=1), {"engaged": True, "reason": "y"}),
    ]
    bad = await graduation(
        FakeJournal(
            bypasses=1,
            bars_short=[NOW.date() - timedelta(days=3)],
            restarts=[
                {"started_at": "2026-11-03T15:00", "gap_seconds": 4000, "during_session": True}
            ],
            kills=kills,
        ),
        NOW,
    )  # type: ignore[arg-type]
    assert set(bad["failing"]) == {"risk", "journal", "uninterrupted", "incidents"}
    assert "[FAIL] Zero risk-rule bypasses" in render_graduation(bad)
    champion = {
        "model_id": "m",
        "metrics": {"pooled": {"calibration": [{"n": 100, "p_mean": 0.7, "up_rate": 0.5}]}},
    }
    miscal = await graduation(FakeJournal(champion=champion), NOW)  # type: ignore[arg-type]
    assert "forecast" in miscal["failing"]
    drawdown = await graduation(
        FakeJournal(equity=[(NOW - timedelta(days=5), Decimal(100000)), (NOW, Decimal(93000))]), NOW
    )  # type: ignore[arg-type]
    assert {"drawdown", "performance"} <= set(drawdown["failing"])


async def test_reviewer_writes_the_weekly_review_on_friday():
    clock = SimClock(NOW)
    bus = InMemoryBus(clock)
    agent = ReviewerAgent(bus, clock, FakeJournal(), FakeHead(), narrative=False)  # type: ignore[arg-type]
    agent.start()
    task = asyncio.create_task(bus.run())
    await bus.publish(ev(Topics.CYCLE_PLAN, NOW, {"trigger": "post_close", "dry_run": False}))
    await bus.wait_idle()
    weekly = [e for e in bus.log if e.type == Topics.REVIEW_WEEKLY]
    assert len(weekly) == 1 and weekly[0].payload["week_ending"] == date(2026, 11, 6).isoformat()
    assert (
        "# Weekly review" in weekly[0].payload["markdown"]
        and weekly[0].payload["facts"]["trades"] == 1
    )
    await bus.stop()
    task.cancel()


async def test_journal_helpers_for_operations(database_url):
    from decimal import Decimal as D

    from desk.core.payloads import MarketBar
    from desk.storage.db import Database

    db = Database(database_url)
    await db.open()
    await db.migrate()
    t0 = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)

    def e(type, when, payload, symbol=None, book=None):
        return Event(
            type=type,
            occurred_at=when,
            ingested_at=when,
            source="opstest@0.1",
            symbol=symbol,
            book=book,
            payload=payload,
        )

    try:
        for x in (
            e(
                Topics.PORTFOLIO_RECONCILED,
                t0,
                {
                    "equity": "100000",
                    "cash": "1",
                    "buying_power": "1",
                    "positions": [],
                    "as_of": t0.isoformat(),
                },
            ),
            e(
                Topics.PORTFOLIO_RECONCILED,
                t0 + timedelta(hours=1),
                {
                    "equity": "100500",
                    "cash": "1",
                    "buying_power": "1",
                    "positions": [],
                    "as_of": t0.isoformat(),
                },
            ),
            e(Topics.SYSTEM_HEARTBEAT, t0, {"status": "ok"}),
            e(Topics.SYSTEM_HEARTBEAT, t0 + timedelta(days=1), {"status": "degraded"}),
            e(
                Topics.RISK_APPROVED,
                t0,
                {"intent_id": "ops-approved", "decision": "approved"},
                "ZZOPS",
                "swing",
            ),
            e(
                Topics.ORDER_FILLED,
                t0 + timedelta(minutes=1),
                {
                    "order_id": "o1",
                    "intent_id": "ops-approved",
                    "status": "filled",
                    "filled_quantity": "1",
                    "avg_price": "10",
                },
                "ZZOPS",
                "swing",
            ),
            e(
                Topics.ORDER_FILLED,
                t0 + timedelta(minutes=2),
                {
                    "order_id": "o2",
                    "intent_id": "ops-rogue",
                    "status": "filled",
                    "filled_quantity": "1",
                    "avg_price": "10",
                },
                "ZZOPS",
                "intraday",
            ),
            e(
                Topics.RISK_KILL_SWITCH,
                t0 + timedelta(minutes=3),
                {"engaged": True, "reason": "test"},
            ),
            e(
                Topics.RISK_KILL_SWITCH,
                t0 + timedelta(minutes=4),
                {"engaged": False, "reason": "test"},
            ),
            e(
                Topics.SYSTEM_RESTARTED,
                t0 + timedelta(minutes=5),
                {"started_at": t0.isoformat(), "gap_seconds": 2000.0, "during_session": True},
            ),
        ):
            assert await db.insert_event(x)
        bars = [
            MarketBar(
                symbol="ZZOPS",
                interval="1m",
                ts=t0 + timedelta(minutes=i),
                open=D(1),
                high=D(1),
                low=D(1),
                close=D(1),
                volume=1,
                source="opstest",
            )
            for i in range(3)
        ]
        await db.insert_bars(bars, t0)
        since, until = t0 - timedelta(hours=1), t0 + timedelta(days=2)
        series = await db.equity_series(since, until)
        assert [float(v) for _, v in series][-2:] == [100000.0, 100500.0]
        assert date(2026, 11, 5) in await db.sessions_seen(since, until)
        assert (
            await db.count_events_between([Topics.SYSTEM_HEARTBEAT], since, until, status_not="ok")
            >= 1
        )
        assert (
            await db.count_events_between([Topics.RISK_KILL_SWITCH], since, until, engaged=True)
            >= 1
        )
        assert (
            await db.count_events_between([Topics.ORDER_FILLED], since, until, book="intraday") >= 1
        )
        assert await db.fills_without_approval(since, until) >= 1
        counts = await db.bar_count_per_session("ZZOPS", since, until)
        assert counts[date(2026, 11, 5)] == 3
        gaps = await db.restart_gaps(since, until)
        assert any(g["gap_seconds"] == 2000.0 and g["during_session"] for g in gaps)
        assert (await db.first_event_at()) is not None
    finally:
        async with db.pool.connection() as conn:
            await conn.execute("DELETE FROM events WHERE source = 'opstest@0.1'")
            await conn.execute("DELETE FROM bars WHERE symbol = 'ZZOPS'")
        await db.close()
