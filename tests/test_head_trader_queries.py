from datetime import timedelta
from decimal import Decimal

import pytest

from desk.agents.head_trader import HeadTrader
from desk.bus.memory import InMemoryBus
from desk.core.config import Settings
from desk.core.events import Topics
from desk.core.payloads import MarketBar
from desk.storage.db import Database
from tests.conftest import make_event

pytestmark = pytest.mark.usefixtures("database_url")


async def test_queries_read_the_database_and_format_for_the_model(database_url, clock):
    db = Database(database_url)
    await db.open()
    await db.migrate()
    head = HeadTrader(InMemoryBus(clock), clock, db, Settings(), back_office=None, client=object())  # type: ignore[arg-type]
    t0 = clock.now()
    await db.insert_bars(
        [
            MarketBar(
                symbol="ZZQ",
                interval="1d",
                ts=t0 - timedelta(days=i),
                open=Decimal(1),
                high=Decimal(2),
                low=Decimal(1),
                close=Decimal(10 + i),
                volume=5,
                source="test",
            )
            for i in range(3)
        ],
        t0,
    )
    raw = make_event(
        clock,
        type=Topics.NEWS_ARTICLE_RAW,
        symbol="ZZQ",
        payload={
            "article_id": "n1",
            "published_at": (t0 - timedelta(hours=1)).isoformat(),
            "source": "benzinga",
            "title": "ZZQ does a thing",
            "symbols": ["ZZQ"],
        },
    )
    scored = make_event(
        clock,
        type=Topics.NEWS_ARTICLE_SCORED,
        symbol="ZZQ",
        payload={
            "article_id": "n1",
            "polarity": 0.4,
            "impact": "medium",
            "event_type": "product",
            "relevance": 0.9,
            "confidence": 0.7,
            "horizon": "days",
            "model": "m",
            "prompt_version": "v",
        },
    )
    alert = make_event(
        clock,
        type=Topics.WATCHER_ALERT,
        symbol="ZZQ",
        payload={"kind": "volume_spike", "severity": "warning", "details": {"multiple": "6.0"}},
    )
    for e in (raw, scored, alert):
        await db.insert_event(e)
    bars = await head.q_bars("zzq", "1d", 2)
    assert bars.count("\n") == 1 and bars.endswith("c=10 v=5")
    assert "no 1m bars stored for ZZQ" in await head.q_bars("ZZQ", "1m")
    news = await head.q_news("ZZQ", 24)
    assert "[n1]" in news and "polarity=0.4" in news and "impact=medium" in news
    alerts = await head.q_alerts(6)
    assert "volume_spike" in alerts and alert.event_id in alerts
    assert "nothing in the journal" in await head.q_journal(1)
    assert "no portfolio snapshot" in await head.q_portfolio()
    assert "autonomy level 1" in head.daily_layer() and head.context_line().startswith("[now ")
    assert len(head.tools()) == 11  # get_research and get_funnel joined the toolbox
    assert "not available" in head.q_risk_state()
    assert "no cycles" in await head.q_cycles(24)
    async with db.pool.connection() as conn:
        await conn.execute("DELETE FROM bars WHERE symbol = 'ZZQ'")
        await conn.execute("DELETE FROM events WHERE symbol = 'ZZQ'")
    await db.close()
