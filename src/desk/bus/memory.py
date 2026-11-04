"""In-process bus for tests and single-process runs. Same contract as the Redis bus."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field

from desk.bus.base import Handler, dead_letter_event, topic_matches
from desk.core.clock import Clock
from desk.core.events import Event, Topics


@dataclass
class _Sub:
    topics: tuple[str, ...]
    group: str
    handler: Handler
    max_deliveries: int
    queue: asyncio.Queue[Event] = field(default_factory=asyncio.Queue)


class InMemoryBus:
    def __init__(self, clock: Clock, *, retry_delay: float = 0.0) -> None:
        self._clock = clock
        self._retry_delay = retry_delay
        self._subs: list[_Sub] = []
        self._tasks: list[asyncio.Task[None]] = []
        self.log: list[Event] = []
        self.dead_letters: list[Event] = []

    async def publish(self, event: Event) -> str:
        self.log.append(event)
        for sub in self._subs:
            if any(topic_matches(p, event.type) for p in sub.topics):
                await sub.queue.put(event)
        return event.event_id

    def subscribe(
        self, topics: Sequence[str], group: str, handler: Handler, *, max_deliveries: int = 3
    ) -> None:
        self._subs.append(_Sub(tuple(topics), group, handler, max_deliveries))

    async def run(self) -> None:
        self._tasks = [
            asyncio.create_task(self._consume(s), name=f"bus:{s.group}") for s in self._subs
        ]
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _consume(self, sub: _Sub) -> None:
        while True:
            event = await sub.queue.get()
            try:
                await self._deliver(sub, event)
            finally:
                sub.queue.task_done()

    async def _deliver(self, sub: _Sub, event: Event) -> None:
        error = ""
        for attempt in range(1, sub.max_deliveries + 1):
            try:
                await sub.handler(event)
                return
            except Exception as exc:  # noqa: BLE001 - the bus must survive any handler failure
                error = f"{type(exc).__name__}: {exc}"
                if attempt < sub.max_deliveries and self._retry_delay:
                    await asyncio.sleep(self._retry_delay)
        dead = dead_letter_event(event, error, sub.max_deliveries, self._clock.now())
        self.dead_letters.append(dead)
        if event.type != Topics.SYSTEM_DEAD_LETTER:
            await self.publish(dead)

    async def wait_idle(self) -> None:
        """Tests: block until every queued event has been handled."""
        await asyncio.gather(*(s.queue.join() for s in self._subs))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def close(self) -> None:
        await self.stop()
