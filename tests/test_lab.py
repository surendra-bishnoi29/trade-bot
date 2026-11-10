"""The lab desk: lines that ratchet and exit, scored predictions, the sentinel's triggers, and the
predictor acting on a scripted model."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from desk.agents.risk_officer import BookLimits, RiskLimits
from desk.bus.memory import InMemoryBus
from desk.core.clock import SimClock
from desk.core.events import Event, Topics
from desk.core.payloads import Call, SentinelTrigger
from desk.funnel.markets import Market
from desk.lab.lines import Line, LinesAgent
from desk.lab.predictor import Objection, Predictor, record_of, render_record
from desk.lab.sentinel import Sentinel, changed

T0 = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)


def ev(
    type: str,
    when: datetime,
    payload: dict,
    symbol: str | None = None,
    book: str | None = None,
    source: str = "test@0.1",
) -> Event:
    return Event(
        type=type,
        occurred_at=when,
        ingested_at=when,
        source=source,
        symbol=symbol,
        book=book,
        payload=payload,
    )


def bar(symbol: str, when: datetime, high: float, low: float, close: float | None = None) -> Event:
    c = close if close is not None else (high + low) / 2
    return ev(
        Topics.MARKET_BAR_1M,
        when,
        {
            "symbol": symbol,
            "interval": "1m",
            "ts": when.isoformat(),
            "open": c,
            "high": high,
            "low": low,
            "close": c,
            "volume": 100,
            "source": "test",
        },
        symbol,
    )


def test_long_lines_step_up_then_exit_on_the_line():
    # steps of 0.5%: one step up moves the max-loss line to the base, each further step raises
    # the min-profit line, and the position closes when the price comes back to it
    line = Line("c1", "AAPL", "stocks", "long", 0.7, Decimal(10), 1.0, 1.0, 0.5, 240)
    line.open(100.0, T0)
    assert (line.stop, line.profit_line) == (99.0, pytest.approx(100.5)) and not line.armed
    assert line.next_step() == pytest.approx(100.5)
    assert line.watch(100.4, 100.2) == [] and line.exit_level() == 99.0
    assert line.watch(100.6, 100.3) == ["base"] and line.armed and line.stop == 100.0
    assert line.profit_line == 100.0 and line.exit_level() == 100.0  # cannot lose from here
    assert line.next_step() == pytest.approx(101.0)
    assert line.watch(100.9, 100.1) == []  # not back to the base, not yet the next step
    assert (
        line.watch(101.6, 100.9) == ["ratchet", "ratchet"]
        and line.steps == 2
        and line.profit_line == pytest.approx(101.0)
    )  # cleared 101.0 and 101.5: the line sits one step below the best price
    assert line.exit_level() == pytest.approx(101.0) and line.next_step() == pytest.approx(102.0)
    assert line.watch(101.8, 101.2) == []
    assert (
        line.watch(101.4, 100.95) == ["exit"] and line.exit_reason == "line"
    )  # back to the latest line


def test_long_lines_stop_and_base_exits_and_short_mirror():
    line = Line("c1", "AAPL", "stocks", "long", 0.7, Decimal(10), 1.0, 1.0, 0.5, 240)
    line.open(100.0, T0)
    assert line.watch(100.4, 98.9) == ["exit"] and line.exit_reason == "stop"
    line = Line("c2", "AAPL", "stocks", "long", 0.7, Decimal(10), 1.0, 1.0, 0.5, 240)
    line.open(100.0, T0)
    assert line.watch(100.7, 100.1) == ["base"]
    assert line.watch(100.6, 99.95) == ["exit"] and line.exit_reason == "base"
    short = Line("c3", "XOM", "commodities", "short", 0.6, Decimal(10), 1.0, 1.0, 0.5, 240)
    short.open(100.0, T0)
    assert (short.stop, short.profit_line) == (101.0, pytest.approx(99.5))
    assert (
        short.watch(99.8, 99.4) == ["base"] and short.stop == 100.0 and short.exit_level() == 100.0
    )
    assert short.watch(99.3, 98.4) == ["ratchet", "ratchet"] and short.profit_line == pytest.approx(
        99.0
    )
    assert short.exit_level() == pytest.approx(99.0)
    assert short.watch(99.05, 98.7) == ["exit"] and short.exit_reason == "line"
    bad = Line("c4", "XOM", "commodities", "short", 0.6, Decimal(10), 1.0, 1.0, 0.5, 240)
    bad.open(100.0, T0)
    assert bad.watch(101.2, 100.4) == ["exit"] and bad.exit_reason == "stop"


def test_the_step_is_held_inside_the_books_bounds():
    agent = LinesAgent(None, None, None, None, None, step_bounds=(0.15, 0.3))  # type: ignore[arg-type]
    assert agent.step_for("stocks", 0.5) == 0.3 and agent.step_for("stocks", 0.05) == 0.15
    assert agent.step_for("commodities", 0.2) == 0.2
    assert agent.step_for("crypto", 0.5) == 0.75 and agent.step_for("crypto", 2.0) == 1.5


class FakeTrading:
    def __init__(self) -> None:
        self.stops: dict[str, Decimal] = {}
        self.canceled: list[str] = []

    async def stop_leg(self, parent_order_id):
        return SimpleNamespace(broker_order_id=f"stop-{parent_order_id}")

    async def replace_stop(self, order_id, stop_price):
        self.stops[order_id] = stop_price
        return SimpleNamespace(broker_order_id=order_id)

    async def cancel_order(self, order_id):
        self.canceled.append(order_id)

    async def get_order(self, order_id):
        return SimpleNamespace(filled_avg_price=None)


class FakeDB:
    def __init__(self, events=None):
        self.events = events or []

    async def recent_events(self, types, since, *, symbol=None, limit=50, until=None):
        rows = [
            e for e in self.events if e.type in types and (symbol is None or e.symbol == symbol)
        ]
        return sorted(rows, key=lambda e: e.occurred_at, reverse=True)[:limit]


def call_event(
    when: datetime, symbol="AAPL", market="stocks", direction="long", acted=True
) -> Event:
    return ev(
        Topics.PREDICTION_CALL,
        when,
        {
            "call_id": "call-1",
            "symbol": symbol,
            "market": market,
            "trigger": "surge",
            "call": {
                "direction": direction,
                "confidence": 0.7,
                "horizon_minutes": 240,
                "max_loss_pct": 1.0,
                "first_profit_pct": 1.0,
                "step_pct": 0.5,
                "reason": "test",
                "evidence_ids": [],
            },
            "price": 100.0,
            "acted": acted,
        },
        symbol,
        "swing",
    )


def intent_event(
    when: datetime, intent_id="i1", side="buy", qty="10", call_id="call-1", symbol="AAPL"
) -> Event:
    return ev(
        Topics.ORDER_INTENT,
        when,
        {
            "intent_id": intent_id,
            "symbol": symbol,
            "book": "swing",
            "side": side,
            "quantity": qty,
            "stop_price": "99",
            "call_id": call_id,
        },
        symbol,
        "swing",
    )


def fill_event(when: datetime, intent_id: str, price: str, qty="10", symbol="AAPL") -> Event:
    return ev(
        Topics.ORDER_FILLED,
        when,
        {
            "order_id": intent_id,
            "intent_id": intent_id,
            "broker_order_id": f"b-{intent_id}",
            "status": "filled",
            "filled_quantity": qty,
            "avg_price": price,
        },
        symbol,
        "swing",
    )


async def test_lines_agent_opens_on_the_fill_ratchets_the_broker_stop_and_scores_the_exit():
    clock = SimClock(T0)
    bus = InMemoryBus(clock)
    trading = FakeTrading()
    office = SimpleNamespace(book_of={})
    agent = LinesAgent(bus, clock, FakeDB(), trading, office, step_bounds=(0.5, 0.5))
    agent.start()
    task = asyncio.create_task(bus.run())
    await bus.publish(call_event(T0))
    await bus.publish(intent_event(T0))
    await bus.wait_idle()
    assert "AAPL" in agent.pending_symbols()
    await bus.publish(fill_event(T0 + timedelta(seconds=5), "i1", "100.00"))
    await bus.wait_idle()
    line = agent.lines["AAPL"]
    assert line.filled and line.base == 100.0 and office.book_of == {"AAPL": "swing"}
    assert trading.stops["stop-b-i1"] == Decimal("99.00")  # the broker holds the maximum-loss line
    # steps of 0.5%: 100.6 arms (max loss to the base), 101.2 clears 101.0 (line to 100.5),
    # 102.2 clears 101.5 and 102.0 (line to 101.5)
    for i, (h, lo) in enumerate([(100.6, 100.1), (101.2, 100.8), (102.2, 101.6)], start=1):
        clock.advance_to(T0 + timedelta(minutes=i))
        await bus.publish(bar("AAPL", clock.now(), h, lo))
    await bus.wait_idle()
    assert line.armed and line.steps == 3 and trading.stops["stop-b-i1"] == Decimal("101.50")
    kinds = [e.payload["event"] for e in bus.log if e.type == Topics.LINES_UPDATED]
    assert kinds == ["opened", "base", "ratchet", "ratchet", "ratchet"]
    clock.advance_to(T0 + timedelta(minutes=4))
    await bus.publish(bar("AAPL", clock.now(), 102.0, 101.4))  # back to the line: exit
    await bus.wait_idle()
    exits = [
        e for e in bus.log if e.type == Topics.ORDER_INTENT and e.payload.get("side") == "sell"
    ]
    assert (
        len(exits) == 1
        and exits[0].payload["call_id"] == "call-1"
        and trading.canceled == ["stop-b-i1"]
    )
    await bus.publish(fill_event(clock.now(), exits[0].payload["intent_id"], "102.00"))
    await bus.wait_idle()
    scores = [e for e in bus.log if e.type == Topics.PREDICTION_SCORE]
    assert (
        len(scores) == 1
        and scores[0].payload["hit"] is True
        and scores[0].payload["reason"] == "line"
    )
    assert scores[0].payload["realized_pct"] == pytest.approx(2.0 - 0.1, abs=0.01)
    assert "AAPL" not in agent.lines and office.book_of == {}
    await bus.stop()
    task.cancel()


async def test_lines_agent_restores_from_the_journal_and_closes_on_a_broker_stop():
    clock = SimClock(T0)
    bus = InMemoryBus(clock)
    journal = FakeDB(
        [
            call_event(T0 - timedelta(hours=1)),
            ev(
                Topics.LINES_UPDATED,
                T0 - timedelta(minutes=30),
                {
                    "symbol": "AAPL",
                    "market": "stocks",
                    "call_id": "call-1",
                    "direction": "long",
                    "base": 100.0,
                    "stop": 100.0,
                    "profit_line": 101.5,
                    "step_pct": 0.5,
                    "armed": True,
                    "steps": 1,
                    "event": "ratchet",
                    "last": 101.7,
                    "quantity": 10.0,
                    "stop_order_id": "stop-x",
                    "opened_at": (T0 - timedelta(hours=1)).isoformat(),
                },
                "AAPL",
                "swing",
            ),
        ]
    )
    agent = LinesAgent(bus, clock, journal, FakeTrading(), SimpleNamespace(book_of={}))
    assert await agent.restore() == 1
    line = agent.lines["AAPL"]
    assert (
        line.armed
        and line.profit_line == 101.5
        and line.quantity == Decimal(10)
        and line.stop_order_id == "stop-x"
    )
    agent.start()
    task = asyncio.create_task(bus.run())
    # the broker's stop fired while we were away: the snapshot shows no position
    await bus.publish(ev(Topics.PORTFOLIO_RECONCILED, T0, {"equity": "100000", "positions": []}))
    await bus.wait_idle()
    scores = [e for e in bus.log if e.type == Topics.PREDICTION_SCORE]
    assert (
        len(scores) == 1 and scores[0].payload["reason"] == "broker" and "AAPL" not in agent.lines
    )
    await bus.stop()
    task.cancel()


def row(
    symbol="AAPL", side="bull", strength=30.0, alerts=0, tone="neutral", rank=5, setup="MOMENTUM"
):
    return {
        "symbol": symbol,
        "side": side,
        "strength": strength,
        "score": strength if side == "bull" else -strength,
        "alerts_total": alerts,
        "rank": rank,
        "setup": setup,
        "last": 100.0,
        "evidence": [
            {
                "label": "News tone, 1 article in 24 hours",
                "value": "0",
                "verdict": tone,
                "source": "News analyst",
            }
        ],
    }


def test_sentinel_sees_what_changed():
    assert changed(None, row(strength=10), False) is None
    assert changed(None, row(strength=40), False) == ("first look", "bull 40")
    assert changed(row(side="bear"), row(side="bull", strength=30), False)[0] == "side flip"
    assert changed(row(strength=40), row(strength=55), False)[0] == "surge"
    assert changed(row(alerts=1), row(alerts=3), False)[0] == "unusual activity"
    assert changed(row(tone="neutral"), row(tone="bullish"), False) == (
        "news tone",
        "news turned bullish",
    )
    assert changed(row(rank=2), row(rank=2), True)[0] == "market turned"
    assert changed(row(rank=9), row(rank=9), True) is None
    assert changed(row(), row(), False) is None


async def test_sentinel_triggers_once_then_waits(monkeypatch):
    clock = SimClock(T0)
    bus = InMemoryBus(clock)
    market = Market("stocks", "US stocks", ("AAPL", "MSFT"), "SPY", "nyse", True)
    state = {"strength": 40.0}

    async def fake_build(db, m, now, **kw):
        return {
            "sentiment": {"verdict": "MIXED"},
            "symbols": [
                row("AAPL", strength=state["strength"], rank=1),
                row("MSFT", strength=10, rank=2),
            ],
        }

    monkeypatch.setattr("desk.lab.sentinel.build_funnel", fake_build)
    s = Sentinel(bus, clock, FakeDB(), [market], quiet_minutes=10, review_minutes=45)
    s.start()
    task = asyncio.create_task(bus.run())
    assert await s.run_once() == 1  # AAPL at 40 is worth a first look; MSFT at 10 is not
    assert await s.run_once() == 0  # nothing changed
    state["strength"] = 60.0
    clock.advance_to(T0 + timedelta(minutes=5))
    assert await s.run_once() == 0  # a surge, but inside the quiet period
    clock.advance_to(T0 + timedelta(minutes=11))
    assert await s.run_once() == 1
    clock.advance_to(T0 + timedelta(minutes=60))
    assert await s.run_once() == 1  # the periodic review
    s.has_position = lambda sym: sym == "AAPL"
    clock.advance_to(T0 + timedelta(minutes=120))
    assert await s.run_once() == 0  # a name with a position is the lines engine's business
    clock.advance_to(T0 + timedelta(hours=7))
    assert await s.run_once() == 0  # the market is closed
    await bus.wait_idle()
    trig = [e for e in bus.log if e.type == Topics.SENTINEL_TRIGGER]
    assert [e.payload["reason"] for e in trig] == ["first look", "surge", "review"]
    await bus.stop()
    task.cancel()


class ScriptedClient:
    """Answers the predictor's two questions from a script instead of the model."""

    def __init__(self, call: Call, objection: Objection) -> None:
        self.answers = {"Call": call, "Objection": objection}
        self.prompts: list[str] = []
        self.messages = SimpleNamespace(parse=self.parse)

    async def parse(self, **kw):
        self.prompts.append(kw["messages"][0]["content"])
        out = self.answers[kw["output_format"].__name__]
        return SimpleNamespace(
            parsed_output=out,
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )


