import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from desk.agents.swing_strategy import SwingStrategy, evaluate_thesis, price_levels
from desk.bus.memory import InMemoryBus
from desk.core.events import Topics
from desk.core.payloads import OrderIntent, OrderUpdate, ThesisPayload
from tests.conftest import make_event

T0 = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)


def thesis(**kw) -> ThesisPayload:
    base = {
        "thesis_id": "th1",
        "symbol": "AAPL",
        "book": "swing",
        "direction": "long",
        "status": "active",
        "cycle_id": "c1",
        "narrative": "x",
        "expected_return_pct": 5,
        "horizon_days": 3,
        "conviction": 0.6,
        "quantity": Decimal(10),
        "entry_price": Decimal(100),
        "stop_price": Decimal(95),
        "target_price": Decimal(110),
        "stop_pct": 5,
        "target_pct": 10,
        "created_at": T0,
        "updated_at": T0,
    }
    base.update(kw)
    return ThesisPayload(**base)


class FakeDB:
    def __init__(self, theses):
        self.rows = {t.thesis_id: t for t in theses}

    async def open_theses(self):
        return [
            t
            for t in self.rows.values()
            if t.status in ("proposed", "pending_entry", "active", "closing")
        ]

    async def update_thesis(self, thesis_id, now, **fields):
        t = self.rows[thesis_id]
        self.rows[thesis_id] = t.model_copy(update={**fields, "updated_at": now})
        return self.rows[thesis_id]


def test_exit_rules():
    t = thesis()
    assert evaluate_thesis(t, Decimal(100), T0 + timedelta(days=1)) is None
    assert evaluate_thesis(t, Decimal("94.99"), T0 + timedelta(days=1)) == "stop"
    assert evaluate_thesis(t, Decimal(110), T0 + timedelta(days=1)) == "target"
    assert (
        evaluate_thesis(t, Decimal(100), T0 + timedelta(days=5)) == "expired"
    )  # Nov 3, 4, 5 = three sessions
    assert evaluate_thesis(thesis(status="pending_entry", entry_price=None), Decimal(1), T0) is None
    assert price_levels(Decimal(250), 4, 8) == (Decimal("240.00"), Decimal("270.00"))


async def test_stop_hit_closes_the_position_and_fill_closes_the_thesis(clock):
    bus = InMemoryBus(clock)
    db = FakeDB([thesis()])
    strategy = SwingStrategy(bus, clock, db)  # type: ignore[arg-type]
    assert await strategy.load() == 1
    strategy.start()
    task = asyncio.create_task(bus.run())
    bar = {
        "symbol": "AAPL",
        "interval": "1m",
        "ts": clock.now().isoformat(),
        "open": "95",
        "high": "95",
        "low": "94",
        "close": "94.50",
        "volume": 10,
        "source": "t",
    }
    await bus.publish(make_event(clock, type=Topics.MARKET_BAR_1M, symbol="AAPL", payload=bar))
    await bus.wait_idle()
    intents = [e for e in bus.log if e.type == Topics.ORDER_INTENT]
    assert (
        len(intents) == 1
        and intents[0].payload["side"] == "sell"
        and intents[0].payload["quantity"] == "10"
    )
    assert db.rows["th1"].status == "closing" and db.rows["th1"].reason.startswith("stop")
    await bus.publish(
        make_event(clock, type=Topics.MARKET_BAR_1M, symbol="AAPL", payload=bar)
    )  # a second bar does not resend
    await bus.wait_idle()
    assert len([e for e in bus.log if e.type == Topics.ORDER_INTENT]) == 1
    fill = OrderUpdate(
        order_id="o1",
        intent_id=intents[0].payload["intent_id"],
        status="filled",
        filled_quantity=Decimal(10),
        avg_price=Decimal("94.40"),
    )
    await bus.publish(
        make_event(
            clock, type=Topics.ORDER_FILLED, symbol="AAPL", payload=fill.model_dump(mode="json")
        )
    )
    await bus.wait_idle()
    assert db.rows["th1"].status == "closed" and db.rows["th1"].realized_pnl == Decimal("-56.00")
    assert strategy.theses == {} and bus.log[-1].type == Topics.THESIS_CLOSED
    await bus.stop()
    await task


async def test_entry_fill_activates_the_thesis_with_levels(clock):
    bus = InMemoryBus(clock)
    pending = thesis(
        status="pending_entry",
        entry_price=None,
        stop_price=None,
        target_price=None,
        stop_pct=4,
        target_pct=8,
    )
    db = FakeDB([pending])
    strategy = SwingStrategy(bus, clock, db)  # type: ignore[arg-type]
    await strategy.load()
    strategy.start()
    task = asyncio.create_task(bus.run())
    intent = OrderIntent(
        intent_id="i9",
        symbol="AAPL",
        book="swing",
        side="buy",
        quantity=Decimal(10),
        thesis_id="th1",
    )
    await bus.publish(
        make_event(
            clock, type=Topics.ORDER_INTENT, symbol="AAPL", payload=intent.model_dump(mode="json")
        )
    )
    fill = OrderUpdate(
        order_id="i9",
        intent_id="i9",
        status="filled",
        filled_quantity=Decimal(10),
        avg_price=Decimal(250),
    )
    await bus.publish(
        make_event(
            clock, type=Topics.ORDER_FILLED, symbol="AAPL", payload=fill.model_dump(mode="json")
        )
    )
    await bus.wait_idle()
    t = db.rows["th1"]
    assert (
        t.status == "active"
        and t.entry_price == Decimal(250)
        and t.stop_price == Decimal("240.00")
        and t.target_price == Decimal("270.00")
    )
    await bus.stop()
    await task


async def test_reconcile_activates_pending_entries_and_closes_gone_positions(clock):
    bus = InMemoryBus(clock)
    pending = thesis(
        thesis_id="p1",
        symbol="CVX",
        status="pending_entry",
        entry_price=None,
        stop_price=None,
        target_price=None,
        stop_pct=4,
        target_pct=7,
    )
    closing = thesis(thesis_id="c1", symbol="AAPL", status="closing")
    strategy = SwingStrategy(bus, clock, FakeDB([pending, closing]))  # type: ignore[arg-type]
    await strategy.load()
    notes = await strategy.reconcile({"CVX": (Decimal(8), Decimal("226.05625"))})
    assert notes == ["CVX activated at 226.05625", "AAPL closed"]
    active = strategy.theses["p1"]
    assert (
        active.status == "active"
        and active.quantity == 8
        and active.stop_price == Decimal("217.01")
        and active.target_price == Decimal("241.88")
    )
    assert "c1" not in strategy.theses
