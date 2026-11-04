import asyncio

import pytest
from pydantic import ValidationError

from desk.agents.base import BaseAgent
from desk.bus.memory import InMemoryBus
from desk.core.events import Event, Topics
from tests.conftest import collector, make_event


async def test_every_group_sees_every_event(clock):
    bus = InMemoryBus(clock)
    a: list[Event] = []
    b: list[Event] = []
    bus.subscribe([Topics.MARKET_BAR_1M], "features", collector(a))
    bus.subscribe([Topics.MARKET_BAR_1M], "strategy", collector(b))
    task = asyncio.create_task(bus.run())
    e = make_event(clock)
    await bus.publish(e)
    await bus.publish(make_event(clock, type=Topics.NEWS_ARTICLE_RAW))
    await bus.wait_idle()
    assert a == [e] and b == [e]
    await bus.stop()
    await task


async def test_wildcard_subscriptions(clock):
    bus = InMemoryBus(clock)
    everything: list[Event] = []
    market: list[Event] = []
    bus.subscribe(["*"], "persister", collector(everything))
    bus.subscribe(["market.*"], "mkt", collector(market))
    task = asyncio.create_task(bus.run())
    e1 = make_event(clock)
    e2 = make_event(clock, type=Topics.NEWS_ARTICLE_RAW)
    await bus.publish(e1)
    await bus.publish(e2)
    await bus.wait_idle()
    assert everything == [e1, e2] and market == [e1]
    await bus.stop()
    await task


async def test_failed_handler_is_retried_then_dead_lettered(clock):
    bus = InMemoryBus(clock)
    attempts: list[str] = []
    dead: list[Event] = []

    async def bad(event: Event) -> None:
        attempts.append(event.event_id)
        raise RuntimeError("boom")

    bus.subscribe([Topics.ORDER_INTENT], "risk", bad, max_deliveries=2)
    bus.subscribe([Topics.SYSTEM_DEAD_LETTER], "persister", collector(dead))
    task = asyncio.create_task(bus.run())
    e = make_event(clock, type=Topics.ORDER_INTENT)
    await bus.publish(e)
    await bus.wait_idle()
    assert attempts == [e.event_id] * 2
    assert len(bus.dead_letters) == 1
    assert dead[0].causation_id == e.event_id
    assert (
        dead[0].payload["error"].startswith("RuntimeError") and dead[0].payload["deliveries"] == 2
    )
    await bus.stop()
    await task


async def test_agent_emit_stamps_lineage_and_validates_payload(clock):
    class Echo(BaseAgent):
        name = "echo"
        topics = (Topics.MARKET_BAR_1M,)

        async def handle(self, event: Event) -> None:
            await self.emit(
                Topics.WATCHER_ALERT,
                {"symbol": event.symbol, "kind": "abnormal_move", "severity": "info"},
                caused_by=event,
            )

    bus = InMemoryBus(clock)
    alerts: list[Event] = []
    Echo(bus, clock).start()
    bus.subscribe([Topics.WATCHER_ALERT], "sink", collector(alerts))
    task = asyncio.create_task(bus.run())
    e = make_event(clock, symbol="AAPL", correlation_id="cycle-1")
    await bus.publish(e)
    await bus.wait_idle()
    out = alerts[0]
    assert (
        out.causation_id == e.event_id and out.correlation_id == "cycle-1" and out.symbol == "AAPL"
    )
    assert out.source == "echo@0.1" and out.payload["details"] == {}
    await bus.stop()
    await task


async def test_agent_emit_rejects_an_invalid_payload(clock):
    agent = BaseAgent(InMemoryBus(clock), clock)
    with pytest.raises(ValidationError):
        await agent.emit(Topics.ORDER_INTENT, {"symbol": "AAPL"})
