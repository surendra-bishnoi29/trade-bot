from datetime import UTC, date, datetime

from desk.core import calendar


def test_known_sessions_and_bounds():
    assert calendar.is_session(date(2026, 11, 2))  # a Monday
    assert not calendar.is_session(date(2026, 11, 7))  # a Saturday
    assert not calendar.is_session(date(2026, 11, 26))  # Thanksgiving
    open_at, close_at = calendar.session_bounds(date(2026, 11, 2))
    assert (open_at.hour, open_at.minute) == (
        14,
        30,
    ) and close_at.hour == 21  # EST after the DST change
    _, early = calendar.session_bounds(date(2026, 11, 27))
    assert early.hour == 18  # 1 pm early close the day after Thanksgiving


def test_is_open_and_session_of():
    assert calendar.is_open(datetime(2026, 11, 2, 15, 0, tzinfo=UTC))
    assert not calendar.is_open(datetime(2026, 11, 2, 2, 0, tzinfo=UTC))
    assert calendar.session_of(datetime(2026, 11, 7, 12, 0, tzinfo=UTC)) == date(2026, 11, 6)


def test_last_sessions_and_next_open():
    sessions = calendar.last_sessions(date(2026, 11, 6), 3)
    assert sessions == [date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6)]
    nxt = calendar.next_open(datetime(2026, 11, 7, 12, 0, tzinfo=UTC))
    assert nxt.date() == date(2026, 11, 9)