async def test_predictor_acts_on_a_confident_call_and_records_every_call():
    clock = SimClock(T0)
    bus = InMemoryBus(clock)
    markets = {"stocks": Market("stocks", "US stocks", ("AAPL",), "SPY", "nyse", True)}
    sentinel = SimpleNamespace(
        funnels={
            "stocks": {
                "sentiment": {"verdict": "MIXED", "score": 3, "evidence": []},
                "symbols": [
                    {
                        **row("AAPL", strength=60),
                        "book": {"tag": "SWING CANDIDATE", "reasons": ["trend"]},
                        "levels": {
                            "s1": 99,
                            "r1": 101,
                            "s2": 95,
                            "r2": 105,
                            "vwap": 100.1,
                            "prior_close": 99.5,
                        },
                        "day_pct": 0.5,
                        "vs_avg20_pct": 2.0,
                    }
                ],
            }
        }
    )
    office = SimpleNamespace(snapshot=SimpleNamespace(equity=Decimal(100000)), book_of={})
    risk = SimpleNamespace(limits=RiskLimits(universe=frozenset({"AAPL"}), allow_short=True))
    good = Call(
        direction="long",
        confidence=0.72,
        horizon_minutes=240,
        max_loss_pct=1.0,
        first_profit_pct=1.0,
        step_pct=0.5,
        reason="trend and news agree",
        evidence_ids=[],
    )
    client = ScriptedClient(
        good, Objection(severity="none", argument="checked the trend, the news and the levels")
    )

    async def fake_detail(db, market, symbol, now, **kw):
        return {
            "symbol": symbol,
            "candles": [],
            "trail": [
                {"at": T0.isoformat(), "agent": "Watcher", "type": "x", "text": "volume spike"}
            ],
        }

    import desk.lab.predictor as mod

    mod.symbol_detail = fake_detail  # type: ignore[assignment]
    p = Predictor(
        bus, clock, FakeDB(), markets, sentinel, office, risk, client=client, min_confidence=0.6
    )  # type: ignore[arg-type]
    p.start()
    task = asyncio.create_task(bus.run())
    trigger = SentinelTrigger(
        symbol="AAPL",
        market="stocks",
        reason="surge",
        detail="40 to 60",
        score=60,
        side="bull",
        strength=60,
        setup="MOMENTUM",
        last=100.0,
    )
    await bus.publish(ev(Topics.SENTINEL_TRIGGER, T0, trigger.model_dump(mode="json"), "AAPL"))
    await bus.wait_idle()
    calls = [e for e in bus.log if e.type == Topics.PREDICTION_CALL]
    intents = [e for e in bus.log if e.type == Topics.ORDER_INTENT]
    assert (
        len(calls) == 1
        and calls[0].payload["acted"]
        and calls[0].payload["objection"].startswith("none")
    )
    assert (
        len(intents) == 1
        and intents[0].payload["side"] == "buy"
        and intents[0].payload["quantity"] == "10"
    )  # 1% of 100,000 at 100
    assert (
        intents[0].payload["stop_price"] == "99.0"
        and intents[0].payload["call_id"] == calls[0].payload["call_id"]
    )
    assert (
        "TRACK RECORD on AAPL: no scored calls yet" in client.prompts[0]
        and "THE CALL" in client.prompts[1]
    )
    assert "AAPL" in p.open_symbols
    # a second trigger while the position is open is ignored; a blocked call is recorded, not acted
    await bus.publish(
        ev(
            Topics.SENTINEL_TRIGGER,
            T0 + timedelta(minutes=1),
            trigger.model_dump(mode="json"),
            "AAPL",
        )
    )
    await bus.wait_idle()
    assert len([e for e in bus.log if e.type == Topics.PREDICTION_CALL]) == 1
    p.released("AAPL")
    client.answers["Objection"] = Objection(
        severity="blocking", argument="the evidence id does not exist"
    )
    await bus.publish(
        ev(
            Topics.SENTINEL_TRIGGER,
            T0 + timedelta(minutes=2),
            trigger.model_dump(mode="json"),
            "AAPL",
        )
    )
    await bus.wait_idle()
    calls = [e for e in bus.log if e.type == Topics.PREDICTION_CALL]
    assert (
        len(calls) == 2
        and not calls[1].payload["acted"]
        and calls[1].payload["skipped_because"] == "blocked by the skeptic"
    )
    assert len([e for e in bus.log if e.type == Topics.ORDER_INTENT]) == 1
    # a short where the book forbids it, and a line that cannot pay its costs, are skipped without the skeptic
    client.answers["Call"] = Call(
        direction="short",
        confidence=0.8,
        horizon_minutes=60,
        max_loss_pct=1.0,
        first_profit_pct=1.0,
        step_pct=0.5,
        reason="x",
    )
    p.risk = SimpleNamespace(limits=RiskLimits(universe=frozenset({"AAPL"}), allow_short=False))
    await bus.publish(
        ev(
            Topics.SENTINEL_TRIGGER,
            T0 + timedelta(minutes=3),
            trigger.model_dump(mode="json"),
            "AAPL",
        )
    )
    await bus.wait_idle()
    assert [e for e in bus.log if e.type == Topics.PREDICTION_CALL][-1].payload[
        "skipped_because"
    ] == "shorts are not allowed in this book"
    client.answers["Call"] = Call(
        direction="long",
        confidence=0.8,
        horizon_minutes=60,
        max_loss_pct=1.0,
        first_profit_pct=0.2,
        step_pct=0.5,
        reason="x",
    )
    await bus.publish(
        ev(
            Topics.SENTINEL_TRIGGER,
            T0 + timedelta(minutes=4),
            trigger.model_dump(mode="json"),
            "AAPL",
        )
    )
    await bus.wait_idle()
    assert (
        "cannot pay the costs"
        in [e for e in bus.log if e.type == Topics.PREDICTION_CALL][-1].payload["skipped_because"]
    )
    await bus.stop()
    task.cancel()


