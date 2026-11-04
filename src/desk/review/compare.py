"""Replay comparison: the desk's decisions against a rules-only baseline over the same sessions.

Each session is replayed twice through the production agents with the simulated broker: once with
the scripted head trader (the rules-only baseline: the scorecard's top name, fixed conviction) and,
when asked, once with the real head trader and skeptic (cached, so a rerun is free). Holding SPY from
the open to the close is the reference for the tape. Costs are always on."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from desk.core import calendar
from desk.core.config import Settings
from desk.replay.assemble import run_replay
from desk.replay.replayer import ReplayReport
from desk.storage.db import Database

log = logging.getLogger(__name__)


def summarize(report: ReplayReport) -> dict[str, Any]:
    pnl = report.realized_pnl + report.unrealized_pnl
    return {
        "pnl": float(pnl),
        "return_pct": float(pnl / report.equity_start * 100) if report.equity_start else 0.0,
        "costs": float(report.costs),
        "fills": len(report.fills),
        "cycles": len(report.cycles),
        "theses": len(report.theses),
        "max_drawdown_pct": report.max_drawdown_pct,
        "kill_switch": len(report.kill_switch),
        "llm_misses": int((report.llm or {}).get("misses", 0)),
    }


async def spy_day_return(db: Database, day: date) -> float | None:
    open_at, close_at = calendar.session_bounds(day)
    bars = await db.bars_between(["SPY"], "1m", open_at, close_at)
    if len(bars) < 2:
        return None
    return float(Decimal(str(bars[-1]["close"])) / Decimal(str(bars[0]["open"])) - 1) * 100


async def compare(
    settings: Settings,
    days: int,
    *,
    llm: bool = False,
    capital: Decimal = Decimal(100000),
    slippage_bps: Decimal = Decimal(5),
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    last = calendar.session_of(now - timedelta(days=1))
    sessions = calendar.last_sessions(last, days)
    db = Database(settings.database_url)
    await db.open()
    rows: list[dict[str, Any]] = []
    try:
        for day in sessions:
            row: dict[str, Any] = {"day": day.isoformat(), "spy_pct": await spy_day_return(db, day)}
            rules = await run_replay(
                settings, day, llm=False, capital=capital, slippage_bps=slippage_bps
            )
            row["rules"] = summarize(rules)
            if llm:
                head = await run_replay(
                    settings, day, llm=True, capital=capital, slippage_bps=slippage_bps
                )
                row["head_trader"] = summarize(head)
            rows.append(row)
            log.info("compared %s", day)
    finally:
        await db.close()
    return {
        "days": days,
        "llm": llm,
        "capital": float(capital),
        "rows": rows,
        "totals": totals(rows),
    }


def totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for leg in ("rules", "head_trader"):
        legs = [r[leg] for r in rows if leg in r]
        if not legs:
            continue
        out[leg] = {
            "pnl": sum(x["pnl"] for x in legs),
            "return_pct": sum(x["return_pct"] for x in legs),
            "costs": sum(x["costs"] for x in legs),
            "fills": sum(x["fills"] for x in legs),
            "worst_day_drawdown_pct": min((x["max_drawdown_pct"] for x in legs), default=0.0),
            "days_positive": sum(1 for x in legs if x["pnl"] > 0),
            "llm_misses": sum(x["llm_misses"] for x in legs),
        }
    spy = [r["spy_pct"] for r in rows if r.get("spy_pct") is not None]
    out["spy_pct"] = sum(spy) if spy else None
    return out


def render(result: dict[str, Any]) -> str:
    lines = [
        f"REPLAY COMPARISON over {len(result['rows'])} sessions, capital {result['capital']:,.0f}, costs on"
        + (
            " (head trader vs rules-only)"
            if result["llm"]
            else " (rules-only; add --llm for the head trader)"
        )
    ]
    head = f"{'day':<11} {'SPY':>7} | {'rules P&L':>10} {'ret':>7} {'fills':>5} {'maxDD':>6}"
    if result["llm"]:
        head += f" | {'head P&L':>10} {'ret':>7} {'fills':>5} {'maxDD':>6} {'calls':>5}"
    lines.append(head)
    for r in result["rows"]:
        spy = f"{r['spy_pct']:+.2f}%" if r.get("spy_pct") is not None else "   n/a"
        a = r["rules"]
        line = f"{r['day']:<11} {spy:>7} | {a['pnl']:>+10,.0f} {a['return_pct']:>+6.2f}% {a['fills']:>5} {a['max_drawdown_pct']:>5.2f}%"
        if "head_trader" in r:
            b = r["head_trader"]
            line += f" | {b['pnl']:>+10,.0f} {b['return_pct']:>+6.2f}% {b['fills']:>5} {b['max_drawdown_pct']:>5.2f}% {b['llm_misses']:>5}"
        lines.append(line)
    t = result["totals"]
    spy_total = f"{t['spy_pct']:+.2f}%" if t.get("spy_pct") is not None else "n/a"
    for leg, label in (("rules", "rules-only"), ("head_trader", "head trader")):
        if leg in t:
            x = t[leg]
            lines.append(
                f"{label}: P&L {x['pnl']:+,.0f} ({x['return_pct']:+.2f}%), costs {x['costs']:,.0f}, {x['fills']} fills, "
                f"{x['days_positive']} of {len(result['rows'])} days positive, worst day drawdown {x['worst_day_drawdown_pct']:.2f}%"
                + (f", {x['llm_misses']} model calls" if leg == "head_trader" else "")
            )
    lines.append(f"SPY open-to-close over the same sessions: {spy_total}")
    return "\n".join(lines)
