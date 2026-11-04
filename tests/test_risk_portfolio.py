"""The risk officer's book and portfolio rules: exposure, sectors, daily loss, the drawdown ladder,
pattern-day-trader awareness, shorts in the intraday book, and the automatic kill switch."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from desk.agents.risk_officer import (
    PortfolioView,
    RiskLimits,
    RiskOfficer,
    RiskState,
    drawdown,
    size_scale,
)
from desk.agents.risk_officer import evaluate as evaluate_rules
from desk.bus.memory import InMemoryBus
from desk.core.clock import SimClock
from desk.core.events import Topics
from desk.core.payloads import OrderIntent
from desk.core.sectors import SECTORS
from tests.conftest import make_event

OPEN = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
TECH = [s for s, sector in SECTORS.items() if sector == SECTORS["AAPL"]][:2]
OTHER = next(s for s, sector in SECTORS.items() if sector != SECTORS["AAPL"])
UNIVERSE = frozenset({"AAPL", "MSFT", "JPM", *TECH, OTHER})
LIMITS = RiskLimits(universe=UNIVERSE)
PRICE = Decimal(250)


def evaluate(*args, price_age=timedelta(minutes=1), **kw):
    return evaluate_rules(*args, price_age=price_age, **kw)


def intent(symbol="AAPL", side="buy", qty="10", book="swing") -> OrderIntent:
    return OrderIntent(intent_id="i1", symbol=symbol, book=book, side=side, quantity=Decimal(qty))


def portfolio(equity=100000, values: dict[str, int] | None = None, **extra) -> PortfolioView:
    """Positions priced at 250 with the given market values."""
    values = values or {}
    return PortfolioView(
        equity=Decimal(equity),
        positions={s: Decimal(v) / PRICE for s, v in values.items()},
        market_values={s: Decimal(v) for s, v in values.items()},
        **extra,
    )


def failed(decision) -> list[str]:
    return [r.rule for r in decision.rules if not r.passed]


def test_gross_exposure_caps_the_account():
    first_of_sector: dict[str, str] = {}
    for s, sector in SECTORS.items():
        first_of_sector.setdefault(sector, s)
    spread = list(first_of_sector.values())[
        :5
    ]  # five names in five sectors, each under the sector cap
    limits = RiskLimits(universe=UNIVERSE | set(spread))
    view = portfolio(values=dict.fromkeys(spread[:4], 24000))
    d = evaluate(intent(spread[4], qty="20"), view, PRICE, OPEN, limits, RiskState())
    assert failed(d) == ["gross_exposure", "book_exposure"]  # 96,000 + 5,000 > 100% of equity
    assert "account gross after 101000.00 vs cap 100000.00" in d.reason
    assert (
        failed(evaluate(intent(spread[4], qty="10"), view, PRICE, OPEN, limits, RiskState())) == []
    )
    # an exit is never blocked by the cap
    sell = intent(spread[0], side="sell", qty="10")
    assert failed(evaluate(sell, view, PRICE, OPEN, limits, RiskState())) == []


def test_sector_concentration_cap():
    a, b = TECH
    view = portfolio(values={a: 28000})
    d = evaluate(intent(b, qty="12"), view, PRICE, OPEN, LIMITS, RiskState())
    assert failed(d) == ["sector_concentration"]  # 28,000 + 3,000 > 30% of equity
    assert SECTORS[a] in d.reason
    assert failed(evaluate(intent(b, qty="8"), view, PRICE, OPEN, LIMITS, RiskState())) == []
    assert failed(evaluate(intent(OTHER, qty="12"), view, PRICE, OPEN, LIMITS, RiskState())) == []


def test_daily_loss_limit_allows_exits_only():
    view = portfolio(equity=97500, values={"AAPL": 5000}, day_start_equity=Decimal(100000))
    d = evaluate(intent("MSFT", qty="4"), view, PRICE, OPEN, LIMITS, RiskState())
    assert failed(d) == ["daily_loss_limit"] and "-2500.00 vs limit -2000.00" in d.reason
    assert (
        failed(
            evaluate(intent("AAPL", side="sell", qty="20"), view, PRICE, OPEN, LIMITS, RiskState())
        )
        == []
    )
    fine = portfolio(equity=98500, day_start_equity=Decimal(100000))
    assert failed(evaluate(intent("MSFT", qty="4"), fine, PRICE, OPEN, LIMITS, RiskState())) == []
    assert size_scale(view, LIMITS, RiskState()) == 0 and size_scale(fine, LIMITS, RiskState()) == 1


def test_drawdown_ladder_halves_then_halts_then_only_exits():
    peak = RiskState(peak_equity=Decimal(100000))
    assert drawdown(Decimal(96000), Decimal(100000)) == Decimal("0.04")
    derisk = portfolio(equity=96000, values={"AAPL": 5000})
    assert size_scale(derisk, LIMITS, peak) == Decimal("0.5")
    big = evaluate(intent("MSFT", qty="8"), derisk, PRICE, OPEN, LIMITS, peak)  # 2,000 > half of 2%
    assert failed(big) == ["drawdown_ladder"] and "half size" in big.reason
    assert failed(evaluate(intent("MSFT", qty="3"), derisk, PRICE, OPEN, LIMITS, peak)) == []
    halted = portfolio(equity=94000, values={"AAPL": 5000})
    assert size_scale(halted, LIMITS, peak) == 0
    d = evaluate(intent("MSFT", qty="1"), halted, PRICE, OPEN, LIMITS, peak)
    assert failed(d) == ["drawdown_ladder"] and "halt" in d.reason
    assert (
        failed(evaluate(intent("AAPL", side="sell", qty="20"), halted, PRICE, OPEN, LIMITS, peak))
        == []
    )
    assert size_scale(portfolio(equity=99000), LIMITS, peak) == 1
    assert size_scale(portfolio(), LIMITS, RiskState(kill_switch=True)) == 0


def test_pattern_day_trader_awareness_below_the_floor():
    state = RiskState(bought_today={"AAPL"})
    small = portfolio(equity=20000, values={"AAPL": 2500}, daytrade_count=3)
    d = evaluate(intent("AAPL", side="sell", qty="10"), small, PRICE, OPEN, LIMITS, state)
    assert failed(d) == ["pattern_day_trader"] and "3 day trades" in d.reason
    fewer = portfolio(equity=20000, values={"AAPL": 2500}, daytrade_count=2)
    assert (
        failed(evaluate(intent("AAPL", side="sell", qty="10"), fewer, PRICE, OPEN, LIMITS, state))
        == []
    )
    large = portfolio(equity=100000, values={"AAPL": 2500}, daytrade_count=3)
    assert (
        failed(evaluate(intent("AAPL", side="sell", qty="10"), large, PRICE, OPEN, LIMITS, state))
        == []
    )
    not_today = evaluate(
        intent("AAPL", side="sell", qty="10"), small, PRICE, OPEN, LIMITS, RiskState()
    )
    assert failed(not_today) == []


def test_shorts_only_in_the_intraday_book_and_under_its_cap():
    swing = evaluate(
        intent("AAPL", side="sell", qty="5"), portfolio(), PRICE, OPEN, LIMITS, RiskState()
    )
    assert failed(swing) == ["short_selling"] and "not allowed in the swing book" in swing.reason
    intraday = evaluate(
        intent("AAPL", side="sell", qty="5", book="intraday"),
        portfolio(),
        PRICE,
        OPEN,
        LIMITS,
        RiskState(),
    )
    assert failed(intraday) == []
    crowded = portfolio(values={"MSFT": -24000}, book_of={"MSFT": "intraday"})
    d = evaluate(
        intent("AAPL", side="sell", qty="8", book="intraday"),
        crowded,
        PRICE,
        OPEN,
        LIMITS,
        RiskState(),
    )
    assert failed(d) == ["short_selling"] and "26000.00 vs cap 25000.00" in d.reason
    # the intraday book never queues an order for the next open
    closed = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
    d = evaluate(
        intent("AAPL", qty="5", book="intraday"), portfolio(), PRICE, closed, LIMITS, RiskState()
    )
    assert "market_hours" in failed(d)


def test_book_exposure_and_position_count_are_per_book():
    view = portfolio(values={"AAPL": 49000}, book_of={"AAPL": "intraday"})
    d = evaluate(intent(OTHER, qty="8", book="intraday"), view, PRICE, OPEN, LIMITS, RiskState())
    assert failed(d) == ["book_exposure"]  # 51,000 > 50% of equity for the intraday book
    assert failed(evaluate(intent(OTHER, qty="8"), view, PRICE, OPEN, LIMITS, RiskState())) == []
    names = list(UNIVERSE)[:10] if len(UNIVERSE) >= 10 else list(UNIVERSE)
    many = RiskLimits(universe=UNIVERSE | {"NEW"}, max_open_positions=len(names))
    full = portfolio(values=dict.fromkeys(names, 2500))
    d = evaluate(intent("NEW", qty="1"), full, PRICE, OPEN, many, RiskState())
    assert "max_positions" in failed(d)
    assert "max_positions" not in failed(
        evaluate(intent(names[0], qty="1"), full, PRICE, OPEN, many, RiskState())
    )


def test_daily_order_cap_counts_per_book():
    state = RiskState(orders_today=20, orders_today_by_book={"swing": 20})
    assert failed(evaluate(intent(), portfolio(), PRICE, OPEN, LIMITS, state)) == [
        "daily_order_cap"
    ]
    assert failed(evaluate(intent(book="intraday"), portfolio(), PRICE, OPEN, LIMITS, state)) == []


async def test_the_officer_engages_the_kill_switch_on_a_deep_drawdown_and_tracks_the_peak():
    clock = SimClock(OPEN)
    bus = InMemoryBus(clock)
    equity = {"value": Decimal(100000)}

    def view() -> PortfolioView:
        return portfolio(equity=equity["value"], values={"AAPL": 5000})

    officer = RiskOfficer(bus, clock, LIMITS, view, lambda s: PRICE, lambda s: timedelta(minutes=1))
    officer.start()
    task = asyncio.create_task(bus.run())
    await bus.publish(
        make_event(
            clock,
            type=Topics.ORDER_INTENT,
            symbol="MSFT",
            payload=intent("MSFT").model_dump(mode="json"),
        )
    )
    await bus.wait_idle()
    assert officer.state.peak_equity == Decimal(100000) and officer.status()["size_scale"] == 1.0
    equity["value"] = Decimal(91000)
    await bus.publish(
        make_event(
            clock,
            type=Topics.ORDER_INTENT,
            symbol="MSFT",
            payload=intent("MSFT").model_dump(mode="json"),
        )
    )
    await bus.wait_idle()
    assert officer.state.kill_switch and "drawdown 9.0%" in officer.state.kill_reason
    kills = [e for e in bus.log if e.type == Topics.RISK_KILL_SWITCH]
    assert kills and kills[0].payload["engaged"] is True
    rejected = [e for e in bus.log if e.type == Topics.RISK_REJECTED]
    assert rejected and "kill_switch" in rejected[-1].payload["reason"]
    assert officer.status()["drawdown"] == 0.09 and officer.status()["size_scale"] == 0.0
    await bus.stop()
    task.cancel()


class FakeDB:
    def __init__(self, approved, peak):
        self.approved, self.peak = approved, peak

    async def recent_events(self, types, since, limit=100, until=None):
        return self.approved if Topics.RISK_APPROVED in types else []

    async def peak_equity(self):
        return self.peak


async def test_restore_brings_back_the_peak_and_the_books_order_counts(clock):
    approved = [
        make_event(
            clock,
            type=Topics.RISK_APPROVED,
            symbol=s,
            book=b,
            payload={
                "intent_id": f"i{s}",
                "intent": {"symbol": s, "side": side, "book": b},
                "decision": "approved",
            },
        )
        for s, side, b in (
            ("AAPL", "buy", "swing"),
            ("MSFT", "sell", "swing"),
            ("JPM", "buy", "intraday"),
        )
    ]
    officer = RiskOfficer(InMemoryBus(clock), clock, LIMITS, portfolio, lambda s: PRICE)
    assert not await officer.restore(FakeDB(approved, Decimal(123456)))
    assert officer.state.orders_today == 3 and officer.state.orders_today_by_book == {
        "swing": 2,
        "intraday": 1,
    }
    assert officer.state.bought_today == {"AAPL", "JPM"} and officer.state.peak_equity == Decimal(
        123456
    )
