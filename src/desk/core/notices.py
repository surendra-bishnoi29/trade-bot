"""One-line, human-readable notices for events: what a person should hear about, in plain words.
Shared by the Telegram notifier and the interface's activity feed."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from desk.core.events import Event, Topics


def money(v: Any) -> str:
    try:
        return f"{Decimal(str(v)):,.2f}"
    except Exception:  # noqa: BLE001 - anything odd is shown as-is
        return str(v)


def first_sentence(text: str, limit: int = 220) -> str:
    text = " ".join(str(text).split())
    cut = text.find(". ")
    out = text[: cut + 1] if 0 < cut < limit else text[:limit]
    return out if len(out) <= limit else out[: limit - 1] + "…"


def render(event: Event) -> str | None:
    """The notification for an event, or None when it is not worth interrupting anyone."""
    p = event.payload
    t = event.type
    if t == Topics.ORDER_FILLED:
        side = str(p.get("side") or "").upper() or "FILLED"
        qty = p.get("filled_quantity") or p.get("quantity") or ""
        price = p.get("avg_price") or p.get("filled_avg_price")
        return f"✅ {side} {qty} {event.symbol} filled" + (f" at {money(price)}" if price else "")
    if t == Topics.RISK_REJECTED:
        intent = p.get("intent") or {}
        return (
            f"⛔ Order rejected: {intent.get('side', '')} {intent.get('quantity', '')} {event.symbol}. "
            f"{p.get('reason', '')}"
        )
    if t == Topics.RISK_KILL_SWITCH:
        if p.get("engaged", True):
            return f"🛑 Kill switch ENGAGED: {p.get('reason', '')}. Only exits pass until it is released."
        return f"🟢 Kill switch released: {p.get('reason', '')}"
    if t == Topics.WATCHER_ALERT:
        kind = p.get("kind")
        if kind == "feed_stale":
            return f"⚠️ Price feed stale: {p.get('detail', '')}"
        if kind == "feed_recovered":
            return "✅ Price feed recovered"
        return None
    if t == Topics.SYSTEM_HEALTH:
        if p.get("status") == "ok":
            return None
        icon = "🔴" if p.get("status") == "down" else "🟠"
        return f"{icon} {p.get('component', 'component')} {p.get('status')}: {p.get('detail', '')}"
    if t == Topics.SYSTEM_RESTARTED:
        gap = p.get("gap_seconds")
        away = f"away {int(gap // 60)} min" if isinstance(gap, (int, float)) else "first start"
        session = " during the session" if p.get("during_session") else ""
        actions = ", ".join(p.get("actions") or [])
        return f"🔁 Desk started ({away}{session}). " + (
            f"Closing the gap: {actions}." if actions else ""
        )
    if t == Topics.SYSTEM_HEARTBEAT:
        if not p.get("morning"):
            return None
        if p.get("lab"):
            return (
                f"☀️ Lab desk is up. Kill switch {'ON' if p.get('kill_switch') else 'off'}, "
                f"{p.get('open_lines', 0)} positions on lines; the sentinel looks every minute."
            )
        return (
            f"☀️ Desk is up. Kill switch {'ON' if p.get('kill_switch') else 'off'}, "
            f"{p.get('open_theses', 0)} open theses, next cycle {p.get('next_cycle', '')}."
        )
    if t == Topics.THESIS_CREATED:
        return (
            f"🧭 New thesis: {str(p.get('direction', 'long')).upper()} {p.get('quantity')} {event.symbol}, "
            f"stop {money(p.get('stop_price'))}, target {money(p.get('target_price'))}, "
            f"horizon {p.get('horizon_days')} days.\nWhy: {first_sentence(p.get('narrative', ''))}"
        )
    if t == Topics.THESIS_CLOSED:
        pnl = p.get("realized_pnl")
        return f"🏁 Closed {event.symbol}: {p.get('reason', '')}" + (
            f". P&L {money(pnl)}" if pnl is not None else ""
        )
    if t == Topics.RESEARCH_REPORT and p.get("kind") == "morning_note":
        n = p.get("note") or {}
        watch = "\n".join(
            f"• {w.get('symbol')}: {w.get('why')}" for w in (n.get("watchlist") or [])[:5]
        )
        return (
            f"🌅 Morning note: {n.get('headline', '')}\n{n.get('tape', '')}"
            + (f"\nWatchlist:\n{watch}" if watch else "")
            + (f"\nRisks: {n.get('risks')}" if n.get("risks") else "")
        )
    if t == Topics.REVIEW_POSTMORTEM:
        lesson = f"\nLesson: {p.get('lesson')}" if p.get("lesson") else ""
        return f"🔍 Post-mortem {event.symbol}: {p.get('verdict', '')}{lesson}"
    if t == Topics.REVIEW_ANALYST_SCORE:
        weights = p.get("weights") or {}
        ranked = sorted(
            ((k, w) for k, w in weights.items() if w.get("status") != "unproven"),
            key=lambda kv: -float(kv[1].get("weight", 1)),
        )
        if not ranked:
            return (
                f"📊 Analyst scoreboard: {p.get('calls', 0)} calls, none with ten scored calls yet"
            )
        best = ", ".join(f"{k} {float(w['hit_5d']):.0%}" for k, w in ranked[:3])
        worst = (
            ", ".join(f"{k} {float(w['hit_5d']):.0%}" for k, w in ranked[-2:])
            if len(ranked) > 3
            else ""
        )
        return f"📊 Analyst scoreboard ({p.get('days')} days): best {best}" + (
            f"; weakest {worst}" if worst else ""
        )
    if t == Topics.INTRADAY_TRADE:
        pnl = p.get("realized_pnl")
        return (
            f"⚡ Intraday {str(p.get('side', '')).upper()} {p.get('quantity')} {event.symbol} closed ({p.get('reason', '')}): "
            f"{money(p.get('entry_price'))} to {money(p.get('exit_price'))}, P&L {money(pnl)}. {p.get('note', '')}"
        )
    if t == Topics.REVIEW_WEEKLY:
        f = p.get("facts") or {}
        pnl = f.get("pnl")
        pnl_text = f"P&L {pnl:+,.0f} USD" if isinstance(pnl, (int, float)) else "no P&L yet"
        inc = f.get("incidents") or {}
        return (
            f"🗓 Weekly review to {p.get('week_ending', '')}: {pnl_text}, {f.get('trades', 0)} fills, "
            f"{len(f.get('grades') or [])} post-mortems, {len(inc.get('kill_switch') or [])} kill-switch events, "
            f"{inc.get('risk_rejections', 0)} orders rejected. Full review in the interface."
        )
    if t == Topics.DESK_BRIEF:
        b = p.get("brief") or {}
        return (
            f"📋 Daily brief {p.get('day', '')}: {b.get('headline', '')}\n{b.get('summary', '')}\n"
            f"Tomorrow: {b.get('tomorrow', '')}"
        )
    if t == Topics.CYCLE_PLAN:
        if p.get("dry_run"):
            return None
        plan = p.get("plan") or {}
        decisions = plan.get("decisions") or []
        trades = [d for d in decisions if d.get("action") in ("buy", "sell")]
        holds = sum(1 for d in decisions if d.get("action") == "hold")
        lines = [f"🧠 Cycle {p.get('trigger', '')}: {len(trades)} trades, {holds} holds"]
        for d in trades[:4]:
            lines.append(
                f"• {d['action'].upper()} {d.get('quantity', '')} {d.get('symbol', '')} "
                f"(conviction {d.get('conviction', 0):.2f}): {first_sentence(d.get('thesis', ''))}"
            )
        challenge = p.get("challenge") or {}
        objections = challenge.get("objections") or []
        if objections:
            rank = {"blocking": 2, "concern": 1}
            worst = max(objections, key=lambda o: rank.get(o.get("severity"), 0))
            lines.append(
                f"🧐 Skeptic ({worst.get('severity')} on {worst.get('symbol')}): "
                f"{first_sentence(worst.get('argument', ''))}"
            )
        return "\n".join(lines)
    return None


def summarize(event: Event) -> str:
    """A line for every journal event the interface lists; render() first, then plain fallbacks."""
    text = render(event)
    if text:
        return text
    p = event.payload
    t = event.type
    sym = event.symbol or ""
    if t == Topics.ORDER_SUBMITTED:
        return f"Order sent to the broker: {p.get('side', '')} {sym}".strip()
    if t == Topics.ORDER_REJECTED:
        return f"Broker rejected the order for {sym}: {p.get('detail', '')}"
    if t == Topics.RISK_APPROVED:
        intent = p.get("intent") or {}
        return f"Risk officer approved {intent.get('side', '')} {intent.get('quantity', '')} {sym}: {p.get('reason', '')}"
    if t == Topics.CYCLE_STAGE:
        return f"{p.get('stage', '')}: {p.get('detail', '')}"
    if t == Topics.RESEARCH_REPORT:
        kind = p.get("kind")
        if kind == "scorecard":
            return f"scorecard ({p.get('when', '')}): top {', '.join((p.get('top') or [])[:5])}"
        if kind == "morning_note":
            return f"morning note: {(p.get('note') or {}).get('headline', '')}"
        if kind == "playbook":
            return first_sentence(p.get("summary", ""), 200)
        stance = f"{p.get('stance')} " if p.get("stance") not in (None, "none") else ""
        return f"{kind} {sym}: {stance}{first_sentence(p.get('summary', ''), 160)}".replace(
            "  ", " "
        )
    if t == Topics.NEWS_STATE:
        return f"news state {sym}: tone {p.get('tone_48h')}, {p.get('count_24h')} articles in 24h"
    if t == Topics.FILING_NEW:
        return f"{p.get('form')} filed by {sym}: items {', '.join(p.get('items') or []) or 'none'}"
    if t == Topics.CYCLE_STARTED:
        return "Cycle started"
    if t == Topics.CYCLE_COMPLETED:
        return f"Cycle finished: {p.get('detail', '')}"
    if t == Topics.CYCLE_FAILED:
        return f"Cycle failed: {p.get('detail', '')}"
    if t == Topics.THESIS_UPDATED:
        return f"Thesis {sym} is now {p.get('status', '')}" + (
            f": {p['reason']}" if p.get("reason") else ""
        )
    if t == Topics.THESIS_INVALIDATED:
        return f"Thesis {sym} invalidated: {p.get('reason', '')}"
    if t == Topics.CONSOLE_QUESTION:
        return f"{p.get('user') or 'console'} asked: {first_sentence(p.get('text', ''), 160)}"
    if t == Topics.CONSOLE_ANSWER:
        return f"Head trader: {first_sentence(p.get('text', ''), 200)}"
    if t == Topics.SYSTEM_HEARTBEAT:
        if p.get("lab"):
            return f"Heartbeat: {p.get('status', '')}, {p.get('open_lines', 0)} positions on lines"
        return f"Heartbeat: {p.get('status', '')}, next cycle {p.get('next_cycle', '')}"
    if t == Topics.WATCHER_ALERT:
        return f"{p.get('kind', 'alert')} on {sym}"
    if t == Topics.DESK_COMMAND_RESULT:
        return f"Command {p.get('command', '')}: {'done' if p.get('ok') else 'failed'}"
    return t.replace(".", " ")
