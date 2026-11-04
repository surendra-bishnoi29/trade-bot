"""Telegram notifier: the desk's phone. Subscribes to the events a person wants to hear about and sends
one short message each (wording in desk.core.notices). Sending never raises; a failed send is logged."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from collections.abc import Awaitable, Callable

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core.clock import Clock
from desk.core.events import Event, Topics
from desk.core.notices import render

log = logging.getLogger(__name__)

Sender = Callable[[str], Awaitable[None]]


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
        Topics.DESK_BRIEF,
        Topics.RESEARCH_REPORT,
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
