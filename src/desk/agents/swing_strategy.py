"""Swing strategy, version 0: the thesis monitor. Exits on stop, target or time, deterministically."""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from ulid import ULID

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core import calendar
from desk.core.clock import Clock
from desk.core.events import Event, Topics, parse_payload
from desk.core.payloads import OrderIntent, OrderUpdate, ThesisPayload
from desk.storage.db import Database

log = logging.getLogger(__name__)


def evaluate_thesis(thesis: ThesisPayload, close: Decimal, now: datetime) -> str | None:
    """Why an active long thesis should exit now, or None. Checked on every bar of the symbol."""
    if thesis.status != "active" or thesis.entry_price is None:
        return None
    if thesis.stop_price is not None and close <= thesis.stop_price:
        return "stop"
    if thesis.target_price is not None and close >= thesis.target_price:
        return "target"
    if calendar.sessions_between(thesis.created_at.date(), now.date()) >= thesis.horizon_days:
        return "expired"
    return None


def price_levels(entry: Decimal, stop_pct: float, target_pct: float) -> tuple[Decimal, Decimal]:
    cent = Decimal("0.01")
    stop = (entry * (Decimal(1) - Decimal(str(stop_pct)) / 100)).quantize(cent)
    target = (entry * (Decimal(1) + Decimal(str(target_pct)) / 100)).quantize(cent)
    return stop, target


