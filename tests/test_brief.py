"""The daily brief: facts gathered from the journal, written by the head trader, carried as an event."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from desk.agents.back_office import Snapshot
from desk.agents.brief import DailyBriefAgent
from desk.bus.memory import InMemoryBus
from desk.core.clock import SimClock
from desk.core.events import Topics
from desk.core.payloads import BriefTrade, DailyBrief, PositionSnapshot
from desk.storage.db import Database
from desk.storage.memory_theses import MemoryTheses
from tests.conftest import make_event as _make_event

pytestmark = pytest.mark.usefixtures("database_url")

SOURCE = "brieftest@0.1"
NOW = datetime(2026, 10, 20, 21, 15, tzinfo=UTC)  # after the close of a Tuesday session


def make_event(clock, **kw):
    return _make_event(clock, **kw).model_copy(update={"source": SOURCE})


class FakeBackOffice:
    snapshot = Snapshot(
        equity=Decimal("100120.50"),
        cash=Decimal(98000),
        buying_power=Decimal(98000),
        positions={
            "ZZBR": PositionSnapshot(
                symbol="ZZBR",
                qty=Decimal(10),
                avg_entry=Decimal(210),
                market_value=Decimal("2120.50"),
                unrealized_pl=Decimal("20.50"),
            )
        },
        as_of=NOW,
    )


class FakeHead:
    def __init__(self):
        self.material = ""

    async def brief(self, material: str) -> DailyBrief:
        self.material = material
        return DailyBrief(
            headline="Up $120.50, one new position, one feed outage",
            summary="The desk bought ZZBR after the Evercore note and held it into the close.",
            trades=[
                BriefTrade(
                    symbol="ZZBR",
                    side="buy",
                    quantity=10,
                    price=210.0,
                    why="Pullback with a catalyst.",
                )
            ],
            book="10 ZZBR, stop 201.60, target 224.70.",
            incidents="The price feed went quiet for twelve minutes at 16:35 UTC and the kill switch engaged.",
            tomorrow="Watch the ZZBR stop and the open for a gap.",
        )


async def test_brief_gathers_facts_and_emits_the_event(database_url):
    db = Database(database_url)
    await db.open()
    await db.migrate()
    clock = SimClock(NOW - timedelta(hours=6))
    await db.insert_event(
        make_event(
            clock,
            type=Topics.PORTFOLIO_RECONCILED,
            payload={
                "equity": "100000",
                "cash": "100000",
                "buying_power": "100000",
                "positions": [],
                "as_of": clock.now().isoformat(),
            },
        )
    )
    clock.advance_to(NOW - timedelta(hours=5))
    await db.insert_event(
        make_event(
            clock,
            type=Topics.ORDER_FILLED,
            symbol="ZZBR",
            payload={
                "order_id": "o",
                "intent_id": "01BRIEFTEST000000000000000",
                "side": "buy",
                "status": "filled",
                "filled_quantity": "10",
                "avg_price": "210",
            },
        )
    )
    clock.advance_to(NOW - timedelta(hours=4))
    await db.insert_event(
        make_event(
            clock,
            type=Topics.RISK_KILL_SWITCH,
            payload={"engaged": True, "reason": "market data feed stale", "source": "monitor"},
        )
    )
    try:
        bus = InMemoryBus(SimClock(NOW))
        head = FakeHead()
        agent = DailyBriefAgent(
            bus, SimClock(NOW), db, head, FakeBackOffice(), theses=MemoryTheses()
        )
        payload = await agent.write()
        facts = payload.facts
        assert facts["day"] == "2026-10-20" and facts["book"]["equity_start"] == 100000.0
        assert facts["fills"][0]["symbol"] == "ZZBR" and facts["fills"][0]["quantity"] == 10.0
        assert any("Kill switch ENGAGED" in i for i in facts["incidents"])
        assert facts["book"]["positions"][0]["symbol"] == "ZZBR"
        assert "ZZBR" in head.material and "FACTS FROM THE JOURNAL" in head.material
        events = [e for e in bus.log if e.type == Topics.DESK_BRIEF]
        assert len(events) == 1 and events[0].payload["brief"]["headline"].startswith("Up $120.50")
        text = DailyBriefAgent.render(payload)
        assert "DAILY BRIEF 2026-10-20" in text and "BUY 10 ZZBR at 210.00" in text
    finally:
        async with db.pool.connection() as conn:
            await conn.execute("DELETE FROM events WHERE source = %s", (SOURCE,))
        await db.close()
