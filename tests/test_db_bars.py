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


async def test_a_bar_stored_again_is_overwritten(database_url, clock):
    """A daily bar fetched during the session is a half-day bar; the next backfill must replace it."""
    db = Database(database_url)
    await db.open()
    await db.migrate()
    t0 = clock.now()

    def bar(close: str) -> MarketBar:
        return MarketBar(
            symbol="ZZUPS",
            interval="1d",
            ts=t0,
            open=Decimal(1),
            high=Decimal(2),
            low=Decimal(1),
            close=Decimal(close),
            volume=10,
            source="test",
        )

    await db.insert_bars([bar("225.69")], t0)
    await db.insert_bars([bar("224.24")], t0 + timedelta(hours=4))
    rows = await db.recent_bars("ZZUPS", "1d", limit=1)
    assert len(rows) == 1 and Decimal(rows[0]["close"]) == Decimal("224.24")
    await db.close()
