"""The research team: deterministic analysts, their reports, and what the head trader reads."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from desk.agents.research import (
    NewsAnalyst,
    RegimeStrategist,
    SectorSpecialist,
    news_state,
    regime_report,
    research_digest,
    sector_report,
    technical_report,
)
from desk.agents.scorecard import ScoreRow
from desk.bus.memory import InMemoryBus
from desk.core.clock import SimClock
from desk.core.config import DEFAULT_UNIVERSE
from desk.core.events import Topics
from desk.core.sectors import SECTORS, sector_of
from desk.data.edgar import impact_for, parse_submissions, ticker_to_cik
from desk.storage.db import Database
from tests.conftest import make_event as _make_event

NOW = datetime(2026, 11, 4, 13, 0, tzinfo=UTC)
SOURCE = "researchtest@0.1"


def make_event(clock, **kw):
    return _make_event(clock, **kw).model_copy(update={"source": SOURCE})


def test_every_name_in_the_universe_has_a_sector():
    assert all(s in SECTORS for s in DEFAULT_UNIVERSE)
    assert sector_of("cvx") == "Energy" and sector_of("ZZZ") == "Other"


def test_technical_report_reads_the_scorecard_row():
    row = ScoreRow(
        symbol="CVX",
        last_close=Decimal("226.06"),
        ret_5d=-0.011,
        ret_20d=0.087,
        vol_20d=0.22,
        rvol=1.3,
        dist_high_20d=-0.046,
        dist_low_20d=0.102,
        sma20_rel=0.009,
        rank=3,
    )
    r = technical_report(row, 30)
    assert r["stance"] == "bullish" and r["symbol"] == "CVX" and r["kind"] == "technical"
    assert (
        "uptrend: +8.7% over 20 days" in r["summary"] and "volatility 22% (normal)" in r["summary"]
    )
    assert 0.3 <= r["conviction"] <= 0.9 and r["data"]["rank"] == 3


def ranked_rows(spy_sma=0.01, breadth_up=24):
    rows = [
        {"symbol": "SPY", "ret_5d": 0.005, "ret_20d": 0.02, "sma20_rel": spy_sma, "vol_20d": 0.12},
        {"symbol": "QQQ", "ret_5d": 0.008, "ret_20d": 0.03, "sma20_rel": 0.012, "vol_20d": 0.18},
    ]
    names = [s for s in DEFAULT_UNIVERSE if s not in ("SPY", "QQQ")]
    for i, s in enumerate(names):
        up = i < breadth_up
        rows.append(
            {
                "symbol": s,
                "ret_5d": 0.01 if up else -0.01,
                "ret_20d": 0.04 if up else -0.03,
                "sma20_rel": 0.015 if up else -0.02,
                "vol_20d": 0.2,
            }
        )
    return rows


def test_regime_and_sector_reports():
    on = regime_report(ranked_rows())
    assert on["stance"] == "bullish" and on["data"]["label"] == "risk-on"
    assert "SPY +2.0% over 20 days" in on["summary"] and "low" in on["summary"]
    off = regime_report(ranked_rows(spy_sma=-0.01, breadth_up=5))
    assert off["data"]["label"] == "risk-off" and off["stance"] == "bearish"
    mixed = regime_report(ranked_rows(spy_sma=0.01, breadth_up=14))
    assert mixed["data"]["label"] == "mixed"
    sectors = sector_report(ranked_rows(breadth_up=4))  # the first four names (IT) lead
    assert sectors["data"]["leaders"][0] == "Information Technology"
    assert (
        "Leading: Information Technology" in sectors["summary"] and "Lagging:" in sectors["summary"]
    )
    assert not any(t["sector"] == "Index" for t in sectors["data"]["sectors"])


def test_news_state_aggregates_scores_into_a_stance():
    def item(h, pol, imp="medium", rel=0.8, title="t"):
        return {
            "article_id": f"a{h}",
            "at": NOW - timedelta(hours=h),
            "polarity": pol,
            "relevance": rel,
            "impact": imp,
            "confidence": 0.9,
            "event_type": "product",
            "title": title,
            "source": "benzinga",
        }

    quiet = news_state("CVX", [], NOW)
    assert quiet["stance"] == "neutral" and quiet["summary"].startswith("No news")
    bullish = news_state("CVX", [item(2, 0.6, "high", title="Big deal"), item(30, 0.4)], NOW)
    assert bullish["stance"] == "bullish" and bullish["data"]["count_24h"] == 1
    assert (
        bullish["data"]["count_48h"] == 2 and bullish["data"]["headlines"][0]["title"] == "Big deal"
    )
    assert "Big deal" in bullish["summary"] and bullish["evidence_ids"][0] == "a2"
    old = news_state("CVX", [item(60, 0.9)], NOW)  # older than 48 hours: not current news
    assert old["data"]["count_48h"] == 0
    bearish = news_state("CVX", [item(1, -0.7), item(3, -0.5)], NOW)
    assert bearish["stance"] == "bearish" and bearish["conviction"] > 0


async def test_strategists_react_to_the_scorecard_and_the_news_analyst_to_scores():
    clock = SimClock(NOW)
    bus = InMemoryBus(clock)
    RegimeStrategist(bus, clock).start()
    SectorSpecialist(bus, clock).start()
    analyst = NewsAnalyst(bus, clock, None, debounce=timedelta(minutes=5))
    analyst.start()
    task = asyncio.create_task(bus.run())
    await bus.publish(
        make_event(
            clock,
            type=Topics.RESEARCH_REPORT,
            payload={
                "analyst": "quant_analyst",
                "kind": "scorecard",
                "ranked": ranked_rows(),
                "top": ["AAPL"],
            },
        )
    )
    await bus.publish(
        make_event(
            clock,
            type=Topics.NEWS_ARTICLE_RAW,
            symbol="CVX",
            payload={
                "article_id": "n1",
                "published_at": (NOW - timedelta(hours=1)).isoformat(),
                "source": "benzinga",
                "title": "Chevron agrees West Qurna-2 terms",
                "symbols": ["CVX"],
            },
        )
    )
    for aid, pol in (("n1", 0.5), ("n2", 0.3)):
        await bus.publish(
            make_event(
                clock,
                type=Topics.NEWS_ARTICLE_SCORED,
                symbol="CVX",
                payload={
                    "article_id": aid,
                    "relevance": 0.9,
                    "polarity": pol,
                    "confidence": 0.9,
                    "event_type": "deal",
                    "impact": "medium",
                    "horizon": "days",
                    "model": "test",
                    "prompt_version": "v1",
                },
            )
        )
    await bus.wait_idle()
    kinds = [e.payload.get("kind") for e in bus.log if e.type == Topics.RESEARCH_REPORT]
    assert (
        "regime" in kinds and "sector" in kinds and kinds.count("news") == 1
    )  # second score debounced
    assert "CVX" in analyst.pending
    news = next(
        e for e in bus.log if e.type == Topics.RESEARCH_REPORT and e.payload.get("kind") == "news"
    )
    assert news.symbol == "CVX" and "West Qurna-2" in news.payload["summary"]
    states = [e for e in bus.log if e.type == Topics.NEWS_STATE]
    assert states and states[0].payload["count_24h"] == 1
    clock.advance_to(NOW + timedelta(minutes=6))
    await analyst._publish("CVX", None)
    await bus.wait_idle()
    assert [e.payload.get("kind") for e in bus.log if e.type == Topics.RESEARCH_REPORT].count(
        "news"
    ) == 2
    await bus.stop()
    task.cancel()


def test_edgar_parsing_and_impact():
    tickers = ticker_to_cik(
        {
            "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple"},
            "1": {"cik_str": 1067983, "ticker": "BRK-B", "title": "Berkshire"},
        }
    )
    assert tickers == {"AAPL": "0000320193", "BRK.B": "0001067983"}
    data = {
        "filings": {
            "recent": {
                "accessionNumber": [
                    "0000320193-26-000101",
                    "0000320193-26-000090",
                    "0000320193-26-000080",
                ],
                "filingDate": ["2026-11-03", "2026-10-31", "2026-10-30"],
                "acceptanceDateTime": [
                    "2026-11-03T21:05:12.000Z",
                    "2026-10-31T20:10:00.000Z",
                    "2026-10-30T12:00:00.000Z",
                ],
                "form": ["8-K", "10-Q", "4"],
                "items": ["2.02,9.01", "", ""],
                "primaryDocument": ["aapl-8k.htm", "aapl-10q.htm", "form4.xml"],
                "primaryDocDescription": ["8-K", "10-Q", "FORM 4"],
            }
        }
    }
    filings = parse_submissions(data, "AAPL", "0000320193")
    assert [f.form for f in filings] == ["8-K", "10-Q"]  # insider forms are not watched
    f = filings[0]
    assert f.items == ["2.02", "9.01"] and f.filed_at == datetime(
        2026, 11, 3, 21, 5, 12, tzinfo=UTC
    )
    assert f.url == "https://www.sec.gov/Archives/edgar/data/320193/000032019326000101/aapl-8k.htm"
    assert impact_for("8-K", ["2.02", "9.01"]) == ("high", "results of operations")
    assert impact_for("8-K", ["7.01", "5.02"]) == (
        "medium",
        "Regulation FD disclosure; officer or director change",
    )
    assert impact_for("8-K", []) == ("low", "8-K with no itemised content")
    assert impact_for("10-Q", []) == ("medium", "quarterly report")


@pytest.mark.usefixtures("database_url")
async def test_research_digest_lists_the_latest_report_per_analyst_and_name(database_url):
    db = Database(database_url)
    await db.open()
    await db.migrate()
    clock = SimClock(NOW - timedelta(minutes=30))
    try:
        await db.insert_event(
            make_event(clock, type=Topics.RESEARCH_REPORT, payload=regime_report(ranked_rows()))
        )
        await db.insert_event(
            make_event(
                clock,
                type=Topics.RESEARCH_REPORT,
                symbol="CVX",
                payload={
                    "analyst": "quant_analyst",
                    "kind": "technical",
                    "symbol": "CVX",
                    "stance": "bullish",
                    "conviction": 0.7,
                    "summary": "old read",
                },
            )
        )
        clock.advance_to(NOW - timedelta(minutes=10))
        newer = make_event(
            clock,
            type=Topics.RESEARCH_REPORT,
            symbol="CVX",
            payload={
                "analyst": "quant_analyst",
                "kind": "technical",
                "symbol": "CVX",
                "stance": "neutral",
                "conviction": 0.4,
                "summary": "new read",
            },
        )
        await db.insert_event(newer)
        text = await research_digest(db, NOW, ["CVX", "LLY"])
        assert "regime regime: bullish" in text and "new read" in text and "old read" not in text
        assert newer.event_id in text
        assert json.dumps(text)  # plain text, safe to embed
    finally:
        async with db.pool.connection() as conn:
            await conn.execute("DELETE FROM events WHERE source = %s", (SOURCE,))
        await db.close()
