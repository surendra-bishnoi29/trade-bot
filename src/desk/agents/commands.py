"""Commands from outside the bot process: the api service (and any other client) publishes desk.command
events; this agent runs them and answers on the bus. Questions to the head trader stream back as
console.answer.delta events, so an interface can show the answer as it is written."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core.clock import Clock
from desk.core.events import Event, Topics, parse_payload
from desk.core.payloads import (
    ConsoleDelta,
    ConsoleMessage,
    ConsoleProgress,
    DeskCommand,
    DeskCommandResult,
)

log = logging.getLogger(__name__)

OPERATOR_ONLY = {"cycle", "kill", "resume"}


class CommandAgent(BaseAgent):
    name = "commands"
    topics = (Topics.DESK_COMMAND,)

    def __init__(
        self,
        bus: Bus,
        clock: Clock,
        commands: dict[str, Callable[[str], Awaitable[str]]],
        head: Any,
        *,
        max_concurrent_asks: int = 3,
    ) -> None:
        super().__init__(bus, clock)
        self.commands = commands
        self.head = head
        self._asks = asyncio.Semaphore(max_concurrent_asks)
        self._tasks: set[asyncio.Task[None]] = set()

    async def handle(self, event: Event) -> None:
        cmd = parse_payload(event)
        assert isinstance(cmd, DeskCommand)
        if cmd.command == "ask":
            # answers take a while: run them aside so other commands are not queued behind them
            task = asyncio.create_task(self._ask(cmd, event), name=f"ask:{cmd.request_id}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        if cmd.role == "viewer" and cmd.command in OPERATOR_ONLY:
            await self._result(cmd, event, ok=False, text="viewers cannot run this command")
            return
        runner = self.commands.get(cmd.command)
        if runner is None:
            await self._result(cmd, event, ok=False, text=f"unknown command {cmd.command}")
            return
        try:
            text = await runner(cmd.args)
            await self._result(cmd, event, ok=True, text=text)
        except Exception as exc:
            log.exception("command %s failed", cmd.command)
            await self._result(cmd, event, ok=False, text=f"{type(exc).__name__}: {exc}")

    async def _result(self, cmd: DeskCommand, event: Event, *, ok: bool, text: str) -> None:
        await self.emit(
            Topics.DESK_COMMAND_RESULT,
            DeskCommandResult(request_id=cmd.request_id, command=cmd.command, ok=ok, text=text),
            caused_by=event,
        )

    async def _ask(self, cmd: DeskCommand, event: Event) -> None:
        async with self._asks:
            question = await self.emit(
                Topics.CONSOLE_QUESTION,
                ConsoleMessage(
                    session_id=cmd.request_id,
                    text=cmd.args,
                    request_id=cmd.request_id,
                    user=cmd.user,
                ),
                caused_by=event,
                correlation_id=cmd.request_id,
            )
            seq = 0

            async def on_text(text: str) -> None:
                nonlocal seq
                seq += 1
                await self.emit(
                    Topics.CONSOLE_ANSWER_DELTA,
                    ConsoleDelta(request_id=cmd.request_id, seq=seq, text=text),
                    caused_by=question,
                )

            async def on_progress(stage: str, detail: str) -> None:
                await self.emit(
                    Topics.CONSOLE_PROGRESS,
                    ConsoleProgress(request_id=cmd.request_id, stage=stage, detail=detail),
                    caused_by=question,
                )

            try:
                await on_progress("thinking", "")
                answer = await self.head.ask(cmd.args, on_text=on_text, on_progress=on_progress)
                await on_progress("done", "")
            except Exception as exc:
                log.exception("ask failed")
                answer = f"The head trader could not answer: {type(exc).__name__}: {exc}"
                await on_progress("error", answer)
            await self.emit(
                Topics.CONSOLE_ANSWER,
                ConsoleMessage(
                    session_id=cmd.request_id, text=answer, request_id=cmd.request_id, user=cmd.user
                ),
                caused_by=question,
            )
