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
        allow_short: bool = False,
        fee_pct: Decimal = Decimal(0),
        crypto_fee_pct: Decimal | None = None,
    ) -> None:
        self.allow_short = allow_short
        self.fee_pct = Decimal(fee_pct)  # a percentage of notional each side, every instrument
        # crypto pairs pay this instead when set (a mixed book: free stocks, 0.25% crypto)
        self.crypto_fee_pct = None if crypto_fee_pct is None else Decimal(crypto_fee_pct)
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
        self.stops: dict[str, tuple[str, Decimal]] = {}  # entry order id -> (symbol, stop level)

    # ---- what the replayer calls ----

    def on_bar(self, bar: MarketBar) -> list[Fill]:
        """Mark the symbol and fill its pending orders at this bar's open plus slippage."""
        fills: list[Fill] = []
        if calendar.is_open(bar.ts) or "/" in bar.symbol:  # crypto trades around the clock
            for order in self.orders.values():
                if (
                    order.status == "accepted"
                    and order.symbol == bar.symbol
                    and bar.ts > order.submitted_at
                ):
                    fills.append(self._fill(order, bar))
            fills += self._resting_stops(bar)
        self.last[bar.symbol] = bar.close
        return fills

    def _resting_stops(self, bar: MarketBar) -> list[Fill]:
        """A stop that rests at the broker fills at its level when the bar trades through it."""
        fills: list[Fill] = []
        for entry_id, (symbol, level) in list(self.stops.items()):
            entry = self.orders.get(entry_id)
            pos = self.holdings.get(symbol)
            if symbol != bar.symbol or entry is None or entry.status != "filled" or pos is None:
                continue
            long = pos.qty > 0
            hit = bar.low <= level if long else bar.high >= level
            if not hit:
                continue
            order = SimOrder(
                broker_order_id=str(ULID()),
                client_order_id=f"stop:{entry_id}",
                symbol=symbol,
                side="sell" if long else "buy",
                qty=abs(pos.qty),
                submitted_at=bar.ts,
            )
            self.orders[order.broker_order_id] = order
            stop_bar = bar.model_copy(update={"open": level})
            fills.append(self._fill(order, stop_bar))
            self.stops.pop(entry_id, None)
        return fills

    def _fill(self, order: SimOrder, bar: MarketBar) -> Fill:
        reference = bar.open
        slip = (reference * self.slippage_bps / Decimal(10000)).quantize(CENT)
        price = (reference + slip if order.side == "buy" else reference - slip).quantize(CENT)
        fee_pct = (
            self.crypto_fee_pct
            if self.crypto_fee_pct is not None and "/" in order.symbol
            else self.fee_pct
        )
        commission = (self.commission_per_share * order.qty).quantize(CENT) + (
            price * order.qty * fee_pct / 100
        ).quantize(CENT)
        pos = self.holdings.setdefault(order.symbol, Holding())
        realized = Decimal(0)
        signed = order.qty if order.side == "buy" else -order.qty
        if pos.qty == 0 or (pos.qty > 0) == (signed > 0):  # opening or adding, long or short
            total = pos.avg_entry * abs(pos.qty) + price * order.qty
            pos.qty += signed
            pos.avg_entry = (total / abs(pos.qty)).quantize(CENT)
        else:  # closing some or all of a position, either side
            closing = min(order.qty, abs(pos.qty))
            direction = 1 if pos.qty > 0 else -1
            realized = ((price - pos.avg_entry) * closing * direction).quantize(CENT)
            pos.qty += signed
            if pos.qty == 0:
                self.holdings.pop(order.symbol, None)
        if order.side == "buy":
            self.cash -= price * order.qty + commission
        else:
            self.cash += price * order.qty - commission
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
        self,
        symbol: str,
        qty: Decimal,
        side: str,
        client_order_id: str,
        *,
        stop_price: Decimal | None = None,
    ) -> BrokerOrder:
        if qty <= 0:
            raise ValueError("quantity must be positive")
        held = self.holdings.get(symbol, Holding()).qty
        if side == "sell" and qty > held and not self.allow_short:
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
        if stop_price is not None:
            self.stops[order.broker_order_id] = (symbol, Decimal(stop_price))
        return self._broker_order(order)

    async def stop_leg(self, parent_order_id: str) -> BrokerOrder | None:
        return None  # the simulated broker keeps the stop level, not a separate order

    async def replace_stop(self, order_id: str, stop_price: Decimal) -> BrokerOrder:
        symbol, _ = self.stops[order_id]
        self.stops[order_id] = (symbol, Decimal(stop_price))
        return self._broker_order(self.orders[order_id])

    async def cancel_order(self, order_id: str) -> None:
        self.stops.pop(order_id, None)
        o = self.orders.get(order_id)
        if o is not None and o.status == "accepted":
            o.status = "canceled"

    async def get_order(self, broker_order_id: str) -> BrokerOrder:
        return self._broker_order(self.orders[broker_order_id])

    async def open_orders(self, limit: int = 100) -> list[BrokerOrder]:
        return [self._broker_order(o) for o in self.orders.values() if o.status == "accepted"][
            :limit
        ]

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
