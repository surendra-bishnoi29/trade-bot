from datetime import UTC
from decimal import Decimal

from desk.agents.cycle import merge_plan
from desk.agents.scorecard import ScoreRow
from desk.core.config import Settings
from desk.core.payloads import Challenge, CyclePlan, Objection, PlanDecision


def decision(symbol, action="buy", **kw) -> PlanDecision:
    base = {
        "symbol": symbol,
        "action": action,
        "quantity": 5,
        "thesis": "because",
        "evidence_ids": ["01A", "01B"],
        "expected_return_pct": 5,
        "horizon_days": 10,
        "stop_pct": 4,
        "target_pct": 8,
        "invalidation": [],
        "conviction": 0.6,
    }
    base.update(kw)
    return PlanDecision(**base)


def rows(**held) -> list[ScoreRow]:
    out = []
    for i, s in enumerate(["AAPL", "MSFT", "NVDA", "GOOGL"], 1):
        r = ScoreRow(symbol=s, last_close=Decimal(200), rank=i)
        if s in held:
            r.position_qty = Decimal(held[s])
        out.append(r)
    return out


SETTINGS = Settings(
    _env_file=None, position_pct=Decimal("0.02"), max_open_positions=10, max_new_per_cycle=2
)
EQUITY = Decimal(100000)


def test_buys_are_sized_capped_and_filtered_by_rules():
    plan = CyclePlan(
        market_view="",
        risk_note="",
        decisions=[
            decision("AAPL"),
            decision("MSFT", conviction=0.3),
            decision("NVDA", stop_pct=1),
            decision("GOOGL"),
            decision("XYZ"),
        ],
    )
    actions = merge_plan(
        plan, Challenge(overall="", objections=[]), rows(), [], EQUITY, SETTINGS, kill_switch=False
    )
    by = {a.symbol: a for a in actions}
    assert by["AAPL"].action == "buy" and by["AAPL"].quantity == 10  # 2% of 100k = 2,000 / 200
    assert by["MSFT"].action == "none" and "conviction" in by["MSFT"].note
    assert by["NVDA"].action == "none" and "stop" in by["NVDA"].note
    assert by["GOOGL"].action == "buy"
    assert by["XYZ"].action == "none" and "universe" in by["XYZ"].note
    plan_pass = CyclePlan(
        market_view="",
        risk_note="",
        decisions=[decision("AAPL", action="none", thesis="long story")],
    )
    assert (
        merge_plan(
            plan_pass,
            Challenge(overall="", objections=[]),
            rows(),
            [],
            EQUITY,
            SETTINGS,
            kill_switch=False,
        )[0].note
        == "passed"
    )
    plan2 = CyclePlan(
        market_view="",
        risk_note="",
        decisions=[decision("AAPL"), decision("MSFT"), decision("NVDA")],
    )
    actions2 = merge_plan(
        plan2, Challenge(overall="", objections=[]), rows(), [], EQUITY, SETTINGS, kill_switch=False
    )
    assert [a.action for a in actions2] == ["buy", "buy", "none"] and "slot" in actions2[2].note


def test_skeptic_blocks_and_kill_switch_stops_buys_but_not_sells():
    plan = CyclePlan(
        market_view="",
        risk_note="",
        decisions=[
            decision("AAPL"),
            decision("MSFT", action="sell", quantity=7),
            decision("NVDA", action="sell"),
        ],
    )
    challenge = Challenge(
        overall="",
        objections=[
            Objection(symbol="AAPL", severity="blocking", argument="no"),
            Objection(symbol="MSFT", severity="concern", argument="hmm"),
        ],
    )
    actions = merge_plan(plan, challenge, rows(MSFT=7), [], EQUITY, SETTINGS, kill_switch=False)
    by = {a.symbol: a for a in actions}
    assert by["AAPL"].action == "none" and "skeptic" in by["AAPL"].note
    assert by["MSFT"].action == "sell" and by["MSFT"].quantity == 7
    assert by["NVDA"].action == "none" and "nothing held" in by["NVDA"].note
    killed = merge_plan(
        plan,
        Challenge(overall="", objections=[]),
        rows(MSFT=7),
        [],
        EQUITY,
        SETTINGS,
        kill_switch=True,
    )
    assert [a.action for a in killed] == ["none", "sell", "none"]


def test_held_names_become_holds_and_rendering_works():
    from desk.agents.cycle import Action, CycleResult

    plan = CyclePlan(
        market_view="calm",
        risk_note="none",
        decisions=[decision("AAPL"), decision("MSFT", action="hold")],
    )
    actions = merge_plan(
        plan,
        Challenge(overall="fine", objections=[]),
        rows(AAPL=3),
        [],
        EQUITY,
        SETTINGS,
        kill_switch=False,
    )
    assert (
        actions[0].action == "hold"
        and "already held" in actions[0].note
        and actions[1].action == "hold"
    )
    text = CycleResult(
        "c1", "test", True, "", plan, Challenge(overall="fine", objections=[]), actions
    ).render()
    assert "MARKET VIEW: calm" in text and "ORDERS: none" in text and "-> hold" in text
    assert isinstance(actions[0], Action)


def test_scheduler_next_run_respects_the_schedule_and_the_interval():
    from datetime import datetime

    from desk.agents.cycle import CycleScheduler
    from desk.bus.memory import InMemoryBus
    from desk.core.clock import SimClock

    clock = SimClock(datetime(2026, 11, 2, 15, 0, tzinfo=UTC))  # Monday, market open
    scheduled = CycleScheduler(
        InMemoryBus(clock), clock, cycle=None, strategy=None, settings=Settings(_env_file=None)
    )  # type: ignore[arg-type]
    at, trigger = scheduled.next_run(clock.now())
    assert trigger == "mid_session" and at == datetime(2026, 11, 2, 17, 30, tzinfo=UTC)
    hourly = CycleScheduler(
        InMemoryBus(clock),
        clock,
        cycle=None,
        strategy=None,
        settings=Settings(_env_file=None, cycle_every_minutes=60),
    )  # type: ignore[arg-type]
    hourly.last_cycle_at = datetime(2026, 11, 2, 14, 40, tzinfo=UTC)
    at, trigger = hourly.next_run(clock.now())
    assert trigger == "every_60m" and at == datetime(2026, 11, 2, 15, 40, tzinfo=UTC)
    closed = SimClock(datetime(2026, 11, 7, 15, 0, tzinfo=UTC))  # Saturday
    weekend = CycleScheduler(
        InMemoryBus(closed),
        closed,
        cycle=None,
        strategy=None,
        settings=Settings(_env_file=None, cycle_every_minutes=60),
    )  # type: ignore[arg-type]
    at, trigger = weekend.next_run(closed.now())
    assert trigger == "pre_market" and at == datetime(2026, 11, 9, 13, 30, tzinfo=UTC)


def test_a_skeptic_concern_needs_conviction():
    concern = Challenge(
        overall="", objections=[Objection(symbol="AAPL", severity="concern", argument="thin")]
    )
    weak = CyclePlan(market_view="", risk_note="", decisions=[decision("AAPL", conviction=0.45)])
    strong = CyclePlan(market_view="", risk_note="", decisions=[decision("AAPL", conviction=0.6)])
    a = merge_plan(weak, concern, rows(), [], EQUITY, SETTINGS, kill_switch=False)[0]
    assert a.action == "none" and "concern" in a.note
    assert (
        merge_plan(strong, concern, rows(), [], EQUITY, SETTINGS, kill_switch=False)[0].action
        == "buy"
    )
