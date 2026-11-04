"""The desk's clock. Every agent reads time from here, never from the system clock.

LiveClock is the wall clock. SimClock moves only when told to; the replayer advances it to each
historical event's occurred_at so agents behave identically in replay and in production.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


def utc(t: datetime) -> datetime:
    """Return t as an aware UTC datetime. A naive datetime is a bug, not a default."""
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError("naive datetime: every timestamp on the desk must carry a timezone")
    return t.astimezone(UTC)


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class LiveClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class SimClock:
    """A clock that moves only when told to."""

    def __init__(self, start: datetime) -> None:
        self._now = utc(start)

    def now(self) -> datetime:
        return self._now

    def advance_to(self, t: datetime) -> None:
        t = utc(t)
        if t < self._now:
            raise ValueError(
                f"simulated clock cannot go backwards: {t.isoformat()} < {self._now.isoformat()}"
            )
        self._now = t

    async def sleep(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
