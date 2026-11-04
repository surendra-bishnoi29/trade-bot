"""Monitor: engages the kill switch on its own when the desk cannot trust what it sees."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core.clock import Clock
from desk.core.events import Event, Topics
from desk.core.payloads import KillSwitch

log = logging.getLogger(__name__)


class Monitor(BaseAgent):
    """Three triggers: the market data feed goes silent during the session, orders are rejected
    repeatedly, or the broker stops answering. Quiet individual symbols are not a trigger: on the
    free IEX feed a name can print no bar for minutes while the feed itself is fine."""

    name = "monitor"
    topics = (
        Topics.WATCHER_ALERT,
        Topics.ORDER_REJECTED,
        Topics.ORDER_FILLED,
        Topics.SYSTEM_HEALTH,
    )

    def __init__(
        self,
        bus: Bus,
        clock: Clock,
        n_symbols: int,
        *,
        rejects_in_a_row: int = 3,
        cooldown: timedelta = timedelta(minutes=15),
    ) -> None:
        super().__init__(bus, clock)
        self.n_symbols = max(n_symbols, 1)
        self.rejects_in_a_row = rejects_in_a_row
        self.cooldown = cooldown
        self.rejects = 0
        self.last_engaged: datetime | None = None

    async def _engage(self, reason: str, caused_by: Event) -> None:
        now = self.clock.now()
        if self.last_engaged and now - self.last_engaged < self.cooldown:
            return
        self.last_engaged = now
        log.error("engaging the kill switch: %s", reason)
        await self.emit(
            Topics.RISK_KILL_SWITCH,
            KillSwitch(engaged=True, reason=reason, source=self.name),
            caused_by=caused_by,
        )

    async def handle(self, event: Event) -> None:
        if event.type == Topics.WATCHER_ALERT and event.payload.get("kind") == "feed_stale":
            await self._engage(f"market data feed stale: {event.payload.get('details')}", event)
        elif event.type == Topics.ORDER_REJECTED:
            self.rejects += 1
            if self.rejects >= self.rejects_in_a_row:
                await self._engage(f"{self.rejects} orders rejected in a row", event)
        elif event.type == Topics.ORDER_FILLED:
            self.rejects = 0
        elif event.type == Topics.SYSTEM_HEALTH and event.payload.get("status") == "down":
            await self._engage(
                f"{event.payload.get('component')} is down: {event.payload.get('detail')}", event
            )
