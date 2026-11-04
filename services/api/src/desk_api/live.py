"""The live feed: tail the bot's Redis streams and fan every event out to connected browsers.
A plain XREAD tail (no consumer group): the api is a listener, nothing is acknowledged or owned."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis

from desk.core.events import Event, Topics

log = logging.getLogger(__name__)

LIVE_TOPICS: tuple[str, ...] = (
    Topics.MARKET_BAR_1M,
    Topics.WATCHER_ALERT,
    Topics.ORDER_INTENT,
    Topics.ORDER_SUBMITTED,
    Topics.ORDER_FILLED,
    Topics.ORDER_REJECTED,
    Topics.ORDER_CANCELED,
    Topics.RISK_APPROVED,
    Topics.RISK_REJECTED,
    Topics.RISK_KILL_SWITCH,
    Topics.PORTFOLIO_RECONCILED,
    Topics.CYCLE_STARTED,
    Topics.CYCLE_PLAN,
    Topics.CYCLE_COMPLETED,
    Topics.CYCLE_FAILED,
    Topics.THESIS_CREATED,
    Topics.THESIS_UPDATED,
    Topics.THESIS_CLOSED,
    Topics.SYSTEM_HEALTH,
    Topics.SYSTEM_RESTARTED,
    Topics.SYSTEM_HEARTBEAT,
    Topics.CONSOLE_QUESTION,
    Topics.CONSOLE_PROGRESS,
    Topics.CONSOLE_ANSWER_DELTA,
    Topics.CONSOLE_ANSWER,
    Topics.DESK_COMMAND_RESULT,
)


def message_for(event: Event) -> dict[str, Any]:
    return {
        "type": event.type,
        "id": event.event_id,
        "symbol": event.symbol,
        "at": event.occurred_at.isoformat(),
        "correlation_id": event.correlation_id,
        "source": event.source,
        "payload": event.model_dump(mode="json")["payload"],
    }


class LiveFeed:
    def __init__(
        self, redis_url: str, *, prefix: str = "desk:", topics: Iterable[str] = LIVE_TOPICS
    ) -> None:
        self.redis_url = redis_url
        self.prefix = prefix
        self.topics = tuple(topics)
        self.clients: set[asyncio.Queue] = set()
        self.connected = False
        self.last_event_at: datetime | None = None

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)

    def broadcast(self, message: dict[str, Any]) -> None:
        for q in list(self.clients):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:  # a browser that stopped reading loses events, not the server
                self.clients.discard(q)

    async def run(self) -> None:
        streams = {f"{self.prefix}{t}": "$" for t in self.topics}
        while True:
            r = aioredis.from_url(self.redis_url, decode_responses=True)
            try:
                await r.ping()
                self.connected = True
                while True:
                    response = await r.xread(streams, count=500, block=1000)
                    for stream, entries in response or []:
                        for msg_id, fields in entries:
                            streams[stream] = msg_id
                            try:
                                event = Event.from_json(fields["event"])
                            except Exception as exc:  # noqa: BLE001 - one bad entry must not stop the tail
                                log.warning("unreadable event on %s: %s", stream, exc)
                                continue
                            self.last_event_at = datetime.now(UTC)
                            self.broadcast(message_for(event))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect after any redis trouble
                self.connected = False
                log.warning("live feed lost redis (%s); reconnecting", exc)
                await asyncio.sleep(2)
            finally:
                try:
                    await r.aclose()
                except Exception as exc:  # noqa: BLE001
                    log.debug("redis close: %s", exc)