async def test_record_of_summarizes_scores():
    scores = [
        ev(
            Topics.PREDICTION_SCORE,
            T0 - timedelta(days=i),
            {
                "call_id": f"c{i}",
                "symbol": "AAPL",
                "market": "stocks",
                "direction": "long",
                "confidence": 0.7,
                "hit": i % 2 == 0,
                "realized_pct": 1.0 if i % 2 == 0 else -0.8,
                "reason": "line",
                "closed_at": (T0 - timedelta(days=i)).isoformat(),
            },
            "AAPL",
        )
        for i in range(4)
    ] + [
        ev(
            Topics.PREDICTION_SCORE,
            T0,
            {
                "call_id": "u",
                "symbol": "AAPL",
                "market": "stocks",
                "direction": "long",
                "confidence": 0.7,
                "reason": "unfilled",
                "closed_at": T0.isoformat(),
            },
            "AAPL",
        )
    ]
    r = await record_of(FakeDB(scores), "AAPL", T0 + timedelta(hours=1))  # type: ignore[arg-type]
    assert r == {"n": 4, "hit": 0.5, "avg_pct": 0.1, "unfilled": 1}
    assert "4 scored calls, 50% reached" in render_record(r)
    assert render_record({"n": 0}) == "no scored calls yet on this instrument"


