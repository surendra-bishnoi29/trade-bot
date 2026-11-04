"""The cost model, its calibration to fills, and the intraday baselines on planted sessions."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from desk.core.events import Event, Topics
from desk.research.costs import CostModel, FillObservation, calibrate, observed_fills, render
from desk.research.intraday import (
    STRATEGIES,
    evaluate,
    momentum,
    open_to_close,
    opening_range_breakout,
    session_frame,
    vwap_reversion,
)
from desk.research.intraday import render as render_intraday

SESSION = date(2026, 11, 3)
OPEN = datetime(2026, 11, 3, 14, 30, tzinfo=UTC)


def bars(path: list[float], volume: int = 1000, wick: float = 0.0005) -> list[dict]:
    """Minute bars that walk the given closes; the first open is the first close."""
    rows = []
    prev = path[0]
    for i, c in enumerate(path):
        o = prev
        hi, lo = max(o, c) * (1 + wick), min(o, c) * (1 - wick)
        rows.append(
            {
                "ts": OPEN + timedelta(minutes=i),
                "open": Decimal(str(round(o, 4))),
                "high": Decimal(str(round(hi, 4))),
                "low": Decimal(str(round(lo, 4))),
                "close": Decimal(str(round(c, 4))),
                "volume": volume,
                "vwap": None,
            }
        )
        prev = c
    return rows


def ramp(a: float, b: float, n: int) -> list[float]:
    return [a + (b - a) * i / max(1, n - 1) for i in range(n)]


def test_cost_model_arithmetic_and_calibration():
    m = CostModel.default()
    assert m.per_side_bps == 5.0 and m.round_trip == pytest.approx(0.001)
    assert m.side_cost(Decimal(10), Decimal(100)) == pytest.approx(0.5)
    assert CostModel.conservative().round_trip == pytest.approx(0.002)
    assert "default" in m.describe()
    buy = FillObservation("V", "buy", Decimal(4), Decimal("100.03"), Decimal(100), OPEN)
    sell = FillObservation("V", "sell", Decimal(4), Decimal("99.97"), Decimal(100), OPEN)
    assert buy.slippage_bps == pytest.approx(3.0) and sell.slippage_bps == pytest.approx(3.0)
    model, report = calibrate([])
    assert model == CostModel.default() and "no fills" in report["note"]
    model, report = calibrate([buy, sell])
    assert model.source == "default" and report["median_bps"] == pytest.approx(3.0)
    assert "fewer than" in report["note"]
    many = [buy] * 15 + [sell] * 10
    model, report = calibrate(many)
    assert model.per_side_bps == pytest.approx(3.0) and "calibrated on 25 fills" in model.source
    assert report["by_symbol"] == {"V": 3.0}
    text = render(model, report)
    assert "COST MODEL" in text and "25" in text and "buys: median +3.00" in text
    improved = [FillObservation("V", "buy", Decimal(1), Decimal("99.9"), Decimal(100), OPEN)] * 25
    assert calibrate(improved)[0].per_side_bps == pytest.approx(0.5)  # floored


class FakeDB:
    def __init__(self, fills, intents, bars_by_minute):
        self.fills, self.intents, self.bars_by_minute = fills, intents, bars_by_minute

    async def recent_events(self, types, since, limit=100, until=None):
        return self.fills if Topics.ORDER_FILLED in types else self.intents

    async def bars_between(self, symbols, interval, start, end):
        return [b for b in self.bars_by_minute if start <= b["ts"] < end and b["symbol"] in symbols]

    async def recent_bars(self, symbol, interval, limit=1, before=None):
        rows = [b for b in self.bars_by_minute if b["symbol"] == symbol and b["ts"] < before]
        return rows[-limit:]


def ev(type: str, when: datetime, symbol: str, payload: dict) -> Event:
    return Event(
        type=type,
        occurred_at=when,
        ingested_at=when,
        source="test@0.1",
        symbol=symbol,
        payload=payload,
    )


async def test_observed_fills_use_the_arrival_bar_and_fall_back_to_the_prior_one(clock):
    t = clock.now().replace(second=0, microsecond=0)
    fill = ev(
        Topics.ORDER_FILLED,
        t + timedelta(seconds=20),
        "V",
        {
            "order_id": "o1",
            "intent_id": "i1",
            "status": "filled",
            "filled_quantity": "4",
            "avg_price": "100.05",
        },
    )
    late = ev(
        Topics.ORDER_FILLED,
        t + timedelta(minutes=3, seconds=10),  # no bar for that minute
        "V",
        {
            "order_id": "o2",
            "intent_id": "i2",
            "status": "filled",
            "filled_quantity": "4",
            "avg_price": "101",
            "side": "sell",
        },
    )
    intents = [
        ev(Topics.ORDER_INTENT, t - timedelta(hours=1), "V", {"intent_id": "i1", "side": "buy"})
    ]
    minute_bars = [
        {"symbol": "V", "ts": t, "open": Decimal(100), "close": Decimal("100.5")},
        {
            "symbol": "V",
            "ts": t + timedelta(minutes=1),
            "open": Decimal("100.5"),
            "close": Decimal("101.2"),
        },
    ]
    obs = await observed_fills(FakeDB([fill, late], intents, minute_bars), t - timedelta(days=1))
    assert len(obs) == 2
    assert obs[0].side == "buy" and obs[0].reference == Decimal(100)
    assert obs[0].slippage_bps == pytest.approx(5.0)
    assert obs[1].side == "sell" and obs[1].reference == Decimal("101.2")
    assert obs[1].slippage_bps == pytest.approx((1 - 101 / 101.2) * 1e4)


def test_session_frame_running_vwap():
    df = session_frame(bars([100, 102, 104] + [104] * 130, volume=1000))
    assert len(df) == 133 and df["vwap"].iloc[0] == pytest.approx(
        (100 * 1.0005 + 100 * 0.9995 + 100) / 3
    )
    assert df["vwap"].iloc[2] < 104 and df["vwap"].is_monotonic_increasing


def test_opening_range_breakout_long_target_and_short_stop():
    # a quiet first quarter hour between 100 and 100.2, then a rally through 101.2 (one range) and beyond
    path = [100 + 0.2 * (i % 2) for i in range(15)] + ramp(100.2, 103, 100) + [103] * 100
    t = opening_range_breakout(session_frame(bars(path, wick=0)), "A", SESSION)
    assert t and t.side == "long" and t.reason == "target"
    assert t.entry == pytest.approx(100.2, abs=0.03) and t.gross == pytest.approx(
        0.2 / 100.2, rel=0.2
    )
    # a break down that reverses back through the midpoint: short, stopped
    path = (
        [100 + 0.2 * (i % 2) for i in range(15)]
        + ramp(100.0, 99.9, 10)
        + ramp(99.9, 101, 40)
        + [101] * 150
    )
    t = opening_range_breakout(session_frame(bars(path, wick=0)), "A", SESSION)
    assert t and t.side == "short" and t.reason == "stop" and t.gross == pytest.approx(-0.001)
    assert (
        opening_range_breakout(session_frame(bars([100] * 200)), "A", SESSION) is None
    )  # no range
    assert (
        opening_range_breakout(session_frame(bars([100] * 50)), "A", SESSION) is None
    )  # too short


def test_vwap_reversion_fades_a_dip_back_to_vwap_and_momentum_rides_the_trend():
    # flat for 40 minutes, a 1% dip over 10 minutes, then a recovery to the start: long at the low, out at VWAP
    path = [100] * 40 + ramp(100, 99, 10) + ramp(99, 100.5, 60) + [100.5] * 150
    t = vwap_reversion(session_frame(bars(path, wick=0)), "A", SESSION)
    assert t and t.side == "long" and t.reason == "vwap" and t.gross > 0
    # a steady climb of 1.5% in the first hour that continues: momentum long to the close
    path = ramp(100, 101.5, 60) + ramp(101.5, 102.5, 200)
    m = momentum(session_frame(bars(path, wick=0)), "A", SESSION)
    assert (
        m
        and m.side == "long"
        and m.reason == "close"
        and m.gross == pytest.approx(102.5 / m.entry - 1)
    )
    # a steady fall that keeps falling: short
    path = ramp(100, 98.8, 60) + ramp(98.8, 98, 200)
    m = momentum(session_frame(bars(path, wick=0)), "A", SESSION)
    assert m and m.side == "short" and m.gross > 0
    assert momentum(session_frame(bars([100] * 260)), "A", SESSION) is None
    ref = open_to_close(session_frame(bars(ramp(100, 101, 260))), "A", SESSION)
    assert ref and ref.gross == pytest.approx(0.01, rel=0.05)


def test_evaluate_reports_every_strategy_after_costs():
    good = ramp(100, 101.5, 60) + ramp(101.5, 102.5, 200)
    flat = [100 + 0.05 * ((i // 7) % 2) for i in range(260)]
    data = {("A", SESSION): bars(good, wick=0), ("B", SESSION): bars(flat, wick=0)}
    data[("A", date(2026, 11, 4))] = bars(flat, wick=0)
    result = evaluate(data, CostModel.default())
    assert result["sessions"] == 2 and result["symbols"] == ["A", "B"]
    assert set(result["strategies"]) == set(STRATEGIES)
    mom = result["strategies"]["momentum"]
    assert mom["trades"] == 1 and mom["avg_net_bps"] == pytest.approx(mom["avg_gross_bps"] - 10)
    assert mom["total_net"] > 0 and mom["exits"] == {"close": 1}
    ref = result["strategies"]["open_to_close"]
    assert ref["trades"] == 3 and ref["win_rate"] <= 1
    text = render_intraday(result)
    assert "INTRADAY BASELINES" in text and "momentum" in text and "open_to_close" in text
    assert evaluate({}, CostModel.default())["strategies"]["momentum"] == {"trades": 0}
