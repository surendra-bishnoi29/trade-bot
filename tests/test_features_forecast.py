"""Feature store, lookahead audit and the forecaster on synthetic bars with a planted signal."""

import itertools
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd

from desk.core import calendar
from desk.features.audit import audit, correlation_check, recompute_check
from desk.features.daily import FEATURES, TARGETS, build_frame, latest_rows, training_rows
from desk.forecast.model import (
    SwingForecaster,
    promotion_gate,
    train_final,
    walk_forward,
    walk_forward_folds,
)

SESSIONS = calendar.last_sessions(date(2026, 10, 30), 320)


def synthetic_bars(seed: int = 3, n_symbols: int = 12, signal: float = 0.0) -> dict:
    """Random walks; with `signal` > 0, a name's 20-day momentum predicts its next 5 days."""
    rng = np.random.default_rng(seed)
    bars: dict[str, list[dict]] = {}
    symbols = ["SPY"] + [f"S{i:02d}" for i in range(n_symbols)]
    market = rng.normal(0.0003, 0.009, len(SESSIONS))
    for s in symbols:
        noise = rng.normal(0, 0.012, len(SESSIONS))
        rets = np.zeros(len(SESSIONS))
        closes = [100.0]
        for i in range(len(SESSIONS)):
            mom = (closes[-1] / closes[-21] - 1) if len(closes) > 21 else 0.0
            # a bounded momentum effect: at most 0.3% a day, so the walk never explodes
            r = (
                0.6 * market[i]
                + noise[i]
                + (signal * 0.003 * np.tanh(mom / 0.1) if s != "SPY" else 0.0)
            )
            rets[i] = r
            closes.append(closes[-1] * (1 + r))
        closes = closes[1:]
        rows = []
        for i, d in enumerate(SESSIONS):
            c = closes[i]
            o = closes[i - 1] * (1 + rng.normal(0, 0.002)) if i else c
            rows.append(
                {
                    "ts": datetime.combine(d, datetime.min.time(), tzinfo=UTC) + timedelta(hours=5),
                    "open": o,
                    "high": max(o, c) * (1 + abs(rng.normal(0, 0.003))),
                    "low": min(o, c) * (1 - abs(rng.normal(0, 0.003))),
                    "close": c,
                    "volume": int(rng.integers(1_000_000, 3_000_000)),
                }
            )
        bars[s] = rows
    return bars


def test_frame_has_point_in_time_features_and_future_targets():
    bars = synthetic_bars()
    frame = build_frame(bars)
    assert set(FEATURES + TARGETS).issubset(frame.columns)
    last = frame["session"].max()
    tail = frame[frame["session"] == last]
    assert (
        tail["fwd_5"].isna().all() and tail["ret_5"].notna().all()
    )  # the future is unknown, the past is not
    assert frame[frame["symbol"] == "SPY"]["is_reference"].all()
    row = frame[(frame["symbol"] == "S01") & (frame["session"] == SESSIONS[100])].iloc[0]
    assert (
        abs(
            row["mkt_ret_20"]
            - frame[(frame["symbol"] == "SPY") & (frame["session"] == SESSIONS[100])].iloc[0][
                "ret_20"
            ]
        )
        < 1e-12
    )
    assert 0 < row["rank_ret_20"] <= 1 and 0 <= row["breadth_20"] <= 1
    as_of = SESSIONS[200]
    rows = training_rows(frame, 5, as_of)
    assert rows["session"].max() == SESSIONS[195] and rows["fwd_5"].notna().all()
    today = latest_rows(frame, as_of)
    assert len(today) == 12 and (today["session"] == as_of).all()


def test_lookahead_audit_passes_on_the_real_pipeline_and_catches_a_leak():
    bars = synthetic_bars()
    frame = build_frame(bars)
    assert recompute_check(bars, frame, samples=8) == []
    assert correlation_check(frame) == []
    leaky = frame.copy()
    leaky["ret_1"] = leaky["fwd_1"]  # a feature that is the answer
    assert correlation_check(leaky)
    report = audit(bars, frame)
    assert report["passed"] and report["sessions"] == len(SESSIONS)


def test_walk_forward_finds_a_planted_signal_and_nothing_in_noise():
    folds = walk_forward_folds(SESSIONS, warmup=120, step=21)
    assert folds and folds[0].train_as_of == SESSIONS[119] and folds[0].test_start == SESSIONS[120]
    assert all(f.test_end < g.test_start for f, g in itertools.pairwise(folds))
    signal = walk_forward(
        build_frame(synthetic_bars(signal=2.5)), 5, warmup=120, step=42, shuffle_control=True
    )
    assert signal.pooled["n"] > 300 and signal.pooled["ic"] > 0.1, signal.render()
    assert abs(signal.shuffled["ic"]) < 0.08, signal.render()
    noise = walk_forward(
        build_frame(synthetic_bars(seed=9)), 5, warmup=120, step=42, shuffle_control=False
    )
    assert abs(noise.pooled["ic"]) < 0.1, noise.render()
    text = signal.render()
    assert "WALK-FORWARD" in text and "calibration" in text and "shuffled-target control" in text


def test_final_model_predicts_a_distribution_and_the_gate_is_strict():
    frame = build_frame(synthetic_bars(signal=2.5))
    wf = walk_forward(frame, 5, warmup=120, step=42, shuffle_control=False)
    model = train_final(frame, 5, SESSIONS[-1], wf)
    assert isinstance(model, SwingForecaster) and model.calibrator is not None
    pred = model.predict(latest_rows(frame, SESSIONS[-1]))
    assert set(pred.columns) >= {"symbol", "exp_ret", "p_up", "q10", "q90"}
    assert (pred["q10"] <= pred["q90"]).all() and pred["p_up"].between(0, 1).all()
    ok, why = promotion_gate(wf.pooled, None)
    assert ok and why.startswith("promoted")
    assert not promotion_gate({"ic": -0.02, "hit": 0.49, "n": 500}, None)[0]
    assert not promotion_gate({"ic": 0.05, "hit": 0.55, "n": 50}, None)[0]
    assert not promotion_gate({"ic": 0.02, "hit": 0.52, "n": 500}, {"ic": 0.08})[0]
    assert promotion_gate({"ic": 0.075, "hit": 0.52, "n": 500}, {"ic": 0.08})[0]
    assert isinstance(pd.DataFrame(wf.pooled["calibration"]), pd.DataFrame)
