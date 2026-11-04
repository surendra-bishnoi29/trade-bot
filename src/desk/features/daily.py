"""Daily features for the swing book, computed from bars that existed at each session's close.

A row is (session, symbol). Its features use only bars with ts <= session. Its targets (forward
returns) use bars after the session and are NaN until those exist. A training set as of date T may
only use rows whose target was realized by T; the walk-forward in desk.forecast enforces it."""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from desk.core.sectors import REFERENCES

MARKET = "SPY"
FEATURES = [
    "ret_1",
    "ret_5",
    "ret_20",
    "ret_60",
    "vol_20",
    "vol_60",
    "rsi_14",
    "sma20_rel",
    "sma50_rel",
    "hi20_dist",
    "lo20_dist",
    "vol_z20",
    "gap",
    "range_20",
    "mkt_ret_5",
    "mkt_ret_20",
    "breadth_20",
    "rank_ret_20",
    "rank_ret_5",
    "rank_vol_20",
]
HORIZONS = (1, 5, 20)
TARGETS = [f"fwd_{h}" for h in HORIZONS]


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0).rolling(n).mean()
    down = (-delta.clip(upper=0)).rolling(n).mean()
    rs = up / down.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def symbol_frame(bars: list[dict[str, Any]]) -> pd.DataFrame:
    """One symbol's features and targets from its ascending daily bars."""
    df = pd.DataFrame(bars)
    if df.empty:
        return df
    df = df.copy()
    df["session"] = pd.to_datetime(df["ts"], utc=True).dt.date
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    df["volume"] = df["volume"].astype(float)
    df = df.sort_values("session").drop_duplicates("session", keep="last").reset_index(drop=True)
    c = df["close"]
    logret = np.log(c / c.shift(1))
    out = pd.DataFrame({"session": df["session"], "close": c})
    for k in (1, 5, 20, 60):
        out[f"ret_{k}"] = c / c.shift(k) - 1
    out["vol_20"] = logret.rolling(20).std() * np.sqrt(252)
    out["vol_60"] = logret.rolling(60).std() * np.sqrt(252)
    out["rsi_14"] = _rsi(c)
    out["sma20_rel"] = c / c.rolling(20).mean() - 1
    out["sma50_rel"] = c / c.rolling(50).mean() - 1
    out["hi20_dist"] = c / df["high"].rolling(20).max() - 1
    out["lo20_dist"] = c / df["low"].rolling(20).min() - 1
    vol_mean = df["volume"].rolling(20).mean()
    vol_std = df["volume"].rolling(20).std()
    out["vol_z20"] = (df["volume"] - vol_mean) / vol_std.replace(0, np.nan)
    out["gap"] = df["open"] / c.shift(1) - 1
    out["range_20"] = ((df["high"] - df["low"]) / c).rolling(20).mean()
    for h in HORIZONS:
        out[f"fwd_{h}"] = c.shift(-h) / c - 1
    return out


def build_frame(bars: dict[str, list[dict[str, Any]]], market: str = MARKET) -> pd.DataFrame:
    """The long frame for every symbol: features, market context, cross-sectional ranks, targets."""
    parts = []
    for symbol, rows in bars.items():
        f = symbol_frame(rows)
        if f.empty:
            continue
        f.insert(0, "symbol", symbol)
        parts.append(f)
    if not parts:
        return pd.DataFrame(columns=["session", "symbol", *FEATURES, *TARGETS])
    frame = pd.concat(parts, ignore_index=True)
    mkt = frame[frame["symbol"] == market][["session", "ret_5", "ret_20"]].rename(
        columns={"ret_5": "mkt_ret_5", "ret_20": "mkt_ret_20"}
    )
    frame = frame.merge(mkt, on="session", how="left")
    names = frame[~frame["symbol"].isin(REFERENCES)]
    breadth = (
        names.groupby("session")["sma20_rel"]
        .apply(lambda s: float((s > 0).mean()) if s.notna().any() else np.nan)
        .rename("breadth_20")
    )
    frame = frame.merge(breadth, on="session", how="left")
    for col, src in (("rank_ret_20", "ret_20"), ("rank_ret_5", "ret_5"), ("rank_vol_20", "vol_20")):
        frame[col] = frame.groupby("session")[src].rank(pct=True)
    frame["is_reference"] = frame["symbol"].isin(REFERENCES)
    cols = ["session", "symbol", "close", "is_reference", *FEATURES, *TARGETS]
    frame = frame[cols].replace(
        [np.inf, -np.inf], np.nan
    )  # a halted or broken series must not poison a model
    return frame.sort_values(["session", "symbol"]).reset_index(drop=True)


def training_rows(frame: pd.DataFrame, horizon: int, as_of: date) -> pd.DataFrame:
    """Rows a model may learn from on `as_of`: features complete, target realized by then, not a
    reference index. Realized means the session plus the horizon's sessions is at or before as_of,
    which the frame encodes by the target being non-null for sessions old enough."""
    target = f"fwd_{horizon}"
    sessions = sorted(frame["session"].unique())
    if not sessions:
        return frame.iloc[0:0]
    # the last session whose target could be known on as_of: as_of itself, minus `horizon` sessions
    eligible = [s for s in sessions if s <= as_of]
    if len(eligible) <= horizon:
        return frame.iloc[0:0]
    last_known = eligible[-1 - horizon]
    rows = frame[(frame["session"] <= last_known) & (~frame["is_reference"])]
    return rows.dropna(subset=[*FEATURES, target])


def latest_rows(frame: pd.DataFrame, as_of: date) -> pd.DataFrame:
    """One row per symbol: the newest session at or before as_of with complete features."""
    rows = frame[(frame["session"] <= as_of) & (~frame["is_reference"])].dropna(subset=FEATURES)
    if rows.empty:
        return rows
    return rows.sort_values("session").groupby("symbol").tail(1)
