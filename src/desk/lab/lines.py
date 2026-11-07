"""The lines engine: every position the lab desk opens carries a base (its entry), a maximum-loss
line and a minimum-profit line. Once price clears the first profit line the stop moves to the base;
each time price clears the next step the profit line ratchets up; when price comes back to the
latest profit line, or touches the stop, or the horizon passes, the position is closed and the
prediction behind it is scored. The desk watches every minute; for stocks and funds the stop also
rests at the broker as a safety net. No model calls."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from ulid import ULID

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core.clock import Clock
from desk.core.events import Event, Topics, parse_payload
from desk.core.payloads import LinesUpdated, OrderIntent, OrderUpdate, PredictionScore
from desk.storage.db import Database

log = logging.getLogger(__name__)

BOOK_OF_MARKET = {"stocks": "swing", "commodities": "commodities", "crypto": "crypto"}
ROUND_TRIP = {"stocks": 0.001, "commodities": 0.001, "crypto": 0.005}


@dataclass
class Line:
    call_id: str
    symbol: str
    market: str
    direction: str  # long or short
    confidence: float
    quantity: Decimal
    max_loss_pct: float
    first_profit_pct: float
    step_pct: float
    horizon_minutes: int
    base: float | None = None
    stop: float | None = None
    profit_line: float | None = None
    armed: bool = False
    steps: int = 0
    opened_at: datetime | None = None
    entry_intent: str = ""
    entry_order_id: str = ""
    stop_order_id: str = ""
    exit_intent: str = ""
    exit_reason: str = ""
    filled: bool = False

    @property
    def sign(self) -> int:
        return 1 if self.direction == "long" else -1

    def level(self, pct: float) -> float:
        """A price `pct` percent in the trade's favour from the base."""
        assert self.base is not None
        return self.base * (1 + self.sign * pct / 100)

    def open(self, entry: float, at: datetime) -> None:
        self.base = entry
        self.stop = self.level(-self.max_loss_pct)
        self.profit_line = self.level(self.first_profit_pct)
        self.opened_at = at
        self.filled = True

    def watch(self, high: float, low: float) -> list[str]:
        """Apply one bar. Returns the events that happened, 'exit' last if the position must close."""
        if self.base is None or self.stop is None or self.profit_line is None:
            return []
        long = self.sign > 0
        favour, against = (high, low) if long else (low, high)

        def reached(price: float, level: float) -> bool:  # moved in the trade's favour to the level
            return price >= level if long else price <= level

        def breached(price: float, level: float) -> bool:  # moved against the trade to the level
            return price <= level if long else price >= level

        if breached(against, self.stop):
            self.exit_reason = "base" if self.stop == self.base else "stop"
            return ["exit"]
        if self.armed and breached(against, self.profit_line):
            self.exit_reason = "line"
            return ["exit"]
        out: list[str] = []
        if not self.armed and reached(favour, self.profit_line):
            self.armed = True
            self.stop = self.base  # above the base: the worst case is now zero
            out.append("base")
        while self.armed and reached(
            favour, self.level(self.first_profit_pct + (self.steps + 1) * self.step_pct)
        ):
            self.steps += 1
            self.profit_line = self.level(self.first_profit_pct + self.steps * self.step_pct)
            out.append("ratchet")
        return out

    def exit_level(self) -> float | None:
        """Where the broker's resting stop should sit right now."""
        if self.base is None:
            return None
        return self.profit_line if self.armed else self.stop


