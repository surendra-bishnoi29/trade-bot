"""Intraday baselines on minute bars: three deterministic strategies, flat by the close, judged
after costs against holding from the open. They are the yardstick the intraday book must beat and
its first candidates.

No lookahead: a signal seen on a bar's close is traded at the next bar's open; stops and targets
fill at their level, or at the open when a bar gaps through; the session's last bar closes what is
left. When a bar touches both the stop and the target, the stop is assumed."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

from desk.core import calendar
from desk.research.costs import CostModel
from desk.storage.db import Database

MIN_BARS = 120  # a session with fewer minute bars is a data gap, not a trading day
LAST_ENTRY_BARS = 30  # no new positions in the last half hour


@dataclass
class Trade:
    strategy: str
    symbol: str
    session: date
    side: str  # long or short
    entry_ts: datetime
    entry: float
    exit_ts: datetime
    exit: float
    reason: str  # target, stop, vwap, close

    @property
    def gross(self) -> float:
        r = self.exit / self.entry - 1
        return r if self.side == "long" else -r


def session_frame(bars: list[dict[str, Any]]) -> pd.DataFrame:
    """A session's minute bars as floats with the running session VWAP."""
    df = pd.DataFrame(bars)
    if df.empty:
        return df
    df = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    df["volume"] = df["volume"].astype(float)
    typical = (df["high"] + df["low"] + df["close"]) / 3
    bar_vwap = df["vwap"].astype(float) if "vwap" in df else typical
    bar_vwap = bar_vwap.where(bar_vwap.notna() & (bar_vwap > 0), typical)
    notional = (bar_vwap * df["volume"]).cumsum()
    volume = df["volume"].cumsum().replace(0, np.nan)
    df["vwap"] = (notional / volume).fillna(typical)
    return df[["ts", "open", "high", "low", "close", "volume", "vwap"]]


def _exit(bar: pd.Series, side: str, stop: float, target: float | None) -> tuple[float, str] | None:
    if side == "long":
        if bar["open"] <= stop:
            return float(bar["open"]), "stop"
        if bar["low"] <= stop:
            return stop, "stop"
        if target is not None and bar["high"] >= target:
            return target, "target"
    else:
        if bar["open"] >= stop:
            return float(bar["open"]), "stop"
        if bar["high"] >= stop:
            return stop, "stop"
        if target is not None and bar["low"] <= target:
            return target, "target"
    return None


def _manage(
    df: pd.DataFrame,
    start: int,
    side: str,
    entry: float,
    stop: float,
    target: float | None,
    *,
    to_vwap: bool = False,
) -> tuple[int, float, str]:
    """Walk the bars after entry until an exit; the last bar closes at its close."""
    last = len(df) - 1
    for j in range(start, last):
        bar = df.iloc[j]
        hit = _exit(bar, side, stop, target)
        if hit is not None:
            return j, hit[0], hit[1]
        if to_vwap:
            v = float(bar["vwap"])
            if (side == "long" and bar["high"] >= v) or (side == "short" and bar["low"] <= v):
                return j, v, "vwap"
    return last, float(df.iloc[last]["close"]), "close"


def opening_range_breakout(
    df: pd.DataFrame, symbol: str, session: date, *, minutes: int = 15, target_r: float = 1.0
) -> Trade | None:
    """The first break of the opening range, stop at its midpoint, target one range away."""
    if len(df) < MIN_BARS:
        return None
    rng = df.iloc[:minutes]
    hi, lo = float(rng["high"].max()), float(rng["low"].min())
    width = hi - lo
    if width <= 0:
        return None
    mid = (hi + lo) / 2
    for i in range(minutes, len(df) - LAST_ENTRY_BARS):
        bar = df.iloc[i]
        if bar["high"] > hi:
            side, entry = "long", max(float(bar["open"]), hi)
            target = entry + width * target_r
        elif bar["low"] < lo:
            side, entry = "short", min(float(bar["open"]), lo)
            target = entry - width * target_r
        else:
            continue
        j, px, reason = _manage(df, i + 1, side, entry, mid, target)
        return Trade(
            "opening_range",
            symbol,
            session,
            side,
            df.iloc[i]["ts"],
            entry,
            df.iloc[j]["ts"],
            px,
            reason,
        )
    return None


def vwap_reversion(
    df: pd.DataFrame,
    symbol: str,
    session: date,
    *,
    after_minutes: int = 30,
    threshold: float = 0.005,
    stop: float = 0.01,
) -> Trade | None:
    """Fade a stretch away from the session VWAP; out at VWAP, the stop or the close."""
    if len(df) < MIN_BARS:
        return None
    for i in range(after_minutes, len(df) - LAST_ENTRY_BARS):
        bar = df.iloc[i]
        v = float(bar["vwap"])
        if v <= 0:
            continue
        dev = float(bar["close"]) / v - 1
        if dev <= -threshold:
            side = "long"
        elif dev >= threshold:
            side = "short"
        else:
            continue
        entry = float(df.iloc[i + 1]["open"])
        stop_px = entry * (1 - stop) if side == "long" else entry * (1 + stop)
        j, px, reason = _manage(df, i + 1, side, entry, stop_px, None, to_vwap=True)
        return Trade(
            "vwap_reversion",
            symbol,
            session,
            side,
            df.iloc[i + 1]["ts"],
            entry,
            df.iloc[j]["ts"],
            px,
            reason,
        )
    return None


