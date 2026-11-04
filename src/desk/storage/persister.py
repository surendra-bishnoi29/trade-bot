"""The persister: one consumer group that writes every event to PostgreSQL."""

from __future__ import annotations

from desk.bus.base import Bus
from desk.core.events import Event, Topics
from desk.storage.db import Database


class Persister:
    name = "persister"
    skip = frozenset(
        {Topics.CONSOLE_ANSWER_DELTA}
    )  # chunks of an answer; the whole answer is journaled

    def __init__(self, bus: Bus, db: Database) -> None:
        self._bus = bus
        self._db = db

    def start(self) -> None:
        self._bus.subscribe(["*"], group=self.name, handler=self.handle, max_deliveries=5)

    async def handle(self, event: Event) -> None:
        if event.type in self.skip:
            return
        await self._db.insert_event(event)
