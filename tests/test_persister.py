import asyncio

import pytest

from desk.bus.memory import InMemoryBus
from desk.storage.db import Database
from desk.storage.persister import Persister
from tests.conftest import make_event

pytestmark = pytest.mark.usefixtures("database_url")


async def test_events_persist_exactly_once(database_url, clock):
    db = Database(database_url)
    await db.open()
    await db.migrate()
    bus = InMemoryBus(clock)
    Persister(bus, db).start()
    task = asyncio.create_task(bus.run())
    e = make_event(clock, symbol="AAPL", payload={"k": "v"})
    await bus.publish(e)
    await bus.publish(e)
    await bus.wait_idle()
    assert await db.count_events(event_id=e.event_id) == 1
    await bus.stop()
    await task
    await db.close()
