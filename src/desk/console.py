"""The desk console: a command line where every message goes to the head trader."""

from __future__ import annotations

import asyncio
import logging

from ulid import ULID

from desk.agents.base import BaseAgent
from desk.agents.head_trader import HeadTrader
from desk.bus.base import Bus
from desk.core.clock import Clock
from desk.core.events import Topics
from desk.core.payloads import ConsoleMessage

log = logging.getLogger(__name__)


class ConsoleAgent(BaseAgent):
    name = "console"
    topics = ()

    def __init__(self, bus: Bus, clock: Clock, head: HeadTrader) -> None:
        super().__init__(bus, clock)
        self.head = head
        self.session_id = str(ULID())
        self.commands: dict = {}

    async def ask(self, text: str) -> str:
        question = await self.emit(
            Topics.CONSOLE_QUESTION,
            ConsoleMessage(session_id=self.session_id, text=text),
            correlation_id=self.session_id,
        )
        answer = await self.head.ask(text)
        await self.emit(
            Topics.CONSOLE_ANSWER,
            ConsoleMessage(session_id=self.session_id, text=answer),
            caused_by=question,
        )
        u = self.head.last_usage
        log.info(
            "head trader tokens: in=%s out=%s cache_read=%s cache_write=%s",
            u.get("input"),
            u.get("output"),
            u.get("cache_read"),
            u.get("cache_write"),
        )
        return answer

    async def repl(self) -> None:
        print(
            "desk console. Ask the head trader anything; /plan runs a dry-run cycle, /cycle a real one, /status, /theses, /kill, /resume. 'exit' to quit."
        )
        while True:
            try:
                line = await asyncio.to_thread(input, "desk> ")
            except (EOFError, KeyboardInterrupt):
                break
            line = line.strip()
            if not line:
                continue
            if line.lower() in {"exit", "quit"}:
                break
            if line.startswith("/"):
                name, _, arg = line[1:].partition(" ")
                command = self.commands.get(name.lower())
                if command is None:
                    print(
                        "commands: "
                        + ", ".join("/" + c for c in self.commands)
                        + ". Anything else goes to the head trader."
                    )
                    continue
                try:
                    print(await command(arg))
                except Exception as exc:  # noqa: BLE001
                    print(f"error: {type(exc).__name__}: {exc}")
                continue
            try:
                print(await self.ask(line))
            except Exception as exc:  # noqa: BLE001 - the console must survive a failed question
                print(f"error: {type(exc).__name__}: {exc}")
