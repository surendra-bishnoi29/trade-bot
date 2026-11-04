"""NYSE session calendar (decision A: exchange_calendars). All inputs and outputs are aware UTC."""

from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd

from desk.core.clock import utc


@lru_cache(maxsize=1)
def _cal():
    return xcals.get_calendar("XNYS")


def is_session(d: date) -> bool:
    return bool(_cal().is_session(pd.Timestamp(d)))


def session_bounds(d: date) -> tuple[datetime, datetime]:
    """(open, close) of the session on date d, aware UTC. Early closes are respected."""
    ts = pd.Timestamp(d)
    return _cal().session_open(ts).to_pydatetime(), _cal().session_close(ts).to_pydatetime()


def is_open(now: datetime) -> bool:
    return bool(_cal().is_open_on_minute(pd.Timestamp(utc(now))))


def previous_close(now: datetime) -> datetime:
    return _cal().previous_close(pd.Timestamp(utc(now))).to_pydatetime()


def next_open(now: datetime) -> datetime:
    return _cal().next_open(pd.Timestamp(utc(now))).to_pydatetime()


def session_of(ts: datetime) -> date:
    """The session a timestamp belongs to; outside hours, the previous session."""
    return _cal().minute_to_session(pd.Timestamp(utc(ts)), direction="previous").date()


def last_sessions(end: date, n: int) -> list[date]:
    sessions = _cal().sessions
    sessions = sessions[sessions <= pd.Timestamp(end)]
    return [s.date() for s in sessions[-n:]]


def sessions_between(start: date, end: date) -> int:
    """Number of sessions after `start` up to and including `end`."""
    sessions = _cal().sessions
    return int(((sessions > pd.Timestamp(start)) & (sessions <= pd.Timestamp(end))).sum())
