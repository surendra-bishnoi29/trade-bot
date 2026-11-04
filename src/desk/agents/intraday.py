"""The intraday book: a pre-market playbook, one deterministic strategy on live minute bars, orders
through the risk officer under the book's own limits, flat before the close. Off by default: the
baselines lose after costs on recent sessions (`desk intraday-eval`), and the book exists so that a
strategy which passes that test can go live without new plumbing.

No LLM. The strategies are the ones the evaluator scores, decided at each bar's close and executed
at the next prints, which is what the evaluator assumes."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd
from ulid import ULID

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core import calendar
from desk.core.clock import Clock
from desk.core.events import Event, Topics, parse_payload
from desk.core.payloads import IntradayTrade, OrderIntent, OrderUpdate
from desk.core.sectors import REFERENCES
from desk.storage.db import Database

log = logging.getLogger(__name__)

PLAYBOOK_BEFORE_OPEN = timedelta(minutes=45)
NO_ENTRIES_BEFORE_CLOSE = timedelta(minutes=30)
STRATEGIES = ("opening_range", "vwap_reversion", "momentum")


class SessionTape:
    """A name's bars since the open with the running session VWAP."""

    def __init__(self) -> None:
        self.rows: list[dict[str, float | datetime]] = []
        self._notional = 0.0
        self._volume = 0.0

    def add(self, bar: dict[str, Any]) -> dict[str, float | datetime]:
        o, h, lo, c = (float(bar[k]) for k in ("open", "high", "low", "close"))
        v = float(bar.get("volume") or 0)
        typical = (h + lo + c) / 3
        bar_vwap = float(bar["vwap"]) if bar.get("vwap") else typical
        self._notional += bar_vwap * v
        self._volume += v
        vwap = self._notional / self._volume if self._volume else typical
        row = {
            "ts": bar["ts"],
            "open": o,
            "high": h,
            "low": lo,
            "close": c,
            "volume": v,
            "vwap": vwap,
        }
        self.rows.append(row)
        return row

    @property
    def df(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows)


@dataclass
class Signal:
    side: str  # long or short
    stop: float
    target: float | None
    to_vwap: bool = False
    note: str = ""


def live_signal(
    strategy: str,
    tape: SessionTape,
    *,
    orb_minutes: int = 15,
    orb_target_r: float = 1.0,
    vwap_after: int = 30,
    vwap_threshold: float = 0.005,
    momentum_at: int = 60,
    momentum_threshold: float = 0.0075,
    stop_pct: float = 0.01,
) -> Signal | None:
    """The strategy's entry decision at the close of the latest bar, matching the evaluator's rules."""
    rows = tape.rows
    n = len(rows)
    if n == 0:
        return None
    last = rows[-1]
    close = float(last["close"])
    if strategy == "opening_range":
        if n <= orb_minutes:
            return None
        rng = rows[:orb_minutes]
        hi, lo = max(float(r["high"]) for r in rng), min(float(r["low"]) for r in rng)
        width = hi - lo
        if width <= 0:
            return None
        mid = (hi + lo) / 2
        if float(last["high"]) > hi:
            return Signal(
                "long",
                mid,
                close + width * orb_target_r,
                note=f"broke above the opening range {lo:.2f}-{hi:.2f}",
            )
        if float(last["low"]) < lo:
            return Signal(
                "short",
                mid,
                close - width * orb_target_r,
                note=f"broke below the opening range {lo:.2f}-{hi:.2f}",
            )
        return None
    if strategy == "vwap_reversion":
        if n < vwap_after + 1:
            return None
        v = float(last["vwap"])
        if v <= 0:
            return None
        dev = close / v - 1
        if dev <= -vwap_threshold:
            return Signal(
                "long",
                close * (1 - stop_pct),
                None,
                to_vwap=True,
                note=f"{dev:+.2%} below VWAP {v:.2f}",
            )
        if dev >= vwap_threshold:
            return Signal(
                "short",
                close * (1 + stop_pct),
                None,
                to_vwap=True,
                note=f"{dev:+.2%} above VWAP {v:.2f}",
            )
        return None
    if strategy == "momentum":
        if n != momentum_at:
            return None
        r = close / float(rows[0]["open"]) - 1
        v = float(last["vwap"])
        if r >= momentum_threshold and close > v:
            return Signal(
                "long", close * (1 - stop_pct), None, note=f"{r:+.2%} in the first hour, above VWAP"
            )
        if r <= -momentum_threshold and close < v:
            return Signal(
                "short",
                close * (1 + stop_pct),
                None,
                note=f"{r:+.2%} in the first hour, below VWAP",
            )
        return None
    raise ValueError(f"unknown intraday strategy {strategy!r}")


