"""The risk officer: deterministic, versioned, logged. No LLM, ever (architecture section 8)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core import calendar
from desk.core.clock import Clock
from desk.core.events import Event, Topics, parse_payload
from desk.core.payloads import OrderIntent, RiskDecision, RuleResult

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskLimits:
    universe: frozenset[str]
    max_order_notional_pct: Decimal = Decimal("0.05")  # of equity
    max_position_pct: Decimal = Decimal("0.10")  # of equity, after the order
    max_orders_per_day: int = 20
    allow_short: bool = False
    allow_when_closed: bool = True  # paper: Alpaca queues day orders for the next open
    max_price_age: timedelta = timedelta(minutes=15)  # while open, never trade on an older price


@dataclass(frozen=True)
class PortfolioView:
    equity: Decimal
    positions: dict[str, Decimal]  # symbol -> signed quantity


@dataclass
class RiskState:
    orders_day: date | None = None
    orders_today: int = 0
    kill_switch: bool = False
    kill_reason: str = ""


def _is_closing(intent: OrderIntent, held: Decimal) -> bool:
    return (intent.side == "sell" and held > 0 and intent.quantity <= held) or (
        intent.side == "buy" and held < 0 and intent.quantity <= -held
    )


def evaluate(
    intent: OrderIntent,
    portfolio: PortfolioView,
    price: Decimal | None,
    now: datetime,
    limits: RiskLimits,
    state: RiskState,
    price_age: timedelta | None = None,
) -> RiskDecision:
    """Pure function: one intent against the rules. Exits are never blocked by day limits."""
    held = portfolio.positions.get(intent.symbol, Decimal(0))
    closing = _is_closing(intent, held)
    rules: list[RuleResult] = []

    def rule(name: str, passed: bool, detail: str) -> None:
        rules.append(RuleResult(rule=name, passed=passed, detail=detail))

    rule(
        "kill_switch",
        closing or not state.kill_switch,
        "engaged: " + state.kill_reason if state.kill_switch else "off",
    )
    rule(
        "universe",
        intent.symbol in limits.universe,
        f"{intent.symbol} {'in' if intent.symbol in limits.universe else 'not in'} universe",
    )
    whole = intent.quantity == intent.quantity.to_integral_value() and intent.quantity >= 1
    rule("quantity", whole, f"{intent.quantity} shares")
    rule(
        "price_known",
        price is not None,
        f"last price {price}" if price is not None else "no recent price",
    )
    market_open = calendar.is_open(now)
    if price is not None and market_open:
        limit_min = int(limits.max_price_age.total_seconds() // 60)
        age_text = (
            f"{int(price_age.total_seconds() // 60)} min old"
            if price_age is not None
            else "age unknown"
        )
        rule(
            "price_fresh",
            closing or (price_age is not None and price_age <= limits.max_price_age),
            f"price {age_text}, limit {limit_min} min",
        )
    if price is not None:
        notional = intent.quantity * price
        cap = portfolio.equity * limits.max_order_notional_pct
        rule("order_notional", closing or notional <= cap, f"{notional:.2f} vs cap {cap:.2f}")
        after = held + intent.quantity if intent.side == "buy" else held - intent.quantity
        position_cap = portfolio.equity * limits.max_position_pct
        rule(
            "position_cap",
            closing or abs(after) * price <= position_cap,
            f"position after {after} shares = {abs(after) * price:.2f} vs cap {position_cap:.2f}",
        )
        rule("short_selling", limits.allow_short or after >= 0, f"position after order {after}")
    rule(
        "market_hours",
        market_open or limits.allow_when_closed or closing,
        "open" if market_open else "closed",
    )
    rule(
        "daily_order_cap",
        closing or state.orders_today < limits.max_orders_per_day,
        f"{state.orders_today} of {limits.max_orders_per_day} today",
    )

    failed = [r for r in rules if not r.passed]
    decision = "rejected" if failed else "approved"
    return RiskDecision(
        intent_id=intent.intent_id,
        intent=intent,
        decision=decision,
        approved_quantity=Decimal(0) if failed else intent.quantity,
        rules=rules,
        reason="; ".join(f"{r.rule}: {r.detail}" for r in failed) if failed else "all rules passed",
    )


class RiskOfficer(BaseAgent):
    name = "risk"
    version = "0.1"
    topics = (Topics.ORDER_INTENT, Topics.RISK_KILL_SWITCH)

    def __init__(
        self,
        bus: Bus,
        clock: Clock,
        limits: RiskLimits,
        portfolio: Callable[[], PortfolioView],
        price_of: Callable[[str], Decimal | None],
        price_age_of: Callable[[str], timedelta | None] | None = None,
    ) -> None:
        super().__init__(bus, clock)
        self.price_age_of = price_age_of
        self.limits = limits
        self.portfolio = portfolio
        self.price_of = price_of
        self.state = RiskState()

    async def restore(self, db) -> bool:
        """After a restart: today's order count comes back from the journal, and a kill switch that was
        engaged stays engaged until a human releases it."""
        now = self.clock.now()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        approved_today = await db.recent_events([Topics.RISK_APPROVED], day_start, limit=500)
        self.state.orders_day, self.state.orders_today = now.date(), len(approved_today)
        events = await db.recent_events([Topics.RISK_KILL_SWITCH], now - timedelta(days=7), limit=1)
        if events and events[0].payload.get("engaged", True):
            self.engage_kill_switch(
                str(events[0].payload.get("reason", ""))
                + " (restored after restart; /resume to release)"
            )
            return True
        return False

    def engage_kill_switch(self, reason: str) -> None:
        self.state.kill_switch, self.state.kill_reason = True, reason

    async def handle(self, event: Event) -> None:
        if event.type == Topics.RISK_KILL_SWITCH:
            if event.payload.get("engaged", True):
                self.engage_kill_switch(str(event.payload.get("reason", "manual")))
                log.warning("kill switch engaged: %s", self.state.kill_reason)
            else:
                self.state.kill_switch, self.state.kill_reason = False, ""
                log.warning("kill switch released: %s", event.payload.get("reason", ""))
            return
        intent = parse_payload(event)
        assert isinstance(intent, OrderIntent)
        now = self.clock.now()
        if self.state.orders_day != now.date():
            self.state.orders_day, self.state.orders_today = now.date(), 0
        age = self.price_age_of(intent.symbol) if self.price_age_of else None
        decision = evaluate(
            intent,
            self.portfolio(),
            self.price_of(intent.symbol),
            now,
            self.limits,
            self.state,
            price_age=age,
        )
        if decision.decision == "approved":
            self.state.orders_today += 1
        topic = Topics.RISK_APPROVED if decision.decision == "approved" else Topics.RISK_REJECTED
        await self.emit(topic, decision, symbol=intent.symbol, book=intent.book, caused_by=event)
        log.info(
            "risk %s %s %s %s: %s",
            decision.decision,
            intent.side,
            intent.quantity,
            intent.symbol,
            decision.reason,
        )