def test_lab_books_in_the_risk_limits():
    settings = SimpleNamespace(
        lab_enabled=True,
        max_open_positions=10,
        position_pct=Decimal("0.02"),
        lab_position_pct=Decimal("0.01"),
        intraday_max_gross_pct=Decimal("0.5"),
        intraday_allow_short=True,
        intraday_max_short_pct=Decimal("0.25"),
        max_gross_pct=Decimal(1),
        max_sector_pct=Decimal("0.3"),
        max_daily_loss_pct=Decimal("0.02"),
        drawdown_derisk_pct=Decimal("0.03"),
        drawdown_halt_pct=Decimal("0.05"),
        drawdown_kill_pct=Decimal("0.08"),
        commodities_max_gross_pct=Decimal("0.10"),
        commodities_allow_short=True,
        crypto_max_gross_pct=Decimal("0.05"),
    )
    limits = RiskLimits.from_settings(settings, ["AAPL", "GLD", "BTC/USD"])
    assert limits.allow_short and limits.for_book("swing").max_short_pct == Decimal("0.25")
    assert limits.for_book("commodities").allow_short and limits.for_book(
        "commodities"
    ).max_gross_pct == Decimal("0.10")
    crypto = limits.for_book("crypto")
    assert isinstance(crypto, BookLimits) and crypto.always_open and not crypto.allow_short


