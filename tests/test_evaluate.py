from datetime import UTC, datetime, timedelta
from decimal import Decimal

from desk.core import prompts
from desk.core.clock import SimClock
from desk.core.events import Topics
from desk.research.evaluate import Score, calls_from_events, forward_return, render
from tests.conftest import make_event

T = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)


def test_prompt_registry_versions_follow_the_text():
    reg = prompts.registry()
    assert set(reg) == {"head_trader", "plan", "brief", "morning", "skeptic"}
    assert reg["plan"].startswith("plan_v1@") and len(reg["plan"].split("@")[1]) == 8
    assert "plan_v1@" in prompts.render_registry()


def test_forward_return_uses_the_close_before_the_call():
    closes = [(T - timedelta(days=i), Decimal(100 + (5 - i))) for i in range(5, -3, -1)]
    # closes at T-5d .. T+2d with values 100 .. 107, oldest first
    assert closes[0][1] == Decimal(100) and closes[-1][1] == Decimal(107)
    r1 = forward_return(closes, T + timedelta(hours=1), 1)  # base is T's close (105), next is 106
    assert r1 is not None and abs(r1 - (106 / 105 - 1)) < 1e-9
    assert forward_return(closes, T + timedelta(hours=1), 3) is None  # not enough sessions yet
    assert forward_return(closes, T - timedelta(days=30), 1) is None  # before the data


def test_calls_are_scored_by_direction():
    clock = SimClock(T)
    events = [
        make_event(
            clock,
            type=Topics.RESEARCH_REPORT,
            symbol="AAA",
            payload={
                "analyst": "quant_analyst",
                "kind": "technical",
                "symbol": "AAA",
                "stance": "bullish",
                "conviction": 0.7,
            },
        ),
        make_event(
            clock,
            type=Topics.CYCLE_PLAN,
            payload={
                "cycle_id": "c",
                "trigger": "cli",
                "dry_run": False,
                "plan": {
                    "market_view": "",
                    "risk_note": "",
                    "decisions": [
                        {
                            "symbol": "AAA",
                            "action": "buy",
                            "quantity": 1,
                            "thesis": "",
                            "evidence_ids": [],
                            "expected_return_pct": 1,
                            "horizon_days": 5,
                            "stop_pct": 4,
                            "target_pct": 7,
                            "invalidation": [],
                            "conviction": 0.6,
                        }
                    ],
                },
                "challenge": {
                    "overall": "",
                    "objections": [{"symbol": "AAA", "severity": "concern", "argument": "thin"}],
                },
            },
        ),
    ]
    calls = calls_from_events(events)
    assert [(c.who, c.kind, c.stance) for c in calls] == [
        ("quant_analyst", "technical", "bullish"),
        ("head_trader", "buy", "bullish"),
        ("skeptic", "concern", "bearish"),
    ]
    for c in calls:
        c.forward = {1: 0.01, 5: -0.02}
    s = Score("quant_analyst", "technical")
    s.add(calls[0])
    assert s.hit_rate(1) == 1.0 and s.hit_rate(5) == 0.0 and s.mean_signed_return(5) == -0.02
    sk = Score("skeptic", "concern")
    sk.add(calls[2])
    assert sk.hit_rate(1) == 0.0 and sk.hit_rate(5) == 1.0 and sk.mean_signed_return(5) == 0.02
    text = render(
        {
            "since": T,
            "until": T,
            "days": 1,
            "calls": 1,
            "sessions_covered": 0,
            "scores": [s.as_dict()],
        }
    )
    assert "quant_analyst" in text and "100%" in text and "hit = share" in text
    empty = render(
        {"since": T, "until": T, "days": 1, "calls": 0, "sessions_covered": 0, "scores": []}
    )
    assert "no stances to score yet" in empty
