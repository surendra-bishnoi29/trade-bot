from datetime import timedelta
from decimal import Decimal

import pytest

from desk.core.events import Topics
from desk.core.payloads import MarketBar
from desk.storage.db import Database
from tests.conftest import make_event

pytestmark = pytest.mark.usefixtures("database_url")


async def test_bars_insert_query_and_recent_events(database_url, clock):
    db = Database(database_url)
    await db.open()
    await db.migrate()
    t0 = clock.now()
    bars = [
        MarketBar(
            symbol="ZZTEST",
            interval="1m",
            ts=t0 + timedelta(minutes=i),
            open=Decimal(1),
            high=Decimal(2),
            low=Decimal(1),
            close=Decimal(str(1 + i)),
            volume=100 + i,
            source="test",
        )
        for i in range(5)
    ]
    assert await db.insert_bars(bars, t0) == 5
    assert (
        await db.insert_bars(bars, t0) == 5
    )  # idempotent at the row level: no error, no duplicates
    recent = await db.recent_bars("ZZTEST", "1m", limit=3)
    assert [r["close"] for r in recent] == [Decimal(3), Decimal(4), Decimal(5)]
    before = await db.recent_bars("ZZTEST", "1m", limit=10, before=t0 + timedelta(minutes=2))
    assert len(before) == 2
    e = make_event(clock, type=Topics.WATCHER_ALERT, symbol="ZZTEST", payload={"k": 1})
    await db.insert_event(e)
    found = await db.recent_events(
        [Topics.WATCHER_ALERT], t0 - timedelta(minutes=1), symbol="ZZTEST"
    )
    assert any(x.event_id == e.event_id for x in found)
    async with db.pool.connection() as conn:
        await conn.execute("DELETE FROM bars WHERE symbol = 'ZZTEST'")
        await conn.execute("DELETE FROM events WHERE symbol = 'ZZTEST'")
    await db.close()