class LinesAgent(BaseAgent):
    name = "lines"
    topics = (
        Topics.PREDICTION_CALL,
        Topics.ORDER_INTENT,
        Topics.ORDER_FILLED,
        Topics.ORDER_REJECTED,
        Topics.ORDER_CANCELED,
        Topics.MARKET_BAR_1M,
        Topics.PORTFOLIO_RECONCILED,
    )

    def __init__(
        self, bus: Bus, clock: Clock, db: Database, trading: Any, back_office: Any = None
    ) -> None:
        super().__init__(bus, clock)
        self.db = db
        self.trading = trading
        self.back_office = back_office
        self.lines: dict[str, Line] = {}  # by symbol: one position per instrument
        self.pending: dict[str, Line] = {}  # by call id, until the entry fills
        self.by_intent: dict[str, str] = {}  # intent id -> call id

    # ---- state ----

    def _tag(self, symbol: str, book: str | None) -> None:
        book_of = getattr(self.back_office, "book_of", None)
        if isinstance(book_of, dict):
            if book:
                book_of[symbol] = book
            else:
                book_of.pop(symbol, None)

    async def restore(self) -> int:
        """After a restart the open lines come back from the journal: the latest state of every
        call that has not exited, kept only when the broker still shows the position."""
        now = self.clock.now()
        events = await self.db.recent_events(
            [Topics.LINES_UPDATED], now - timedelta(days=5), limit=2000, until=now
        )
        seen: set[str] = set()
        calls = await self.db.recent_events(
            [Topics.PREDICTION_CALL], now - timedelta(days=5), limit=2000, until=now
        )
        params = {c.payload["call_id"]: c.payload for c in calls}
        restored = 0
        for e in events:  # newest first
            p = e.payload
            cid = p["call_id"]
            if cid in seen:
                continue
            seen.add(cid)
            if p.get("event") == "exit":
                continue
            call = (params.get(cid) or {}).get("call") or {}
            line = Line(
                call_id=cid,
                symbol=p["symbol"],
                market=p["market"],
                direction=p["direction"],
                confidence=float(call.get("confidence") or 0),
                quantity=Decimal(str(p.get("quantity") or call.get("quantity") or 0)),
                max_loss_pct=float(call.get("max_loss_pct") or 1.0),
                first_profit_pct=float(call.get("first_profit_pct") or 1.0),
                step_pct=float(p.get("step_pct") or 0.5),
                horizon_minutes=int(call.get("horizon_minutes") or 240),
                base=float(p["base"]),
                stop=float(p["stop"]),
                profit_line=p.get("profit_line"),
                armed=bool(p.get("armed")),
                steps=int(p.get("steps") or 0),
                opened_at=datetime.fromisoformat(p["opened_at"])
                if p.get("opened_at")
                else e.occurred_at,
                stop_order_id=str(p.get("stop_order_id") or ""),
                filled=True,
            )
            self.lines[line.symbol] = line
            self._tag(line.symbol, BOOK_OF_MARKET.get(line.market))
            restored += 1
        if restored:
            log.warning("lines restored for %s", ", ".join(sorted(self.lines)))
        return restored

    # ---- events ----

    async def handle(self, event: Event) -> None:
        if event.type == Topics.PREDICTION_CALL:
            p = event.payload
            if p.get("acted"):
                c = p["call"]
                self.pending[p["call_id"]] = Line(
                    call_id=p["call_id"],
                    symbol=p["symbol"],
                    market=p["market"],
                    direction=c["direction"],
                    confidence=float(c["confidence"]),
                    quantity=Decimal(0),
                    max_loss_pct=float(c["max_loss_pct"]),
                    first_profit_pct=float(c["first_profit_pct"]),
                    step_pct=float(c["step_pct"]),
                    horizon_minutes=int(c["horizon_minutes"]),
                )
        elif event.type == Topics.ORDER_INTENT:
            p = event.payload
            cid = p.get("call_id")
            if cid and cid in self.pending:
                self.by_intent[p["intent_id"]] = cid
                self.pending[cid].entry_intent = p["intent_id"]
                self.pending[cid].quantity = Decimal(str(p["quantity"]))
        elif event.type in (Topics.ORDER_FILLED, Topics.ORDER_REJECTED, Topics.ORDER_CANCELED):
            await self._on_order(event)
        elif event.type == Topics.MARKET_BAR_1M:
            if event.symbol in self.lines:
                await self._on_bar(event)
        elif event.type == Topics.PORTFOLIO_RECONCILED:
            await self._reconcile(event)

    async def _on_order(self, event: Event) -> None:
        update = parse_payload(event)
        assert isinstance(update, OrderUpdate)
        cid = self.by_intent.get(update.intent_id)
        if cid is None:
            return
        line = self.pending.get(cid) or self.lines.get(self._symbol_of(cid) or "")
        if line is None:
            return
        if event.type == Topics.ORDER_FILLED and update.intent_id == line.entry_intent:
            entry = float(update.avg_price or 0)
            if entry <= 0:
                return
            if update.filled_quantity:
                line.quantity = update.filled_quantity
            line.open(entry, self.clock.now())
            line.entry_order_id = update.broker_order_id or ""
            self.pending.pop(cid, None)
            self.lines[line.symbol] = line
            self._tag(line.symbol, BOOK_OF_MARKET.get(line.market))
            await self._broker_stop(line)
            await self._announce(line, "opened", entry, event)
            return
        if event.type == Topics.ORDER_FILLED and update.intent_id == line.exit_intent:
            exit_px = float(update.avg_price or 0)
            await self._close(line, exit_px, line.exit_reason or "exit", event)
            return
        if event.type in (Topics.ORDER_REJECTED, Topics.ORDER_CANCELED):
            if update.intent_id == line.entry_intent:
                self.pending.pop(cid, None)
                await self.emit(
                    Topics.PREDICTION_SCORE,
                    PredictionScore(
                        call_id=cid,
                        symbol=line.symbol,
                        market=line.market,
                        direction=line.direction,
                        confidence=line.confidence,
                        reason="unfilled",
                        closed_at=self.clock.now(),
                    ),
                    symbol=line.symbol,
                    book=BOOK_OF_MARKET.get(line.market),  # type: ignore[arg-type]
                    caused_by=event,
                )
            elif update.intent_id == line.exit_intent:
                line.exit_intent, line.exit_reason = "", ""
                log.error("lines: exit for %s %s, will try again", line.symbol, update.status)

    def _symbol_of(self, call_id: str) -> str | None:
        for s, line in self.lines.items():
            if line.call_id == call_id:
                return s
        return None

    async def _on_bar(self, event: Event) -> None:
        line = self.lines[event.symbol]  # type: ignore[index]
        if not line.filled or line.exit_intent:
            return
        p = event.payload
        high, low, close = float(p["high"]), float(p["low"]), float(p["close"])
        happened = line.watch(high, low)
        for what in happened:
            if what == "exit":
                await self._exit(line, event)
                return
            await self._announce(line, what, close, event)
        if what := self._expired(line):
            line.exit_reason = what
            await self._exit(line, event)
        elif happened:
            await self._broker_stop(line)

    def _expired(self, line: Line) -> str | None:
        if line.opened_at and self.clock.now() - line.opened_at >= timedelta(
            minutes=line.horizon_minutes
        ):
            return "horizon"
        return None

    async def _reconcile(self, event: Event) -> None:
        """The broker's stop may have fired while we watched: the position is gone, close the call."""
        positions = {
            p.get("symbol"): float(p.get("qty") or 0) for p in event.payload.get("positions") or []
        }
        for symbol, line in list(self.lines.items()):
            if line.filled and not line.exit_intent and not positions.get(symbol):
                exit_px = line.exit_level() or line.base or 0.0
                if line.stop_order_id:
                    try:
                        o = await self.trading.get_order(line.stop_order_id)
                        if o.filled_avg_price:
                            exit_px = float(o.filled_avg_price)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("lines: stop order lookup failed for %s: %s", symbol, exc)
                await self._close(line, exit_px, "broker", event)

    # ---- actions ----

    async def _broker_stop(self, line: Line) -> None:
        """Keep the broker's resting stop at our exit level, where the broker offers one."""
        level = line.exit_level()
        if level is None or line.market == "crypto" or not line.entry_order_id:
            return
        try:
            if not line.stop_order_id:
                leg = await self.trading.stop_leg(line.entry_order_id)
                if leg is None:
                    return
                line.stop_order_id = leg.broker_order_id
            await self.trading.replace_stop(line.stop_order_id, Decimal(str(round(level, 2))))
        except Exception as exc:  # noqa: BLE001 - the desk's own watch still protects the position
            log.warning("lines: broker stop for %s not updated: %s", line.symbol, exc)

    async def _exit(self, line: Line, caused_by: Event) -> None:
        if line.stop_order_id:
            try:
                await self.trading.cancel_order(line.stop_order_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("lines: could not cancel the broker stop for %s: %s", line.symbol, exc)
            line.stop_order_id = ""
        intent = OrderIntent(
            intent_id=str(ULID()),
            symbol=line.symbol,
            book=BOOK_OF_MARKET.get(line.market, "swing"),  # type: ignore[arg-type]
            side="sell" if line.direction == "long" else "buy",
            quantity=line.quantity,
            urgency="now",
            rationale_event_ids=[caused_by.event_id],
            call_id=line.call_id,
            allow_fractional=line.market == "crypto",
        )
        line.exit_intent = intent.intent_id
        self.by_intent[intent.intent_id] = line.call_id
        await self.emit(
            Topics.ORDER_INTENT, intent, symbol=line.symbol, book=intent.book, caused_by=caused_by
        )
        log.info("lines: exit %s (%s)", line.symbol, line.exit_reason)

    async def _close(self, line: Line, exit_px: float, reason: str, caused_by: Event) -> None:
        realized = None
        if line.base and exit_px:
            realized = (exit_px / line.base - 1) * 100 * line.sign - ROUND_TRIP[line.market] * 100
        await self._announce(line, "exit", exit_px, caused_by)
        await self.emit(
            Topics.PREDICTION_SCORE,
            PredictionScore(
                call_id=line.call_id,
                symbol=line.symbol,
                market=line.market,
                direction=line.direction,
                confidence=line.confidence,
                entry=line.base,
                exit=exit_px,
                realized_pct=round(realized, 4) if realized is not None else None,
                hit=realized > 0 if realized is not None else None,
                reason=reason,
                opened_at=line.opened_at,
                closed_at=self.clock.now(),
            ),
            symbol=line.symbol,
            book=BOOK_OF_MARKET.get(line.market),  # type: ignore[arg-type]
            caused_by=caused_by,
        )
        self.lines.pop(line.symbol, None)
        self._tag(line.symbol, None)
        log.info(
            "lines: %s %s closed (%s) %s",
            line.direction,
            line.symbol,
            reason,
            f"{realized:+.2f}%" if realized is not None else "",
        )

    async def _announce(self, line: Line, what: str, last: float, caused_by: Event) -> None:
        await self.emit(
            Topics.LINES_UPDATED,
            LinesUpdated(
                symbol=line.symbol,
                market=line.market,
                call_id=line.call_id,
                direction=line.direction,
                base=line.base or 0.0,
                stop=line.stop or 0.0,
                profit_line=line.profit_line,
                step_pct=line.step_pct,
                armed=line.armed,
                steps=line.steps,
                event=what,
                last=last,
                quantity=float(line.quantity),
                stop_order_id=line.stop_order_id,
                opened_at=line.opened_at,
            ),
            symbol=line.symbol,
            book=BOOK_OF_MARKET.get(line.market),  # type: ignore[arg-type]
            caused_by=caused_by,
        )

    def pending_symbols(self) -> set[str]:
        return {line.symbol for line in self.pending.values()}

    def snapshot(self) -> list[dict[str, Any]]:
        return [asdict(line) for line in self.lines.values()]
