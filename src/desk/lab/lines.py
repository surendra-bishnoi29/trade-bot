"""The lines engine: every position the lab desk opens carries a base (its entry), a maximum-loss
line and a minimum-profit line. Once price clears the first profit line the stop moves to the base;
each time price clears the next step the profit line ratchets up; when price comes back to the
latest profit line, or touches the stop, or the horizon passes, the position is closed and the
prediction behind it is scored. The desk watches every minute; for stocks and funds the stop also
rests at the broker as a safety net. Stocks and funds are closed before the bell: a line cannot
protect a position through an overnight gap. No model calls."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from ulid import ULID

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core import calendar
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
    exit_at: datetime | None = None  # when the exit order was sent
    exit_tries: int = 0  # exits refused so far; each one waits longer before the next
    exit_retry_at: datetime | None = None
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
        self.profit_line = self.level(self.step_pct)  # the first step: reaching it arms the line
        self.opened_at = at
        self.filled = True

    def next_step(self) -> float | None:
        """The price the trade must reach for the lines to move again."""
        if self.base is None:
            return None
        cleared = self.steps + 1 if self.armed else 0
        return self.level((cleared + 1) * self.step_pct)

    def watch(self, high: float, low: float) -> list[str]:
        """Apply one bar. Returns the events that happened, 'exit' last if the position must close.

        The price moves in steps of `step_pct` from the base. One step in the trade's favour and
        the maximum-loss line moves up to the base: from then on the trade cannot lose. Every
        further step raises the minimum-profit line one step, so it always sits one step below
        the best price seen. The position closes when the price comes back to the minimum-profit
        line (or to the base before the line has risen), or at the maximum-loss line."""
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
        if self.armed and self.steps > 0 and breached(against, self.profit_line):
            self.exit_reason = "line"
            return ["exit"]
        out: list[str] = []
        if not self.armed and reached(favour, self.level(self.step_pct)):
            self.armed = True
            self.stop = self.base  # one step up: the worst case is now zero
            self.profit_line = self.base
            out.append("base")
        while self.armed and reached(favour, self.level((self.steps + 2) * self.step_pct)):
            self.steps += 1
            self.profit_line = self.level(self.steps * self.step_pct)
            out.append("ratchet")
        return out

    def exit_level(self) -> float | None:
        """Where the broker's resting stop should sit right now."""
        if self.base is None:
            return None
        return self.profit_line if self.armed and self.steps > 0 else self.stop


