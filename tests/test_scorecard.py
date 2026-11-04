from datetime import UTC, datetime, timedelta
from decimal import Decimal

from desk.agents.scorecard import compute_scorecard, render_scorecard

NOW = datetime(2026, 11, 3, 21, 0, tzinfo=UTC)


def series(start: float, step: float, n: int = 60, volume: int = 1000) -> list[dict]:
    rows = []
    for i in range(n):
        c = Decimal(str(round(start + step * i, 2)))
        rows.append(
            {
                "ts": NOW - timedelta(days=n - i),
                "open": c,
                "high": c + 1,
                "low": c - 1,
                "close": c,
                "volume": volume,
            }
        )
    return rows


def test_metrics_ranking_and_news_blend():
    bars = {
        "UP": series(100, 1.0),
        "DOWN": series(100, -0.5),
        "FLAT": series(100, 0.0),
        "SHORT": series(100, 0.0, n=3),
    }
    news = {
        "FLAT": [
            {
                "polarity": 0.8,
                "relevance": 1.0,
                "confidence": 0.9,
                "impact": "high",
                "occurred_at": NOW - timedelta(hours=3),
            }
        ]
    }
    rows = compute_scorecard(bars, news, {"DOWN": 2}, {"DOWN": (Decimal(5), Decimal(80))}, NOW)
    by = {r.symbol: r for r in rows}
    assert by["UP"].ret_20d > 0 > by["DOWN"].ret_20d and by["FLAT"].ret_20d == 0
    assert by["UP"].rank == 1 and by["SHORT"].ret_20d is None and by["SHORT"].vol_20d is None
    assert (
        by["FLAT"].news_1d > 0.5
        and by["FLAT"].news_count_5d == 1
        and by["FLAT"].news_max_impact_5d == "high"
    )
    assert (
        by["DOWN"].alerts_24h == 2
        and by["DOWN"].position_qty == 5
        and by["DOWN"].position_pnl_pct < 0
    )
    assert by["FLAT"].rank < by["DOWN"].rank  # positive news lifts a flat name above a falling one
    text = render_scorecard(rows, top_n=2)
    assert (
        text.splitlines()[0].startswith("rank symbol") and "DOWN" in text
    )  # held names always shown
    assert "UP" in text and "SHORT" not in text