def exit_reason(position: Position, bar: dict[str, Any]) -> str | None:
    """Stop, target or VWAP touched on this bar; the stop wins when both are touched."""
    high, low = float(bar["high"]), float(bar["low"])
    if position.side == "long":
        if low <= position.stop:
            return "stop"
        if position.target is not None and high >= position.target:
            return "target"
        if position.to_vwap and high >= float(bar["vwap"]):
            return "vwap"
    else:
        if high >= position.stop:
            return "stop"
        if position.target is not None and low <= position.target:
            return "target"
        if position.to_vwap and low <= float(bar["vwap"]):
            return "vwap"
    return None


@dataclass
class Position:
    symbol: str
    side: str
    quantity: Decimal
    stop: float
    target: float | None
    to_vwap: bool
    note: str
    opened_at: datetime
    entry_intent: str
    entry_price: Decimal | None = None
    filled: bool = False
    exit_intent: str | None = None
    exit_reason: str = ""
    session: date | None = None


@dataclass
class Playbook:
    day: date
    names: list[dict[str, Any]] = field(default_factory=list)

    @property
    def symbols(self) -> list[str]:
        return [n["symbol"] for n in self.names]


async def build_playbook(
    db: Database, universe: list[str], now: datetime, *, size: int = 10
) -> Playbook:
    """The day's watchlist: the names that moved, traded and made news yesterday, ranked."""
    day = (
        calendar.session_of(now) if now.hour >= 12 else calendar.session_of(now - timedelta(days=1))
    )
    sessions = [
        d for d in calendar.last_sessions(now.date() + timedelta(days=1), 3) if d >= now.date()
    ]
    day = sessions[0] if sessions else day
    rows: list[dict[str, Any]] = []
    news_since = now - timedelta(hours=24)
    news = await db.recent_events([Topics.NEWS_STATE], news_since, limit=500, until=now)
    news_count: dict[str, int] = {}
    for e in news:
        if e.symbol and e.symbol not in news_count:
            news_count[e.symbol] = int(e.payload.get("count_24h") or 0)
    for symbol in universe:
        if symbol in REFERENCES:
            continue
        bars = await db.recent_bars(symbol, "1d", limit=21, before=now)
        if len(bars) < 21:
            continue
        closes = [float(b["close"]) for b in bars]
        vols = [float(b["volume"] or 0) for b in bars]
        last_ret = closes[-1] / closes[-2] - 1
        avg_vol = sum(vols[:-1]) / 20 if sum(vols[:-1]) else 0.0
        rvol = vols[-1] / avg_vol if avg_vol else 0.0
        rng = sum(float(b["high"]) - float(b["low"]) for b in bars[-20:]) / 20 / closes[-1]
        rows.append(
            {
                "symbol": symbol,
                "last_return": last_ret,
                "rvol": rvol,
                "range_20d": rng,
                "news_24h": news_count.get(symbol, 0),
            }
        )
    if not rows:
        return Playbook(day=day)

    def rank(key: str, reverse: bool = True) -> dict[str, float]:
        ordered = sorted(
            rows, key=lambda r: abs(r[key]) if key == "last_return" else r[key], reverse=reverse
        )
        n = len(ordered)
        return {r["symbol"]: 1 - i / max(1, n - 1) for i, r in enumerate(ordered)}

    r_move, r_vol, r_rng, r_news = (
        rank("last_return"),
        rank("rvol"),
        rank("range_20d"),
        rank("news_24h"),
    )
    for r in rows:
        s = r["symbol"]
        r["score"] = round(0.35 * r_move[s] + 0.3 * r_vol[s] + 0.2 * r_rng[s] + 0.15 * r_news[s], 4)
        r["why"] = (
            f"yesterday {r['last_return']:+.1%}, relative volume {r['rvol']:.1f}x, "
            f"average range {r['range_20d']:.1%}, {r['news_24h']} articles in 24h"
        )
    rows.sort(key=lambda r: -r["score"])
    return Playbook(day=day, names=rows[:size])


