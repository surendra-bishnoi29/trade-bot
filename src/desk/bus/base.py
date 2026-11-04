"""The bus contract. Agents never call each other; they publish and subscribe here.

Delivery is at least once. A handler that raises is retried up to max_deliveries, then the event
goes to the dead-letter topic and is acknowledged, so a poison message never blocks a group.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from desk.core.events import Event, Topics

Handler = Callable[[Event], Awaitable[None]]


@dataclass(frozen=True)
class Subscription:
    topics: tuple[str, ...]
    group: str
    handler: Handler
    max_deliveries: int = 3


def topic_matches(pattern: str, topic: str) -> bool:
    """Exact topic, '*' for everything, or 'market.*' for a domain."""
    if pattern == "*" or pattern == topic:
        return True
    if pattern.endswith(".*"):
        return topic.startswith(pattern[:-1])
    return False


def expand_topics(patterns: Sequence[str]) -> tuple[str, ...]:
    """Resolve patterns against the known topic list (streams need concrete names)."""
    out: list[str] = []
    for p in patterns:
        if "*" in p:
            out.extend(t for t in Topics.all() if topic_matches(p, t))
        else:
            out.append(p)
    return tuple(dict.fromkeys(out))


def dead_letter_event(original: Event, error: str, deliveries: int, now: datetime) -> Event:
    return Event(
        type=Topics.SYSTEM_DEAD_LETTER,
        occurred_at=now,
        ingested_at=now,
        symbol=original.symbol,
        book=original.book,
        correlation_id=original.correlation_id,
        causation_id=original.event_id,
        source="bus",
        payload={
            "original": original.model_dump(mode="json"),
            "error": error,
            "deliveries": deliveries,
        },
    )


class Bus(Protocol):
    async def publish(self, event: Event) -> str: ...

    def subscribe(
        self, topics: Sequence[str], group: str, handler: Handler, *, max_deliveries: int = 3
    ) -> None: ...

    async def run(self) -> None: ...

    async def stop(self) -> None: ...

    async def close(self) -> None: ...
