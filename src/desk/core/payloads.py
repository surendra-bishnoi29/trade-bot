"""Version 1 payload schemas for the topics phase 1 uses. Importing this module registers them."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from desk.core.events import Book, Topics, payload_for

Interval = Literal["1m", "5m", "15m", "1h", "1d"]
_INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MarketBar(Payload):
    """One completed bar. ts is the bar start; the bar exists only once ts + interval has passed."""

    symbol: str
    interval: Interval
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    vwap: Decimal | None = None
    trade_count: int | None = None
    source: str

    @property
    def closes_at(self) -> datetime:
        return self.ts + timedelta(seconds=_INTERVAL_SECONDS[self.interval])


payload_for(Topics.MARKET_BAR_1M)(MarketBar)
payload_for(Topics.MARKET_BAR_1D)(MarketBar)


@payload_for(Topics.MARKET_SESSION)
class MarketSession(Payload):
    date: str
    open_at: datetime
    close_at: datetime
    early_close: bool = False
    phase: Literal["pre", "open", "closed"]


@payload_for(Topics.NEWS_ARTICLE_RAW)
class NewsArticleRaw(Payload):
    article_id: str
    published_at: datetime
    source: str
    url: str | None = None
    title: str
    summary: str | None = None
    symbols: list[str] = Field(default_factory=list)


@payload_for(Topics.NEWS_ARTICLE_SCORED)
class NewsArticleScored(Payload):
    article_id: str
    relevance: float = Field(ge=0, le=1)
    polarity: float = Field(ge=-1, le=1)
    confidence: float = Field(ge=0, le=1)
    event_type: str
    impact: Literal["low", "medium", "high"]
    horizon: Literal["intraday", "days", "weeks"]
    model: str
    prompt_version: str


@payload_for(Topics.WATCHER_ALERT)
class WatcherAlert(Payload):
    symbol: str
    kind: Literal[
        "abnormal_move",
        "volume_spike",
        "halt",
        "level_break",
        "stale_data",
        "feed_stale",
        "feed_recovered",
    ]
    severity: Literal["info", "warning", "critical"]
    details: dict[str, Any] = Field(default_factory=dict)


@payload_for(Topics.ORDER_INTENT)
class OrderIntent(Payload):
    intent_id: str
    symbol: str
    book: Book
    side: Literal["buy", "sell"]
    quantity: Decimal = Field(gt=0)
    order_type: Literal["market", "limit"] = "market"
    limit_price: Decimal | None = None
    time_in_force: Literal["day", "gtc"] = "day"
    thesis_id: str | None = None
    urgency: Literal["normal", "now"] = "normal"
    rationale_event_ids: list[str] = Field(default_factory=list)


class RuleResult(Payload):
    rule: str
    passed: bool
    detail: str = ""


class RiskDecision(Payload):
    intent_id: str
    intent: OrderIntent
    decision: Literal["approved", "modified", "rejected"]
    approved_quantity: Decimal
    rules: list[RuleResult]
    reason: str = ""


for _t in (Topics.RISK_APPROVED, Topics.RISK_MODIFIED, Topics.RISK_REJECTED):
    payload_for(_t)(RiskDecision)


class OrderUpdate(Payload):
    order_id: str
    intent_id: str
    side: Literal["buy", "sell"] | None = None
    broker_order_id: str | None = None
    status: Literal["submitted", "partial", "filled", "canceled", "rejected"]
    filled_quantity: Decimal = Decimal(0)
    avg_price: Decimal | None = None
    detail: str = ""


for _t in (
    Topics.ORDER_SUBMITTED,
    Topics.ORDER_PARTIAL,
    Topics.ORDER_FILLED,
    Topics.ORDER_CANCELED,
    Topics.ORDER_REJECTED,
):
    payload_for(_t)(OrderUpdate)


class CycleEvent(Payload):
    cycle_id: str
    book: Book
    stage: str
    status: Literal["started", "completed", "failed"]
    detail: str = ""


for _t in (Topics.CYCLE_STARTED, Topics.CYCLE_STAGE, Topics.CYCLE_COMPLETED, Topics.CYCLE_FAILED):
    payload_for(_t)(CycleEvent)


@payload_for(Topics.SYSTEM_HEALTH)
class SystemHealth(Payload):
    component: str
    status: Literal["ok", "degraded", "down"]
    detail: str = ""


class PositionSnapshot(Payload):
    symbol: str
    qty: Decimal
    avg_entry: Decimal | None = None
    market_value: Decimal | None = None
    unrealized_pl: Decimal | None = None


@payload_for(Topics.PORTFOLIO_RECONCILED)
class PortfolioSnapshot(Payload):
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    positions: list[PositionSnapshot] = Field(default_factory=list)
    as_of: datetime
    broker: str = "alpaca-paper"


payload_for(Topics.PORTFOLIO_POSITION)(PositionSnapshot)


@payload_for(Topics.RESEARCH_REQUEST)
class ResearchRequest(Payload):
    request_id: str
    kind: Literal["news_scan"]
    symbol: str
    note: str = ""


class ConsoleMessage(Payload):
    session_id: str
    text: str
    request_id: str = ""  # set when the question came through the api service
    user: str = ""


for _t in (Topics.CONSOLE_QUESTION, Topics.CONSOLE_ANSWER):
    payload_for(_t)(ConsoleMessage)


@payload_for(Topics.RISK_KILL_SWITCH)
class KillSwitch(Payload):
    engaged: bool = True
    reason: str
    source: str = ""


ThesisStatus = Literal["proposed", "pending_entry", "active", "closing", "closed", "rejected"]


class ThesisPayload(Payload):
    thesis_id: str
    symbol: str
    book: Book
    direction: Literal["long", "short"]
    status: ThesisStatus
    cycle_id: str
    narrative: str
    evidence_ids: list[str] = Field(default_factory=list)
    expected_return_pct: float
    horizon_days: int
    conviction: float
    quantity: Decimal
    entry_price: Decimal | None = None
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    stop_pct: float
    target_pct: float
    invalidation: list[str] = Field(default_factory=list)
    size_rationale: str = ""
    reason: str = ""
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    realized_pnl: Decimal | None = None


for _t in (
    Topics.THESIS_CREATED,
    Topics.THESIS_UPDATED,
    Topics.THESIS_INVALIDATED,
    Topics.THESIS_CLOSED,
):
    payload_for(_t)(ThesisPayload)


class PlanDecision(BaseModel):
    """One line of the head trader's plan. All fields required: this is a structured model output."""

    symbol: str
    action: Literal["buy", "sell", "hold", "none"]
    quantity: int
    thesis: str
    evidence_ids: list[str]
    expected_return_pct: float
    horizon_days: int
    stop_pct: float
    target_pct: float
    invalidation: list[str]
    conviction: float


