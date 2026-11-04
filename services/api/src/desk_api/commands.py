"""Requests to the bot: questions for the head trader and operator commands, published as desk.command
events. The bot answers on the bus; the live feed carries the answer back to the browser."""

from __future__ import annotations

from ulid import ULID

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core.clock import Clock
from desk.core.events import Topics
from desk.core.payloads import DeskCommand
from desk_api.users import User


class ApiAgent(BaseAgent):
    """Publishes on the bus with the api's own source stamp; subscribes to nothing."""

    name = "api"
    topics = ()


class Commands:
    def __init__(self, bus: Bus, clock: Clock) -> None:
        self.agent = ApiAgent(bus, clock)

    async def ask(self, text: str, user: User) -> str:
        request_id = str(ULID())
        await self.agent.emit(
            Topics.DESK_COMMAND,
            DeskCommand(
                request_id=request_id, command="ask", args=text, user=user.name, role=user.role
            ),
            correlation_id=request_id,
        )
        return request_id

    async def run(self, command: str, args: str, user: User) -> str:
        request_id = str(ULID())
        await self.agent.emit(
            Topics.DESK_COMMAND,
            DeskCommand(
                request_id=request_id, command=command, args=args, user=user.name, role=user.role
            ),
            correlation_id=request_id,
        )
        return request_id
