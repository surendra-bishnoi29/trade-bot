"""The risk officer: deterministic, versioned, logged. No LLM, ever (architecture section 8).

Every order intent passes through named rules that each return pass or fail with the numbers.
Per order: universe, whole shares, one working order per side, a known and fresh price, notional
and position caps. Per book: orders per day, open positions, gross exposure, shorts with a cap,
market hours. Portfolio: gross exposure, sector concentration, a daily loss limit, a drawdown ladder
(de-risk, halt, kill), pattern-day-trader awareness. Exits are never blocked by limits that exist
to stop new risk; the kill switch allows only exits until a human releases it."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core import calendar
from desk.core.clock import Clock
from desk.core.events import Event, Topics, parse_payload
from desk.core.payloads import OrderIntent, RiskDecision, RuleResult
from desk.core.sectors import sector_of

log = logging.getLogger(__name__)

ONE = Decimal(1)
HALF = Decimal("0.5")
ZERO = Decimal(0)


@dataclass(frozen=True)
class BookLimits:
    max_order_notional_pct: Decimal = Decimal("0.05")  # of equity
    max_position_pct: Decimal = Decimal("0.10")  # of equity, after the order
    max_orders_per_day: int = 20
    max_open_positions: int = 10
    max_gross_pct: Decimal = Decimal("1.0")  # the book's positions, of equity
    allow_short: bool = False
    max_short_pct: Decimal = Decimal(0)  # the book's gross short exposure, of equity
    allow_when_closed: bool = True  # paper: Alpaca queues day orders for the next open
    new_position_pct: Decimal = Decimal("0.02")  # the planner's budget per new name
    always_open: bool = False  # crypto: no session, no queueing, no price-age rule by the clock


@dataclass(frozen=True)
class RiskLimits:
    universe: frozenset[str]
    # the swing book (flat fields, so older call sites keep working)
    max_order_notional_pct: Decimal = Decimal("0.05")
    max_position_pct: Decimal = Decimal("0.10")
    max_orders_per_day: int = 20
    max_open_positions: int = 10
    allow_short: bool = False
    allow_when_closed: bool = True
    new_position_pct: Decimal = Decimal("0.02")
    max_price_age: timedelta = timedelta(minutes=15)  # while open, never trade on an older price
    # the intraday book
    intraday: BookLimits = field(
        default_factory=lambda: BookLimits(
            max_orders_per_day=40,
            max_gross_pct=Decimal("0.5"),
            allow_short=True,
            max_short_pct=Decimal("0.25"),
            allow_when_closed=False,
        )
    )
    # the whole account
    max_gross_pct: Decimal = Decimal("1.0")
    max_sector_pct: Decimal = Decimal("0.30")
    max_daily_loss_pct: Decimal = Decimal("0.02")  # of the day's starting equity; then exits only
    drawdown_derisk_pct: Decimal = Decimal(
        "0.03"
    )  # from the equity peak: new positions at half size
    drawdown_halt_pct: Decimal = Decimal("0.05")  # no new positions
    drawdown_kill_pct: Decimal = Decimal("0.08")  # kill switch, released by a human
    pdt_equity_floor: Decimal = Decimal(25000)
    pdt_max_day_trades: int = 3  # in five business days, below the floor
    books: dict[str, BookLimits] = field(default_factory=dict)  # the lab's extra books
    max_short_pct: Decimal = Decimal("0.25")  # the swing book's gross short exposure, when allowed

    def for_book(self, book: str | None) -> BookLimits:
        if book == "intraday":
            return self.intraday
        if book in self.books:
            return self.books[book]
        return BookLimits(
            max_order_notional_pct=self.max_order_notional_pct,
            max_position_pct=self.max_position_pct,
            max_orders_per_day=self.max_orders_per_day,
            max_open_positions=self.max_open_positions,
            max_gross_pct=self.max_gross_pct,
            allow_short=self.allow_short,
            max_short_pct=self.max_short_pct if self.allow_short else ZERO,
            allow_when_closed=self.allow_when_closed,
            new_position_pct=self.new_position_pct,
        )

    @classmethod
    def from_settings(cls, settings, universe: list[str]) -> RiskLimits:
        lab = bool(getattr(settings, "lab_enabled", False))
        books: dict[str, BookLimits] = {}
        if lab:
            books["commodities"] = BookLimits(
                max_orders_per_day=40,
                max_gross_pct=settings.commodities_max_gross_pct,
                allow_short=settings.commodities_allow_short,
                max_short_pct=settings.commodities_max_gross_pct,
                allow_when_closed=False,
                new_position_pct=settings.lab_position_pct,
            )
            books["crypto"] = BookLimits(
                max_orders_per_day=60,
                max_gross_pct=settings.crypto_max_gross_pct,
                allow_short=False,
                allow_when_closed=True,
                always_open=True,
                new_position_pct=settings.lab_position_pct,
            )
        return cls(
            universe=frozenset(universe),
            max_open_positions=settings.max_open_positions,
            new_position_pct=settings.lab_position_pct if lab else settings.position_pct,
            allow_short=lab,  # the lab desk may short stocks; the swing cycle stays long-only
            books=books,
            intraday=BookLimits(
                max_orders_per_day=40,
                max_gross_pct=settings.intraday_max_gross_pct,
                allow_short=settings.intraday_allow_short,
                max_short_pct=settings.intraday_max_short_pct,
                allow_when_closed=False,
            ),
            max_gross_pct=settings.max_gross_pct,
            max_sector_pct=settings.max_sector_pct,
            max_daily_loss_pct=settings.max_daily_loss_pct,
            drawdown_derisk_pct=settings.drawdown_derisk_pct,
            drawdown_halt_pct=settings.drawdown_halt_pct,
            drawdown_kill_pct=settings.drawdown_kill_pct,
        )


@dataclass(frozen=True)
class PortfolioView:
    equity: Decimal
    positions: dict[str, Decimal]  # symbol -> signed quantity
    open_orders: dict[str, set[str]] = field(
        default_factory=dict
    )  # symbol -> sides working at the broker
    market_values: dict[str, Decimal] = field(default_factory=dict)  # symbol -> signed value
    day_start_equity: Decimal | None = None  # the previous close's equity, from the broker
    daytrade_count: int = 0  # day trades in the last five business days, from the broker
    pattern_day_trader: bool = False
    book_of: dict[str, str] = field(default_factory=dict)  # symbol -> book; unknown means swing


@dataclass
class RiskState:
    orders_day: date | None = None
    orders_today: int = 0
    orders_today_by_book: dict[str, int] = field(default_factory=dict)
    bought_today: set[str] = field(default_factory=set)  # for pattern-day-trader awareness
    kill_switch: bool = False
    kill_reason: str = ""
    peak_equity: Decimal | None = None  # the high-water mark the drawdown ladder measures from


def _is_closing(intent: OrderIntent, held: Decimal) -> bool:
    return (intent.side == "sell" and held > 0 and intent.quantity <= held) or (
        intent.side == "buy" and held < 0 and intent.quantity <= -held
    )


def _value(portfolio: PortfolioView, symbol: str, price: Decimal | None) -> Decimal:
    """A position's signed market value: the broker's number, or quantity times the last price."""
    if symbol in portfolio.market_values:
        return portfolio.market_values[symbol]
    qty = portfolio.positions.get(symbol, ZERO)
    return qty * price if price is not None and qty else ZERO


def drawdown(equity: Decimal, peak: Decimal | None) -> Decimal:
    if not peak or peak <= 0:
        return ZERO
    return max(ZERO, ONE - equity / peak)


def day_pnl(portfolio: PortfolioView) -> Decimal | None:
    if portfolio.day_start_equity is None or portfolio.day_start_equity <= 0:
        return None
    return portfolio.equity - portfolio.day_start_equity


def size_scale(portfolio: PortfolioView, limits: RiskLimits, state: RiskState) -> Decimal:
    """How much of the normal new-position budget the desk may use right now: 1, 0.5 or 0."""
    if state.kill_switch:
        return ZERO
    pnl = day_pnl(portfolio)
    if pnl is not None and pnl <= -(portfolio.day_start_equity or ZERO) * limits.max_daily_loss_pct:
        return ZERO
    dd = drawdown(portfolio.equity, state.peak_equity)
    if dd >= limits.drawdown_halt_pct:
        return ZERO
    if dd >= limits.drawdown_derisk_pct:
        return HALF
    return ONE


def evaluate(
    intent: OrderIntent,
    portfolio: PortfolioView,
    price: Decimal | None,
    now: datetime,
    limits: RiskLimits,
    state: RiskState,
    price_age: timedelta | None = None,
) -> RiskDecision:
    """Pure function: one intent against the rules. Exits are never blocked by limits on new risk."""
    book = limits.for_book(intent.book)
    held = portfolio.positions.get(intent.symbol, ZERO)
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
    rule(
        "quantity",
        whole or (intent.allow_fractional and intent.quantity > 0),
        f"{intent.quantity} {'units' if intent.allow_fractional else 'shares'}",
    )
    working = portfolio.open_orders.get(intent.symbol, set())
    rule(
        "duplicate_order",
        intent.side not in working,
        f"a {intent.side} order for {intent.symbol} is already working at the broker"
        if intent.side in working
        else "no working order on this side",
    )
    rule(
        "price_known",
        price is not None,
        f"last price {price}" if price is not None else "no recent price",
    )
    market_open = calendar.is_open(now) or book.always_open
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
    equity = portfolio.equity
    if price is not None:
        notional = intent.quantity * price
        cap = equity * book.max_order_notional_pct
        rule("order_notional", closing or notional <= cap, f"{notional:.2f} vs cap {cap:.2f}")
        after = held + intent.quantity if intent.side == "buy" else held - intent.quantity
        position_cap = equity * book.max_position_pct
        rule(
            "position_cap",
            closing or abs(after) * price <= position_cap,
            f"position after {after} shares = {abs(after) * price:.2f} vs cap {position_cap:.2f}",
        )
        # exposure after the order: every other position as it is, this one as it will be
        own_book = {
            s
            for s in portfolio.positions
            if portfolio.book_of.get(s, "swing") == (intent.book or "swing")
        }
        others = {s: _value(portfolio, s, None) for s in portfolio.positions if s != intent.symbol}
        gross_after = sum((abs(v) for v in others.values()), ZERO) + abs(after) * price
        book_gross_after = (
            sum((abs(v) for s, v in others.items() if s in own_book), ZERO) + abs(after) * price
        )
        short_after = sum((-v for s, v in others.items() if s in own_book and v < 0), ZERO) + (
            abs(after) * price if after < 0 else ZERO
        )
        if after < 0:
            short_cap = equity * book.max_short_pct
            rule(
                "short_selling",
                book.allow_short and short_after <= short_cap,
                f"position after order {after}; book short exposure {short_after:.2f} vs cap {short_cap:.2f}"
                if book.allow_short
                else f"position after order {after}; shorts not allowed in the {intent.book} book",
            )
        else:
            rule("short_selling", True, f"position after order {after}")
        gross_cap = equity * limits.max_gross_pct
        rule(
            "gross_exposure",
            closing or gross_after <= gross_cap,
            f"account gross after {gross_after:.2f} vs cap {gross_cap:.2f}",
        )
        book_cap = equity * book.max_gross_pct
        rule(
            "book_exposure",
            closing or book_gross_after <= book_cap,
            f"{intent.book} book gross after {book_gross_after:.2f} vs cap {book_cap:.2f}",
        )
        sector = sector_of(intent.symbol)
        sector_after = (
            sum((abs(v) for s, v in others.items() if sector_of(s) == sector), ZERO)
            + abs(after) * price
        )
        sector_cap = equity * limits.max_sector_pct
        rule(
            "sector_concentration",
            closing or sector_after <= sector_cap,
            f"{sector} after {sector_after:.2f} vs cap {sector_cap:.2f}",
        )
        opening_new = held == 0 and not closing
        open_count = sum(1 for s in own_book if portfolio.positions.get(s))
        rule(
            "max_positions",
            not opening_new or open_count + 1 <= book.max_open_positions,
            f"{open_count} open in the {intent.book} book, limit {book.max_open_positions}",
        )
        dd = drawdown(equity, state.peak_equity)
        derisk_cap = equity * book.new_position_pct * HALF
        if closing or dd < limits.drawdown_derisk_pct:
            ladder_ok, ladder_text = True, f"drawdown {dd:.1%} from peak"
        elif dd >= limits.drawdown_halt_pct:
            ladder_ok, ladder_text = (
                False,
                f"drawdown {dd:.1%} at or beyond the halt level {limits.drawdown_halt_pct:.0%}: no new risk",
            )
        else:
            ladder_ok = notional <= derisk_cap
            ladder_text = (
                f"drawdown {dd:.1%} past {limits.drawdown_derisk_pct:.0%}: new risk at half size, "
                f"{notional:.2f} vs {derisk_cap:.2f}"
            )
        rule("drawdown_ladder", ladder_ok, ladder_text)
    pnl = day_pnl(portfolio)
    if pnl is not None:
        loss_cap = -(portfolio.day_start_equity or ZERO) * limits.max_daily_loss_pct
        rule(
            "daily_loss_limit",
            closing or pnl > loss_cap,
            f"day P&L {pnl:+.2f} vs limit {loss_cap:.2f}",
        )
    completes_day_trade = intent.side == "sell" and held > 0 and intent.symbol in state.bought_today
    if equity < limits.pdt_equity_floor and completes_day_trade:
        rule(
            "pattern_day_trader",
            portfolio.daytrade_count < limits.pdt_max_day_trades,
            f"equity {equity:.0f} below {limits.pdt_equity_floor:.0f}; {portfolio.daytrade_count} day trades in five days, "
            f"this would be another",
        )
    rule(
        "market_hours",
        market_open or book.allow_when_closed or closing,
        "open" if market_open else "closed",
    )
    book_name = intent.book or "swing"
    book_orders = state.orders_today_by_book.get(
        book_name, state.orders_today if book_name == "swing" else 0
    )  # a state without per-book counts (older journals) counts everything as swing
    rule(
        "daily_order_cap",
        closing or book_orders < book.max_orders_per_day,
        f"{book_orders} of {book.max_orders_per_day} today in the {intent.book} book",
    )

    failed = [r for r in rules if not r.passed]
    decision = "rejected" if failed else "approved"
    return RiskDecision(
        intent_id=intent.intent_id,
        intent=intent,
        decision=decision,
        approved_quantity=ZERO if failed else intent.quantity,
        rules=rules,
        reason="; ".join(f"{r.rule}: {r.detail}" for r in failed) if failed else "all rules passed",
    )


class RiskOfficer(BaseAgent):
    name = "risk"
    version = "0.2"
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
        """After a restart: today's order counts and buys come back from the journal, the equity
        peak from every snapshot ever recorded, and a kill switch that was engaged stays engaged
        until a human releases it."""
        now = self.clock.now()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        approved_today = await db.recent_events([Topics.RISK_APPROVED], day_start, limit=500)
        self.state.orders_day, self.state.orders_today = now.date(), len(approved_today)
        self.state.orders_today_by_book = {}
        self.state.bought_today = set()
        for e in approved_today:
            intent = (e.payload or {}).get("intent") or {}
            book = intent.get("book") or e.book or "swing"
            self.state.orders_today_by_book[book] = self.state.orders_today_by_book.get(book, 0) + 1
            if intent.get("side") == "buy" and intent.get("symbol"):
                self.state.bought_today.add(str(intent["symbol"]))
        peak = getattr(db, "peak_equity", None)
        if peak is not None:
            self.state.peak_equity = await peak()
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

    def size_scale(self) -> Decimal:
        """What the planner may size new positions at right now: 1, 0.5 or 0 of the normal budget."""
        view = self.portfolio()
        self._track_peak(view)
        return size_scale(view, self.limits, self.state)

    def status(self) -> dict:
        view = self.portfolio()
        self._track_peak(view)
        pnl = day_pnl(view)
        return {
            "kill_switch": self.state.kill_switch,
            "kill_reason": self.state.kill_reason,
            "drawdown": float(drawdown(view.equity, self.state.peak_equity)),
            "peak_equity": float(self.state.peak_equity) if self.state.peak_equity else None,
            "day_pnl": float(pnl) if pnl is not None else None,
            "size_scale": float(size_scale(view, self.limits, self.state)),
            "orders_today": self.state.orders_today,
        }

    def _track_peak(self, view: PortfolioView) -> None:
        if view.equity > 0 and (
            self.state.peak_equity is None or view.equity > self.state.peak_equity
        ):
            self.state.peak_equity = view.equity

    async def _check_automatic_kill(self, view: PortfolioView) -> None:
        if self.state.kill_switch:
            return
        dd = drawdown(view.equity, self.state.peak_equity)
        if dd >= self.limits.drawdown_kill_pct:
            reason = (
                f"drawdown {dd:.1%} from the equity peak {self.state.peak_equity:.0f} "
                f"(limit {self.limits.drawdown_kill_pct:.0%}); exits only until /resume"
            )
            self.engage_kill_switch(reason)
            log.error("kill switch engaged automatically: %s", reason)
            await self.emit(Topics.RISK_KILL_SWITCH, {"engaged": True, "reason": reason})

    async def handle(self, event: Event) -> None:
        if event.type == Topics.RISK_KILL_SWITCH:
            if event.source == self.source:
                return  # our own announcement
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
            self.state.orders_today_by_book = {}
            self.state.bought_today = set()
        view = self.portfolio()
        self._track_peak(view)
        await self._check_automatic_kill(view)
        age = self.price_age_of(intent.symbol) if self.price_age_of else None
        decision = evaluate(
            intent,
            view,
            self.price_of(intent.symbol),
            now,
            self.limits,
            self.state,
            price_age=age,
        )
        if decision.decision == "approved":
            self.state.orders_today += 1
            book = intent.book or "swing"
            self.state.orders_today_by_book[book] = self.state.orders_today_by_book.get(book, 0) + 1
            if intent.side == "buy":
                self.state.bought_today.add(intent.symbol)
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