async def test_sim_filler_fills_at_the_next_bar_and_charges_crypto_only():
    from desk.lab.sim import SimFiller
    from desk.replay.broker import SimulatedBroker

    clock = SimClock(T0)
    bus = InMemoryBus(clock)
    broker = SimulatedBroker(
        clock, Decimal(100000), slippage_bps=Decimal(0), crypto_fee_pct=Decimal("0.25")
    )
    filler = SimFiller(bus, clock, broker)
    filler.start()
    task = asyncio.create_task(bus.run())
    stock = await broker.submit_market_order("AAPL", Decimal(10), "buy", "a" * 26)
    coin = await broker.submit_market_order("BTC/USD", Decimal("0.01"), "buy", "b" * 26)
    clock.advance_to(T0 + timedelta(minutes=1))
    await bus.publish(bar("AAPL", T0 + timedelta(minutes=1), 101, 99, 100))
    await bus.publish(bar("BTC/USD", T0 + timedelta(minutes=1), 101000, 99000, 100000))
    await bus.wait_idle()
    assert (await broker.get_order(stock.broker_order_id)).status == "filled"
    assert (await broker.get_order(coin.broker_order_id)).status == "filled"
    by_symbol = {f.symbol: f for f in broker.fills}
    assert by_symbol["AAPL"].cost == Decimal("0.00")
    assert by_symbol["BTC/USD"].cost == Decimal("2.50")  # 0.25% of 1,000
    task.cancel()


async def test_polled_bars_deliver_each_completed_bar_once_in_order():
    from desk.core.payloads import MarketBar
    from desk.data.watch import PolledBars

    def mb(symbol: str, minute: int, close: float) -> MarketBar:
        return MarketBar(
            symbol=symbol,
            interval="1m",
            ts=T0 + timedelta(minutes=minute),
            open=Decimal(str(close)),
            high=Decimal(str(close)),
            low=Decimal(str(close)),
            close=Decimal(str(close)),
            volume=100,
            source="test",
        )

    class FakeData:
        def __init__(self) -> None:
            self.rows: list[MarketBar] = []
            self.starts: list[datetime] = []

        async def bars(self, symbols, interval, start, end=None):
            self.starts.append(start)
            return [b for b in self.rows if b.ts >= start and b.symbol in symbols]

    clock = SimClock(T0 + timedelta(minutes=2, seconds=5))
    data = FakeData()
    open_now = {"v": True}
    feed = PolledBars(data, clock, is_open=lambda at: open_now["v"])
    # minutes 0 and 1 are complete, minute 2 is still forming
    data.rows = [mb("MSFT", 1, 2), mb("AAPL", 0, 1), mb("AAPL", 1, 1.1), mb("AAPL", 2, 1.2)]
    first = await feed.poll(["AAPL", "MSFT"])
    assert [(b.symbol, b.ts.minute) for b in first] == [("AAPL", 0), ("AAPL", 1), ("MSFT", 1)]
    assert await feed.poll(["AAPL", "MSFT"]) == []  # nothing new: nothing twice
    clock.advance_to(T0 + timedelta(minutes=3, seconds=5))
    data.rows.append(mb("MSFT", 2, 2.1))
    second = await feed.poll(["AAPL", "MSFT"])
    assert [(b.symbol, b.ts.minute) for b in second] == [("AAPL", 2), ("MSFT", 2)]
    assert data.starts[-1] == T0 + timedelta(minutes=1) - timedelta(
        minutes=2
    )  # a small overlap only
    open_now["v"] = False
    data.rows.append(mb("AAPL", 3, 1.3))
    clock.advance_to(T0 + timedelta(minutes=5))
    assert await feed.poll(["AAPL", "MSFT"]) == []  # closed: no calls, no bars
    assert len(data.starts) == 3


# ---- the fixes of 2026-11-09: refusals, the bell, the broker's stop, reconciliation ----


async def _open_line(agent, bus, when=T0, price="100.00"):
    await bus.publish(call_event(when))
    await bus.publish(intent_event(when))
    await bus.wait_idle()
    await bus.publish(fill_event(when + timedelta(seconds=5), "i1", price))
    await bus.wait_idle()
    return agent.lines["AAPL"]


def risk_rejected(when: datetime, intent_id: str, reason="duplicate_order: working") -> Event:
    return ev(
        Topics.RISK_REJECTED,
        when,
        {"intent_id": intent_id, "decision": "rejected", "reason": reason, "rules": []},
        "AAPL",
        "swing",
    )


