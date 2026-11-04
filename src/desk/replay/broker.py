"""The simulated broker: the execution trader and the back office talk to it exactly as they talk to
Alpaca. Market orders fill at the open of the next bar the replayer feeds it, with slippage and
commission always on. Orders placed while the market is closed wait for the next session's first bar,
like a day order queued at a real broker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ulid import ULID

from desk.core import calendar
from desk.core.clock import Clock
from desk.core.payloads import MarketBar
from desk.data.alpaca import AccountInfo, BrokerOrder, PositionInfo

CENT = Decimal("0.01")


@dataclass
class SimOrder:
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    qty: Decimal
    submitted_at: datetime
    status: str = "accepted"
    filled_qty: Decimal = Decimal(0)
    filled_avg_price: Decimal | None = None
    filled_at: datetime | None = None


@dataclass
class Holding:
    qty: Decimal = Decimal(0)
    avg_entry: Decimal = Decimal(0)


@dataclass
class Fill:
    at: datetime
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    reference: Decimal  # the bar's open the fill is measured against
    cost: Decimal  # slippage plus commission, in dollars
    client_order_id: str = ""
    realized_pnl: Decimal = Decimal(0)


class SimulatedBroker:
    paper = True

    def __init__(
        self,
        clock: Clock,
        cash: Decimal,
        *,
        slippage_bps: Decimal = Decimal(5),
        commission_per_share: Decimal = Decimal(0),
    ) -> None:
        self.clock = clock
        self.cash = Decimal(cash)
        self.starting_cash = Decimal(cash)
        self.slippage_bps = Decimal(slippage_bps)
        self.commission_per_share = Decimal(commission_per_share)
        self.last: dict[str, Decimal] = {}
        self.holdings: dict[str, Holding] = {}
        self.orders: dict[str, SimOrder] = {}
        self.fills: list[Fill] = []
        self.realized_pnl = Decimal(0)
        self.costs = Decimal(0)

    # ---- what the replayer calls ----

    def on_bar(self, bar: MarketBar) -> list[Fill]:
        """Mark the symbol and fill its pending orders at this bar's open plus slippage."""
        fills: list[Fill] = []
        if calendar.is_open(bar.ts):
            for order in self.orders.values():
                if (
                    order.status == "accepted"
                    and order.symbol == bar.symbol
                    and bar.ts > order.submitted_at
                ):
                    fills.append(self._fill(order, bar))
        self.last[bar.symbol] = bar.close
        return fills

    def _fill(self, order: SimOrder, bar: MarketBar) -> Fill:
        reference = bar.open
        slip = (reference * self.slippage_bps / Decimal(10000)).quantize(CENT)
        price = (reference + slip if order.side == "buy" else reference - slip).quantize(CENT)
        commission = (self.commission_per_share * order.qty).quantize(CENT)
        pos = self.holdings.setdefault(order.symbol, Holding())
        realized = Decimal(0)
        if order.side == "buy":
            total = pos.avg_entry * pos.qty + price * order.qty
            pos.qty += order.qty
            pos.avg_entry = (total / pos.qty).quantize(CENT)
            self.cash -= price * order.qty + commission
        else:
            realized = ((price - pos.avg_entry) * order.qty).quantize(CENT)
            pos.qty -= order.qty
            self.cash += price * order.qty - commission
            if pos.qty == 0:
                self.holdings.pop(order.symbol, None)
        self.realized_pnl += realized - commission
        cost = (slip * order.qty + commission).quantize(CENT)
        self.costs += cost
        order.status = "filled"
        order.filled_qty = order.qty
        order.filled_avg_price = price
        order.filled_at = bar.ts
        fill = Fill(
            at=bar.ts,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            price=price,
            reference=reference,
            cost=cost,
            client_order_id=order.client_order_id,
            realized_pnl=realized,
        )
        self.fills.append(fill)
        return fill

    def equity(self) -> Decimal:
        value = sum(
            (p.qty * self.last.get(s, p.avg_entry) for s, p in self.holdings.items()), Decimal(0)
        )
        return (self.cash + value).quantize(CENT)

    def unrealized_pnl(self) -> Decimal:
        return sum(
            (
                (self.last.get(s, p.avg_entry) - p.avg_entry) * p.qty
                for s, p in self.holdings.items()
            ),
            Decimal(0),
        ).quantize(CENT)

    # ---- the broker interface the agents use ----

    async def account(self) -> AccountInfo:
        return AccountInfo(
            status="ACTIVE",
            equity=self.equity(),
            cash=self.cash.quantize(CENT),
            buying_power=self.cash.quantize(CENT),
            pattern_day_trader=False,
            daytrade_count=0,
        )

    async def positions(self) -> list[PositionInfo]:
        out = []
        for symbol, p in self.holdings.items():
            last = self.last.get(symbol, p.avg_entry)
            out.append(
                PositionInfo(
                    symbol=symbol,
                    qty=p.qty,
                    avg_entry=p.avg_entry,
                    market_value=(p.qty * last).quantize(CENT),
                    unrealized_pl=((last - p.avg_entry) * p.qty).quantize(CENT),
                )
            )
        return out

    async def submit_market_order(
        self, symbol: str, qty: Decimal, side: str, client_order_id: str
    ) -> BrokerOrder:
        if qty <= 0:
            raise ValueError("quantity must be positive")
        held = self.holdings.get(symbol, Holding()).qty
        if side == "sell" and qty > held:
            raise ValueError(f"insufficient position: holding {held} {symbol}, selling {qty}")
        if side == "buy" and symbol in self.last and self.last[symbol] * qty > self.cash:
            raise ValueError("insufficient buying power")
        order = SimOrder(
            broker_order_id=str(ULID()),
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            qty=Decimal(qty),
            submitted_at=self.clock.now(),
        )
        self.orders[order.broker_order_id] = order
        return self._broker_order(order)

    async def get_order(self, broker_order_id: str) -> BrokerOrder:
        return self._broker_order(self.orders[broker_order_id])

    async def recent_orders(self, limit: int = 50) -> list[BrokerOrder]:
        orders = sorted(self.orders.values(), key=lambda o: o.submitted_at, reverse=True)
        return [self._broker_order(o) for o in orders[:limit]]

    @staticmethod
    def _broker_order(o: SimOrder) -> BrokerOrder:
        return BrokerOrder(
            broker_order_id=o.broker_order_id,
            status=o.status,
            filled_qty=o.filled_qty,
            filled_avg_price=o.filled_avg_price,
            client_order_id=o.client_order_id,
            symbol=o.symbol,
            side=o.side,
        )
