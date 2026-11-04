"""The event envelope: the one shape every message on the bus has (architecture section 5.1).

Two clocks on every event: occurred_at is when it happened in the world, ingested_at is when the
desk learned of it. correlation_id ties one cycle together; causation_id names the event that
directly caused this one.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from ulid import ULID

from desk.core.clock import utc

TOPIC_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")
Book = Literal["swing", "intraday"]


class Topics:
    """Topic names, grouped as in architecture section 5.2."""

    MARKET_BAR_1M = "market.bar.1m"
    MARKET_BAR_1D = "market.bar.1d"
    MARKET_QUOTE = "market.quote"
    MARKET_HALT = "market.halt"
    MARKET_SESSION = "market.session"
    NEWS_ARTICLE_RAW = "news.article.raw"
    NEWS_ARTICLE_SCORED = "news.article.scored"
    NEWS_STATE = "news.state"
    FILING_NEW = "filing.new"
    WATCHER_ALERT = "watcher.alert"
    RESEARCH_REQUEST = "research.request"
    RESEARCH_REPORT = "research.report"
    ORDER_INTENT = "order.intent"
    RISK_APPROVED = "risk.approved"
    RISK_MODIFIED = "risk.modified"
    RISK_REJECTED = "risk.rejected"
    RISK_KILL_SWITCH = "risk.kill_switch"
    ORDER_SUBMITTED = "order.submitted"
    ORDER_PARTIAL = "order.partial"
    ORDER_FILLED = "order.filled"
    ORDER_CANCELED = "order.canceled"
    ORDER_REJECTED = "order.rejected"
    PORTFOLIO_POSITION = "portfolio.position.updated"
    PORTFOLIO_PNL = "portfolio.pnl.updated"
    PORTFOLIO_RECONCILED = "portfolio.reconciled"
    CYCLE_STARTED = "cycle.started"
    CYCLE_STAGE = "cycle.stage.completed"
    CYCLE_COMPLETED = "cycle.completed"
    CYCLE_FAILED = "cycle.failed"
    SYSTEM_HEALTH = "system.health"
    SYSTEM_ALERT = "system.alert"
    SYSTEM_DEAD_LETTER = "system.dead_letter"
    THESIS_CREATED = "thesis.created"
    THESIS_UPDATED = "thesis.updated"
    THESIS_INVALIDATED = "thesis.invalidated"
    THESIS_CLOSED = "thesis.closed"
    THESIS_CHALLENGE = "thesis.challenge"
    CYCLE_PLAN = "cycle.plan"
    CONSOLE_QUESTION = "console.question"
    CONSOLE_ANSWER = "console.answer"
    CONSOLE_ANSWER_DELTA = "console.answer.delta"
    CONSOLE_PROGRESS = "console.progress"
    DESK_COMMAND = "desk.command"
    DESK_COMMAND_RESULT = "desk.command.result"
    SYSTEM_RESTARTED = "system.restarted"
    SYSTEM_HEARTBEAT = "system.heartbeat"
    DESK_BRIEF = "desk.brief"
    REVIEW_POSTMORTEM = "review.trade.postmortem"
    REVIEW_ANALYST_SCORE = "review.analyst.score"

    @classmethod
    def all(cls) -> list[str]:
        return sorted(v for k, v in vars(cls).items() if k.isupper() and isinstance(v, str))


class Event(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(default_factory=lambda: str(ULID()))
    type: str
    schema_version: int = 1
    occurred_at: datetime
    ingested_at: datetime
    symbol: str | None = None
    book: Book | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    source: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def _valid_topic(cls, v: str) -> str:
        if not TOPIC_RE.match(v):
            raise ValueError(f"topic must look like 'domain.name', got {v!r}")
        return v

    @field_validator("event_id")
    @classmethod
    def _valid_ulid(cls, v: str) -> str:
        ULID.from_str(v)
        return v

    @field_validator("occurred_at", "ingested_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return utc(v)

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, data: str | bytes) -> Event:
        return cls.model_validate_json(data)


# ---- payload registry: a topic may declare the pydantic model its payload must satisfy ----

_REGISTRY: dict[str, type[BaseModel]] = {}


def payload_for(topic: str):
    """Class decorator: register `model` as the payload schema of `topic`."""

    def register(model: type[BaseModel]) -> type[BaseModel]:
        _REGISTRY[topic] = model
        return model

    return register


def payload_model(topic: str) -> type[BaseModel] | None:
    return _REGISTRY.get(topic)


def validate_payload(topic: str, payload: dict[str, Any] | BaseModel) -> dict[str, Any]:
    """Validate a payload against the topic's model, if one is registered, and return it as
    plain JSON-compatible data. Unregistered topics pass through unchanged."""
    model = _REGISTRY.get(topic)
    if model is None:
        return payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
    instance = payload if isinstance(payload, model) else model.model_validate(payload)
    return instance.model_dump(mode="json")


def parse_payload(event: Event) -> BaseModel | dict[str, Any]:
    model = _REGISTRY.get(event.type)
    return model.model_validate(event.payload) if model is not None else event.payload