async def test_an_entry_the_risk_officer_refuses_frees_the_instrument():
    clock = SimClock(T0)
    bus = InMemoryBus(clock)
    agent = LinesAgent(bus, clock, FakeDB(), FakeTrading(), SimpleNamespace(book_of={}))
    agent.start()
    task = asyncio.create_task(bus.run())
    await bus.publish(call_event(T0))
    await bus.publish(intent_event(T0))
    await bus.wait_idle()
    assert agent.pending_symbols() == {"AAPL"}
    await bus.publish(risk_rejected(T0, "i1", "price_fresh: 20 min old"))
    await bus.wait_idle()
    assert agent.pending_symbols() == set() and not agent.lines
    scores = [e for e in bus.log if e.type == Topics.PREDICTION_SCORE]
    assert len(scores) == 1 and scores[0].payload["reason"] == "risk"
    assert scores[0].payload["hit"] is None  # never opened: it does not count for or against
    task.cancel()


async def test_a_refused_exit_stays_decided_and_is_sent_again_after_a_wait():
    clock = SimClock(T0)
    bus = InMemoryBus(clock)
    agent = LinesAgent(bus, clock, FakeDB(), FakeTrading(), SimpleNamespace(book_of={}))
    agent.start()
    task = asyncio.create_task(bus.run())
    line = await _open_line(agent, bus)
    clock.advance_to(T0 + timedelta(minutes=1))
    await bus.publish(bar("AAPL", clock.now(), 99.5, 98.9))  # through the stop at 99
    await bus.wait_idle()
    exits = [e for e in bus.log if e.type == Topics.ORDER_INTENT and e.payload["side"] == "sell"]
    assert len(exits) == 1 and line.exit_reason == "stop"
    await bus.publish(risk_rejected(clock.now(), exits[0].payload["intent_id"]))
    await bus.wait_idle()
    assert line.exit_intent == "" and line.exit_tries == 1 and "AAPL" in agent.lines
    # a bar back above the stop does not cancel the decision, and nothing is sent inside the wait
    clock.advance_to(T0 + timedelta(minutes=1, seconds=10))
    await bus.publish(bar("AAPL", clock.now(), 100.4, 100.1))
    await bus.wait_idle()
    assert await agent.tick() == 0
    clock.advance_to(T0 + timedelta(minutes=1, seconds=25))
    assert await agent.tick() == 1
    await bus.wait_idle()
    exits = [e for e in bus.log if e.type == Topics.ORDER_INTENT and e.payload["side"] == "sell"]
    assert len(exits) == 2 and line.exit_intent == exits[1].payload["intent_id"]
    await bus.publish(fill_event(clock.now(), exits[1].payload["intent_id"], "99.80"))
    await bus.wait_idle()
    score = next(e for e in bus.log if e.type == Topics.PREDICTION_SCORE)
    assert score.payload["reason"] == "stop" and "AAPL" not in agent.lines
    task.cancel()


async def test_stocks_are_closed_before_the_bell_and_crypto_is_not():
    # Friday 2026-11-06: New York closes at 21:00 UTC
    start = datetime(2026, 11, 6, 20, 0, tzinfo=UTC)
    clock = SimClock(start)
    bus = InMemoryBus(clock)
    agent = LinesAgent(
        bus, clock, FakeDB(), FakeTrading(), SimpleNamespace(book_of={}), flat_minutes=10
    )
    agent.start()
    task = asyncio.create_task(bus.run())
    line = await _open_line(agent, bus, when=start)
    coin = Line("c2", "BTC/USD", "crypto", "long", 0.7, Decimal("0.01"), 1.0, 1.0, 0.5, 600)
    coin.open(100000.0, start)
    agent.lines["BTC/USD"] = coin
    clock.advance_to(start + timedelta(minutes=49))
    assert await agent.tick() == 0  # 20:49: eleven minutes left
    clock.advance_to(start + timedelta(minutes=50, seconds=5))
    assert await agent.tick() == 1  # 20:50: the stock goes, the coin stays
    await bus.wait_idle()
    assert line.exit_reason == "close" and coin.exit_intent == ""
    # after the bell nothing can be sold: a stock still held waits for the open, without orders
    held = Line("c3", "KO", "stocks", "long", 0.7, Decimal(10), 1.0, 1.0, 0.5, 30)
    held.open(70.0, start)
    agent.lines["KO"] = held
    clock.advance_to(start + timedelta(hours=3))
    before = len([e for e in bus.log if e.type == Topics.ORDER_INTENT])
    await agent.tick()
    await bus.wait_idle()
    assert len([e for e in bus.log if e.type == Topics.ORDER_INTENT]) == before
    task.cancel()


async def test_when_the_brokers_stop_fired_first_the_exit_is_not_sent_twice():
    class StopFilled(FakeTrading):
        async def get_order(self, order_id):
            return SimpleNamespace(status="filled", filled_avg_price=Decimal("99.02"))

    clock = SimClock(T0)
    bus = InMemoryBus(clock)
    agent = LinesAgent(bus, clock, FakeDB(), StopFilled(), SimpleNamespace(book_of={}))
    agent.start()
    task = asyncio.create_task(bus.run())
    await _open_line(agent, bus)
    clock.advance_to(T0 + timedelta(minutes=1))
    await bus.publish(bar("AAPL", clock.now(), 99.5, 98.9))
    await bus.wait_idle()
    sells = [e for e in bus.log if e.type == Topics.ORDER_INTENT and e.payload["side"] == "sell"]
    assert sells == []  # the position is already gone: a second sell would open a short
    score = next(e for e in bus.log if e.type == Topics.PREDICTION_SCORE)
    assert score.payload["reason"] == "broker" and score.payload["exit"] == 99.02
    assert "AAPL" not in agent.lines
    task.cancel()


