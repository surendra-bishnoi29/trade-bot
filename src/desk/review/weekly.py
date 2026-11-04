"""The weekly review: performance, post-mortems, analyst scores, incidents, prompt and model
changes, assembled from the journal into Markdown. Deterministic; the daily brief already narrates."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from desk.core import prompts
from desk.core.events import Topics
from desk.research.costs import CostModel
from desk.review.postmortem import grade_closed, render_weights
from desk.storage.db import Database


async def weekly_facts(db: Database, now: datetime, days: int = 7) -> dict[str, Any]:
    since = now - timedelta(days=days)
    equity = await db.equity_series(since, now)
    start = float(equity[0][1]) if equity else None
    end = float(equity[-1][1]) if equity else None
    peak, worst = 0.0, 0.0
    for _, e in equity:
        v = float(e)
        peak = max(peak, v)
        if peak:
            worst = min(worst, v / peak - 1)
    fills = await db.recent_events([Topics.ORDER_FILLED], since, limit=2000, until=now)
    seen: set[str] = set()
    notional = Decimal(0)
    trades = 0
    for e in fills:
        iid = e.payload.get("intent_id")
        if iid in seen:
            continue
        seen.add(iid)
        trades += 1
        try:
            notional += Decimal(str(e.payload.get("filled_quantity") or 0)) * Decimal(
                str(e.payload.get("avg_price") or 0)
            )
        except (TypeError, ValueError, ArithmeticError):
            continue
    cost = CostModel.default()
    grades = await grade_closed(db, since, now)
    realized = sum((g.realized_pnl or 0.0) for g in grades)
    scores = await db.recent_events(
        [Topics.REVIEW_ANALYST_SCORE], since - timedelta(days=30), limit=1, until=now
    )
    kills = await db.recent_events([Topics.RISK_KILL_SWITCH], since, limit=50, until=now)
    restarts = await db.restart_gaps(since, now)
    rejected = await db.count_events_between([Topics.RISK_REJECTED], since, now)
    degraded = await db.count_events_between([Topics.SYSTEM_HEALTH], since, now, status_not="ok")
    cycles = await db.count_events_between([Topics.CYCLE_PLAN], since, now)
    briefs = await db.count_events_between([Topics.DESK_BRIEF], since, now)
    notes = await db.count_events_between([Topics.RESEARCH_REPORT], since, now, kind="morning_note")
    models = await db.models("swing", limit=5) if hasattr(db, "models") else []
    return {
        "since": since,
        "until": now,
        "days": days,
        "equity_start": start,
        "equity_end": end,
        "pnl": (end - start) if start is not None and end is not None else None,
        "max_drawdown_pct": worst * 100,
        "trades": trades,
        "notional": float(notional),
        "estimated_costs": float(notional) * cost.per_side_bps / 1e4,
        "cost_model": cost.describe(),
        "grades": [g.as_dict() for g in grades],
        "realized_closed": realized,
        "weights": (scores[0].payload.get("weights") if scores else None) or {},
        "incidents": {
            "kill_switch": [
                {
                    "at": e.occurred_at.isoformat(),
                    "engaged": e.payload.get("engaged", True),
                    "reason": e.payload.get("reason", ""),
                }
                for e in kills
            ],
            "restarts": restarts,
            "risk_rejections": rejected,
            "health_degraded": degraded,
        },
        "model_calls_estimate": cycles * 2 + briefs + notes,
        "cycles": cycles,
        "prompts": prompts.registry(),
        "models": [
            {
                "model_id": m["model_id"],
                "status": m["status"],
                "horizon": m["horizon"],
                "ic": ((m.get("metrics") or {}).get("pooled") or {}).get("ic"),
                "note": m.get("note", ""),
            }
            for m in models
        ],
    }


def render_weekly(f: dict[str, Any]) -> str:
    def money(v: float | None) -> str:
        return f"{v:+,.0f} USD" if v is not None else "n/a"

    lines = [f"# Weekly review, {f['since']:%Y-%m-%d} to {f['until']:%Y-%m-%d}", ""]
    lines.append("## Performance")
    if f["equity_start"] is not None:
        pct = (f["pnl"] / f["equity_start"] * 100) if f["equity_start"] else 0.0
        lines.append(
            f"- Equity {f['equity_start']:,.0f} to {f['equity_end']:,.0f}: **{money(f['pnl'])} ({pct:+.2f}%)**, "
            f"worst drawdown in the week {f['max_drawdown_pct']:.2f}%"
        )
    else:
        lines.append("- No portfolio snapshots in the window")
    lines.append(
        f"- {f['trades']} fills, {f['notional']:,.0f} USD traded, estimated costs {f['estimated_costs']:,.0f} USD ({f['cost_model']})"
    )
    lines.append(f"- Realized on theses closed this week: {money(f['realized_closed'])}")
    lines.append("")
    lines.append("## Post-mortems")
    if not f["grades"]:
        lines.append("- No theses closed this week")
    for g in f["grades"]:
        real = f"{g['realized_pct']:+.2f}%" if g.get("realized_pct") is not None else "n/a"
        lines.append(
            f"- **{g['symbol']}** {g['direction']}, {g['exit_kind']}, {real} vs {g['expected_pct']:+.1f}% expected: {g['verdict']}"
            + (f" _Lesson: {g['lesson']}_" if g.get("lesson") else "")
        )
    lines.append("")
    lines.append("## Analyst scoreboard")
    lines.append(f"- {render_weights(f['weights'])}")
    lines.append("")
    lines.append("## Incidents")
    inc = f["incidents"]
    lines.append(f"- Kill switch events: {len(inc['kill_switch'])}")
    for k in inc["kill_switch"][:5]:
        lines.append(
            f"  - {k['at'][:16]} {'engaged' if k['engaged'] else 'released'}: {k['reason']}"
        )
    long_gaps = [r for r in inc["restarts"] if (r.get("gap_seconds") or 0) > 1800]
    lines.append(
        f"- Restarts: {len(inc['restarts'])}, of which {len(long_gaps)} after a gap over 30 minutes"
    )
    lines.append(f"- Orders rejected by the risk officer: {inc['risk_rejections']}")
    lines.append(f"- Health events not ok: {inc['health_degraded']}")
    lines.append("")
    lines.append("## Models and prompts")
    lines.append(
        f"- Decision cycles: {f['cycles']}; model calls (estimate): {f['model_calls_estimate']}"
    )
    if f["models"]:
        for m in f["models"]:
            ic = f"ic {m['ic']:+.3f}" if m.get("ic") is not None else "no record"
            lines.append(
                f"- Forecaster {m['model_id']}: {m['status']}, {m['horizon']}-session horizon, {ic}. {m['note']}"
            )
    else:
        lines.append("- No forecaster trained yet")
    lines.append("- Prompts: " + ", ".join(f"{k} {v}" for k, v in sorted(f["prompts"].items())))
    return "\n".join(lines)
