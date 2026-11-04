"""The swing forecaster and its honest evaluation.

The model predicts the distribution of the forward return over a horizon: an expected value, a
probability of an up move (from the expected value and the residual spread, then calibrated on
out-of-sample outcomes) and the 10th and 90th percentiles. It is judged by walk-forward: train on
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
from scipy.stats import norm, spearmanr
from sklearn.ensemble import HistGradientBoostingRegressor
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
        self.scale = 0.05  # spread of the target; the raw p(up) is Phi(expected / scale)
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
        # the target's own spread, not the in-sample residual: a boosted model fits its training
        # rows far better than it will fit tomorrow, and a small residual would make p(up) overconfident
        self.scale = max(float(np.std(y)), 1e-6)
        self.q10.fit(X, y)
        self.q90.fit(X, y)
        self.trained_through = max(rows["session"])
        return self

    def predict(self, rows: pd.DataFrame) -> pd.DataFrame:
        X = rows[FEATURES].to_numpy(dtype=float)
        exp = self.reg.predict(X)
        # one source of skill: the direction probability comes from the return model, not from a
        # second model fitted to a noisier label; the calibrator then maps it to realized up-rates
        p_raw = norm.cdf(exp / self.scale)
        p = self.calibrator.predict(p_raw) if self.calibrator is not None else p_raw
        q10, q90 = self.q10.predict(X), self.q90.predict(X)
        lo, hi = np.minimum(q10, q90), np.maximum(q10, q90)
        return pd.DataFrame(
            {
                "symbol": rows["symbol"].to_numpy(),
                "session": rows["session"].to_numpy(),
                "exp_ret": exp,
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
    q = pd.qcut(pred["exp_ret"].rank(method="first"), 5, labels=False)
    out["spread_top_minus_bottom"] = float(realized[q == 4].mean() - realized[q == 0].mean())
    out["coverage_10_90"] = float(np.mean((realized >= pred["q10"]) & (realized <= pred["q90"])))
    # probabilities are judged where a calibrator existed (the first folds have none), when enough
    p_up, up_cal = pred["p_up"].to_numpy(), up
    if "calibrated" in pred.columns and int(pred["calibrated"].sum()) >= 300:
        mask = pred["calibrated"].to_numpy(dtype=bool)
        p_up, up_cal = p_up[mask], up[mask]
        out["brier_rows"] = int(mask.sum())
    out["brier"] = float(np.mean((p_up - up_cal) ** 2))
    out["brier_naive"] = float(np.mean((up_cal.mean() - up_cal) ** 2))
    bins = pd.cut(pd.Series(p_up), [0, 0.3, 0.4, 0.5, 0.6, 0.7, 1.0], include_lowest=True)
    cal = pd.DataFrame({"bin": bins, "p": p_up, "up": up_cal}).groupby("bin", observed=True)
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


MAX_FOLDS = 24  # about two years of monthly folds; longer histories use longer folds
SHUFFLE_FOLDS = 8  # the shuffled-target control runs on this many folds, evenly spaced


def walk_forward(
    frame: pd.DataFrame,
    horizon: int,
    *,
    warmup: int = 120,
    step: int | None = None,
    shuffle_control: bool = True,
) -> WalkForwardResult:
    target = f"fwd_{horizon}"
    sessions = sorted(frame["session"].unique())
    if step is None:
        step = max(21, (len(sessions) - warmup) // MAX_FOLDS)
    result = WalkForwardResult(horizon=horizon)
    preds: list[pd.DataFrame] = []
    shuffled_preds: list[pd.DataFrame] = []
    rng = np.random.default_rng(11)
    folds = walk_forward_folds(sessions, warmup=warmup, step=step)
    shuffle_every = max(1, len(folds) // SHUFFLE_FOLDS)
    seen_raw: list[
        np.ndarray
    ] = []  # earlier folds' raw probabilities and outcomes, for calibration
    seen_up: list[np.ndarray] = []
    for i, fold in enumerate(folds):
        train = training_rows(frame, horizon, fold.train_as_of)
        test = frame[
            (frame["session"] >= fold.test_start)
            & (frame["session"] <= fold.test_end)
            & (~frame["is_reference"])
        ].dropna(subset=[*FEATURES, target])
        if len(train) < 200 or test.empty:
            continue
        model = SwingForecaster(horizon).fit(train)
        # probabilities are calibrated on the folds already scored, never on this one: the Brier
        # score and the calibration table then describe what the desk would actually have seen
        if seen_raw:
            model.calibrate(np.concatenate(seen_raw), np.concatenate(seen_up))
        pred = model.predict(test)
        pred["realized"] = test[target].to_numpy()
        pred["calibrated"] = model.calibrator is not None
        seen_raw.append(pred["p_up_raw"].to_numpy())
        seen_up.append((pred["realized"].to_numpy() > 0).astype(float))
        fm = _metrics(pred, pred["realized"].to_numpy())
        result.folds.append(
            {
                "test_start": str(fold.test_start),
                "test_end": str(fold.test_end),
                **{k: v for k, v in fm.items() if k != "calibration"},
            }
        )
        preds.append(pred)
        if shuffle_control and i % shuffle_every == 0:
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


MIN_IC = 0.02  # below this a ranking is not worth a third of the scorecard
MIN_T = 2.0  # fold-to-fold reliability of the rank correlation


def promotion_gate(metrics: dict[str, Any], champion: dict[str, Any] | None) -> tuple[bool, str]:
    """A candidate becomes champion only with reliable out-of-sample skill, probabilities that beat
    the base rate, an after-cost backtest that beats the universe, and no regression against the
    incumbent. Every refusal says why, so the training report is the review."""
    pooled = metrics.get("pooled") or {}
    folds = [f for f in metrics.get("folds") or [] if f.get("ic") is not None]
    ic, hit, n = pooled.get("ic"), pooled.get("hit"), pooled.get("n", 0)
    if ic is None or n < 300 or len(folds) < 6:
        return False, f"too little out-of-sample evidence (n={n}, folds={len(folds)})"
    fold_ic = np.array([f["ic"] for f in folds], dtype=float)
    t_stat = (
        float(fold_ic.mean() / (fold_ic.std(ddof=1) / np.sqrt(len(fold_ic))))
        if len(fold_ic) > 1 and fold_ic.std(ddof=1) > 0
        else 0.0
    )
    if ic < MIN_IC or fold_ic.mean() <= 0 or t_stat < MIN_T:
        return (
            False,
            f"no reliable out-of-sample skill (ic {ic:+.3f}, fold mean {fold_ic.mean():+.3f}, t {t_stat:.1f}, hit {hit:.0%})",
        )
    if (hit or 0) < 0.5:
        return False, f"hit rate below a coin (hit {hit:.0%})"
    brier, naive = pooled.get("brier"), pooled.get("brier_naive")
    if brier is not None and naive is not None and brier > naive:
        return (
            False,
            f"probabilities worse than the base rate (brier {brier:.4f} vs naive {naive:.4f})",
        )
    bt = metrics.get("backtest") or {}
    if bt.get("signal") and bt.get("universe"):
        s, u = bt["signal"]["sharpe"], bt["universe"]["sharpe"]
        if s <= 0 or s < u:
            return (
                False,
                f"top quintile does not beat the universe after costs (sharpe {s:.2f} vs {u:.2f})",
            )
    champ_ic = ((champion or {}).get("pooled") or {}).get("ic")
    if champ_ic is not None and ic < champ_ic - 0.01:
        return False, f"worse than the champion (ic {ic:+.3f} vs {champ_ic:+.3f})"
    return True, f"promoted (ic {ic:+.3f}, t {t_stat:.1f}, hit {hit:.0%}, n={n})"


def save(model: SwingForecaster, path: str) -> None:
    joblib.dump(model, path)


def load(path: str) -> SwingForecaster:
    return joblib.load(path)


def model_id(horizon: int, trained_through: date, now: datetime) -> str:
    return f"swing_{horizon}d_{trained_through:%Y%m%d}_{now:%Y%m%d%H%M}"