async def test_a_snapshot_older_than_the_fill_does_not_close_a_fresh_line():
    clock = SimClock(T0)
    bus = InMemoryBus(clock)
    agent = LinesAgent(bus, clock, FakeDB(), FakeTrading(), SimpleNamespace(book_of={}))
    agent.start()
    task = asyncio.create_task(bus.run())
    await _open_line(agent, bus)
    stale = {"equity": "100000", "positions": [], "as_of": (T0 + timedelta(seconds=3)).isoformat()}
    await bus.publish(ev(Topics.PORTFOLIO_RECONCILED, T0 + timedelta(seconds=6), stale))
    await bus.wait_idle()
    assert "AAPL" in agent.lines  # taken before the fill reached the broker's list
    later = {
        "equity": "100000",
        "positions": [{"symbol": "MSFT", "qty": "5"}],
        "as_of": (T0 + timedelta(minutes=5)).isoformat(),
    }
    clock.advance_to(T0 + timedelta(minutes=5))
    await bus.publish(ev(Topics.PORTFOLIO_RECONCILED, clock.now(), later))
    await bus.wait_idle()
    assert "AAPL" not in agent.lines and agent.unmanaged == {"MSFT"}
    task.cancel()


async def test_a_resting_stop_is_not_a_working_order():
    from desk.agents.back_office import BackOffice
    from desk.data.alpaca import AccountInfo, BrokerOrder

    class Trading:
        paper = True

        async def account(self):
            return AccountInfo("ACTIVE", Decimal(100000), Decimal(90000), Decimal(200000), False, 0)

        async def positions(self):
            return []

        async def open_orders(self, limit=100):
            zero = Decimal(0)
            return [
                BrokerOrder(
                    "s1", "held", zero, None, symbol="AAPL", side="sell", order_type="stop"
                ),
                BrokerOrder("m1", "new", zero, None, symbol="KO", side="buy", order_type="market"),
            ]

        async def recent_orders(self, limit=50):
            return []

    clock = SimClock(T0)
    office = BackOffice(InMemoryBus(clock), clock, Trading(), Decimal(100000))
    await office.refresh()
    assert office.view().open_orders == {"KO": {"buy"}}


async def test_predictor_prices_the_stop_to_the_cent_sizes_first_and_stays_out_near_the_bell():
    import desk.lab.predictor as mod

    async def no_detail(db, market, symbol, now, **kw):
        return {"symbol": symbol, "candles": [], "trail": []}

    mod.symbol_detail = no_detail  # type: ignore[assignment]
    markets = {
        "stocks": Market("stocks", "US stocks", ("AAPL", "BIG"), "SPY", "nyse", True),
        "crypto": Market("crypto", "Crypto", ("BTC/USD",), "BTC/USD", "utc_day", True),
    }
    office = SimpleNamespace(snapshot=SimpleNamespace(equity=Decimal(100000)), book_of={})
    limits = RiskLimits(
        universe=frozenset({"AAPL", "BIG", "BTC/USD"}),
        allow_short=True,
        books={"crypto": BookLimits(allow_short=False, always_open=True)},
    )

    def call(max_loss=0.7):
        return Call(
            direction="long",
            confidence=0.8,
            horizon_minutes=120,
            max_loss_pct=max_loss,
            first_profit_pct=1.6,
            step_pct=0.5,
            reason="test",
            evidence_ids=[],
        )

    def trigger(symbol, market, last):
        return SentinelTrigger(
            symbol=symbol,
            market=market,
            reason="surge",
            detail="40 to 60",
            score=60,
            side="bull",
            strength=60,
            setup="MOMENTUM",
            last=last,
        )

    async def run(at, t, *answers):
        clock = SimClock(at)
        bus = InMemoryBus(clock)
        client = ScriptedClient(*answers)
        p = Predictor(
            bus,
            clock,
            FakeDB(),
            markets,
            SimpleNamespace(funnels={}),
            office,
            SimpleNamespace(limits=limits),
            client=client,
        )  # type: ignore[arg-type]
        rec = await p.decide(
            t, ev(Topics.SENTINEL_TRIGGER, at, t.model_dump(mode="json"), t.symbol)
        )
        return rec, [e for e in bus.log if e.type == Topics.ORDER_INTENT], client

    fine = Objection(severity="none", argument="nothing")
    # 268.37 less 0.7% is 266.49141: the broker takes 266.49, not four decimals
    rec, intents, _ = await run(T0, trigger("AAPL", "stocks", 268.37), call(), fine)
    assert rec.acted and intents[0].payload["stop_price"] == "266.49"
    assert intents[0].payload["quantity"] == "3"  # 1% of 100,000 buys three whole shares
    # crypto keeps its finer prices and fractional size
    rec, intents, _ = await run(T0, trigger("BTC/USD", "crypto", 0.512345), call(), fine)
    assert rec.acted and intents[0].payload["stop_price"] == "0.5088"
    # one share costs more than the position's budget: recorded as not acted, no order, no pending
    rec, intents, _ = await run(T0, trigger("BIG", "stocks", 1500.0), call(), fine)
    assert not rec.acted and "less than one unit" in rec.skipped_because and intents == []
    # 20:40 UTC on a day that closes at 21:00: no stock call at all, and no model call spent
    late = datetime(2026, 11, 6, 20, 40, tzinfo=UTC)
    rec, intents, client = await run(late, trigger("AAPL", "stocks", 268.37), call(), fine)
    assert rec is None and intents == [] and client.prompts == []
    # crypto has no bell
    rec, intents, _ = await run(late, trigger("BTC/USD", "crypto", 100000.0), call(), fine)
    assert rec.acted and len(intents) == 1


class FlakyClient(ScriptedClient):
    """A scripted client whose answer for one question can be an error instead."""

    async def parse(self, **kw):
        out = self.answers[kw["output_format"].__name__]
        if isinstance(out, Exception):
            self.prompts.append(kw["messages"][0]["content"])
            raise out
        return await super().parse(**kw)