def momentum(
    df: pd.DataFrame,
    symbol: str,
    session: date,
    *,
    at_minutes: int = 60,
    threshold: float = 0.0075,
    stop: float = 0.01,
) -> Trade | None:
    """After the first hour, go with a strong move that is on the right side of VWAP; hold to the close."""
    if len(df) < MIN_BARS or at_minutes >= len(df) - LAST_ENTRY_BARS:
        return None
    bar = df.iloc[at_minutes - 1]
    r = float(bar["close"]) / float(df.iloc[0]["open"]) - 1
    v = float(bar["vwap"])
    if r >= threshold and float(bar["close"]) > v:
        side = "long"
    elif r <= -threshold and float(bar["close"]) < v:
        side = "short"
    else:
        return None
    entry = float(df.iloc[at_minutes]["open"])
    stop_px = entry * (1 - stop) if side == "long" else entry * (1 + stop)
    j, px, reason = _manage(df, at_minutes, side, entry, stop_px, None)
    return Trade(
        "momentum",
        symbol,
        session,
        side,
        df.iloc[at_minutes]["ts"],
        entry,
        df.iloc[j]["ts"],
        px,
        reason,
    )


def open_to_close(df: pd.DataFrame, symbol: str, session: date) -> Trade | None:
    """The reference: long from the first bar's open to the last bar's close."""
    if len(df) < MIN_BARS:
        return None
    return Trade(
        "open_to_close",
        symbol,
        session,
        "long",
        df.iloc[0]["ts"],
        float(df.iloc[0]["open"]),
        df.iloc[-1]["ts"],
        float(df.iloc[-1]["close"]),
        "close",
    )


STRATEGIES = {
    "opening_range": opening_range_breakout,
    "vwap_reversion": vwap_reversion,
    "momentum": momentum,
    "open_to_close": open_to_close,
}


def _stats(trades: list[Trade], cost: CostModel, sessions: list[date]) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    gross = np.array([t.gross for t in trades])
    net = gross - cost.round_trip
    by_session: dict[date, list[float]] = defaultdict(list)
    for t, r in zip(trades, net, strict=True):
        by_session[t.session].append(float(r))
    daily = pd.Series({s: float(np.mean(v)) for s, v in by_session.items()}).reindex(
        sessions, fill_value=0.0
    )
    cum = (1 + daily).cumprod()
    drawdown = float((cum / cum.cummax() - 1).min())
    sharpe = float(daily.mean() / daily.std() * math.sqrt(252)) if daily.std() > 0 else 0.0
    losses = -net[net < 0].sum()
    return {
        "trades": n,
        "long": int(sum(t.side == "long" for t in trades)),
        "short": int(sum(t.side == "short" for t in trades)),
        "win_rate": float((net > 0).mean()),
        "avg_gross_bps": float(gross.mean() * 1e4),
        "avg_net_bps": float(net.mean() * 1e4),
        "total_net": float(cum.iloc[-1] - 1),
        "sharpe": sharpe,
        "max_drawdown": drawdown,
        "profit_factor": float(net[net > 0].sum() / losses) if losses > 0 else float("inf"),
        "exits": dict(Counter(t.reason for t in trades)),
    }


def evaluate(
    bars: dict[tuple[str, date], list[dict[str, Any]]], cost: CostModel | None = None
) -> dict[str, Any]:
    """Run every strategy over every (symbol, session) and summarize after costs."""
    cost = cost or CostModel.default()
    sessions = sorted({s for _, s in bars})
    symbols = sorted({sym for sym, _ in bars})
    trades: dict[str, list[Trade]] = {name: [] for name in STRATEGIES}
    frames = {key: session_frame(rows) for key, rows in bars.items()}
    for (symbol, session), df in sorted(frames.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        if df.empty:
            continue
        for name, fn in STRATEGIES.items():
            t = fn(df, symbol, session)
            if t is not None:
                trades[name].append(t)
    return {
        "sessions": len(sessions),
        "symbols": symbols,
        "cost": cost.describe(),
        "strategies": {name: _stats(ts, cost, sessions) for name, ts in trades.items()},
        "trades": trades,
    }


def render(result: dict[str, Any]) -> str:
    lines = [
        f"INTRADAY BASELINES: {result['sessions']} sessions, {len(result['symbols'])} names, costs {result['cost']}",
        f"{'strategy':<16} {'trades':>6} {'L/S':>8} {'win':>5} {'gross':>8} {'net':>8} {'total':>8} {'sharpe':>6} {'maxDD':>7} {'PF':>5}  exits",
    ]
    for name, s in result["strategies"].items():
        if not s.get("trades"):
            lines.append(f"{name:<16} {0:>6}")
            continue
        pf = f"{s['profit_factor']:.2f}" if math.isfinite(s["profit_factor"]) else "inf"
        exits = ", ".join(f"{k} {v}" for k, v in sorted(s["exits"].items()))
        lines.append(
            f"{name:<16} {s['trades']:>6} {s['long']:>3}/{s['short']:<4} {s['win_rate']:>5.0%} "
            f"{s['avg_gross_bps']:>+7.1f}b {s['avg_net_bps']:>+7.1f}b {s['total_net']:>+7.1%} "
            f"{s['sharpe']:>6.2f} {s['max_drawdown']:>7.1%} {pf:>5}  {exits}"
        )
    lines.append(
        "gross/net: average per trade in basis points; total: compounded daily, equal weight across the day's trades; open_to_close is the reference"
    )
    return "\n".join(lines)


async def load_minute_sessions(
    db: Database, symbols: list[str], sessions: list[date]
) -> dict[tuple[str, date], list[dict[str, Any]]]:
    """Regular-hours minute bars per (symbol, session)."""
    out: dict[tuple[str, date], list[dict[str, Any]]] = {}
    for s in sessions:
        open_, close = calendar.session_bounds(s)
        rows = await db.bars_between(symbols, "1m", open_, close)
        for r in rows:
            out.setdefault((r["symbol"], s), []).append(r)
    return out
