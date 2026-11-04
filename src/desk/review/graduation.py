"""The graduation checklist: the roadmap's criteria for moving from paper to live with an approval
gate, each computed from the journal with its number, so the answer is never a feeling."""

from __future__ import annotations

import itertools
from datetime import date, datetime, timedelta
from typing import Any

from desk.core import calendar
from desk.core.events import Topics
from desk.storage.db import Database

MIN_SESSIONS = 60
MIN_SPAN_DAYS = 90
LOOKBACK_DAYS = 30


def _criterion(
    key: str, title: str, status: str, value: str, target: str, detail: str = ""
) -> dict[str, str]:
    return {
        "key": key,
        "title": title,
        "status": status,
        "value": value,
        "target": target,
        "detail": detail,
    }


async def graduation(
    db: Database, now: datetime, *, drawdown_limit_pct: float = 5.0
) -> dict[str, Any]:
    start = await db.first_event_at()
    span_days = (now - start).days if start else 0
    sessions = await db.sessions_seen(start or now, now) if start else []
    month_ago = now - timedelta(days=LOOKBACK_DAYS)
    criteria: list[dict[str, str]] = []

    n = len(sessions)
    criteria.append(
        _criterion(
            "operation",
            "At least 3 months and 60 trading days of paper operation",
            "pass" if n >= MIN_SESSIONS and span_days >= MIN_SPAN_DAYS else "pending",
            f"{n} sessions over {span_days} days",
            f"{MIN_SESSIONS} sessions, {MIN_SPAN_DAYS} days",
            f"since {start:%Y-%m-%d}" if start else "no journal yet",
        )
    )

    restarts = await db.restart_gaps(month_ago, now)
    long_gaps = [
        r for r in restarts if (r.get("gap_seconds") or 0) > 1800 and r.get("during_session")
    ]
    criteria.append(
        _criterion(
            "uninterrupted",
            "Uninterrupted operation (no session gap over 30 minutes in the last 30 days)",
            "pass" if not long_gaps else "fail",
            f"{len(long_gaps)} long gaps, {len(restarts)} restarts",
            "0 long gaps",
            "; ".join(
                f"{r.get('started_at', '')[:16]} away {int((r.get('gap_seconds') or 0) // 60)} min"
                for r in long_gaps[:3]
            ),
        )
    )

    equity = await db.equity_series(start or month_ago, now) if start else []
    if equity:
        first, last = float(equity[0][1]), float(equity[-1][1])
        peak, worst = 0.0, 0.0
        for _, e in equity:
            v = float(e)
            peak = max(peak, v)
            if peak:
                worst = min(worst, v / peak - 1)
        pnl = last - first
        pnl_pct = pnl / first * 100 if first else 0.0
        criteria.append(
            _criterion(
                "performance",
                "Positive after-cost performance in the swing book",
                "pending" if n < 20 else "pass" if pnl > 0 else "fail",
                f"{pnl:+,.0f} USD ({pnl_pct:+.2f}%) since the start",
                "> 0 after at least 20 sessions",
                f"equity {first:,.0f} to {last:,.0f}",
            )
        )
        criteria.append(
            _criterion(
                "drawdown",
                "Drawdown inside the limit",
                "pass" if -worst * 100 <= drawdown_limit_pct else "fail",
                f"worst {worst * 100:.2f}% from the peak",
                f"within {drawdown_limit_pct:.0f}%",
            )
        )
    else:
        criteria.append(
            _criterion(
                "performance",
                "Positive after-cost performance in the swing book",
                "pending",
                "no snapshots yet",
                "> 0",
            )
        )
        criteria.append(
            _criterion(
                "drawdown",
                "Drawdown inside the limit",
                "pending",
                "no snapshots yet",
                f"within {drawdown_limit_pct:.0f}%",
            )
        )

    intraday_fills = await db.count_events_between(
        [Topics.ORDER_FILLED], start or month_ago, now, book="intraday"
    )
    criteria.append(
        _criterion(
            "intraday",
            "Intraday book positive after costs, or switched off",
            "pass" if intraday_fills == 0 else "pending",
            "switched off (no intraday fills)"
            if intraday_fills == 0
            else f"{intraday_fills} intraday fills",
            "positive after costs or off",
        )
    )

    champion = await db.champion("swing", 5) if hasattr(db, "champion") else None
    if champion is None:
        criteria.append(
            _criterion(
                "forecast",
                "Forecast calibration within tolerance",
                "pass",
                "no champion model: the desk trades without forecasts",
                "calibration gap within 10 points when a champion exists",
                "the training gate refused every candidate so far",
            )
        )
    else:
        cal = ((champion.get("metrics") or {}).get("pooled") or {}).get("calibration") or []
        gap = max(
            (abs(float(c["p_mean"]) - float(c["up_rate"])) for c in cal if c.get("n", 0) >= 50),
            default=0.0,
        )
        criteria.append(
            _criterion(
                "forecast",
                "Forecast calibration within tolerance",
                "pass" if gap <= 0.10 else "fail",
                f"largest calibration gap {gap * 100:.0f} points ({champion['model_id']})",
                "within 10 points",
            )
        )

    recent_sessions = [
        s
        for s in calendar.last_sessions(calendar.session_of(now - timedelta(days=1)), 30)
        if start and s >= start.date()
    ]
    bars = await db.bar_count_per_session("SPY", month_ago, now)
    gaps = [s for s in recent_sessions if bars.get(s, 0) < 300]
    criteria.append(
        _criterion(
            "journal",
            "No unexplained journal gaps (SPY minute bars every session)",
            "pass" if not gaps else "fail",
            f"{len(gaps)} of {len(recent_sessions)} recent sessions short of 300 bars",
            "0",
            ", ".join(s.isoformat() for s in gaps[:5]),
        )
    )

    bypasses = await db.fills_without_approval(start or month_ago, now)
    criteria.append(
        _criterion(
            "risk",
            "Zero risk-rule bypasses (every fill traces to an approval)",
            "pass" if bypasses == 0 else "fail",
            f"{bypasses} fills without an approval",
            "0",
        )
    )

    weeks = []
    for w in range(4):
        a, b = now - timedelta(days=7 * (w + 1)), now - timedelta(days=7 * w)
        weeks.append(await db.count_events_between([Topics.RISK_KILL_SWITCH], a, b, engaged=True))
    weeks.reverse()  # oldest first
    trending_down = all(x >= y for x, y in itertools.pairwise(weeks)) or sum(weeks) == 0
    criteria.append(
        _criterion(
            "incidents",
            "Incident count trending down (kill switch engagements per week)",
            "pass" if trending_down else "fail",
            " / ".join(str(x) for x in weeks) + " over the last four weeks",
            "non-increasing",
        )
    )

    reconciled = await db.sessions_with_events(Topics.PORTFOLIO_RECONCILED, month_ago, now)
    missing = [s for s in recent_sessions if s not in set(reconciled)]
    criteria.append(
        _criterion(
            "reconciliation",
            "Positions reconciled against the broker every session",
            "pass" if not missing else "fail",
            f"{len(recent_sessions) - len(missing)} of {len(recent_sessions)} sessions reconciled",
            "every session",
            ", ".join(s.isoformat() for s in missing[:5]),
        )
    )

    statuses = {c["status"] for c in criteria}
    return {
        "as_of": now,
        "started": start,
        "sessions": n,
        "criteria": criteria,
        "ready": statuses == {"pass"},
        "failing": [c["key"] for c in criteria if c["status"] == "fail"],
        "pending": [c["key"] for c in criteria if c["status"] == "pending"],
    }


def render_graduation(result: dict[str, Any]) -> str:
    mark = {"pass": "PASS", "fail": "FAIL", "pending": "...."}
    lines = [
        f"GRADUATION TO LIVE WITH AN APPROVAL GATE, as of {result['as_of']:%Y-%m-%d}: "
        + (
            "READY"
            if result["ready"]
            else f"not yet ({len(result['failing'])} failing, {len(result['pending'])} pending)"
        )
    ]
    for c in result["criteria"]:
        lines.append(
            f"[{mark[c['status']]}] {c['title']}: {c['value']} (target {c['target']})"
            + (f" {c['detail']}" if c["detail"] else "")
        )
    return "\n".join(lines)


def session_dates(start: date, end: date) -> list[date]:
    return [s for s in calendar.last_sessions(end, 400) if s >= start]
