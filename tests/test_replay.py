"""Replay: driven clock, simulated broker, in-memory theses, and one full session through the real
agents with a scripted head trader (phase 1 exit criteria)."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from desk.core import calendar
from desk.core.clock import SimClock
from desk.core.config import Settings
from desk.core.events import Topics
from desk.core.payloads import MarketBar, ThesisPayload
from desk.replay.assemble import build_replay
from desk.replay.broker import SimulatedBroker
from desk.storage.db import Database
from desk.storage.memory_theses import MemoryTheses
from tests.conftest import make_event as _make_event

SOURCE = "replaytest@0.1"
DAY = datetime(2026, 10, 20, tzinfo=UTC).date()  # a Tuesday, a normal session
OPEN, CLOSE = calendar.session_bounds(DAY)


def make_event(clock, **kw):
    return _make_event(clock, **kw).model_copy(update={"source": SOURCE})


def bar(symbol: str, ts: datetime, open_: str, close: str, interval: str = "1m") -> MarketBar:
    return MarketBar(
        symbol=symbol,
        interval=interval,
        ts=ts,
        open=Decimal(open_),
        high=max(Decimal(open_), Decimal(close)),
        low=min(Decimal(open_), Decimal(close)),
        close=Decimal(close),
        volume=1000,
        source="test",
    )


# ---- units ----


async def test_driven_clock_sleeps_until_time_is_advanced():
    clock = SimClock(datetime(2026, 10, 20, 12, 0, tzinfo=UTC), driven=True)
    woke = []

    async def sleeper():
        await clock.sleep(300)
        woke.append(clock.now())

    task = asyncio.create_task(sleeper())
    await asyncio.sleep(0)
    clock.advance_to(datetime(2026, 10, 20, 12, 4, tzinfo=UTC))
    await asyncio.sleep(0)
    assert woke == [] and clock.next_wakeup() == datetime(2026, 10, 20, 12, 5, tzinfo=UTC)
    clock.advance_to(datetime(2026, 10, 20, 12, 6, tzinfo=UTC))
    await asyncio.sleep(0)
    await task
    assert woke == [datetime(2026, 10, 20, 12, 6, tzinfo=UTC)]
    free = SimClock(datetime(2026, 10, 20, 12, 0, tzinfo=UTC))
    await free.sleep(60)
    assert free.now() == datetime(2026, 10, 20, 12, 1, tzinfo=UTC)


async def test_simulated_broker_fills_at_the_next_open_with_costs():
    clock = SimClock(OPEN - timedelta(hours=1))
    broker = SimulatedBroker(
        clock, Decimal(10000), slippage_bps=Decimal(10), commission_per_share=Decimal("0.01")
    )
    order = await broker.submit_market_order(
        "ZZQ", Decimal(10), "buy", "01INTENT00000000000000000A"
    )
    assert order.status == "accepted"
    assert broker.on_bar(bar("ZZQ", OPEN - timedelta(minutes=30), "99", "99.5")) == []  # closed
    fills = broker.on_bar(bar("ZZQ", OPEN, "100", "101"))
    assert (
        len(fills) == 1 and fills[0].price == Decimal("100.10") and fills[0].cost == Decimal("1.10")
    )
    assert (await broker.get_order(order.broker_order_id)).status == "filled"
    account = await broker.account()
    assert account.cash == Decimal("8998.90") and account.equity == Decimal("10008.90")
    with pytest.raises(ValueError):
        await broker.submit_market_order("ZZQ", Decimal(11), "sell", "01INTENT00000000000000000B")
    clock.advance_to(OPEN + timedelta(minutes=1))
    await broker.submit_market_order("ZZQ", Decimal(10), "sell", "01INTENT00000000000000000C")
    assert broker.on_bar(bar("ZZQ", OPEN + timedelta(minutes=1), "102", "103")) == []  # same minute
    fills = broker.on_bar(bar("ZZQ", OPEN + timedelta(minutes=2), "104", "103"))
    assert fills[0].side == "sell" and fills[0].price == Decimal("103.90")
    assert fills[0].realized_pnl == Decimal("38.00") and broker.holdings == {}
    assert broker.realized_pnl == Decimal("37.80") and broker.costs == Decimal("2.20")


async def test_memory_theses_behave_like_the_table():
    store = MemoryTheses()
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    t = ThesisPayload(
        thesis_id="T1",
        symbol="ZZQ",
        book="swing",
        direction="long",
        status="pending_entry",
        cycle_id="C1",
        narrative="x",
        expected_return_pct=3.0,
        horizon_days=10,
        conviction=0.6,
        quantity=Decimal(5),
        stop_pct=4.0,
        target_pct=7.0,
        created_at=now,
        updated_at=now,
    )
    await store.insert_thesis(t)
    assert [x.thesis_id for x in await store.open_theses()] == ["T1"]
    updated = await store.update_thesis("T1", now + timedelta(minutes=1), status="closed")
    assert updated and updated.status == "closed" and updated.updated_at > now
    assert await store.open_theses() == [] and await store.update_thesis("nope", now) is None
    with pytest.raises(ValueError):
        await store.update_thesis("T1", now, symbol="X")


# ---- one session through the real agents ----


async def seed(db: Database) -> None:
    clock = SimClock(datetime.combine(DAY, datetime.min.time(), tzinfo=UTC) - timedelta(days=1))
    sessions = [d for d in calendar.last_sessions(DAY - timedelta(days=1), 30) if d < DAY]
    daily = []
    for i, d in enumerate(sessions):
        ts = datetime.combine(d, datetime.min.time(), tzinfo=UTC) + timedelta(hours=5)
        a = 90 + 10 * i / max(1, len(sessions) - 1)  # ZZRA trends up to 100: rank 1
        daily.append(bar("ZZRA", ts, f"{a:.2f}", f"{a:.2f}", "1d"))
        daily.append(bar("ZZRB", ts, "50", "50", "1d"))
    await db.insert_bars(daily, clock.now())
    minutes = []
    n = int((CLOSE - OPEN).total_seconds() // 60)
    prev_a, prev_b = Decimal(100), Decimal(50)
    for i in range(n):
        ts = OPEN + timedelta(minutes=i)
        a = (Decimal(100) + Decimal(8) * Decimal(i) / Decimal(n)).quantize(Decimal("0.01"))
        b = (Decimal(50) + Decimal("0.05") * (i % 3)).quantize(Decimal("0.01"))
        minutes.append(bar("ZZRA", ts, str(prev_a), str(a)))
        minutes.append(bar("ZZRB", ts, str(prev_b), str(b)))
        prev_a, prev_b = a, b
    await db.insert_bars(minutes, clock.now())
    news_clock = SimClock(
        datetime.combine(DAY, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)
    )
    for symbol in ("ZZRA", "ZZRB"):
        for k in (1, 2):
            await db.insert_event(
                make_event(
                    news_clock,
                    type=Topics.NEWS_ARTICLE_SCORED,
                    symbol=symbol,
                    payload={
                        "article_id": f"{symbol}-N{k}",
                        "relevance": 0.9,
                        "polarity": 0.4,
                        "confidence": 0.8,
                        "event_type": "product",
                        "impact": "medium",
                        "horizon": "days",
                        "model": "test",
                        "prompt_version": "v1",
                    },
                )
            )


async def cleanup(db: Database) -> None:
    async with db.pool.connection() as conn:
        await conn.execute("DELETE FROM bars WHERE symbol IN ('ZZRA', 'ZZRB')")
        await conn.execute("DELETE FROM events WHERE source = %s", (SOURCE,))


@pytest.fixture
async def seeded_db(database_url):
    db = Database(database_url)
    await db.open()
    await db.migrate()
    await cleanup(db)
    await seed(db)
    try:
        yield db
    finally:
        await cleanup(db)
        await db.close()


SETTINGS = Settings(
    _env_file=None,
    core_universe=["ZZRA", "ZZRB"],
    position_pct=Decimal("0.02"),
    max_open_positions=10,
    max_new_per_cycle=3,
    cycle_every_minutes=0,
)


async def test_a_session_replays_through_the_agents_with_simulated_fills(seeded_db):
    desk = await build_replay(SETTINGS, seeded_db, DAY, capital=Decimal(100000))
    report = await desk.run_day(DAY)
    assert [c["trigger"] for c in report.cycles] == ["pre_market", "mid_session", "post_close"]
    assert not any(c["failed"] for c in report.cycles), report.render()
    buys = [f for f in report.fills if f["side"] == "buy" and f["symbol"] == "ZZRA"]
    assert buys and buys[0]["qty"] == "20", report.render()  # 2% of 100k at about 100 a share
    assert buys[0]["at"] == OPEN.strftime("%H:%M")  # queued pre-market, filled on the first bar
    sells = [f for f in report.fills if f["side"] == "sell" and f["symbol"] == "ZZRA"]
    assert sells, report.render()  # the thesis monitor took the 7% target during the day
    closed = [t for t in report.theses if t["symbol"] == "ZZRA" and t["status"] == "closed"]
    assert (
        closed
        and closed[0]["reason"].startswith("target")
        and Decimal(closed[0]["realized_pnl"]) > 0
    )
    assert report.costs > 0 and report.equity_end > report.equity_start
    assert report.kill_switch == []
    assert report.bars_fed == 2 * int((CLOSE - OPEN).total_seconds() // 60)
    # with the feed healthy, the mid-session cycle adds the second name
    assert [f for f in report.fills if f["symbol"] == "ZZRB" and f["side"] == "buy"], (
        report.render()
    )


async def test_a_feed_gap_in_replay_engages_the_kill_switch(seeded_db):
    gap = (OPEN + timedelta(minutes=20), OPEN + timedelta(minutes=35))
    desk = await build_replay(SETTINGS, seeded_db, DAY, capital=Decimal(100000), feed_gap=gap)
    report = await desk.run_day(DAY)
    assert report.minutes_skipped == 15
    assert report.kill_switch and "feed stale" in report.kill_switch[0]
    # the merge turns every new buy into nothing while the switch is engaged, before any order exists
    mid = next(c for c in report.cycles if c["trigger"] == "mid_session")
    blocked = [a for a in mid["actions"] if a["symbol"] == "ZZRB"]
    assert blocked and blocked[0]["action"] == "none" and "kill switch" in blocked[0]["note"]
    assert not [f for f in report.fills if f["symbol"] == "ZZRB"], report.render()