class CyclePlan(BaseModel):
    market_view: str
    risk_note: str
    decisions: list[PlanDecision]


class Objection(BaseModel):
    symbol: str
    severity: Literal["blocking", "concern", "minor"]
    argument: str


class Challenge(BaseModel):
    overall: str
    objections: list[Objection]


@payload_for(Topics.CYCLE_PLAN)
class CyclePlanPayload(Payload):
    cycle_id: str
    trigger: str
    dry_run: bool
    plan: CyclePlan
    challenge: Challenge | None = None
    final_actions: list[dict[str, Any]] = Field(default_factory=list)
    prompt_versions: dict[str, str] = Field(default_factory=dict)


@payload_for(Topics.THESIS_CHALLENGE)
class ChallengePayload(Payload):
    cycle_id: str
    challenge: Challenge


# ---- operations and the api service (slice 4) ----


@payload_for(Topics.SYSTEM_RESTARTED)
class SystemRestarted(Payload):
    """Once per start: how long the desk was away and what it does to close the gap."""

    started_at: datetime
    last_bar_at: datetime | None = None
    gap_seconds: float | None = None
    during_session: bool = False
    actions: list[str] = Field(default_factory=list)


@payload_for(Topics.SYSTEM_HEARTBEAT)
class Heartbeat(Payload):
    status: Literal["ok", "degraded"] = "ok"
    feed_age_seconds: float | None = None
    kill_switch: bool = False
    open_theses: int = 0
    next_cycle: str = ""
    morning: bool = False  # the first heartbeat of a trading day, worth a message


@payload_for(Topics.DESK_COMMAND)
class DeskCommand(Payload):
    """A request to the bot from the api service or another client."""

    request_id: str
    command: Literal["ask", "cycle", "plan", "kill", "resume", "status", "theses", "brief"]
    args: str = ""
    user: str = ""
    role: Literal["operator", "viewer", "system"] = "operator"


@payload_for(Topics.DESK_COMMAND_RESULT)
class DeskCommandResult(Payload):
    request_id: str
    command: str
    ok: bool
    text: str = ""


@payload_for(Topics.CONSOLE_PROGRESS)
class ConsoleProgress(Payload):
    request_id: str
    stage: Literal["thinking", "tool", "done", "error"]
    detail: str = ""


@payload_for(Topics.CONSOLE_ANSWER_DELTA)
class ConsoleDelta(Payload):
    request_id: str
    seq: int
    text: str


# ---- the daily brief ----


class BriefTrade(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    quantity: int
    price: float
    why: str


class DailyBrief(BaseModel):
    """What the head trader writes after the close. All fields required: a structured model output."""

    headline: str
    summary: str
    trades: list[BriefTrade]
    book: str
    incidents: str
    tomorrow: str


@payload_for(Topics.DESK_BRIEF)
class DailyBriefPayload(Payload):
    day: date
    generated_at: datetime
    brief: DailyBrief
    facts: dict[str, Any] = Field(default_factory=dict)


# ---- the morning note ----


class WatchItem(BaseModel):
    symbol: str
    why: str
    what_to_watch: str


class MorningNote(BaseModel):
    """What the head trader reads out before the open. All fields required: a structured model output."""

    headline: str
    tape: str
    overnight_news: list[str]
    watchlist: list[WatchItem]
    risks: str


@payload_for(Topics.FILING_NEW)
class FilingNew(Payload):
    symbol: str
    cik: str
    accession: str
    form: str
    filed_at: datetime
    items: list[str] = Field(default_factory=list)
    document: str = ""
    description: str = ""
    url: str = ""
