"""Collectors parse fixtures offline; the analysts built on them produce the expected reports."""

import asyncio
from datetime import UTC, date, datetime, timedelta

from desk.agents.fundamentals import FundamentalAnalyst, event_report, fundamental_report
from desk.agents.macro import MacroStrategist, macro_report
from desk.bus.memory import InMemoryBus
from desk.core.clock import SimClock
from desk.core.events import Topics
from desk.data.finnhub import parse_earnings, parse_metrics
from desk.data.fred import parse_observations, parse_release_dates
from desk.data.rss import parse_rss

NOW = datetime(2026, 11, 4, 11, 0, tzinfo=UTC)

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Yahoo</title>
<item><title>Chevron raises buyback</title><link>https://finance.yahoo.com/news/cvx-buyback.html</link>
<pubDate>Tue, 04 Nov 2026 09:30:00 +0000</pubDate><description>Chevron said it will...</description></item>
<item><title>No date item</title><link>https://example.com/x</link></item>
<item><title>Old item</title><link>https://example.com/old</link><pubDate>Mon, 03 Nov 2025 09:30:00 +0000</pubDate></item>
</channel></rss>"""


def test_rss_items_become_articles_with_stable_ids():
    items = parse_rss(RSS, "CVX")
    assert len(items) == 2 and items[0].title == "Chevron raises buyback"
    assert items[0].article_id.startswith("rss:") and items[0].source == "finance.yahoo.com"
    assert items[0].published_at == datetime(2026, 11, 4, 9, 30, tzinfo=UTC) and items[
        0
    ].symbols == ["CVX"]
    assert parse_rss(RSS, "CVX")[0].article_id == items[0].article_id  # same link, same id


def test_fred_parsing_and_the_macro_report():
    obs = parse_observations(
        {
            "observations": [
                {"date": "2026-10-01", "value": "4.10"},
                {"date": "2026-10-02", "value": "."},
                {"date": "2026-10-03", "value": "4.20"},
            ]
        }
    )
    assert obs == [(date(2026, 10, 1), 4.10), (date(2026, 10, 3), 4.20)]
    rel = parse_release_dates(
        {"release_dates": [{"date": "2026-11-05", "release_name": "Employment Situation"}]}
    )
    assert rel == [(date(2026, 11, 5), "Employment Situation")]
    days = [date(2026, 10, 1) + timedelta(days=i) for i in range(30)]
    data = {
        "DGS10": [(d, 4.0 + i * 0.01) for i, d in enumerate(days)],
        "DGS2": [(d, 3.8) for d in days],
        "T10Y2Y": [(d, 0.2) for d in days],
        "DFF": [(d, 4.33) for d in days],
        "VIXCLS": [(d, 28.0) for d in days],
        "DCOILWTICO": [(d, 60.0) for d in days],
        "UNRATE": [(date(2026, 9, 1), 4.3)],
        "CPIAUCSL": [
            (date(2025, 10, 1) + timedelta(days=30 * i), 300.0 + 0.8 * i) for i in range(13)
        ],
    }
    r = macro_report(data, [(date(2026, 11, 5), "Employment Situation")], NOW)
    assert r["stance"] == "bearish" and r["data"]["vix_label"] == "stressed"
    assert "10-year 4.29%" in r["summary"] and "curve positive at +0.20" in r["summary"]
    assert "Employment Situation tomorrow" in r["summary"] and "CPI" in r["summary"]
    calm = macro_report({"VIXCLS": [(days[-1], 12.0)], "DGS10": [(d, 4.0) for d in days]}, [], NOW)
    assert calm["stance"] == "bullish"


def test_finnhub_parsing_and_the_fundamental_and_event_reports():
    m = parse_metrics(
        {
            "metric": {
                "peTTM": 24.5,
                "revenueGrowthTTMYoy": 12.0,
                "netProfitMarginTTM": 18.2,
                "52WeekHigh": 250,
                "52WeekLow": 150,
                "beta": "1.1",
            }
        }
    )
    assert m["peTTM"] == 24.5 and m["beta"] == 1.1 and m["roeTTM"] is None
    r = fundamental_report("CVX", m, 230.0)
    assert (
        r["stance"] == "bullish"
        and "P/E 24.5" in r["summary"]
        and "80% of the way up" in r["summary"]
    )
    weak = fundamental_report("XYZ", {**m, "revenueGrowthTTMYoy": -3.0}, None)
    assert weak["stance"] == "bearish"
    e = parse_earnings(
        {
            "earningsCalendar": [
                {"symbol": "cvx", "date": "2026-11-06", "hour": "bmo", "epsEstimate": 2.1},
                {"symbol": "CVX", "date": "bad"},
            ]
        }
    )
    assert len(e) == 1 and e[0]["date"] == date(2026, 11, 6)
    ev = event_report("CVX", e, NOW)
    assert (
        ev
        and ev["kind"] == "event"
        and "Earnings on 2026-11-06 before the open (2 sessions away)" in ev["summary"]
    )
    assert (
        event_report("CVX", [{"symbol": "CVX", "date": date(2026, 10, 1), "hour": ""}], NOW) is None
    )


class FakeFred:
    async def observations(self, series_id, start):
        d = [date(2026, 10, 1) + timedelta(days=i) for i in range(25)]
        return [(x, 20.0) for x in d] if series_id == "VIXCLS" else [(x, 4.0) for x in d]

    async def release_dates(self, start, end):
        return [(start, "FOMC Press Release")]


class FakeFinnhub:
    async def metrics(self, symbol):
        return {
            "peTTM": 20.0,
            "revenueGrowthTTMYoy": 15.0,
            "netProfitMarginTTM": 20.0,
            "52WeekHigh": 100.0,
            "52WeekLow": 50.0,
        }

    async def earnings(self, symbol, start, end):
        return [
            {
                "symbol": symbol,
                "date": start + timedelta(days=5),
                "hour": "amc",
                "eps_estimate": 1.0,
            }
        ]


async def test_macro_and_fundamental_agents_publish_reports():
    clock = SimClock(NOW)
    bus = InMemoryBus(clock)
    task = asyncio.create_task(bus.run())
    macro = MacroStrategist(bus, clock, None, FakeFred())  # type: ignore[arg-type]
    await macro.run_once()
    fundamentals = FundamentalAnalyst(bus, clock, None, FakeFinnhub(), ["CVX", "SPY"])  # type: ignore[arg-type]
    fundamentals_task = asyncio.create_task(fundamentals.run_once())
    await asyncio.wait_for(fundamentals_task, timeout=10)
    await bus.wait_idle()
    kinds = [(e.payload.get("kind"), e.symbol) for e in bus.log if e.type == Topics.RESEARCH_REPORT]
    assert (
        ("macro", None) in kinds and ("fundamental", "CVX") in kinds and ("event", "CVX") in kinds
    )
    assert not any(s == "SPY" for _, s in kinds)  # references are not companies
    macro_ev = next(e for e in bus.log if e.payload.get("kind") == "macro")
    assert (
        "FOMC Press Release today" in macro_ev.payload["summary"]
        and macro_ev.payload["stance"] == "neutral"
    )
    await bus.stop()
    task.cancel()
