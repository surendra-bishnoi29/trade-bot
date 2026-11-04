import asyncio
from datetime import UTC, datetime

from desk.agents.monitor import Monitor
from desk.bus.memory import InMemoryBus
from desk.core.clock import SimClock
from desk.core.events import Topics
from tests.conftest import make_event

OPEN = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)


async def test_feed_stale_engages_the_kill_switch_but_quiet_symbols_do_not():
    clock = SimClock(OPEN)
    bus = InMemoryBus(clock)
    Monitor(bus, clock, n_symbols=4).start()
    task = asyncio.create_task(bus.run())
    for s in ("A", "B", "C"):
        await bus.publish(
            make_event(
                clock,
                type=Topics.WATCHER_ALERT,
                symbol=s,
                payload={"kind": "stale_data", "severity": "info", "details": {}},
            )
        )
    await bus.wait_idle()
    assert not [e for e in bus.log if e.type == Topics.RISK_KILL_SWITCH]
    await bus.publish(
        make_event(
            clock,
            type=Topics.WATCHER_ALERT,
            symbol="FEED",
            payload={"kind": "feed_stale", "severity": "critical", "details": {"last_bar": None}},
        )
    )
    await bus.wait_idle()
    kills = [e for e in bus.log if e.type == Topics.RISK_KILL_SWITCH]
    assert len(kills) == 1 and "feed stale" in kills[0].payload["reason"]
    await bus.stop()
    await task


async def test_repeated_rejections_and_broker_down(clock):
    bus = InMemoryBus(clock)
    Monitor(bus, clock, n_symbols=30, rejects_in_a_row=2).start()
    task = asyncio.create_task(bus.run())
    for _ in range(2):
        await bus.publish(
            make_event(
                clock,
                type=Topics.ORDER_REJECTED,
                symbol="AAPL",
                payload={"order_id": "o", "intent_id": "i", "status": "rejected"},
            )
        )
    await bus.wait_idle()
    assert [e.payload["reason"] for e in bus.log if e.type == Topics.RISK_KILL_SWITCH] == [
        "2 orders rejected in a row"
    ]
    await bus.stop()
    await task
    clock2 = SimClock(clock.now())
    bus2 = InMemoryBus(clock2)
    Monitor(bus2, clock2, n_symbols=30).start()
    task2 = asyncio.create_task(bus2.run())
    await bus2.publish(
        make_event(
            clock2,
            type=Topics.SYSTEM_HEALTH,
            payload={"component": "broker", "status": "down", "detail": "3 reconciliations failed"},
        )
    )
    await bus2.wait_idle()
    assert any(
        "broker is down" in e.payload["reason"]
        for e in bus2.log
        if e.type == Topics.RISK_KILL_SWITCH
    )
    await bus2.stop()
    await task2
