import os
from datetime import UTC, datetime

import pytest

import desk.core.payloads  # noqa: F401  (registers the payload schemas)
from desk.core.clock import SimClock
from desk.core.events import Event, Topics

T0 = datetime(2026, 11, 3, 14, 30, tzinfo=UTC)


@pytest.fixture
def clock() -> SimClock:
    return SimClock(T0)


@pytest.fixture
def redis_url() -> str:
    url = os.environ.get("DESK_TEST_REDIS_URL")
    if not url:
        pytest.skip("set DESK_TEST_REDIS_URL to run the Redis bus tests")
    return url


@pytest.fixture
def database_url() -> str:
    url = os.environ.get("DESK_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set DESK_TEST_DATABASE_URL to run the PostgreSQL tests")
    return url


def make_event(clock: SimClock, type: str = Topics.MARKET_BAR_1M, **kw) -> Event:
    now = clock.now()
    return Event(type=type, occurred_at=now, ingested_at=now, source="test@0.1", **kw)


def collector(sink: list):
    async def handler(event: Event) -> None:
        sink.append(event)

    return handler