class IntradayBook(BaseAgent):
    name = "intraday"
    topics = (
        Topics.MARKET_BAR_1M,
        Topics.ORDER_FILLED,
        Topics.ORDER_REJECTED,
        Topics.ORDER_CANCELED,
        Topics.RISK_REJECTED,
    )

    def __init__(
        self,
        bus: Bus,
        clock: Clock,
        db: Database,
        universe: list[str],
        back_office: Any,
        risk: Any,
        *,
        strategy: str = "vwap_reversion",
        position_pct: Decimal = Decimal("0.01"),
        watchlist_size: int = 10,
        flat_before_close: timedelta = timedelta(minutes=10),
        max_positions: int = 5,
    ) -> None:
        super().__init__(bus, clock)
        if strategy not in STRATEGIES:
            raise ValueError(f"unknown intraday strategy {strategy!r}; choose from {STRATEGIES}")
        self.db = db
        self.universe = universe
        self.back_office = back_office
        self.risk = risk
        self.strategy = strategy
        self.position_pct = position_pct
        self.watchlist_size = watchlist_size
        self.flat_before_close = flat_before_close
        self.max_positions = max_positions
        self.playbook: Playbook | None = None
        self.tapes: dict[str, SessionTape] = {}
        self.tape_day: date | None = None
        self.positions: dict[str, Position] = {}  # symbol -> the book's position (one per name)
        self.traded_today: set[str] = set()
        self.intent_to_symbol: dict[str, str] = {}
        self.trades: list[IntradayTrade] = []

    # ---- state ----

    def _tag(self, symbol: str, held: bool) -> None:
        book_of = getattr(self.back_office, "book_of", None)
        if isinstance(book_of, dict):
            if held:
                book_of[symbol] = "intraday"
            else:
                book_of.pop(symbol, None)

    def _reset_day(self, day: date) -> None:
        self.tapes = {}
        self.tape_day = day
        self.traded_today = set()

    async def restore(self) -> int:
        """After a restart during the session: the book's positions come back from today's fills."""
        now = self.clock.now()
        if not calendar.is_session(now.date()):
            return 0
        open_at, _ = calendar.session_bounds(now.date())
        fills = await self.db.recent_events(
            [Topics.ORDER_FILLED], open_at - timedelta(hours=1), limit=500, until=now
        )
        net: dict[str, Decimal] = {}
        for e in fills:
            if e.book != "intraday" or not e.symbol:
                continue
            qty = Decimal(str(e.payload.get("filled_quantity") or 0))
            side = e.payload.get("side") or "buy"
            net[e.symbol] = net.get(e.symbol, Decimal(0)) + (qty if side == "buy" else -qty)
        restored = 0
        for symbol, qty in net.items():
            if qty == 0:
                continue
            side = "long" if qty > 0 else "short"
            price = (
                float(self.back_office.price_of(symbol) or 0)
                if hasattr(self.back_office, "price_of")
                else 0.0
            )
            stop = price * (0.99 if side == "long" else 1.01) if price else 0.0
            self.positions[symbol] = Position(
                symbol,
                side,
                abs(qty),
                stop,
                None,
                False,
                "restored after a restart",
                now,
                "restored",
                filled=True,
                session=now.date(),
            )
            self.traded_today.add(symbol)
            self._tag(symbol, True)
            restored += 1
        if restored:
            log.warning(
                "intraday book restored %d positions from today's fills; they close by the close",
                restored,
            )
        return restored

    # ---- the playbook ----

    async def write_playbook(self, caused_by: Event | None = None) -> Playbook:
        now = self.clock.now()
        self.playbook = await build_playbook(self.db, self.universe, now, size=self.watchlist_size)
        names = ", ".join(f"{n['symbol']} ({n['why']})" for n in self.playbook.names[:5])
        await self.emit(
            Topics.RESEARCH_REPORT,
            {
                "analyst": "intraday",
                "kind": "playbook",
                "symbol": None,
                "stance": "none",
                "conviction": 0.0,
                "horizon": "today",
                "summary": f"Intraday watchlist for {self.playbook.day} ({self.strategy}): {names or 'nothing qualifies'}",
                "data": {
                    "day": self.playbook.day.isoformat(),
                    "strategy": self.strategy,
                    "watchlist": self.playbook.names,
                },
            },
            caused_by=caused_by,
        )
        log.info(
            "intraday playbook for %s: %s", self.playbook.day, ", ".join(self.playbook.symbols)
        )
        return self.playbook

    def playbook_time(self, day: date) -> datetime:
        return calendar.session_bounds(day)[0] - PLAYBOOK_BEFORE_OPEN

    def flat_time(self, day: date) -> datetime:
        return calendar.session_bounds(day)[1] - self.flat_before_close

    async def run_forever(self) -> None:
        """The two timed duties: the playbook before the open and the sweep before the close."""
        await self.restore()
        while True:
            now = self.clock.now()
            day = next(
                (
                    d
                    for d in calendar.last_sessions(now.date() + timedelta(days=10), 10)
                    if d >= now.date()
                ),
                None,
            )
            if day is None:
                await self.clock.sleep(3600)
                continue
            duties = []
            if self.playbook is None or self.playbook.day != day:
                at = self.playbook_time(day)
                if now >= at and now < calendar.session_bounds(day)[1]:
                    try:
                        await self.write_playbook()
                    except Exception as exc:  # noqa: BLE001
                        log.error("playbook failed: %s", exc)
                        self.playbook = Playbook(day=day)
                    continue
                duties.append(at)
            flat_at = self.flat_time(day)
            if now >= flat_at and now < calendar.session_bounds(day)[1] + timedelta(minutes=1):
                if any(p.filled and not p.exit_intent for p in self.positions.values()):
                    await self.flatten("flat before the close")
            elif now < flat_at:
                duties.append(flat_at)
            if not duties:
                nxt = calendar.session_bounds(day)[1] + timedelta(minutes=2)
            else:
                nxt = min(duties)
            remaining = (nxt - self.clock.now()).total_seconds()
            await self.clock.sleep(min(60.0, max(1.0, remaining)))

    # ---- bars ----

    async def handle(self, event: Event) -> None:
        if event.type == Topics.MARKET_BAR_1M:
            await self._on_bar(event)
        elif event.type == Topics.RISK_REJECTED:
            symbol = self.intent_to_symbol.pop(event.payload.get("intent_id", ""), None)
            if symbol and symbol in self.positions and not self.positions[symbol].filled:
                self.positions.pop(symbol, None)  # the entry never happened
                log.info("intraday entry for %s refused by the risk officer", symbol)
        else:
            await self._on_order(event)

    async def _on_bar(self, event: Event) -> None:
        symbol = event.symbol
        if not symbol or self.playbook is None:
            return
        now = self.clock.now()
        day = event.occurred_at.date()
        if not calendar.is_session(day):
            return
        open_at, close_at = calendar.session_bounds(day)
        if event.occurred_at < open_at or event.occurred_at >= close_at:
            return
        if self.tape_day != day:
            self._reset_day(day)
        if symbol in self.positions:
            p = self.positions[symbol]
            if p.filled and not p.exit_intent:
                reason = exit_reason(p, event.payload)
                if reason:
                    await self._exit(p, reason, event)
        if symbol not in self.playbook.symbols:
            return
        tape = self.tapes.setdefault(symbol, SessionTape())
        tape.add({**event.payload, "ts": event.occurred_at})
        if symbol in self.positions or symbol in self.traded_today:
            return
        if now >= close_at - NO_ENTRIES_BEFORE_CLOSE:
            return
        if getattr(getattr(self.risk, "state", None), "kill_switch", False):
            return
        if sum(1 for p in self.positions.values() if not p.exit_intent) >= self.max_positions:
            return
        signal = live_signal(self.strategy, tape)
        if signal is None:
            return
        await self._enter(symbol, signal, event)

    def _size(self, price: float) -> Decimal:
        equity = Decimal(str(getattr(self.back_office.snapshot, "equity", 0)))
        budget = equity * self.position_pct
        qty = math.floor(float(budget) / price) if price > 0 else 0
        return Decimal(qty)

    async def _enter(self, symbol: str, signal: Signal, caused_by: Event) -> None:
        price = float(caused_by.payload["close"])
        qty = self._size(price)
        if qty < 1:
            return
        intent = OrderIntent(
            intent_id=str(ULID()),
            symbol=symbol,
            book="intraday",
            side="buy" if signal.side == "long" else "sell",
            quantity=qty,
            urgency="now",
            rationale_event_ids=[caused_by.event_id],
        )
        self.positions[symbol] = Position(
            symbol,
            signal.side,
            qty,
            signal.stop,
            signal.target,
            signal.to_vwap,
            signal.note,
            self.clock.now(),
            intent.intent_id,
            session=caused_by.occurred_at.date(),
        )
        self.traded_today.add(symbol)
        self.intent_to_symbol[intent.intent_id] = symbol
        await self.emit(
            Topics.ORDER_INTENT, intent, symbol=symbol, book="intraday", caused_by=caused_by
        )
        log.info("intraday %s %s %s: %s", signal.side, qty, symbol, signal.note)

    async def _exit(self, p: Position, reason: str, caused_by: Event | None) -> None:
        intent = OrderIntent(
            intent_id=str(ULID()),
            symbol=p.symbol,
            book="intraday",
            side="sell" if p.side == "long" else "buy",
            quantity=p.quantity,
            urgency="now",
            rationale_event_ids=[caused_by.event_id] if caused_by else [],
        )
        p.exit_intent, p.exit_reason = intent.intent_id, reason
        self.intent_to_symbol[intent.intent_id] = p.symbol
        await self.emit(
            Topics.ORDER_INTENT, intent, symbol=p.symbol, book="intraday", caused_by=caused_by
        )
        log.info("intraday exit %s: %s", p.symbol, reason)

    async def flatten(self, reason: str) -> int:
        n = 0
        for p in list(self.positions.values()):
            if p.filled and not p.exit_intent:
                await self._exit(p, reason, None)
                n += 1
        return n

    # ---- fills ----

    async def _on_order(self, event: Event) -> None:
        update = parse_payload(event)
        assert isinstance(update, OrderUpdate)
        symbol = self.intent_to_symbol.get(update.intent_id)
        p = self.positions.get(symbol) if symbol else None
        if p is None:
            return
        if event.type == Topics.ORDER_FILLED and update.intent_id == p.entry_intent:
            p.filled = True
            p.entry_price = update.avg_price
            if update.filled_quantity:
                p.quantity = update.filled_quantity
            self._tag(p.symbol, True)
            return
        if event.type == Topics.ORDER_FILLED and update.intent_id == p.exit_intent:
            exit_price = update.avg_price or Decimal(0)
            entry = p.entry_price or exit_price
            sign = 1 if p.side == "long" else -1
            pnl = (exit_price - entry) * p.quantity * sign
            trade = IntradayTrade(
                symbol=p.symbol,
                side=p.side,
                quantity=p.quantity,
                entry_price=entry,
                exit_price=exit_price,
                realized_pnl=pnl,
                opened_at=p.opened_at,
                closed_at=self.clock.now(),
                strategy=self.strategy,
                reason=p.exit_reason,
                note=p.note,
            )
            self.trades.append(trade)
            self.positions.pop(p.symbol, None)
            self._tag(p.symbol, False)
            await self.emit(
                Topics.INTRADAY_TRADE, trade, symbol=p.symbol, book="intraday", caused_by=event
            )
            log.info("intraday round trip %s %s: %s, P&L %s", p.side, p.symbol, p.exit_reason, pnl)
            return
        if event.type in (Topics.ORDER_REJECTED, Topics.ORDER_CANCELED):
            if update.intent_id == p.entry_intent and not p.filled:
                self.positions.pop(p.symbol, None)
                log.warning("intraday entry for %s %s: %s", p.symbol, update.status, update.detail)
            elif update.intent_id == p.exit_intent:
                p.exit_intent, p.exit_reason = None, ""
                log.error("intraday exit for %s %s; will try again", p.symbol, update.status)