class SwingStrategy(BaseAgent):
    name = "swing_strategy"
    topics = (
        Topics.MARKET_BAR_1M,
        Topics.MARKET_BAR_1D,
        Topics.ORDER_INTENT,
        Topics.ORDER_FILLED,
        Topics.ORDER_REJECTED,
        Topics.ORDER_CANCELED,
        Topics.THESIS_CREATED,
    )

    def __init__(self, bus: Bus, clock: Clock, db: Database) -> None:
        super().__init__(bus, clock)
        self.db = db
        self.theses: dict[str, ThesisPayload] = {}  # thesis_id -> thesis (open ones)
        self.intent_to_thesis: dict[str, str] = {}
        self.exiting: set[str] = set()

    async def load(self) -> int:
        self.theses = {t.thesis_id: t for t in await self.db.open_theses()}
        return len(self.theses)

    async def reconcile(self, positions: dict[str, tuple[Decimal, Decimal | None]]) -> list[str]:
        """Settle theses against the broker's positions after a restart: a pending entry whose position
        exists becomes active at the broker's average price; a closing thesis with no position is closed."""
        now = self.clock.now()
        notes: list[str] = []
        for thesis in list(self.theses.values()):
            held = positions.get(thesis.symbol)
            if thesis.status == "pending_entry" and held and held[0] > 0:
                entry = held[1] or thesis.entry_price or Decimal(0)
                stop, target = price_levels(entry, thesis.stop_pct, thesis.target_pct)
                updated = await self.db.update_thesis(
                    thesis.thesis_id,
                    now,
                    status="active",
                    entry_price=entry,
                    stop_price=stop,
                    target_price=target,
                    quantity=held[0],
                )
                if updated:
                    self.theses[thesis.thesis_id] = updated
                    await self.emit(
                        Topics.THESIS_UPDATED, updated, symbol=thesis.symbol, book="swing"
                    )
                    notes.append(f"{thesis.symbol} activated at {entry}")
            elif thesis.status == "closing" and (not held or held[0] <= 0):
                updated = await self.db.update_thesis(
                    thesis.thesis_id,
                    now,
                    status="closed",
                    closed_at=now,
                    reason=thesis.reason + " (reconciled: position gone)",
                )
                if updated:
                    self.theses.pop(thesis.thesis_id, None)
                    self.exiting.discard(thesis.thesis_id)
                    await self.emit(
                        Topics.THESIS_CLOSED, updated, symbol=thesis.symbol, book="swing"
                    )
                    notes.append(f"{thesis.symbol} closed")
        return notes

    def by_symbol(self, symbol: str) -> list[ThesisPayload]:
        return [t for t in self.theses.values() if t.symbol == symbol]

    async def handle(self, event: Event) -> None:
        if event.type == Topics.THESIS_CREATED:
            thesis = parse_payload(event)
            assert isinstance(thesis, ThesisPayload)
            self.theses[thesis.thesis_id] = thesis
        elif event.type == Topics.ORDER_INTENT:
            if event.payload.get("thesis_id"):
                self.intent_to_thesis[event.payload["intent_id"]] = event.payload["thesis_id"]
        elif event.type in (Topics.ORDER_FILLED, Topics.ORDER_REJECTED, Topics.ORDER_CANCELED):
            await self._on_order(event)
        elif event.type in (Topics.MARKET_BAR_1M, Topics.MARKET_BAR_1D) and event.symbol:
            close = Decimal(str(event.payload["close"]))
            for thesis in self.by_symbol(event.symbol):
                reason = evaluate_thesis(thesis, close, self.clock.now())
                if reason and thesis.thesis_id not in self.exiting:
                    await self._exit(thesis, reason, close, event)

    async def _exit(
        self, thesis: ThesisPayload, reason: str, close: Decimal, caused_by: Event
    ) -> None:
        self.exiting.add(thesis.thesis_id)
        now = self.clock.now()
        updated = await self.db.update_thesis(
            thesis.thesis_id, now, status="closing", reason=f"{reason} at {close}"
        )
        if updated:
            self.theses[thesis.thesis_id] = updated
            await self.emit(
                Topics.THESIS_INVALIDATED if reason != "target" else Topics.THESIS_UPDATED,
                updated,
                symbol=thesis.symbol,
                book="swing",
                caused_by=caused_by,
            )
        intent = OrderIntent(
            intent_id=str(ULID()),
            symbol=thesis.symbol,
            book="swing",
            side="sell",
            quantity=thesis.quantity,
            thesis_id=thesis.thesis_id,
            urgency="now",
            rationale_event_ids=[caused_by.event_id],
        )
        self.intent_to_thesis[intent.intent_id] = thesis.thesis_id
        await self.emit(
            Topics.ORDER_INTENT, intent, symbol=thesis.symbol, book="swing", caused_by=caused_by
        )
        log.info("exit %s %s: %s at %s", thesis.symbol, thesis.thesis_id, reason, close)

    async def _on_order(self, event: Event) -> None:
        update = parse_payload(event)
        assert isinstance(update, OrderUpdate)
        thesis_id = self.intent_to_thesis.get(update.intent_id)
        thesis = self.theses.get(thesis_id) if thesis_id else None
        if thesis is None:
            return
        now = self.clock.now()
        if event.type == Topics.ORDER_FILLED and thesis.status == "pending_entry":
            entry = update.avg_price or thesis.entry_price or Decimal(0)
            stop, target = price_levels(entry, thesis.stop_pct, thesis.target_pct)
            updated = await self.db.update_thesis(
                thesis.thesis_id,
                now,
                status="active",
                entry_price=entry,
                stop_price=stop,
                target_price=target,
                quantity=update.filled_quantity or thesis.quantity,
            )
            topic = Topics.THESIS_UPDATED
        elif event.type == Topics.ORDER_FILLED and thesis.status == "closing":
            exit_price = update.avg_price or Decimal(0)
            pnl = (exit_price - (thesis.entry_price or exit_price)) * (
                update.filled_quantity or thesis.quantity
            )
            updated = await self.db.update_thesis(
                thesis.thesis_id, now, status="closed", closed_at=now, realized_pnl=pnl
            )
            topic = Topics.THESIS_CLOSED
        elif (
            event.type in (Topics.ORDER_REJECTED, Topics.ORDER_CANCELED)
            and thesis.status == "pending_entry"
        ):
            updated = await self.db.update_thesis(
                thesis.thesis_id,
                now,
                status="rejected",
                reason=f"entry order {update.status}: {update.detail}"[:300],
            )
            topic = Topics.THESIS_CLOSED
        elif (
            event.type in (Topics.ORDER_REJECTED, Topics.ORDER_CANCELED)
            and thesis.status == "closing"
        ):
            self.exiting.discard(thesis.thesis_id)
            updated = await self.db.update_thesis(
                thesis.thesis_id,
                now,
                status="active",
                reason=f"exit order {update.status}, still holding",
            )
            topic = Topics.THESIS_UPDATED
        else:
            return
        if updated is None:
            return
        if updated.status in ("closed", "rejected"):
            self.theses.pop(thesis.thesis_id, None)
            self.exiting.discard(thesis.thesis_id)
        else:
            self.theses[thesis.thesis_id] = updated
        await self.emit(topic, updated, symbol=thesis.symbol, book="swing", caused_by=event)
