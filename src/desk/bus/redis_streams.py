"""Redis Streams bus: one stream per topic, one consumer group per agent (technical design 3).

Semantics: XADD with MAXLEN; XREADGROUP per group; XACK after the handler returns; a failed
handler leaves the entry pending, XAUTOCLAIM hands it back after min_idle_ms, and after
max_deliveries attempts the event is dead-lettered and acknowledged.
"""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Sequence
from dataclasses import dataclass

import redis.asyncio as aioredis
from redis.exceptions import ResponseError

from desk.bus.base import Handler, dead_letter_event, expand_topics
from desk.core.clock import Clock
from desk.core.events import Event, Topics


@dataclass(frozen=True)
class _Sub:
    topics: tuple[str, ...]
    group: str
    handler: Handler
    max_deliveries: int


class RedisStreamsBus:
    def __init__(
        self,
        url: str,
        clock: Clock,
        *,
        prefix: str = "desk:",
        maxlen: int = 1_000_000,
        block_ms: int = 500,
        min_idle_ms: int = 30_000,
        consumer: str | None = None,
    ) -> None:
        self._r = aioredis.from_url(url, decode_responses=True)
        self._clock = clock
        self._prefix = prefix
        self._maxlen = maxlen
        self._block_ms = block_ms
        self._min_idle_ms = min_idle_ms
        self._consumer = consumer or f"{socket.gethostname()}-{os.getpid()}"
        self._subs: list[_Sub] = []
        self._stop = asyncio.Event()

    def stream(self, topic: str) -> str:
        return f"{self._prefix}{topic}"

    async def publish(self, event: Event) -> str:
        return await self._r.xadd(
            self.stream(event.type),
            {"event": event.to_json()},
            maxlen=self._maxlen,
            approximate=True,
        )

    def subscribe(
        self, topics: Sequence[str], group: str, handler: Handler, *, max_deliveries: int = 3
    ) -> None:
        self._subs.append(_Sub(expand_topics(topics), group, handler, max_deliveries))

    async def _ensure_groups(self, sub: _Sub) -> None:
        for topic in sub.topics:
            try:
                await self._r.xgroup_create(self.stream(topic), sub.group, id="$", mkstream=True)
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise

    async def run(self) -> None:
        self._stop.clear()
        for sub in self._subs:
            await self._ensure_groups(sub)
        await asyncio.gather(*(self._loop(sub) for sub in self._subs))

    async def _loop(self, sub: _Sub) -> None:
        streams = {self.stream(t): ">" for t in sub.topics}
        while not self._stop.is_set():
            await self._reclaim(sub)
            response = await self._r.xreadgroup(
                sub.group, self._consumer, streams, count=100, block=self._block_ms
            )
            for stream, entries in response or []:
                for msg_id, fields in entries:
                    await self._handle(sub, stream, msg_id, fields, deliveries=1)

    async def _reclaim(self, sub: _Sub) -> None:
        for topic in sub.topics:
            stream = self.stream(topic)
            result = await self._r.xautoclaim(
                stream,
                sub.group,
                self._consumer,
                min_idle_time=self._min_idle_ms,
                start_id="0-0",
                count=100,
            )
            entries = result[1] if result and len(result) > 1 else []
            for msg_id, fields in entries:
                pending = await self._r.xpending_range(
                    stream, sub.group, min=msg_id, max=msg_id, count=1
                )
                deliveries = int(pending[0]["times_delivered"]) if pending else 1
                await self._handle(sub, stream, msg_id, fields, deliveries=deliveries)

    async def _handle(
        self, sub: _Sub, stream: str, msg_id: str, fields: dict, deliveries: int
    ) -> None:
        try:
            event = Event.from_json(fields["event"])
        except Exception as exc:  # noqa: BLE001 - a malformed entry is dead-lettered, never retried
            now = self._clock.now()
            await self.publish(
                Event(
                    type=Topics.SYSTEM_DEAD_LETTER,
                    occurred_at=now,
                    ingested_at=now,
                    source="bus",
                    payload={
                        "raw": fields,
                        "error": f"{type(exc).__name__}: {exc}",
                        "deliveries": deliveries,
                    },
                )
            )
            await self._r.xack(stream, sub.group, msg_id)
            return
        try:
            await sub.handler(event)
        except Exception as exc:  # noqa: BLE001 - retried via pending entries, then dead-lettered
            if deliveries >= sub.max_deliveries:
                dead = dead_letter_event(
                    event, f"{type(exc).__name__}: {exc}", deliveries, self._clock.now()
                )
                if event.type != Topics.SYSTEM_DEAD_LETTER:
                    await self.publish(dead)
                await self._r.xack(stream, sub.group, msg_id)
            return
        await self._r.xack(stream, sub.group, msg_id)

    async def stop(self) -> None:
        self._stop.set()

    async def close(self) -> None:
        self._stop.set()
        await self._r.aclose()
