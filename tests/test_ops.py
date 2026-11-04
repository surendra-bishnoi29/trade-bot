import os
import socket
import tempfile
from datetime import UTC, datetime, timedelta

from desk.agents.cycle import CycleScheduler
from desk.core import sdnotify
from desk.core.clock import SimClock
from desk.core.config import Settings
from desk.core.events import Topics
from tests.conftest import make_event

SETTINGS = Settings(_env_file=None, cycle_every_minutes=0)


def test_sdnotify_is_a_no_op_outside_systemd(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    assert sdnotify.ready() is False and sdnotify.watchdog() is False
    assert sdnotify.watchdog_interval() is None


def test_sdnotify_talks_to_the_notify_socket(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "notify")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(path)
        server.settimeout(2)
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        monkeypatch.setenv("WATCHDOG_USEC", "180000000")
        assert sdnotify.ready() and server.recv(64) == b"READY=1"
        assert sdnotify.watchdog() and server.recv(64) == b"WATCHDOG=1"
        assert sdnotify.watchdog_interval() == 90.0
        server.close()


class FakeDB:
    def __init__(self, plans):
        self.plans = plans

    async def recent_events(self, types, since, *, limit=50):
        return [e for e in self.plans if e.occurred_at >= since]


class FakeCycle:
    def __init__(self, db):
        self.db = db
        self.runs: list[str] = []

    async def run(self, trigger: str, dry_run: bool = False):
        self.runs.append(trigger)

        class Result:
            def render(self) -> str:
                return "ORDERS: none"

        return Result()


def scheduler(now: datetime, plans=()) -> CycleScheduler:
    clock = SimClock(now)
    return CycleScheduler(None, clock, FakeCycle(FakeDB(list(plans))), None, SETTINGS)


def test_missed_slot_is_the_latest_recent_slot():
    # Tuesday 3 Nov 2026: pre-market 13:30, mid-session 17:30, post-close 21:10 UTC
    s = scheduler(datetime(2026, 11, 4, 3, 30, tzinfo=UTC))
    at, trigger = s.missed_slot(s.clock.now())
    assert trigger == "post_close" and at == datetime(2026, 11, 3, 21, 10, tzinfo=UTC)
    s = scheduler(datetime(2026, 11, 4, 20, 0, tzinfo=UTC))
    assert s.missed_slot(s.clock.now())[1] == "mid_session"
    s = scheduler(datetime(2026, 11, 4, 12, 0, tzinfo=UTC))  # post-close was 14h50 ago
    assert s.missed_slot(s.clock.now()) is None


def test_late_cycles_only_run_when_it_still_makes_sense():
    closed = datetime(2026, 11, 4, 2, 0, tzinfo=UTC)
    assert CycleScheduler.may_run_late("post_close", closed)
    assert not CycleScheduler.may_run_late("mid_session", closed)
    assert CycleScheduler.may_run_late("mid_session", datetime(2026, 11, 4, 19, 0, tzinfo=UTC))
    # a pre-market cycle that is late by an hour still runs before the open, or during the session
    assert CycleScheduler.may_run_late("pre_market", datetime(2026, 11, 4, 14, 0, tzinfo=UTC))
    assert CycleScheduler.may_run_late("pre_market", datetime(2026, 11, 4, 16, 0, tzinfo=UTC))
    assert not CycleScheduler.may_run_late("pre_market", datetime(2026, 11, 4, 22, 0, tzinfo=UTC))


async def test_catch_up_runs_a_missed_cycle_once():
    now = datetime(2026, 11, 4, 3, 30, tzinfo=UTC)
    s = scheduler(now)
    assert await s.catch_up() is True and s.cycle.runs == ["post_close"]
    done = make_event(
        SimClock(datetime(2026, 11, 3, 21, 12, tzinfo=UTC)),
        type=Topics.CYCLE_PLAN,
        payload={
            "cycle_id": "c",
            "trigger": "post_close",
            "dry_run": False,
            "plan": {"market_view": "", "risk_note": "", "decisions": []},
        },
    )
    s = scheduler(now, plans=[done])
    assert await s.catch_up() is False and s.cycle.runs == []
    # mid-session missed and the market closed at 21:00: a late mid-session cycle makes no sense
    s = scheduler(datetime(2026, 11, 4, 21, 5, tzinfo=UTC))
    assert await s.catch_up() is False and s.cycle.runs == []


async def test_sliced_schedule_runs_a_due_cycle_after_a_long_sleep():
    import asyncio
    import contextlib

    start = datetime(2026, 11, 4, 17, 0, tzinfo=UTC)
    pre_market_done = make_event(
        SimClock(datetime(2026, 11, 4, 13, 31, tzinfo=UTC)),
        type=Topics.CYCLE_PLAN,
        payload={
            "cycle_id": "c",
            "trigger": "pre_market",
            "dry_run": False,
            "plan": {"market_view": "", "risk_note": "", "decisions": []},
        },
    )
    s = scheduler(start, plans=[pre_market_done])
    jumped = False

    async def sleep_through_the_slot(seconds: float) -> None:
        """The first sleep is the machine sleeping through the 17:30 slot; later ones are real slices."""
        nonlocal jumped
        await asyncio.sleep(0)
        if not jumped:
            jumped = True
            s.clock.advance_to(start + timedelta(hours=1, minutes=5))
        else:
            s.clock.advance_to(s.clock.now() + timedelta(seconds=seconds))

    s.clock.sleep = sleep_through_the_slot  # type: ignore[method-assign]
    task = asyncio.create_task(s.run_forever())
    for _ in range(100):
        await asyncio.sleep(0)
        if s.cycle.runs:
            break
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert s.cycle.runs == ["mid_session"]
