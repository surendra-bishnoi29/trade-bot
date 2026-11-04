"""The intraday book: live signals match the evaluator, orders go through the risk officer under the
book's name, fills become positions, exits happen on the stop, the target, VWAP or the close sweep,
and a restart recovers the book's positions from today's fills."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from desk.agents.intraday import (
    IntradayBook,
    Playbook,
    Position,
    SessionTape,
    build_playbook,
    exit_reason,
    live_signal,
)
from desk.bus.memory import InMemoryBus
from desk.core.clock import SimClock
from desk.core.events import Event, Topics
from desk.research.intraday import momentum, opening_range_breakout, session_frame, vwap_reversion
from tests.test_costs_intraday import bars as batch_bars
from tests.test_costs_intraday import ramp

DAY = datetime(2026, 11, 3, 14, 30, tzinfo=UTC)


def tape_of(path: list[float], upto: int) -> SessionTape:
    tape = SessionTape()
    for b in batch_bars(path, wick=0)[:upto]:
        tape.add(b)
    return tape


def test_live_signals_fire_on_the_same_bar_as_the_evaluator():
    # opening range: the evaluator enters at the bar after the break; live, the signal is at that bar's close
    path = [100 + 0.2 * (i % 2) for i in range(15)] + ramp(100.2, 103, 100) + [103] * 100
    batch = opening_range_breakout(session_frame(batch_bars(path, wick=0)), "A", DAY.date())
    assert batch is not None
    first = next(
        i
        for i in range(16, len(path))
        if live_signal("opening_range", tape_of(path, i)) is not None
    )
    assert batch_bars(path, wick=0)[first - 1]["ts"] == batch.entry_ts
    sig = live_signal("opening_range", tape_of(path, first))
    assert sig and sig.side == "long" and sig.stop == pytest.approx(100.1) and sig.target
    assert live_signal("opening_range", tape_of(path, 10)) is None
    # VWAP reversion: live signal one bar before the evaluator's entry (which is the next open)
    path = [100] * 40 + ramp(100, 99, 10) + ramp(99, 100.5, 60) + [100.5] * 150
    batch = vwap_reversion(session_frame(batch_bars(path, wick=0)), "A", DAY.date())
    assert batch is not None
    first = next(
        i
        for i in range(31, len(path))
        if live_signal("vwap_reversion", tape_of(path, i)) is not None
    )
    assert batch_bars(path, wick=0)[first]["ts"] == batch.entry_ts
    sig = live_signal("vwap_reversion", tape_of(path, first))
    assert sig and sig.side == "long" and sig.to_vwap
    # momentum: at the 60th bar
    path = ramp(100, 101.5, 60) + ramp(101.5, 102.5, 200)
    batch = momentum(session_frame(batch_bars(path, wick=0)), "A", DAY.date())
    assert batch is not None and live_signal("momentum", tape_of(path, 59)) is None
    sig = live_signal("momentum", tape_of(path, 60))
    assert sig and sig.side == "long" and batch_bars(path, wick=0)[60]["ts"] == batch.entry_ts
    with pytest.raises(ValueError):
        live_signal("nope", tape_of(path, 60))


def test_exit_reasons():
    p = Position("A", "long", Decimal(10), 99.0, 102.0, False, "", DAY, "i", filled=True)
    assert exit_reason(p, {"high": 101, "low": 99.5, "vwap": 100}) is None
    assert exit_reason(p, {"high": 102.5, "low": 98.5, "vwap": 100}) == "stop"  # the stop wins
    assert exit_reason(p, {"high": 102.5, "low": 100, "vwap": 100}) == "target"
    v = Position("A", "short", Decimal(10), 101.0, None, True, "", DAY, "i", filled=True)
    assert exit_reason(v, {"high": 100.5, "low": 99.9, "vwap": 100}) == "vwap"
    assert exit_reason(v, {"high": 101.2, "low": 100.2, "vwap": 100}) == "stop"


class FakeBackOffice:
    def __init__(self) -> None:
        self.book_of: dict[str, str] = {}
        self.snapshot = type("S", (), {"equity": Decimal(100000)})()

    def price_of(self, symbol: str):
        return Decimal(100)


class FakeRisk:
    state = type("R", (), {"kill_switch": False})()


class FakeDB:
    def __init__(self, fills=None):
        self.fills = fills or []

    async def recent_events(self, types, since, limit=100, until=None, **kw):
        return self.fills if Topics.ORDER_FILLED in types else []

    async def recent_bars(self, symbol, interval, limit=30, before=None):
        base = {"A": 100.0, "B": 50.0, "SPY": 500.0}.get(symbol, 80.0)
        rows = []
        for i in range(21):
            ret = 0.03 if (symbol == "A" and i == 20) else 0.0
            rows.append(
                {
                    "ts": DAY - timedelta(days=21 - i),
                    "open": base,
                    "high": base * 1.01,
                    "low": base * 0.99,
                    "close": base * (1 + ret),
                    "volume": 1_000_000 * (3 if symbol == "A" and i == 20 else 1),
                }
            )
        return rows


def bar_event(
    clock: SimClock, symbol: str, o: float, h: float, lo: float, c: float, at: datetime
) -> Event:
    return Event(
        type=Topics.MARKET_BAR_1M,
        occurred_at=at,
        ingested_at=at,
        source="test@0.1",
        symbol=symbol,
        payload={
            "symbol": symbol,
            "interval": "1m",
            "ts": at.isoformat(),
            "open": o,
            "high": h,
            "low": lo,
            "close": c,
            "volume": 1000,
            "source": "test",
        },
    )


def fill(clock: SimClock, intent: Event, price: str) -> Event:
    p = intent.payload
    return Event(
        type=Topics.ORDER_FILLED,
        occurred_at=clock.now(),
        ingested_at=clock.now(),
        source="test@0.1",
        symbol=intent.symbol,
        book="intraday",
        payload={
            "order_id": p["intent_id"],
            "intent_id": p["intent_id"],
            "side": p["side"],
            "status": "filled",
            "filled_quantity": p["quantity"],
            "avg_price": price,
        },
    )


async def test_playbook_ranks_movers_with_volume_and_news():
    pb = await build_playbook(FakeDB(), ["A", "B", "SPY", "C"], DAY - timedelta(minutes=45), size=2)  # type: ignore[arg-type]
    assert pb.symbols[0] == "A" and "SPY" not in pb.symbols and len(pb.names) == 2
    assert "relative volume 3.0x" in pb.names[0]["why"] and pb.day == DAY.date()


async def test_book_enters_on_a_breakout_tracks_the_fill_and_closes_on_the_target():
    clock = SimClock(DAY)
    bus = InMemoryBus(clock)
    office = FakeBackOffice()
    book = IntradayBook(
        bus,
        clock,
        FakeDB(),
        ["A", "B"],
        office,
        FakeRisk(),
        strategy="opening_range",
        position_pct=Decimal("0.01"),
    )  # type: ignore[arg-type]
    book.playbook = Playbook(day=DAY.date(), names=[{"symbol": "A"}])
    book.start()
    task = asyncio.create_task(bus.run())
    # a 15-minute range between 100 and 100.2, then a break
    for i in range(15):
        at = DAY + timedelta(minutes=i)
        clock.advance_to(at)
        await bus.publish(bar_event(clock, "A", 100, 100.2, 100, 100 + 0.2 * (i % 2), at))
    at = DAY + timedelta(minutes=15)
    clock.advance_to(at)
    await bus.publish(bar_event(clock, "A", 100.2, 100.6, 100.2, 100.5, at))
    await bus.wait_idle()
    intents = [e for e in bus.log if e.type == Topics.ORDER_INTENT]
    assert (
        len(intents) == 1 and intents[0].book == "intraday" and intents[0].payload["side"] == "buy"
    )
    assert Decimal(intents[0].payload["quantity"]) == Decimal(
        9
    )  # 1% of 100,000 at 100.5, whole shares
    assert "A" in book.positions and not book.positions["A"].filled
    await bus.publish(fill(clock, intents[0], "100.52"))
    await bus.wait_idle()
    assert book.positions["A"].filled and office.book_of == {"A": "intraday"}
    # the same name is not traded twice today even on another break
    at = DAY + timedelta(minutes=16)
    clock.advance_to(at)
    await bus.publish(bar_event(clock, "A", 100.5, 100.69, 100.4, 100.6, at))
    await bus.wait_idle()
    assert len([e for e in bus.log if e.type == Topics.ORDER_INTENT]) == 1
    # the target (entry + range width = 100.5 + 0.2) is touched
    at = DAY + timedelta(minutes=17)
    clock.advance_to(at)
    await bus.publish(bar_event(clock, "A", 100.6, 100.75, 100.55, 100.7, at))
    await bus.wait_idle()
    intents = [e for e in bus.log if e.type == Topics.ORDER_INTENT]
    assert (
        len(intents) == 2
        and intents[1].payload["side"] == "sell"
        and book.positions["A"].exit_reason == "target"
    )
    await bus.publish(fill(clock, intents[1], "100.70"))
    await bus.wait_idle()
    trades = [e for e in bus.log if e.type == Topics.INTRADAY_TRADE]
    assert len(trades) == 1 and Decimal(trades[0].payload["realized_pnl"]) == pytest.approx(
        Decimal("1.62")
    )
    assert (
        trades[0].payload["reason"] == "target"
        and "A" not in book.positions
        and office.book_of == {}
    )
    await bus.stop()
    task.cancel()


async def test_kill_switch_blocks_entries_the_sweep_flattens_and_restart_recovers():
    clock = SimClock(DAY)
    bus = InMemoryBus(clock)
    office = FakeBackOffice()
    risk = FakeRisk()
    book = IntradayBook(bus, clock, FakeDB(), ["A"], office, risk, strategy="momentum")  # type: ignore[arg-type]
    book.playbook = Playbook(day=DAY.date(), names=[{"symbol": "A"}])
    book.start()
    task = asyncio.create_task(bus.run())
    risk.state.kill_switch = True
    for i in range(60):
        at = DAY + timedelta(minutes=i)
        clock.advance_to(at)
        px = 100 + i * 0.03
        await bus.publish(bar_event(clock, "A", px, px + 0.05, px - 0.01, px + 0.03, at))
    await bus.wait_idle()
    assert not [e for e in bus.log if e.type == Topics.ORDER_INTENT]  # kill switch on: no entries
    risk.state.kill_switch = False
    # a position opened by hand (as if filled) is swept before the close
    book.positions["A"] = Position(
        "A",
        "long",
        Decimal(5),
        99.0,
        None,
        False,
        "",
        clock.now(),
        "i1",
        entry_price=Decimal(100),
        filled=True,
    )
    assert await book.flatten("flat before the close") == 1
    await bus.wait_idle()
    sweep = [e for e in bus.log if e.type == Topics.ORDER_INTENT]
    assert (
        len(sweep) == 1
        and sweep[0].payload["side"] == "sell"
        and book.positions["A"].exit_reason == "flat before the close"
    )
    # restart during the session: today's fills under the intraday book rebuild the position
    buy = Event(
        type=Topics.ORDER_FILLED,
        occurred_at=clock.now(),
        ingested_at=clock.now(),
        source="t",
        symbol="B",
        book="intraday",
        payload={
            "order_id": "x",
            "intent_id": "x",
            "side": "sell",
            "status": "filled",
            "filled_quantity": "7",
            "avg_price": "50",
        },
    )
    fresh = IntradayBook(bus, clock, FakeDB([buy]), ["B"], office, risk)  # type: ignore[arg-type]
    assert (
        await fresh.restore() == 1
        and fresh.positions["B"].side == "short"
        and fresh.positions["B"].quantity == 7
    )
    assert office.book_of.get("B") == "intraday"
    assert fresh.flat_time(DAY.date()) == datetime(2026, 11, 3, 20, 50, tzinfo=UTC)
    assert fresh.playbook_time(DAY.date()) == datetime(2026, 11, 3, 13, 45, tzinfo=UTC)
    await bus.stop()
    task.cancel()
