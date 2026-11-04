import asyncio
from decimal import Decimal

from desk.agents.back_office import BackOffice
from desk.agents.execution import ExecutionAgent
from desk.bus.memory import InMemoryBus
from desk.core.events import Topics
from desk.core.payloads import OrderIntent, RiskDecision, RuleResult
from desk.data.alpaca import AccountInfo, BrokerOrder, PositionInfo
from tests.conftest import make_event


class FakeTrading:
    paper = True

    def __init__(self) -> None:
        self.submitted: list[tuple] = []
        self.polls = 0

    async def account(self) -> AccountInfo:
        return AccountInfo(
            status="ACTIVE",
            equity=Decimal(100500),
            cash=Decimal(90000),
            buying_power=Decimal(200000),
            pattern_day_trader=False,
            daytrade_count=0,
        )

    async def positions(self) -> list[PositionInfo]:
        return [
            PositionInfo(
                symbol="AAPL",
                qty=Decimal(10),
                avg_entry=Decimal(250),
                market_value=Decimal(2600),
                unrealized_pl=Decimal(100),
            )
        ]

    async def submit_market_order(self, symbol, qty, side, client_order_id) -> BrokerOrder:
        self.submitted.append((symbol, qty, side, client_order_id))
        return BrokerOrder(
            broker_order_id="b-1", status="accepted", filled_qty=Decimal(0), filled_avg_price=None
        )

    async def recent_orders(self, limit=50):
        return [
            BrokerOrder(
                broker_order_id="b-9",
                status="filled",
                filled_qty=Decimal(8),
                filled_avg_price=Decimal("226.05625"),
                client_order_id="01M6GZJ4C6C6VW19W1KKFG8A2Q",
                symbol="CVX",
                side="buy",
            ),
            BrokerOrder(
                broker_order_id="b-8",
                status="new",
                filled_qty=Decimal(0),
                filled_avg_price=None,
                client_order_id="01M6GZJ4C6C6VW19W1KKFG8A2R",
                symbol="AAPL",
                side="buy",
            ),
            BrokerOrder(
                broker_order_id="b-7",
                status="filled",
                filled_qty=Decimal(1),
                filled_avg_price=Decimal(1),
                client_order_id="manual",
                symbol="XYZ",
                side="buy",
            ),
        ]

    async def get_order(self, broker_order_id) -> BrokerOrder:
        self.polls += 1
        if self.polls < 2:
            return BrokerOrder(
                broker_order_id=broker_order_id,
                status="partially_filled",
                filled_qty=Decimal(4),
                filled_avg_price=Decimal("250.10"),
            )
        return BrokerOrder(
            broker_order_id=broker_order_id,
            status="filled",
            filled_qty=Decimal(10),
            filled_avg_price=Decimal("250.20"),
        )


def approved(symbol="AAPL") -> RiskDecision:
    intent = OrderIntent(
        intent_id="i-1", symbol=symbol, book="swing", side="buy", quantity=Decimal(10)
    )
    return RiskDecision(
        intent_id="i-1",
        intent=intent,
        decision="approved",
        approved_quantity=Decimal(10),
        rules=[RuleResult(rule="universe", passed=True)],
    )


async def test_execution_submits_tracks_partials_and_reports_the_fill(clock):
    bus = InMemoryBus(clock)
    trading = FakeTrading()
    agent = ExecutionAgent(bus, clock, trading, poll_s=1.0, poll_for_s=10.0)  # type: ignore[arg-type]
    agent.start()
    task = asyncio.create_task(bus.run())
    await bus.publish(
        make_event(
            clock,
            type=Topics.RISK_APPROVED,
            symbol="AAPL",
            payload=approved().model_dump(mode="json"),
        )
    )
    await bus.wait_idle()
    await asyncio.gather(*agent._tasks)
    await bus.wait_idle()
    types = [e.type for e in bus.log]
    assert trading.submitted == [("AAPL", Decimal(10), "buy", "i-1")]
    assert types[1:] == [Topics.ORDER_SUBMITTED, Topics.ORDER_PARTIAL, Topics.ORDER_FILLED]
    filled = bus.log[-1]
    assert filled.payload["filled_quantity"] == "10" and filled.payload["avg_price"] == "250.20"
    assert filled.causation_id == bus.log[1].event_id
    await bus.stop()
    await task


async def test_back_office_snapshot_view_and_price(clock):
    bus = InMemoryBus(clock)
    office = BackOffice(bus, clock, FakeTrading(), Decimal(100000))  # type: ignore[arg-type]
    snap = await office.refresh()
    assert snap.equity == Decimal(100500) and set(snap.positions) == {"AAPL"}
    assert office.view().positions == {"AAPL": Decimal(10)} and office.price_of("AAPL") == Decimal(
        260
    )
    assert office.price_of("MSFT") is None
    assert (
        bus.log[-1].type == Topics.PORTFOLIO_RECONCILED
        and bus.log[-1].payload["positions"][0]["symbol"] == "AAPL"
    )


async def test_back_office_reconciles_broker_fills_once(clock):
    bus = InMemoryBus(clock)
    office = BackOffice(bus, clock, FakeTrading(), Decimal(100000), db=None)  # type: ignore[arg-type]
    await office.refresh()
    fills = [e for e in bus.log if e.type == Topics.ORDER_FILLED]
    assert (
        len(fills) == 1
        and fills[0].payload["intent_id"] == "01M6GZJ4C6C6VW19W1KKFG8A2Q"
        and fills[0].payload["avg_price"] == "226.05625"
    )
    await office.refresh()
    assert (
        len([e for e in bus.log if e.type == Topics.ORDER_FILLED]) == 1
    )  # recorded once, never twice
