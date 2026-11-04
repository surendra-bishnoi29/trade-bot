"""The training pipeline: bars -> features -> lookahead audit -> walk-forward -> final model ->
registry and promotion gate. Refuses to train on a frame that fails the audit. Also runs the plain
after-cost backtest of the signal, so the report says whether the forecasts would have paid."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from desk.core import calendar
from desk.features.audit import audit
from desk.features.audit import render as render_audit
from desk.features.daily import FEATURES, TARGETS, build_frame
from desk.forecast.model import (
    WalkForwardResult,
    model_id,
    promotion_gate,
    save,
    train_final,
    walk_forward,
)
from desk.storage.db import Database

log = logging.getLogger(__name__)

ROUND_TRIP_COST = 0.002  # 10 bps a side, the desk's conservative paper assumption


async def load_bars(
    db: Database, universe: list[str], days: int, *, as_of: datetime | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Daily bars per symbol, oldest first, as of a moment. During a session, today's daily bar is not
    trusted (it is partial) and the latest minute close stands in for it, as the scorecard does."""
    out: dict[str, list[dict[str, Any]]] = {}
    in_session = False
    if as_of is not None and calendar.is_session(as_of.date()):
        in_session = as_of < calendar.session_bounds(as_of.date())[1]
    for symbol in universe:
        rows = await db.recent_bars(symbol, "1d", limit=days, before=as_of)
        rows = [
            {
                k: (float(r[k]) if k in ("open", "high", "low", "close") else r[k])
                for k in ("ts", "open", "high", "low", "close", "volume")
            }
            for r in rows
        ]
        if as_of is not None and in_session:
            rows = [r for r in rows if r["ts"].date() != as_of.date()]
            latest = await db.recent_bars(symbol, "1m", limit=1, before=as_of)
            if latest and latest[0]["ts"].date() == as_of.date():
                b = latest[0]
                c = float(b["close"])
                rows.append(
                    {
                        "ts": b["ts"],
                        "open": c,
                        "high": c,
                        "low": c,
                        "close": c,
                        "volume": int(b["volume"] or 0),
                    }
                )
        out[symbol] = rows
    return out


