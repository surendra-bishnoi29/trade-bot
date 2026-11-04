from datetime import UTC, datetime, timedelta
from decimal import Decimal

from desk.agents.risk_officer import PortfolioView, RiskLimits, RiskState
from desk.agents.risk_officer import evaluate as evaluate_rules
from desk.core.payloads import OrderIntent

OPEN = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
CLOSED = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
LIMITS = RiskLimits(universe=frozenset({"AAPL", "MSFT"}))
FRESH = timedelta(minutes=1)


def evaluate(*args, price_age=FRESH, **kw):
    """The rules under test see a one-minute-old price unless a test says otherwise."""
    return evaluate_rules(*args, price_age=price_age, **kw)


def intent(symbol="AAPL", side="buy", qty="10") -> OrderIntent:
    return OrderIntent(
        intent_id="i1", symbol=symbol, book="swing", side=side, quantity=Decimal(qty)
    )


def portfolio(**positions) -> PortfolioView:
    return PortfolioView(
        equity=Decimal(100000), positions={k: Decimal(v) for k, v in positions.items()}
    )


def failed(decision) -> list[str]:
    return [r.rule for r in decision.rules if not r.passed]


def test_a_small_buy_inside_the_universe_is_approved():
    d = evaluate(intent(), portfolio(), Decimal(250), OPEN, LIMITS, RiskState())
    assert d.decision == "approved" and d.approved_quantity == 10 and failed(d) == []


def test_order_notional_and_position_caps():
    d = evaluate(intent(qty="30"), portfolio(), Decimal(250), OPEN, LIMITS, RiskState())
    assert failed(d) == ["order_notional"]  # 7,500 > 5% of 100,000
    d = evaluate(intent(qty="15"), portfolio(AAPL="30"), Decimal(250), OPEN, LIMITS, RiskState())
    assert failed(d) == ["position_cap"]  # 45 shares * 250 = 11,250 > 10% of equity


def test_short_selling_universe_quantity_and_price_rules():
    assert failed(
        evaluate(intent(side="sell", qty="5"), portfolio(), Decimal(250), OPEN, LIMITS, RiskState())
    ) == ["short_selling"]
    assert failed(
        evaluate(intent(symbol="TSLA"), portfolio(), Decimal(250), OPEN, LIMITS, RiskState())
    ) == ["universe"]
    assert failed(
        evaluate(intent(qty="2.5"), portfolio(), Decimal(250), OPEN, LIMITS, RiskState())
    ) == ["quantity"]
    assert failed(evaluate(intent(), portfolio(), None, OPEN, LIMITS, RiskState())) == [
        "price_known"
    ]


def test_kill_switch_and_daily_cap_never_block_an_exit():
    engaged = RiskState(kill_switch=True, kill_reason="test")
    assert failed(evaluate(intent(), portfolio(), Decimal(250), OPEN, LIMITS, engaged)) == [
        "kill_switch"
    ]
    closing = intent(side="sell", qty="5")
    assert (
        evaluate(closing, portfolio(AAPL="10"), Decimal(250), OPEN, LIMITS, engaged).decision
        == "approved"
    )
    capped = RiskState(orders_today=20)
    assert failed(evaluate(intent(), portfolio(), Decimal(250), OPEN, LIMITS, capped)) == [
        "daily_order_cap"
    ]
    assert (
        evaluate(closing, portfolio(AAPL="10"), Decimal(250), OPEN, LIMITS, capped).decision
        == "approved"
    )


def test_market_hours_policy():
    strict = RiskLimits(universe=frozenset({"AAPL"}), allow_when_closed=False)
    assert failed(evaluate(intent(), portfolio(), Decimal(250), CLOSED, strict, RiskState())) == [
        "market_hours"
    ]
    assert (
        evaluate(intent(), portfolio(), Decimal(250), CLOSED, LIMITS, RiskState()).decision
        == "approved"
    )
    assert (
        evaluate(intent(), portfolio(), Decimal(250), OPEN, strict, RiskState()).decision
        == "approved"
    )


def test_rejection_reason_names_every_failed_rule():
    d = evaluate(intent(symbol="TSLA", qty="2.5"), portfolio(), None, OPEN, LIMITS, RiskState())
    assert d.decision == "rejected" and d.approved_quantity == 0
    assert "universe" in d.reason and "quantity" in d.reason and "price_known" in d.reason


async def test_kill_switch_and_order_count_are_restored_after_a_restart(clock):
    from desk.agents.risk_officer import RiskOfficer
    from desk.bus.memory import InMemoryBus
    from desk.core.events import Topics
    from tests.conftest import make_event

    class FakeDB:
        def __init__(self, events):
            self.events = events

        async def recent_events(self, types, since, *, symbol=None, limit=50):
            return [e for e in self.events if e.type in types][:limit]

    approved = make_event(
        clock, type=Topics.RISK_APPROVED, symbol="AAPL", payload={"intent_id": "i"}
    )
    engaged = make_event(
        clock,
        type=Topics.RISK_KILL_SWITCH,
        payload={"engaged": True, "reason": "feed stale", "source": "monitor"},
    )
    officer = RiskOfficer(
        InMemoryBus(clock), clock, LIMITS, lambda: portfolio(), lambda s: Decimal(250)
    )
    assert await officer.restore(FakeDB([engaged, approved, approved])) is True
    assert (
        officer.state.kill_switch
        and "restored" in officer.state.kill_reason
        and officer.state.orders_today == 2
    )
    released = make_event(
        clock,
        type=Topics.RISK_KILL_SWITCH,
        payload={"engaged": False, "reason": "manual resume", "source": "console"},
    )
    fresh = RiskOfficer(
        InMemoryBus(clock), clock, LIMITS, lambda: portfolio(), lambda s: Decimal(250)
    )
    assert (
        await fresh.restore(FakeDB([released])) is False
        and not fresh.state.kill_switch
        and fresh.state.orders_today == 0
    )


def test_no_order_on_a_stale_price_while_the_market_is_open():
    def run(now, age):
        return evaluate(
            intent(), portfolio(), Decimal(250), now, LIMITS, RiskState(), price_age=age
        )

    assert run(OPEN, timedelta(minutes=5)).decision == "approved"
    stale = run(OPEN, timedelta(minutes=20))
    assert failed(stale) == ["price_fresh"] and "20 min old" in stale.reason
    assert failed(run(OPEN, None)) == ["price_fresh"]
    # closed: the order queues for the next open (allow_when_closed), so the rule does not apply
    assert run(CLOSED, timedelta(hours=5)).decision == "approved"
    # exits are never blocked by staleness, like every other limit
    exit_ = evaluate(
        intent(side="sell", qty="5"),
        portfolio(AAPL="10"),
        Decimal(250),
        OPEN,
        LIMITS,
        RiskState(),
        price_age=timedelta(hours=5),
    )
    assert exit_.decision == "approved"


def test_a_second_order_on_a_side_already_working_at_the_broker_is_rejected():
    """Queued sells from another process must not be doubled into a short at the open."""
    view = PortfolioView(
        equity=Decimal(100000), positions={"AAPL": Decimal(8)}, open_orders={"AAPL": {"sell"}}
    )
    dup = evaluate(intent(side="sell", qty="8"), view, Decimal(250), OPEN, LIMITS, RiskState())
    assert failed(dup) == ["duplicate_order"] and "already working" in dup.reason
    buy = evaluate(intent(side="buy", qty="2"), view, Decimal(250), OPEN, LIMITS, RiskState())
    assert "duplicate_order" not in failed(buy)
