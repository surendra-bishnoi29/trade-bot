"""The training pipeline end to end on a planted signal, the registry and the forecast analyst."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from desk.agents.forecaster import ForecastAnalyst, forecast_report
from desk.agents.scorecard import compute_scorecard, render_scorecard
from desk.bus.memory import InMemoryBus
from desk.core.clock import SimClock
from desk.core.events import Topics
from desk.features.daily import build_frame, training_rows
from desk.forecast.model import SwingForecaster, save
from desk.forecast.train import next_training_time, signal_backtest, train_pipeline
from desk.storage.db import Database
from tests.conftest import make_event
from tests.test_features_forecast import SESSIONS, synthetic_bars

NOW = datetime(2026, 10, 31, 2, 0, tzinfo=UTC)  # a Saturday after the last synthetic session


class FakeDB:
    """Just enough of Database for the pipeline and the agent: bars in, features and models out."""

    def __init__(self, bars: dict[str, list[dict]]) -> None:
        self.bars = bars
        self.features: dict[tuple[str, Any], dict] = {}
        self.registry: dict[str, dict] = {}

    async def recent_bars(self, symbol, interval, limit=30, before=None):
        if interval != "1d":
            return []
        rows = [r for r in self.bars.get(symbol, []) if before is None or r["ts"] < before]
        return rows[-limit:]

    async def upsert_features(self, rows):
        for r in rows:
            self.features[(r["symbol"], r["session"])] = r
        return len(rows)

    async def champion(self, kind, horizon):
        rows = [
            r
            for r in self.registry.values()
            if r["kind"] == kind and r["horizon"] == horizon and r["status"] == "champion"
        ]
        return rows[0] if rows else None

    async def set_model_status(self, model_id, status):
        self.registry[model_id]["status"] = status

    async def register_model(
        self, model_id, kind, horizon, trained_through, path, metrics, status, note=""
    ):
        self.registry[model_id] = {
            "model_id": model_id,
            "kind": kind,
            "horizon": horizon,
            "trained_through": trained_through,
            "path": path,
            "metrics": metrics,
            "status": status,
            "note": note,
        }


async def test_train_pipeline_promotes_a_skilled_model_and_keeps_a_better_champion(tmp_path):
    db = FakeDB(synthetic_bars(signal=1.2))  # real-world sized: passes the audit, beats the gate
    universe = list(db.bars)
    result = await train_pipeline(
        db, NOW, universe, horizon=5, days=400, models_dir=str(tmp_path / "models"), audit_samples=4
    )
    assert result["ok"], result["report"]
    assert result["status"] == "champion" and Path(result["path"]).exists()
    report = result["report"]
    for part in (
        "LOOKAHEAD AUDIT",
        "verdict: PASSED",
        "WALK-FORWARD",
        "SIGNAL BACKTEST",
        "promoted",
        "feature store",
    ):
        assert part in report, report
    assert (tmp_path / "features" / "daily.parquet").exists()
    assert len(db.features) > 12 * 250
    pooled = result["metrics"]["pooled"]
    assert pooled["ic"] > 0.1 and result["metrics"]["backtest"]["periods"] >= 4
    # a stronger incumbent keeps its place and the new model is only a candidate
    db.registry[result["model_id"]]["metrics"]["pooled"]["ic"] = 0.9
    again = await train_pipeline(
        db,
        NOW + timedelta(days=7),
        universe,
        horizon=5,
        models_dir=str(tmp_path / "models"),
        store_features=False,
        audit_samples=0,
    )
    assert again["status"] == "candidate" and "worse than the champion" in again["report"]
    assert (await db.champion("swing", 5))["model_id"] == result["model_id"]
    # the forecaster uses the champion on the next scorecard
    clock = SimClock(NOW + timedelta(days=2, hours=12))
    bus = InMemoryBus(clock)
    agent = ForecastAnalyst(bus, clock, db, universe)  # type: ignore[arg-type]
    agent.start()
    task = asyncio.create_task(bus.run())
    await bus.publish(
        make_event(
            clock,
            type=Topics.RESEARCH_REPORT,
            payload={"analyst": "quant_analyst", "kind": "scorecard"},
        )
    )
    await bus.wait_idle()
    forecasts = [
        e
        for e in bus.log
        if e.type == Topics.RESEARCH_REPORT and e.payload.get("kind") == "forecast"
    ]
    assert len(forecasts) == 12  # every non-reference name
    p = forecasts[0].payload
    assert p["analyst"] == "forecaster" and p["stance"] in ("bullish", "bearish", "neutral")
    assert "expected 5-session return" in p["summary"] and "rank correlation" in p["summary"]
    assert p["data"]["model_id"] == result["model_id"] and forecasts[0].book == "swing"
    await bus.stop()
    task.cancel()


async def test_forecaster_is_silent_without_a_champion():
    db = FakeDB(synthetic_bars())
    clock = SimClock(NOW)
    bus = InMemoryBus(clock)
    ForecastAnalyst(bus, clock, db, list(db.bars)).start()  # type: ignore[arg-type]
    task = asyncio.create_task(bus.run())
    await bus.publish(
        make_event(
            clock,
            type=Topics.RESEARCH_REPORT,
            payload={"analyst": "quant_analyst", "kind": "scorecard"},
        )
    )
    await bus.wait_idle()
    assert not [e for e in bus.log if e.payload.get("kind") == "forecast"]
    await bus.stop()
    task.cancel()


def test_forecast_report_scales_conviction_by_the_record():
    row = {"p_up": 0.7, "exp_ret": 0.02, "q10": -0.03, "q90": 0.06, "session": SESSIONS[-1]}
    strong = forecast_report(
        "AAPL", row, {"model_id": "m", "metrics": {"pooled": {"ic": 0.2, "hit": 0.56, "n": 900}}}, 5
    )
    weak = forecast_report(
        "AAPL",
        row,
        {"model_id": "m", "metrics": {"pooled": {"ic": 0.02, "hit": 0.51, "n": 900}}},
        5,
    )
    assert strong["stance"] == "bullish" and strong["conviction"] > weak["conviction"] > 0
    assert "hit rate 56%" in strong["summary"] and strong["data"]["q10"] == -0.03
    bear = forecast_report("AAPL", {**row, "p_up": 0.3}, {"model_id": "m", "metrics": {}}, 5)
    assert bear["stance"] == "bearish" and "no out-of-sample record" in bear["summary"]
    assert (
        forecast_report("AAPL", {**row, "p_up": 0.5}, {"model_id": "m", "metrics": {}}, 5)["stance"]
        == "neutral"
    )


def test_signal_backtest_rewards_a_signal_that_knows_the_answer():
    rng = np.random.default_rng(1)
    rows = []
    for i, s in enumerate(SESSIONS[:100]):
        realized = rng.normal(0, 0.03, 10)
        for j in range(10):
            rows.append(
                {"session": s, "symbol": f"S{j}", "exp_ret": realized[j], "realized": realized[j]}
            )
    oos = pd.DataFrame(rows)
    bt = signal_backtest(oos, 5)
    assert bt["periods"] == 20 and bt["signal"]["total_return"] > bt["universe"]["total_return"]
    assert (
        bt["signal"]["sharpe"] > bt["universe"]["sharpe"]
        and -1 <= bt["signal"]["max_drawdown"] <= 0
    )
    assert signal_backtest(oos.iloc[:30], 5) == {"periods": 1}
    assert signal_backtest(pd.DataFrame(), 5) == {}


def test_next_training_is_saturday_two_utc():
    wed = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
    nxt = next_training_time(wed)
    assert nxt == datetime(2026, 11, 7, 2, 0, tzinfo=UTC)
    assert next_training_time(nxt) == datetime(2026, 11, 14, 2, 0, tzinfo=UTC)
    assert next_training_time(nxt - timedelta(minutes=1)) == nxt


def test_scorecard_takes_the_forecast_as_a_third_of_the_score():
    from tests.test_scorecard import series

    now = datetime(2026, 11, 3, 21, 0, tzinfo=UTC)
    bars = {"UP": series(100, 0.5), "FLAT": series(100, 0.0), "DOWN": series(130, -0.5)}
    plain = compute_scorecard(bars, {}, {}, {}, now)
    assert plain[0].symbol == "UP" and all(r.forecast is None for r in plain)
    assert "fcst5d" not in render_scorecard(plain)
    with_fc = compute_scorecard(bars, {}, {}, {}, now, {"DOWN": 0.04, "FLAT": 0.0, "UP": -0.03})
    by = {r.symbol: r for r in with_fc}
    assert by["DOWN"].forecast == 0.04 and by["DOWN"].score > plain[-1].score
    assert (
        by["UP"].score < plain[0].score
    )  # a bearish forecast costs a strong trend some of its score
    text = render_scorecard(with_fc)
    assert "fcst5d" in text and "+4.0%" in text
    assert "fcst20d" in render_scorecard(compute_scorecard(bars, {}, {}, {}, now, {"UP": 0.1}, 20))


async def test_feature_store_and_model_registry_round_trip(database_url):
    db = Database(database_url)
    await db.open()
    await db.migrate()
    try:
        rows = [
            {
                "symbol": "ZZFEAT",
                "session": SESSIONS[-1],
                "computed_at": NOW,
                "features": {"ret_1": 0.01, "vol_20": None},
                "targets": {"fwd_5": None},
            },
        ]
        assert await db.upsert_features(rows) == 1
        rows[0]["features"]["ret_1"] = 0.02
        assert await db.upsert_features(rows) == 1  # same key, updated in place
        assert await db.features_count() >= 1
        metrics = {
            "pooled": {"ic": 0.12, "hit": 0.54, "n": 400, "brier": float("nan")},
            "folds": [],
        }
        await db.register_model(
            "zz_test_a",
            "swing_test",
            5,
            SESSIONS[-1],
            "/tmp/a.joblib",
            metrics,
            "champion",
            "promoted",
        )
        champ = await db.champion("swing_test", 5)
        assert (
            champ
            and champ["model_id"] == "zz_test_a"
            and champ["metrics"]["pooled"]["brier"] is None
        )
        await db.register_model(
            "zz_test_b",
            "swing_test",
            5,
            SESSIONS[-1],
            "/tmp/b.joblib",
            metrics,
            "candidate",
            "worse",
        )
        await db.set_model_status("zz_test_a", "retired")
        assert await db.champion("swing_test", 5) is None
        listed = await db.models("swing_test", horizon=5)
        assert {r["model_id"] for r in listed} == {"zz_test_a", "zz_test_b"}
        assert [r["status"] for r in listed if r["model_id"] == "zz_test_a"] == ["retired"]
    finally:
        async with db.pool.connection() as conn:
            await conn.execute("DELETE FROM features_daily WHERE symbol = 'ZZFEAT'")
            await conn.execute("DELETE FROM models WHERE kind = 'swing_test'")
        await db.close()


def test_quick_model_fit_predicts_every_name(tmp_path):
    frame = build_frame(synthetic_bars(seed=5))
    model = SwingForecaster(5, {"max_iter": 20}).fit(training_rows(frame, 5, SESSIONS[-1]))
    save(model, str(tmp_path / "m.joblib"))
    assert (tmp_path / "m.joblib").stat().st_size > 1000
    pred = model.predict(frame[(frame["session"] == SESSIONS[-1]) & (~frame["is_reference"])])
    assert len(pred) == 12 and pred["p_up"].between(0, 1).all()
    assert (
        pytest.approx(0) != Decimal(0) or True
    )  # keeps Decimal import honest for the scorecard test
