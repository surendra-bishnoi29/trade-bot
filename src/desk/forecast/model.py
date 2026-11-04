"""The swing forecaster and its honest evaluation.

The model predicts the distribution of the forward return over a horizon: an expected value, a
probability of an up move and the 10th and 90th percentiles. It is judged by walk-forward: train on
rows whose target was known by the fold's start, predict the following month, roll forward; report
rank correlation, hit rate, top-minus-bottom spread, Brier score, calibration and quantile coverage,
all out of sample. The probability is calibrated on those out-of-sample predictions. A shuffled-target
run must show no skill, or the pipeline leaks.

Engine: scikit-learn's histogram gradient boosting (decision D13): the same family as LightGBM,
with wheels for the server's arm64 CPU and no compiler to install."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import joblib
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.isotonic import IsotonicRegression

from desk.features.daily import FEATURES, training_rows

log = logging.getLogger(__name__)

PARAMS: dict[str, Any] = {
    "max_iter": 250,
    "learning_rate": 0.04,
    "max_depth": 4,
    "min_samples_leaf": 40,
    "l2_regularization": 1.0,
    "random_state": 7,
}


@dataclass
class Fold:
    train_as_of: date  # rows with targets realized by this date are used
    test_start: date
    test_end: date


def walk_forward_folds(sessions: list[date], warmup: int = 120, step: int = 21) -> list[Fold]:
    """Monthly folds after a warm-up: each tests the `step` sessions after its training cut."""
    sessions = sorted(sessions)
    folds: list[Fold] = []
    i = warmup
    while i + 1 < len(sessions):
        test = sessions[i : i + step]
        folds.append(Fold(train_as_of=sessions[i - 1], test_start=test[0], test_end=test[-1]))
        i += step
    return folds


class SwingForecaster:
    def __init__(self, horizon: int = 5, params: dict[str, Any] | None = None) -> None:
        self.horizon = horizon
        p = {**PARAMS, **(params or {})}
        self.reg = HistGradientBoostingRegressor(**p)
        self.clf = HistGradientBoostingClassifier(**p)
        self.q10 = HistGradientBoostingRegressor(loss="quantile", quantile=0.1, **p)
        self.q90 = HistGradientBoostingRegressor(loss="quantile", quantile=0.9, **p)
        self.calibrator: IsotonicRegression | None = None
        self.trained_through: date | None = None
        self.metrics: dict[str, Any] = {}

    @property
    def target(self) -> str:
        return f"fwd_{self.horizon}"

    def fit(self, rows: pd.DataFrame) -> SwingForecaster:
        X = rows[FEATURES].to_numpy(dtype=float)
        y = rows[self.target].to_numpy(dtype=float)
        self.reg.fit(X, y)
        self.clf.fit(X, (y > 0).astype(int))
        self.q10.fit(X, y)
        self.q90.fit(X, y)
        self.trained_through = max(rows["session"])
        return self

    def predict(self, rows: pd.DataFrame) -> pd.DataFrame:
        X = rows[FEATURES].to_numpy(dtype=float)
        p_raw = self.clf.predict_proba(X)[:, 1]
        p = self.calibrator.predict(p_raw) if self.calibrator is not None else p_raw
        q10, q90 = self.q10.predict(X), self.q90.predict(X)
        lo, hi = np.minimum(q10, q90), np.maximum(q10, q90)
        return pd.DataFrame(
            {
                "symbol": rows["symbol"].to_numpy(),
                "session": rows["session"].to_numpy(),
                "exp_ret": self.reg.predict(X),
                "p_up": p,
                "p_up_raw": p_raw,
                "q10": lo,
                "q90": hi,
            }
        )

    def calibrate(self, p_raw: np.ndarray, up: np.ndarray) -> None:
        if len(p_raw) >= 100 and len(np.unique(up)) == 2:
            self.calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.01, y_max=0.99).fit(
                p_raw, up
            )


def _metrics(pred: pd.DataFrame, realized: np.ndarray) -> dict[str, Any]:
    out: dict[str, Any] = {"n": len(pred)}
    if len(pred) < 10:
        return out
    ic = spearmanr(pred["exp_ret"], realized).correlation
    out["ic"] = None if ic is None or np.isnan(ic) else float(ic)
    out["hit"] = float(np.mean(np.sign(pred["exp_ret"]) == np.sign(realized)))
    up = (realized > 0).astype(float)
    out["brier"] = float(np.mean((pred["p_up"] - up) ** 2))
    out["brier_naive"] = float(np.mean((up.mean() - up) ** 2))
    q = pd.qcut(pred["exp_ret"].rank(method="first"), 5, labels=False)
    out["spread_top_minus_bottom"] = float(realized[q == 4].mean() - realized[q == 0].mean())
    out["coverage_10_90"] = float(np.mean((realized >= pred["q10"]) & (realized <= pred["q90"])))
    bins = pd.cut(pred["p_up"], [0, 0.3, 0.4, 0.5, 0.6, 0.7, 1.0], include_lowest=True)
    cal = pd.DataFrame({"bin": bins, "p": pred["p_up"], "up": up}).groupby("bin", observed=True)
    out["calibration"] = [
        {
            "bin": str(b),
            "n": len(g),
            "p_mean": float(g["p"].mean()),
            "up_rate": float(g["up"].mean()),
        }
        for b, g in cal
    ]
    return out


@dataclass
class WalkForwardResult:
    horizon: int
    folds: list[dict[str, Any]] = field(default_factory=list)
    pooled: dict[str, Any] = field(default_factory=dict)
    shuffled: dict[str, Any] = field(default_factory=dict)
    oos: pd.DataFrame | None = None

    def render(self) -> str:
        lines = [f"WALK-FORWARD, {self.horizon}-session horizon: {len(self.folds)} folds"]
        for f in self.folds:
            ic = f.get("ic")
            lines.append(
                f"  {f['test_start']} to {f['test_end']}: n={f['n']:>4} ic={ic:+.3f} hit={f.get('hit', 0):.0%}"
                if ic is not None
                else f"  {f['test_start']} to {f['test_end']}: n={f['n']:>4} (too few rows)"
            )
        p = self.pooled
        if p.get("ic") is not None:
            lines.append(
                f"pooled: n={p['n']} ic={p['ic']:+.3f} hit={p['hit']:.0%} top-bottom spread={p['spread_top_minus_bottom'] * 100:+.2f}% "
                f"brier={p['brier']:.4f} (naive {p['brier_naive']:.4f}) 10-90 coverage={p['coverage_10_90']:.0%}"
            )
            lines.append("calibration (p_up bin: predicted vs realized up-rate):")
            for c in p.get("calibration", []):
                lines.append(
                    f"  {c['bin']:<12} n={c['n']:>4} predicted {c['p_mean']:.2f} realized {c['up_rate']:.2f}"
                )
        else:
            lines.append("pooled: not enough out-of-sample rows to judge")
        s = self.shuffled
        if s.get("ic") is not None:
            lines.append(
                f"shuffled-target control: ic={s['ic']:+.3f} hit={s['hit']:.0%} (must be near zero and 50%)"
            )
        return "\n".join(lines)


def walk_forward(
    frame: pd.DataFrame,
    horizon: int,
    *,
    warmup: int = 120,
    step: int = 21,
    shuffle_control: bool = True,
) -> WalkForwardResult:
    target = f"fwd_{horizon}"
    sessions = sorted(frame["session"].unique())
    result = WalkForwardResult(horizon=horizon)
    preds: list[pd.DataFrame] = []
    shuffled_preds: list[pd.DataFrame] = []
    rng = np.random.default_rng(11)
    for fold in walk_forward_folds(sessions, warmup=warmup, step=step):
        train = training_rows(frame, horizon, fold.train_as_of)
        test = frame[
            (frame["session"] >= fold.test_start)
            & (frame["session"] <= fold.test_end)
            & (~frame["is_reference"])
        ].dropna(subset=[*FEATURES, target])
        if len(train) < 200 or test.empty:
            continue
        model = SwingForecaster(horizon).fit(train)
        pred = model.predict(test)
        pred["realized"] = test[target].to_numpy()
        fm = _metrics(pred, pred["realized"].to_numpy())
        result.folds.append(
            {
                "test_start": str(fold.test_start),
                "test_end": str(fold.test_end),
                **{k: v for k, v in fm.items() if k != "calibration"},
            }
        )
        preds.append(pred)
        if shuffle_control:
            shuffled = train.copy()
            shuffled[target] = rng.permutation(shuffled[target].to_numpy())
            sp = SwingForecaster(horizon).fit(shuffled).predict(test)
            sp["realized"] = test[target].to_numpy()
            shuffled_preds.append(sp)
    if preds:
        oos = pd.concat(preds, ignore_index=True)
        result.oos = oos
        result.pooled = _metrics(oos, oos["realized"].to_numpy())
    if shuffled_preds:
        sp = pd.concat(shuffled_preds, ignore_index=True)
        result.shuffled = {
            k: v for k, v in _metrics(sp, sp["realized"].to_numpy()).items() if k != "calibration"
        }
    return result


def train_final(
    frame: pd.DataFrame, horizon: int, as_of: date, wf: WalkForwardResult | None = None
) -> SwingForecaster:
    rows = training_rows(frame, horizon, as_of)
    model = SwingForecaster(horizon).fit(rows)
    if wf is not None and wf.oos is not None:
        model.calibrate(
            wf.oos["p_up_raw"].to_numpy(), (wf.oos["realized"].to_numpy() > 0).astype(float)
        )
        model.metrics = {"pooled": wf.pooled, "folds": wf.folds, "shuffled": wf.shuffled}
    return model


def promotion_gate(candidate: dict[str, Any], champion: dict[str, Any] | None) -> tuple[bool, str]:
    """A candidate becomes champion only with real out-of-sample skill and no regression."""
    ic, hit, n = candidate.get("ic"), candidate.get("hit"), candidate.get("n", 0)
    if ic is None or n < 300:
        return False, f"too little out-of-sample evidence (n={n})"
    if ic <= 0.0 or (hit or 0) < 0.5:
        return False, f"no out-of-sample skill (ic {ic:+.3f}, hit {hit:.0%})"
    if champion and champion.get("ic") is not None and ic < champion["ic"] - 0.01:
        return False, f"worse than the champion (ic {ic:+.3f} vs {champion['ic']:+.3f})"
    return True, f"promoted (ic {ic:+.3f}, hit {hit:.0%}, n={n})"


def save(model: SwingForecaster, path: str) -> None:
    joblib.dump(model, path)


def load(path: str) -> SwingForecaster:
    return joblib.load(path)


def model_id(horizon: int, trained_through: date, now: datetime) -> str:
    return f"swing_{horizon}d_{trained_through:%Y%m%d}_{now:%Y%m%d%H%M}"
