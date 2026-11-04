from datetime import UTC, datetime, timedelta

import pytest

from desk.core.clock import LiveClock, SimClock, utc

T0 = datetime(2026, 11, 3, 14, 30, tzinfo=UTC)


def test_sim_clock_only_moves_forward():
    c = SimClock(T0)
    c.advance_to(T0 + timedelta(hours=1))
    assert c.now() == T0 + timedelta(hours=1)
    with pytest.raises(ValueError):
        c.advance_to(T0)


async def test_sim_clock_sleep_advances_without_waiting():
    c = SimClock(T0)
    await c.sleep(90)
    assert c.now() == T0 + timedelta(seconds=90)


def test_naive_datetimes_are_refused():
    with pytest.raises(ValueError):
        SimClock(datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        utc(datetime(2026, 1, 1))


def test_live_clock_is_utc_aware():
    assert LiveClock().now().tzinfo == UTC
