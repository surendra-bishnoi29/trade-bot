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
    """A clock that moves only when told to.

    Free-running (the default): sleep() advances the clock itself, which is what unit tests want.
    Driven: sleep() waits until advance_to() passes the wake-up time. The replayer drives the clock
    from historical events, and every agent's timer (polling a fill, refreshing the book, the
    scheduler's slices) wakes in historical order instead of racing ahead."""

    def __init__(self, start: datetime, *, driven: bool = False) -> None:
        self._now = utc(start)
        self.driven = driven
        self._waiters: list[tuple[datetime, asyncio.Future[None]]] = []

    def now(self) -> datetime:
        return self._now

    def advance_to(self, t: datetime) -> None:
        t = utc(t)
        if t < self._now:
            raise ValueError(
                f"simulated clock cannot go backwards: {t.isoformat()} < {self._now.isoformat()}"
            )
        self._now = t
        self._wake()

    def next_wakeup(self) -> datetime | None:
        """The earliest time a sleeping task wants to resume, if any."""
        return min((at for at, _ in self._waiters), default=None)

    def _wake(self) -> None:
        due = [(at, fut) for at, fut in self._waiters if at <= self._now]
        self._waiters = [(at, fut) for at, fut in self._waiters if at > self._now]
        for _, fut in due:
            if not fut.done():
                fut.set_result(None)

    async def sleep(self, seconds: float) -> None:
        if not self.driven:
            self._now += timedelta(seconds=seconds)
            self._wake()
            return
        target = self._now + timedelta(seconds=max(0.0, seconds))
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((target, fut))
        await fut
