"""Execution trader: sends risk-approved intents to the broker and reports what happened."""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core.clock import Clock
from desk.core.events import Event, Topics, parse_payload
from desk.core.payloads import OrderUpdate, RiskDecision
from desk.data.alpaca import AlpacaTrading

log = logging.getLogger(__name__)

TERMINAL = {
    "filled": Topics.ORDER_FILLED,
    "canceled": Topics.ORDER_CANCELED,
    "expired": Topics.ORDER_CANCELED,
    "rejected": Topics.ORDER_REJECTED,
}


class ExecutionAgent(BaseAgent):
    name = "execution"
    topics = (Topics.RISK_APPROVED,)

    def __init__(
        self,
        bus: Bus,
        clock: Clock,
        trading: AlpacaTrading,
        *,
        poll_s: float = 5.0,
        poll_for_s: float = 8 * 3600.0,
    ) -> None:
        super().__init__(bus, clock)
        self.trading = trading
        self.poll_s = poll_s
        self.poll_for_s = poll_for_s
        self._tasks: set[asyncio.Task[None]] = set()

    async def handle(self, event: Event) -> None:
        decision = parse_payload(event)
        assert isinstance(decision, RiskDecision)
        intent = decision.intent
        try:
            order = await self.trading.submit_market_order(
                intent.symbol,
                decision.approved_quantity,
                intent.side,
                client_order_id=intent.intent_id,
                stop_price=intent.stop_price,
            )
        except Exception as exc:  # noqa: BLE001 - the broker said no; record it, do not retry blindly
            await self.emit(
                Topics.ORDER_REJECTED,
                OrderUpdate(
                    order_id=intent.intent_id,
                    intent_id=intent.intent_id,
                    side=intent.side,
                    status="rejected",
                    detail=f"{type(exc).__name__}: {exc}"[:300],
                ),
                symbol=intent.symbol,
                book=intent.book,
                caused_by=event,
            )
            return
        submitted = await self.emit(
            Topics.ORDER_SUBMITTED,
            OrderUpdate(
                order_id=intent.intent_id,
                intent_id=intent.intent_id,
                side=intent.side,
                broker_order_id=order.broker_order_id,
                status="submitted",
                detail=order.status,
            ),
            symbol=intent.symbol,
            book=intent.book,
            caused_by=event,
        )
        task = asyncio.create_task(
            self._track(
                submitted, intent.intent_id, order.broker_order_id, intent.symbol, intent.book
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _track(
        self, submitted: Event, intent_id: str, broker_order_id: str, symbol: str, book: str
    ) -> None:
        waited = 0.0
        last_filled = Decimal(0)
        while waited < self.poll_for_s:
            await self.clock.sleep(self.poll_s)
            waited += self.poll_s
            order = await self.trading.get_order(broker_order_id)
            if order.status in TERMINAL:
                await self.emit(
                    TERMINAL[order.status],
                    OrderUpdate(
                        order_id=intent_id,
                        intent_id=intent_id,
                        broker_order_id=broker_order_id,
                        status="filled"
                        if order.status == "filled"
                        else (
                            "canceled"
                            if TERMINAL[order.status] == Topics.ORDER_CANCELED
                            else "rejected"
                        ),
                        filled_quantity=order.filled_qty,
                        avg_price=order.filled_avg_price,
                        detail=order.status,
                    ),
                    symbol=symbol,
                    book=book,
                    caused_by=submitted,  # type: ignore[arg-type]
                )
                return
            if order.filled_qty > last_filled:
                last_filled = order.filled_qty
                await self.emit(
                    Topics.ORDER_PARTIAL,
                    OrderUpdate(
                        order_id=intent_id,
                        intent_id=intent_id,
                        broker_order_id=broker_order_id,
                        status="partial",
                        filled_quantity=order.filled_qty,
                        avg_price=order.filled_avg_price,
                        detail=order.status,
                    ),
                    symbol=symbol,
                    book=book,
                    caused_by=submitted,  # type: ignore[arg-type]
                )
        log.info(
            "order %s still open after %.0fs (market closed? it stays queued at the broker)",
            intent_id,
            waited,
        )
