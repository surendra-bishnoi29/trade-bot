"""BaseAgent: subscribe to topics, handle events, emit new ones with lineage stamped on."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from desk.bus.base import Bus
from desk.core.clock import Clock
from desk.core.events import Book, Event, validate_payload


class BaseAgent:
    name: str = "agent"
    version: str = "0.1"
    topics: Sequence[str] = ()

    def __init__(self, bus: Bus, clock: Clock) -> None:
        self.bus = bus
        self.clock = clock

    @property
    def source(self) -> str:
        return f"{self.name}@{self.version}"

    def start(self, *, max_deliveries: int = 3) -> None:
        if self.topics:
            self.bus.subscribe(
                self.topics, group=self.name, handler=self.handle, max_deliveries=max_deliveries
            )

    async def handle(self, event: Event) -> None:  # pragma: no cover - overridden by agents
        raise NotImplementedError

    async def emit(
        self,
        type: str,
        payload: dict[str, Any] | BaseModel,
        *,
        symbol: str | None = None,
        book: Book | None = None,
        caused_by: Event | None = None,
        correlation_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> Event:
        now = self.clock.now()
        event = Event(
            type=type,
            occurred_at=occurred_at or now,
            ingested_at=now,
            symbol=symbol if symbol is not None else (caused_by.symbol if caused_by else None),
            book=book if book is not None else (caused_by.book if caused_by else None),
            correlation_id=correlation_id or (caused_by.correlation_id if caused_by else None),
            causation_id=caused_by.event_id if caused_by else None,
            source=self.source,
            payload=validate_payload(type, payload),
        )
        await self.bus.publish(event)
        return event
