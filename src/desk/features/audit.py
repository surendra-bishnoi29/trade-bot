"""The lookahead audit: three independent checks that no feature knows the future.

1. Recompute: a row's features recomputed from bars truncated at its session must equal the stored
   features. Any difference means a rolling window, a rank or a merge peeked ahead.
2. Correlation: no feature may correlate strongly with its own forward target across the frame. A
   genuine feature predicts weakly; a leak predicts suspiciously well.
3. Shuffle: a model trained on shuffled targets must have no skill out of sample (done in training)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from desk.features.daily import FEATURES, TARGETS, build_frame

CORRELATION_LIMIT = 0.5


def recompute_check(
    bars: dict[str, list[dict[str, Any]]], frame: pd.DataFrame, samples: int = 25, seed: int = 7
) -> list[str]:
    """Rows whose features change when the bars after their session are removed."""
    rng = np.random.default_rng(seed)
    candidates = frame.dropna(subset=FEATURES)
    if candidates.empty:
        return []
    picks = candidates.sample(
        min(samples, len(candidates)), random_state=int(rng.integers(1 << 30))
    )
    problems: list[str] = []
    for _, row in picks.iterrows():
        session = row["session"]
        truncated = {
            s: [b for b in rows if pd.to_datetime(b["ts"], utc=True).date() <= session]
            for s, rows in bars.items()
        }
        again = build_frame(truncated)
        match = again[(again["symbol"] == row["symbol"]) & (again["session"] == session)]
        if match.empty:
            problems.append(f"{row['symbol']} {session}: row missing after truncation")
            continue
        for col in FEATURES:
            a, b = float(row[col]), float(match.iloc[0][col])
            if not (np.isclose(a, b, rtol=1e-9, atol=1e-12) or (np.isnan(a) and np.isnan(b))):
                problems.append(
                    f"{row['symbol']} {session} {col}: stored {a:.6g}, recomputed {b:.6g}"
                )
    return problems


def correlation_check(frame: pd.DataFrame, limit: float = CORRELATION_LIMIT) -> list[str]:
    rows = frame[~frame["is_reference"]]
    problems: list[str] = []
    for target in TARGETS:
        sub = rows.dropna(subset=[target])
        if len(sub) < 50:
            continue
        for col in FEATURES:
            s = sub[[col, target]].dropna()
            if len(s) < 50:
                continue
            corr = float(s[col].corr(s[target], method="spearman"))
            if abs(corr) >= limit:
                problems.append(f"{col} vs {target}: spearman {corr:+.2f} (limit {limit})")
    return problems


def audit(bars: dict[str, list[dict[str, Any]]], frame: pd.DataFrame) -> dict[str, Any]:
    recompute = recompute_check(bars, frame)
    correlation = correlation_check(frame)
    return {
        "rows": len(frame),
        "sessions": int(frame["session"].nunique()) if not frame.empty else 0,
        "recompute_problems": recompute,
        "correlation_problems": correlation,
        "passed": not recompute and not correlation,
    }


def render(result: dict[str, Any]) -> str:
    lines = [f"LOOKAHEAD AUDIT: {result['rows']} rows over {result['sessions']} sessions"]
    lines.append(
        "recompute check: "
        + (
            "passed"
            if not result["recompute_problems"]
            else f"{len(result['recompute_problems'])} problems"
        )
    )
    lines += [f"  {p}" for p in result["recompute_problems"][:10]]
    lines.append(
        "correlation check: "
        + (
            "passed"
            if not result["correlation_problems"]
            else f"{len(result['correlation_problems'])} problems"
        )
    )
    lines += [f"  {p}" for p in result["correlation_problems"][:10]]
    lines.append("verdict: " + ("PASSED" if result["passed"] else "FAILED"))
    return "\n".join(lines)
