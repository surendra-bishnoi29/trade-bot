import asyncio
import time
import uuid

import pytest

from desk.bus.redis_streams import RedisStreamsBus
from desk.core.events import Event, Topics
from tests.conftest import collector, make_event

pytestmark = pytest.mark.usefixtures("redis_url")


def make_bus(redis_url: str, clock) -> RedisStreamsBus:
    return RedisStreamsBus(
        redis_url, clock, prefix=f"test:{uuid.uuid4().hex[:8]}:", block_ms=100, min_idle_ms=50
    )


async def wait_for(condition, timeout: float = 5.0) -> None:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


async def test_groups_fan_out_and_acknowledge(redis_url, clock):
    bus = make_bus(redis_url, clock)
    a: list[Event] = []
    b: list[Event] = []
    bus.subscribe([Topics.MARKET_BAR_1M], "features", collector(a))
    bus.subscribe([Topics.MARKET_BAR_1M], "strategy", collector(b))
    task = asyncio.create_task(bus.run())
    await asyncio.sleep(0.2)
    e = make_event(clock)
    await bus.publish(e)
    await wait_for(lambda: len(a) == 1 and len(b) == 1)
    assert a[0] == e and b[0] == e
    await bus.stop()
    await task
    await bus.close()


async def test_pending_entries_are_redelivered_until_the_handler_succeeds(redis_url, clock):
    bus = make_bus(redis_url, clock)
    calls: list[str] = []

    async def flaky(event: Event) -> None:
        calls.append(event.event_id)
        if len(calls) < 3:
            raise RuntimeError("not yet")

    bus.subscribe([Topics.ORDER_INTENT], "risk", flaky, max_deliveries=5)
    task = asyncio.create_task(bus.run())
    await asyncio.sleep(0.2)
    e = make_event(clock, type=Topics.ORDER_INTENT)
    await bus.publish(e)
    await wait_for(lambda: len(calls) >= 3)
    await asyncio.sleep(0.3)
    assert calls == [e.event_id] * 3
    await bus.stop()
    await task
    await bus.close()


async def test_poison_message_is_dead_lettered_after_max_deliveries(redis_url, clock):
    bus = make_bus(redis_url, clock)
    calls: list[str] = []
    dead: list[Event] = []

    async def bad(event: Event) -> None:
        calls.append(event.event_id)
        raise RuntimeError("boom")

    bus.subscribe([Topics.ORDER_INTENT], "risk", bad, max_deliveries=2)
    bus.subscribe([Topics.SYSTEM_DEAD_LETTER], "persister", collector(dead))
    task = asyncio.create_task(bus.run())
    await asyncio.sleep(0.2)
    e = make_event(clock, type=Topics.ORDER_INTENT)
    await bus.publish(e)
    await wait_for(lambda: len(dead) == 1)
    assert calls == [e.event_id] * 2
    assert dead[0].causation_id == e.event_id and dead[0].payload["deliveries"] == 2
    await asyncio.sleep(0.3)
    assert len(calls) == 2
    await bus.stop()
    await task
    await bus.close()