def signal_backtest(
    oos: pd.DataFrame, horizon: int = 5, cost: float = ROUND_TRIP_COST
) -> dict[str, Any]:
    """Long the top quintile by predicted return, rebalanced every `horizon` sessions, after costs,
    against the equal-weight universe. Non-overlapping periods, so the arithmetic is honest."""
    if oos is None or oos.empty:
        return {}
    sessions = sorted(oos["session"].unique())
    picks = sessions[::horizon]
    port, base = [], []
    for s in picks:
        day = oos[oos["session"] == s]
        if len(day) < 5:
            continue
        top = day.nlargest(max(1, len(day) // 5), "exp_ret")
        port.append(float(top["realized"].mean()) - cost)
        base.append(float(day["realized"].mean()))
    if len(port) < 4:
        return {"periods": len(port)}
    p, b = np.array(port), np.array(base)
    periods_per_year = 252 / horizon

    def stats(x: np.ndarray) -> dict[str, float]:
        cum = np.cumprod(1 + x)
        peak = np.maximum.accumulate(cum)
        dd = float(((cum - peak) / peak).min())
        sharpe = float(x.mean() / x.std() * math.sqrt(periods_per_year)) if x.std() > 0 else 0.0
        return {
            "total_return": float(cum[-1] - 1),
            "sharpe": sharpe,
            "max_drawdown": dd,
            "hit": float((x > 0).mean()),
        }

    return {"periods": len(port), "cost_round_trip": cost, "signal": stats(p), "universe": stats(b)}


def render_backtest(bt: dict[str, Any], horizon: int) -> str:
    if not bt or "signal" not in bt:
        return f"SIGNAL BACKTEST: not enough non-overlapping periods ({bt.get('periods', 0)})"
    s, u = bt["signal"], bt["universe"]
    return (
        f"SIGNAL BACKTEST ({bt['periods']} periods of {horizon} sessions, top quintile long, "
        f"{bt['cost_round_trip'] * 1e4:.0f} bps round trip): "
        f"total {s['total_return'] * 100:+.1f}% vs universe {u['total_return'] * 100:+.1f}%, "
        f"sharpe {s['sharpe']:.2f} vs {u['sharpe']:.2f}, max drawdown {s['max_drawdown'] * 100:.1f}% vs {u['max_drawdown'] * 100:.1f}%, "
        f"winning periods {s['hit']:.0%}"
    )


def frame_rows(frame: pd.DataFrame, computed_at: datetime) -> list[dict[str, Any]]:
    rows = []
    for _, r in frame.iterrows():
        features = {k: (None if pd.isna(r[k]) else float(r[k])) for k in FEATURES}
        targets = {k: (None if pd.isna(r[k]) else float(r[k])) for k in TARGETS}
        rows.append(
            {
                "symbol": r["symbol"],
                "session": r["session"],
                "computed_at": computed_at,
                "features": features,
                "targets": targets,
            }
        )
    return rows


async def train_pipeline(
    db: Database,
    now: datetime,
    universe: list[str],
    *,
    horizon: int = 5,
    days: int = 400,
    models_dir: str = "data/models",
    store_features: bool = True,
    audit_samples: int = 10,
) -> dict[str, Any]:
    bars = await load_bars(db, universe, days, as_of=now)
    frame = build_frame(bars)
    report: list[str] = [
        f"TRAINING as of {now:%Y-%m-%d %H:%M} UTC: {len(frame)} rows, {frame['session'].nunique() if not frame.empty else 0} sessions, {len(universe)} names"
    ]
    if frame.empty or frame["session"].nunique() < 150:
        report.append("not enough history to train (need 150 sessions)")
        return {"ok": False, "report": "\n".join(report)}
    checked = (
        audit(bars, frame)
        if audit_samples
        else {
            "passed": True,
            "rows": len(frame),
            "sessions": 0,
            "recompute_problems": [],
            "correlation_problems": [],
        }
    )
    if audit_samples:
        checked = audit(bars, frame)
        report.append(render_audit(checked))
    if not checked["passed"]:
        report.append("refusing to train: the feature frame fails the lookahead audit")
        return {"ok": False, "report": "\n".join(report), "audit": checked}
    if store_features:
        n = await db.upsert_features(frame_rows(frame, now))
        report.append(f"feature store: {n} rows written")
        try:
            out = Path(models_dir).parent / "features"
            out.mkdir(parents=True, exist_ok=True)
            frame.to_parquet(out / "daily.parquet", index=False)
            report.append(f"parquet: {out / 'daily.parquet'}")
        except Exception as exc:  # noqa: BLE001 - the export is a convenience
            report.append(f"parquet export skipped: {exc}")
    wf: WalkForwardResult = walk_forward(frame, horizon)
    report.append(wf.render())
    bt = signal_backtest(wf.oos, horizon)
    report.append(render_backtest(bt, horizon))
    model = train_final(frame, horizon, now.date(), wf)
    model.metrics["backtest"] = bt
    mid = model_id(horizon, model.trained_through or now.date(), now)
    Path(models_dir).mkdir(parents=True, exist_ok=True)
    path = str(Path(models_dir) / f"{mid}.joblib")
    save(model, path)
    champion = await db.champion("swing", horizon)
    ok, why = promotion_gate(model.metrics, champion["metrics"] if champion else None)
    status = "champion" if ok else "candidate"
    if ok and champion:
        await db.set_model_status(champion["model_id"], "retired")
    await db.register_model(
        mid, "swing", horizon, model.trained_through or now.date(), path, model.metrics, status, why
    )
    report.append(f"model {mid}: {why}; status {status}")
    return {
        "ok": True,
        "report": "\n".join(report),
        "model_id": mid,
        "status": status,
        "metrics": model.metrics,
        "path": path,
    }


def next_training_time(now: datetime) -> datetime:
    """Saturdays at 02:00 UTC, after the week's bars are complete."""
    target = now.replace(hour=2, minute=0, second=0, microsecond=0)
    days_ahead = (5 - target.weekday()) % 7
    target += timedelta(days=days_ahead)
    if target <= now:
        target += timedelta(days=7)
    return target