def _unreadable() -> Exception:
    try:
        Objection.model_validate_json('{"severity":"concern","argument":"cut off mid')
    except Exception as exc:  # noqa: BLE001 - the error the SDK raises on a cut-off answer
        return exc
    raise AssertionError("expected a validation error")


async def test_the_bar_the_skeptic_the_budget_and_a_cut_off_answer(monkeypatch):
    clock = SimClock(T0)
    bus = InMemoryBus(clock)
    markets = {"stocks": Market("stocks", "US stocks", ("AAPL",), "SPY", "nyse", True)}
    sentinel = SimpleNamespace(funnels={})
    office = SimpleNamespace(snapshot=SimpleNamespace(equity=Decimal(100000)), book_of={})
    risk = SimpleNamespace(limits=RiskLimits(universe=frozenset({"AAPL"}), allow_short=True))

    async def fake_detail(db, market, symbol, now, **kw):
        return {"symbol": symbol, "candles": [], "trail": []}

    import desk.lab.predictor as mod

    monkeypatch.setattr(mod, "symbol_detail", fake_detail)

    def a_call(**kw):
        base = {
            "direction": "long",
            "confidence": 0.56,
            "horizon_minutes": 60,
            "max_loss_pct": 0.8,
            "first_profit_pct": 1.0,
            "step_pct": 0.5,
            "reason": "x",
        }
        return Call(**{**base, **kw})

    client = FlakyClient(a_call(), Objection(severity="concern", argument="extended from VWAP"))
    p = Predictor(bus, clock, FakeDB(), markets, sentinel, office, risk, client=client)  # type: ignore[arg-type]
    assert p.min_confidence == 0.55 and p.concern_margin == 0.0 and p.max_calls_per_hour == 120

    def trig(reason="surge"):
        return SentinelTrigger(
            symbol="AAPL",
            market="stocks",
            reason=reason,
            detail="d",
            score=55,
            side="bull",
            strength=55,
            setup="MOMENTUM",
            last=100.0,
        )

    cause = ev(Topics.SENTINEL_TRIGGER, T0, {}, "AAPL")
    # a call just over the bar trades, and the skeptic's concern is recorded beside it
    rec = await p.decide(trig(), cause)
    assert rec.acted and rec.objection.startswith("concern") and rec.call.lean == "long"
    assert "confidence 0.55 or more" in client.prompts[0]
    p.released("AAPL")
    # with a margin, the same concern holds the same call back
    p.concern_margin = 0.05
    rec = await p.decide(trig(), cause)
    assert not rec.acted and "skeptic concern" in rec.skipped_because
    p.concern_margin = 0.0
    # a skeptic whose answer is cut off is not a clean bill, and it does not lose the call
    client.answers["Objection"] = _unreadable()
    rec = await p.decide(trig(), cause)
    assert rec.acted and "no readable answer" in rec.objection
    p.released("AAPL")
    client.answers["Objection"] = Objection(severity="none", argument="checked")
    # a trade that risks more than it aims for is not taken
    client.answers["Call"] = a_call(max_loss_pct=1.2, first_profit_pct=0.8)
    rec = await p.decide(trig(), cause)
    assert not rec.acted and "risks more than it aims for" in rec.skipped_because
    # a pass keeps its lean, so the report can grade it
    client.answers["Call"] = a_call(direction="none", lean="short", confidence=0.5)
    rec = await p.decide(trig(), cause)
    assert not rec.acted and rec.call.lean == "short" and rec.model
    # a cut-off call is recorded as unanswered (no model), not lost
    client.answers["Call"] = _unreadable()
    rec = await p.decide(trig(), cause)
    assert not rec.acted and rec.skipped_because == "the model declined" and rec.model == ""
    # the budget: routine looks stop at two thirds of it, a trigger that carries news still gets in
    client.answers["Call"] = a_call(direction="none", lean="long", confidence=0.5)
    p.max_calls_per_hour = 6
    p.calls_at = [clock.now()] * 4
    rec = await p.decide(trig("review"), cause)
    assert rec.skipped_because.startswith("budget") and rec.model == ""
    rec = await p.decide(trig("surge"), cause)
    assert rec.skipped_because == "the predictor sees no edge"
    p.calls_at = [clock.now()] * 6
    rec = await p.decide(trig("surge"), cause)
    assert rec.skipped_because.startswith("budget")


async def test_bars_from_before_the_fill_do_not_move_a_line():
    """At a restart the desk re-reads the day's bars; a low from the morning must not fire a
    line drawn at noon. Seen live on 10 Nov: UNG, GLD and IAU closed as 'stop' at prices that
    never touched their lines."""
    clock = SimClock(T0)
    bus = InMemoryBus(clock)
    agent = LinesAgent(bus, clock, FakeDB(), FakeTrading(), SimpleNamespace(book_of={}))
    agent.start()
    task = asyncio.create_task(bus.run())
    line = await _open_line(agent, bus)  # long from 100, stop 99
    assert line.stop < 100 < line.profit_line
    # the morning's bars, replayed after the fill: well below the stop, and then above the line
    for k in range(30, 0, -5):
        await bus.publish(bar("AAPL", T0 - timedelta(minutes=k), 97.5, 96.0))
    await bus.publish(bar("AAPL", T0 - timedelta(minutes=1), 103.0, 102.0))
    await bus.wait_idle()
    assert "AAPL" in agent.lines and not line.exit_reason and not line.armed
    assert not [
        e for e in bus.log if e.type == Topics.ORDER_INTENT and e.payload.get("side") == "sell"
    ]
    # a bar after the fill counts as before
    await bus.publish(bar("AAPL", T0 + timedelta(minutes=2), 99.5, 98.8))
    await bus.wait_idle()
    assert line.exit_reason == "stop"
    task.cancel()