class LinesAgent(BaseAgent):
    name = "lines"
    topics = (
        Topics.PREDICTION_CALL,
        Topics.ORDER_INTENT,
        Topics.ORDER_FILLED,
        Topics.ORDER_REJECTED,
        Topics.ORDER_CANCELED,
        Topics.RISK_REJECTED,
        Topics.MARKET_BAR_1M,
        Topics.PORTFOLIO_RECONCILED,
    )

    def __init__(
        self,
        bus: Bus,
        clock: Clock,
        db: Database,
        trading: Any,
        back_office: Any = None,
        *,
        flat_minutes: int = 10,
        step_bounds: tuple[float, float] = (0.15, 0.3),
    ) -> None:
        super().__init__(bus, clock)
        self.db = db
        self.trading = trading
        self.back_office = back_office
        # stocks and funds are closed this many minutes before the bell; 0 holds them overnight
        self.flat_minutes = flat_minutes
        self.step_bounds = step_bounds  # the ratchet step in percent, for stocks and funds
        self.cancel_wait_s = 0.25  # between looks at a stop being cancelled
        self.unmanaged: set[str] = set()  # broker positions no line holds, for the status line
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

    def step_for(self, market: str, asked: float) -> float:
        """The ratchet step the desk uses: the predictor's, held inside the book's bounds. The
        bounds scale with the market's round-trip cost, so a step is never inside the costs."""
        k = ROUND_TRIP.get(market, ROUND_TRIP["stocks"]) / ROUND_TRIP["stocks"]
        lo, hi = self.step_bounds[0] * k, self.step_bounds[1] * k
        return round(min(max(asked, lo), hi), 4)

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
                    step_pct=self.step_for(p["market"], float(c["step_pct"])),
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
        elif event.type == Topics.RISK_REJECTED:
            await self._on_risk_rejected(event)
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
                await self._unfilled(cid, line, "unfilled", event)
            elif update.intent_id == line.exit_intent:
                self._exit_refused(line, f"{update.status} by the broker")

    def _exit_refused(self, line: Line, why: str) -> None:
        """A decided exit stays decided: it is sent again, each time after a longer wait (20 seconds
        doubling to five minutes) so a broker that keeps saying no is not flooded."""
        line.exit_intent, line.exit_at = "", None
        line.exit_tries += 1
        wait = min(20 * 2 ** (line.exit_tries - 1), 300)
        line.exit_retry_at = self.clock.now() + timedelta(seconds=wait)
        log.error(
            "lines: exit for %s %s (try %d), again in %ds",
            line.symbol,
            why,
            line.exit_tries,
            wait,
        )

    def _retry_due(self, line: Line, now: datetime) -> bool:
        return bool(line.exit_reason) and (line.exit_retry_at is None or now >= line.exit_retry_at)

    async def _unfilled(self, cid: str, line: Line, reason: str, caused_by: Event) -> None:
        """The entry never became a position: forget it and say so, so the instrument is free again."""
        self.pending.pop(cid, None)
        await self.emit(
            Topics.PREDICTION_SCORE,
            PredictionScore(
                call_id=cid,
                symbol=line.symbol,
                market=line.market,
                direction=line.direction,
                confidence=line.confidence,
                reason=reason,
                closed_at=self.clock.now(),
            ),
            symbol=line.symbol,
            book=BOOK_OF_MARKET.get(line.market),  # type: ignore[arg-type]
            caused_by=caused_by,
        )

    async def _on_risk_rejected(self, event: Event) -> None:
        """The risk officer said no. An entry is dropped and scored as never opened; an exit is
        tried again at the next look, because a position must not be left without its way out."""
        intent_id = str(event.payload.get("intent_id") or "")
        cid = self.by_intent.get(intent_id)
        if cid is None:
            return
        line = self.pending.get(cid) or self.lines.get(self._symbol_of(cid) or "")
        if line is None:
            return
        if intent_id == line.entry_intent and not line.filled:
            log.warning(
                "lines: entry for %s refused by risk: %s", line.symbol, event.payload.get("reason")
            )
            await self._unfilled(cid, line, "risk", event)
        elif intent_id == line.exit_intent:
            self._exit_refused(line, f"refused by risk ({event.payload.get('reason')})")

    def _symbol_of(self, call_id: str) -> str | None:
        for s, line in self.lines.items():
            if line.call_id == call_id:
                return s
        return None

    async def _on_bar(self, event: Event) -> None:
        line = self.lines[event.symbol]  # type: ignore[index]
        if not line.filled or line.exit_intent:
            return
        if line.exit_reason:  # an exit already decided and refused: only the retry clock matters
            if self._retry_due(line, self.clock.now()):
                await self._exit(line, event)
            return
        if line.opened_at and event.occurred_at <= line.opened_at:
            # a bar that closed before the fill: after a restart the bus hands back the day's
            # earlier bars, and a low from the morning must not fire a line drawn at noon
            return
        p = event.payload
        high, low, close = float(p["high"]), float(p["low"]), float(p["close"])
        happened = line.watch(high, low)
        for what in happened:
            if what == "exit":
                await self._exit(line, event)
                return
            await self._announce(line, what, close, event)
        if what := self._due(line, self.clock.now()):
            line.exit_reason = what
            await self._exit(line, event)
        elif happened:
            await self._broker_stop(line)

    def _tradable(self, line: Line, now: datetime) -> bool:
        return line.market == "crypto" or calendar.is_open(now)

    def _due(self, line: Line, now: datetime) -> str | None:
        """A reason to close that no price bar brings: the horizon passed, or the bell is near."""
        if not self._tradable(line, now):
            return None  # nothing can be sold now; the first look after the open decides
        if line.opened_at and now - line.opened_at >= timedelta(minutes=line.horizon_minutes):
            return "horizon"
        if line.market != "crypto" and self.flat_minutes > 0:
            close_at = calendar.session_bounds(calendar.session_of(now))[1]
            if now >= close_at - timedelta(minutes=self.flat_minutes):
                return "close"
        return None

    async def tick(self) -> int:
        """The look that needs no bar: a quiet instrument still reaches its horizon and the bell,
        and an exit that never got an answer is sent again. Returns the exits sent."""
        now = self.clock.now()
        sent = 0
        for line in list(self.lines.values()):
            if not line.filled:
                continue
            if line.exit_intent:
                # an exit unanswered for three minutes: if the broker still shows the position and
                # no order working on it, the order was lost; clear it so the next look sends another
                snap = getattr(self.back_office, "snapshot", None)
                waited = line.exit_at is not None and now - line.exit_at > timedelta(minutes=3)
                if waited and snap is not None and self._tradable(line, now):
                    side = "sell" if line.direction == "long" else "buy"
                    held = line.symbol in getattr(snap, "positions", {})
                    working = side in getattr(snap, "open_orders", {}).get(line.symbol, set())
                    if held and not working and getattr(snap, "as_of", now) > line.exit_at:
                        self._exit_refused(line, "got no answer")
                continue
            if not self._tradable(line, now):
                continue
            if line.exit_reason:
                if not self._retry_due(line, now):
                    continue
            elif reason := self._due(line, now):
                line.exit_reason = reason
            else:
                continue
            await self._exit(line, None)
            sent += 1
        return sent

    async def run_forever(self, every_s: float = 20.0) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001 - the watch must outlive one bad look
                log.error("lines tick failed: %s", exc)
            await self.clock.sleep(every_s)

    async def _reconcile(self, event: Event) -> None:
        """The broker's stop may have fired while we watched: the position is gone, close the call."""
        positions = {
            p.get("symbol"): float(p.get("qty") or 0) for p in event.payload.get("positions") or []
        }
        as_of = event.payload.get("as_of")
        taken = datetime.fromisoformat(as_of) if isinstance(as_of, str) else event.occurred_at
        waiting = self.pending_symbols()
        unmanaged = {
            s for s, q in positions.items() if q and s not in self.lines and s not in waiting
        }
        if unmanaged - self.unmanaged:
            log.error(
                "lines: the broker holds %s with no line on it; nothing here will close it",
                ", ".join(sorted(unmanaged - self.unmanaged)),
            )
        self.unmanaged = unmanaged
        for symbol, line in list(self.lines.items()):
            if line.opened_at and line.opened_at > taken - timedelta(minutes=2):
                continue  # the snapshot may be older than the fill: wait for the next one
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
        if level is None or line.market == "crypto":
            return
        if not (line.entry_order_id or line.stop_order_id):
            return  # after a restart the stop's own id is enough to keep moving it
        try:
            if not line.stop_order_id:
                leg = await self.trading.stop_leg(line.entry_order_id)
                if leg is None:
                    return
                line.stop_order_id = leg.broker_order_id
            await self.trading.replace_stop(line.stop_order_id, Decimal(str(round(level, 2))))
        except Exception as exc:  # noqa: BLE001 - the desk's own watch still protects the position
            log.warning("lines: broker stop for %s not updated: %s", line.symbol, exc)

    async def _release_stop(self, line: Line) -> float | None:
        """Cancel the broker's resting stop and wait until it is gone, because the shares it holds
        cannot be sold by another order. Returns the fill price if the stop fired first."""
        order_id, line.stop_order_id = line.stop_order_id, ""
        try:
            await self.trading.cancel_order(order_id)
        except Exception as exc:  # noqa: BLE001 - already filled or gone: the look below tells
            log.warning("lines: could not cancel the broker stop for %s: %s", line.symbol, exc)
        for _ in range(8):
            try:
                o = await self.trading.get_order(order_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("lines: stop order lookup failed for %s: %s", line.symbol, exc)
                return None
            status = str(getattr(o, "status", "") or "")
            if status == "filled" and getattr(o, "filled_avg_price", None):
                return float(o.filled_avg_price)
            if status in ("canceled", "expired", "rejected", "replaced", "done_for_day", ""):
                return None
            await asyncio.sleep(self.cancel_wait_s)
        return None

    async def _exit(self, line: Line, caused_by: Event | None) -> None:
        if line.stop_order_id:
            stopped_at = await self._release_stop(line)
            if stopped_at is not None:  # the broker's stop got there first: the position is gone
                await self._close(line, stopped_at, "broker", caused_by)
                return
        intent = OrderIntent(
            intent_id=str(ULID()),
            symbol=line.symbol,
            book=BOOK_OF_MARKET.get(line.market, "swing"),  # type: ignore[arg-type]
            side="sell" if line.direction == "long" else "buy",
            quantity=line.quantity,
            urgency="now",
            rationale_event_ids=[caused_by.event_id] if caused_by is not None else [],
            call_id=line.call_id,
            allow_fractional=line.market == "crypto",
        )
        line.exit_intent = intent.intent_id
        line.exit_at = self.clock.now()
        self.by_intent[intent.intent_id] = line.call_id
        await self.emit(
            Topics.ORDER_INTENT, intent, symbol=line.symbol, book=intent.book, caused_by=caused_by
        )
        log.info("lines: exit %s (%s)", line.symbol, line.exit_reason)

    async def _close(
        self, line: Line, exit_px: float, reason: str, caused_by: Event | None
    ) -> None:
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

    async def _announce(self, line: Line, what: str, last: float, caused_by: Event | None) -> None:
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
