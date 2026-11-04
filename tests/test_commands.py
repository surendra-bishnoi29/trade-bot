import asyncio
from datetime import UTC, datetime
from typing import ClassVar

from desk.agents.base import BaseAgent
from desk.agents.commands import CommandAgent
from desk.bus.memory import InMemoryBus
from desk.core.clock import SimClock
from desk.core.events import Topics
from desk.core.payloads import DeskCommand

NOW = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)


class Client(BaseAgent):
    name = "api"
    topics = ()


class FakeHead:
    last_usage: ClassVar[dict] = {}

    async def ask(self, question, *, on_text=None, on_progress=None):
        await on_text("The book ")
        await on_progress("tool", "get_portfolio")
        await on_text("is flat.")
        return "The book is flat."


async def test_commands_run_and_viewers_cannot_trade():
    clock = SimClock(NOW)
    bus = InMemoryBus(clock)

    async def status(arg: str) -> str:
        return "equity 100000"

    CommandAgent(bus, clock, {"status": status, "cycle": status}, head=FakeHead()).start()
    task = asyncio.create_task(bus.run())
    client = Client(bus, clock)
    await client.emit(Topics.DESK_COMMAND, DeskCommand(request_id="r1", command="status"))
    await client.emit(
        Topics.DESK_COMMAND, DeskCommand(request_id="r2", command="cycle", role="viewer")
    )
    await client.emit(Topics.DESK_COMMAND, DeskCommand(request_id="r3", command="theses"))
    await bus.wait_idle()
    results = {
        e.payload["request_id"]: e.payload for e in bus.log if e.type == Topics.DESK_COMMAND_RESULT
    }
    assert results["r1"]["ok"] and results["r1"]["text"] == "equity 100000"
    assert not results["r2"]["ok"] and "viewers" in results["r2"]["text"]
    assert not results["r3"]["ok"] and "unknown" in results["r3"]["text"]
    await bus.stop()
    task.cancel()


async def test_ask_streams_deltas_progress_and_the_answer():
    clock = SimClock(NOW)
    bus = InMemoryBus(clock)
    agent = CommandAgent(bus, clock, {}, head=FakeHead())
    agent.start()
    task = asyncio.create_task(bus.run())
    client = Client(bus, clock)
    await client.emit(
        Topics.DESK_COMMAND,
        DeskCommand(request_id="q1", command="ask", args="how is the book?", user="ahmed"),
    )
    await bus.wait_idle()
    for _ in range(50):  # the answer runs as its own task
        if any(e.type == Topics.CONSOLE_ANSWER for e in bus.log):
            break
        await asyncio.sleep(0.01)
    await bus.wait_idle()
    types = [e.type for e in bus.log]
    assert types.index(Topics.CONSOLE_QUESTION) < types.index(Topics.CONSOLE_ANSWER_DELTA)
    deltas = [e.payload for e in bus.log if e.type == Topics.CONSOLE_ANSWER_DELTA]
    assert [d["seq"] for d in deltas] == [1, 2] and "".join(
        d["text"] for d in deltas
    ) == "The book is flat."
    progress = [e.payload["stage"] for e in bus.log if e.type == Topics.CONSOLE_PROGRESS]
    assert progress == ["thinking", "tool", "done"]
    answer = next(e for e in bus.log if e.type == Topics.CONSOLE_ANSWER)
    assert answer.payload["text"] == "The book is flat." and answer.payload["request_id"] == "q1"
    assert answer.payload["user"] == "ahmed" and answer.correlation_id == "q1"
    await bus.stop()
    task.cancel()
