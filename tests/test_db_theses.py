from datetime import timedelta
from decimal import Decimal

import pytest

from desk.core.payloads import ThesisPayload
from desk.storage.db import Database

pytestmark = pytest.mark.usefixtures("database_url")


async def test_thesis_round_trip_and_updates(database_url, clock):
    db = Database(database_url)
    await db.open()
    await db.migrate()
    now = clock.now()
    t = ThesisPayload(
        thesis_id="zz-th-1",
        symbol="ZZQ",
        book="swing",
        direction="long",
        status="pending_entry",
        cycle_id="c1",
        narrative="test thesis",
        evidence_ids=["01A", "01B"],
        expected_return_pct=4.5,
        horizon_days=5,
        conviction=0.55,
        quantity=Decimal(3),
        stop_pct=4,
        target_pct=8,
        invalidation=["guidance cut"],
        created_at=now,
        updated_at=now,
    )
    await db.insert_thesis(t)
    await db.insert_thesis(t)  # idempotent
    opened = [x for x in await db.open_theses() if x.thesis_id == "zz-th-1"]
    assert (
        len(opened) == 1
        and opened[0].evidence_ids == ["01A", "01B"]
        and opened[0].invalidation == ["guidance cut"]
    )
    updated = await db.update_thesis(
        "zz-th-1",
        now + timedelta(minutes=1),
        status="active",
        entry_price=Decimal(100),
        stop_price=Decimal(96),
        target_price=Decimal(108),
    )
    assert updated and updated.status == "active" and updated.stop_price == Decimal(96)
    closed = await db.update_thesis(
        "zz-th-1",
        now + timedelta(days=1),
        status="closed",
        closed_at=now + timedelta(days=1),
        realized_pnl=Decimal("12.5"),
    )
    assert closed and closed.realized_pnl == Decimal("12.5")
    assert not [x for x in await db.open_theses() if x.thesis_id == "zz-th-1"]
    with pytest.raises(ValueError):
        await db.update_thesis("zz-th-1", now, narrative="no")
    async with db.pool.connection() as conn:
        await conn.execute("DELETE FROM theses WHERE thesis_id = 'zz-th-1'")
    await db.close()
