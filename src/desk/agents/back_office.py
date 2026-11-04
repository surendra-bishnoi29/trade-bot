"""Back office: positions, cash and equity from the broker, reconciled on fills and on a timer."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from desk.agents.base import BaseAgent
from desk.agents.risk_officer import PortfolioView
from desk.bus.base import Bus
from desk.core.clock import Clock
from desk.core.events import Event, Topics
from desk.core.payloads import OrderUpdate, PortfolioSnapshot, PositionSnapshot, SystemHealth
from desk.data.alpaca import AlpacaTrading

log = logging.getLogger(__name__)


@dataclass
class Snapshot:
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    positions: dict[str, PositionSnapshot]
    as_of: datetime


class BackOffice(BaseAgent):
    name = "back_office"
    topics = (Topics.ORDER_FILLED, Topics.ORDER_PARTIAL)

    def __init__(
        self, bus: Bus, clock: Clock, trading: AlpacaTrading, paper_capital: Decimal, db=None
    ) -> None:
        super().__init__(bus, clock)
        self.trading = trading
        self.db = db
        self.recorded: set[str] | None = None
        self.snapshot = Snapshot(paper_capital, paper_capital, paper_capital, {}, clock.now())

    async def refresh(self) -> Snapshot:
        account = await self.trading.account()
        positions = await self.trading.positions()
        try:
            notes = await self.reconcile_orders()
            if notes:
                log.info("orders reconciled from the broker: %s", "; ".join(notes))
        except Exception as exc:  # noqa: BLE001 - reconciliation must not block the snapshot
            log.warning("order reconciliation failed: %s", exc)
        self.snapshot = Snapshot(
            equity=account.equity,
            cash=account.cash,
            buying_power=account.buying_power,
            positions={
                p.symbol: PositionSnapshot(
                    symbol=p.symbol,
                    qty=p.qty,
                    avg_entry=p.avg_entry,
                    market_value=p.market_value,
                    unrealized_pl=p.unrealized_pl,
                )
                for p in positions
            },
            as_of=self.clock.now(),
        )
        await self.emit(
            Topics.PORTFOLIO_RECONCILED,
            PortfolioSnapshot(
                equity=self.snapshot.equity,
                cash=self.snapshot.cash,
                buying_power=self.snapshot.buying_power,
                positions=list(self.snapshot.positions.values()),
                as_of=self.snapshot.as_of,
                broker="alpaca-paper" if self.trading.paper else "alpaca-live",
            ),
        )
        return self.snapshot

    async def reconcile_orders(self) -> list[str]:
        """Record broker fills, cancels and rejections that no running process tracked, so theses and the
        journal are right no matter which process placed the order."""
        terminal = {
            "filled": ("filled", Topics.ORDER_FILLED),
            "canceled": ("canceled", Topics.ORDER_CANCELED),
            "expired": ("canceled", Topics.ORDER_CANCELED),
            "rejected": ("rejected", Topics.ORDER_REJECTED),
        }
        if self.recorded is None:
            self.recorded = (
                await self.db.terminal_order_intents(self.clock.now() - timedelta(days=7))
                if self.db
                else set()
            )
        notes: list[str] = []
        for order in await self.trading.recent_orders(limit=50):
            intent_id = order.client_order_id
            if len(intent_id) != 26 or intent_id in self.recorded or order.status not in terminal:
                continue
            status, topic = terminal[order.status]
            self.recorded.add(intent_id)
            await self.emit(
                topic,
                OrderUpdate(
                    order_id=intent_id,
                    intent_id=intent_id,
                    broker_order_id=order.broker_order_id,
                    status=status,
                    side=order.side if order.side in ("buy", "sell") else None,
                    filled_quantity=order.filled_qty,
                    avg_price=order.filled_avg_price,
                    detail=f"reconciled from broker: {order.status}",
                ),
                symbol=order.symbol or None,
                book="swing",
            )
            notes.append(f"{order.symbol} {order.side} {order.filled_qty} {status}")
        return notes

    def view(self) -> PortfolioView:
        return PortfolioView(
            equity=self.snapshot.equity,
            positions={s: p.qty for s, p in self.snapshot.positions.items()},
        )

    def price_of(self, symbol: str) -> Decimal | None:
        p = self.snapshot.positions.get(symbol)
        if p and p.market_value is not None and p.qty:
            return abs(p.market_value / p.qty)
        return None

    async def handle(self, event: Event) -> None:
        await self.refresh()
        symbol = event.symbol
        if symbol and symbol in self.snapshot.positions:
            await self.emit(
                Topics.PORTFOLIO_POSITION,
                self.snapshot.positions[symbol],
                symbol=symbol,
                caused_by=event,
            )

    async def run_forever(
        self, interval_s: float = 300.0, *, failures_before_down: int = 3
    ) -> None:
        failures = 0
        while True:
            try:
                await self.refresh()
                failures = 0
            except Exception as exc:  # noqa: BLE001 - a broker hiccup must not kill the loop
                failures += 1
                log.warning("reconcile failed (%d): %s", failures, exc)
                if failures == failures_before_down:
                    await self.emit(
                        Topics.SYSTEM_HEALTH,
                        SystemHealth(
                            component="broker",
                            status="down",
                            detail=f"{failures} reconciliations failed: {exc}"[:300],
                        ),
                    )
            await self.clock.sleep(interval_s)
