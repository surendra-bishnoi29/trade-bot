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


async def test_on_the_lab_desk_the_head_trader_knows_the_mandate_and_reads_the_lab(
    database_url, clock
):
    db = Database(database_url)
    await db.open()
    await db.migrate()
    settings = Settings(lab_enabled=True)
    head = HeadTrader(InMemoryBus(clock), clock, db, settings, back_office=None, client=object())  # type: ignore[arg-type]
    layer = head.daily_layer()
    assert "THIS IS THE LAB DESK" in layer
    assert "GLD" in layer and "USO" in layer and "BTC/USD" in layer
    assert "Long only" in layer and "never short" in layer  # crypto
    assert "no hourly cycle" in layer and "get_lab" in layer
    assert len(head.tools()) == 12  # the lab adds get_lab
    assert set(head.traded_markets()) == {"stocks", "commodities", "crypto"}
    now = clock.now()
    for i, (acted, why) in enumerate([(True, ""), (False, "confidence 0.20 below 0.60")]):
        await db.insert_event(
            make_event(
                clock,
                type=Topics.PREDICTION_CALL,
                symbol="BTC/USD",
                payload={
                    "call_id": f"labq{i}",
                    "symbol": "BTC/USD",
                    "market": "crypto",
                    "trigger": "surge",
                    "call": {
                        "direction": "long" if acted else "none",
                        "confidence": 0.7 if acted else 0.2,
                        "horizon_minutes": 120,
                        "max_loss_pct": 0.6,
                        "first_profit_pct": 0.9,
                        "step_pct": 0.4,
                        "reason": "volume confirmed the move",
                    },
                    "price": 100000.0,
                    "acted": acted,
                    "skipped_because": why,
                },
            )
        )
    await db.insert_event(
        make_event(
            clock,
            type=Topics.PREDICTION_SCORE,
            symbol="GLD",
            payload={
                "call_id": "labq-s",
                "symbol": "GLD",
                "market": "commodities",
                "direction": "short",
                "confidence": 0.65,
                "realized_pct": 0.8,
                "hit": True,
                "reason": "line",
                "closed_at": now.isoformat(),
            },
        )
    )
    text = await head.q_lab(24)
    assert "Markets traded: stocks, commodities, crypto." in text
    assert "1 of 1 scored calls hit (100%), +0.80% a call. Too few to judge" in text
    assert "commodities: 1 of 1" in text
    assert "Open positions on lines: none." in text
    assert "2, of which 1 became orders" in text and "by market: crypto 2" in text
    assert "ORDER, loss line 0.6%" in text and "no trade (confidence 0.20 below 0.60)" in text
    assert "no such market" in await head.q_funnel("bonds")
    # the main desk is unchanged: no lab section, no lab tool
    main = HeadTrader(InMemoryBus(clock), clock, db, Settings(), back_office=None, client=object())  # type: ignore[arg-type]
    assert "LAB DESK" not in main.daily_layer() and len(main.tools()) == 11
    assert "not switched on" in await main.q_lab()
    async with db.pool.connection() as conn:
        await conn.execute("DELETE FROM events WHERE payload->>'call_id' LIKE 'labq%'")
    await db.close()
