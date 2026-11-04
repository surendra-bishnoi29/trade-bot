"""Telegram notifier: the desk's phone. Subscribes to the events a person wants to hear about and sends
one short message each. Sending never raises; a failed send is logged and dropped."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core.clock import Clock
from desk.core.events import Event, Topics

log = logging.getLogger(__name__)

Sender = Callable[[str], Awaitable[None]]


def _money(v: Any) -> str:
    try:
        return f"{Decimal(str(v)):,.2f}"
    except Exception:  # noqa: BLE001
        return str(v)


def _first_sentence(text: str, limit: int = 220) -> str:
    text = " ".join(str(text).split())
    cut = text.find(". ")
    out = text[: cut + 1] if 0 < cut < limit else text[:limit]
    return out if len(out) <= limit else out[: limit - 1] + "…"


def render(event: Event) -> str | None:
    """The message for an event, or None when the event is not worth a notification."""
    p = event.payload
    t = event.type
    if t == Topics.ORDER_FILLED:
        side = str(p.get("side") or "").upper() or "FILLED"
        qty = p.get("filled_quantity") or p.get("quantity") or ""
        price = p.get("avg_price") or p.get("filled_avg_price")
        return f"✅ {side} {qty} {event.symbol} filled" + (f" at {_money(price)}" if price else "")
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
        return (
            f"☀️ Desk is up. Kill switch {'ON' if p.get('kill_switch') else 'off'}, "
            f"{p.get('open_theses', 0)} open theses, next cycle {p.get('next_cycle', '')}."
        )
    if t == Topics.THESIS_CREATED:
        return (
            f"🧭 New thesis: {str(p.get('direction', 'long')).upper()} {p.get('quantity')} {event.symbol}, "
            f"stop {_money(p.get('stop_price'))}, target {_money(p.get('target_price'))}, "
            f"horizon {p.get('horizon_days')} days.\nWhy: {_first_sentence(p.get('narrative', ''))}"
        )
    if t == Topics.THESIS_CLOSED:
        pnl = p.get("realized_pnl")
        return f"🏁 Closed {event.symbol}: {p.get('reason', '')}" + (
            f". P&L {_money(pnl)}" if pnl is not None else ""
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
                f"(conviction {d.get('conviction', 0):.2f}): {_first_sentence(d.get('thesis', ''))}"
            )
        challenge = p.get("challenge") or {}
        objections = challenge.get("objections") or []
        if objections:
            worst = max(
                objections, key=lambda o: {"blocking": 2, "concern": 1}.get(o.get("severity"), 0)
            )
            lines.append(
                f"🧐 Skeptic ({worst.get('severity')} on {worst.get('symbol')}): {_first_sentence(worst.get('argument', ''))}"
            )
        return "\n".join(lines)
    return None


class TelegramNotifier(BaseAgent):
    name = "telegram"
    topics = (
        Topics.ORDER_FILLED,
        Topics.RISK_REJECTED,
        Topics.RISK_KILL_SWITCH,
        Topics.WATCHER_ALERT,
        Topics.SYSTEM_HEALTH,
        Topics.SYSTEM_RESTARTED,
        Topics.SYSTEM_HEARTBEAT,
        Topics.THESIS_CREATED,
        Topics.THESIS_CLOSED,
        Topics.CYCLE_PLAN,
    )

    def __init__(
        self, bus: Bus, clock: Clock, token: str, chat_id: str, *, send: Sender | None = None
    ) -> None:
        super().__init__(bus, clock)
        self.token = token
        self.chat_id = chat_id
        self.send = send or self._send_http
        self.sent: list[str] = []

    async def handle(self, event: Event) -> None:
        text = render(event)
        if not text:
            return
        try:
            await self.send(text)
            self.sent.append(text)
        except Exception as exc:  # noqa: BLE001 - a notification must never break the desk
            log.warning("telegram send failed: %s", exc)

    async def _send_http(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        body = json.dumps({"chat_id": self.chat_id, "text": text[:4000]}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})

        def post() -> None:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()

        await asyncio.to_thread(post)
