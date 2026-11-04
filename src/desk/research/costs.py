"""Transaction costs: what a trade pays to get in and out, assumed up front and calibrated to the
desk's own fills.

A side costs half the spread plus impact, in basis points of the price, plus commission per share.
The defaults suit the liquid large caps of the core universe; the conservative variant doubles
them for gates and backtests. The calibration reads every fill in the journal, compares it with the
arrival price (the open of the minute bar the fill happened in) and reports the median slippage per
side; with enough fills it replaces the default."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import mean, median
from typing import Any

from desk.core.events import Topics
from desk.storage.db import Database

MIN_FILLS_TO_CALIBRATE = 20


@dataclass(frozen=True)
class CostModel:
    half_spread_bps: float = 2.0
    impact_bps: float = 3.0
    commission_per_share: float = 0.0
    source: str = "default"

    @property
    def per_side_bps(self) -> float:
        return self.half_spread_bps + self.impact_bps

    @property
    def round_trip(self) -> float:
        """Both sides, as a fraction of notional, before commission."""
        return 2 * self.per_side_bps / 1e4

    def side_cost(self, quantity: Decimal | float, price: Decimal | float) -> float:
        """Dollars paid to cross once at this size and price."""
        q, p = float(quantity), float(price)
        return q * p * self.per_side_bps / 1e4 + q * self.commission_per_share

    @classmethod
    def default(cls) -> CostModel:
        return cls()

    @classmethod
    def conservative(cls) -> CostModel:
        """Twice the default: a model or a strategy must pay for a bad day too."""
        return cls(half_spread_bps=4.0, impact_bps=6.0, source="conservative")

    def describe(self) -> str:
        return (
            f"{self.per_side_bps:.1f} bps a side ({self.half_spread_bps:.1f} half spread + "
            f"{self.impact_bps:.1f} impact), {self.round_trip * 1e4:.0f} bps round trip, {self.source}"
        )


@dataclass
class FillObservation:
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    reference: Decimal
    filled_at: datetime

    @property
    def slippage_bps(self) -> float:
        """Positive when the fill was worse than the arrival price, for either side."""
        sign = 1 if self.side == "buy" else -1
        return sign * (float(self.price) / float(self.reference) - 1) * 1e4


async def observed_fills(
    db: Database, since: datetime, until: datetime | None = None
) -> list[FillObservation]:
    """Every fill in the window with an arrival price: the open of the minute bar it happened in,
    or the last bar within five minutes before it when that bar is missing."""
    fills = await db.recent_events([Topics.ORDER_FILLED], since, limit=2000, until=until)
    intents = await db.recent_events(
        [Topics.ORDER_INTENT], since - timedelta(days=7), limit=4000, until=until
    )
    side_of = {e.payload.get("intent_id"): e.payload.get("side") for e in intents}
    out: list[FillObservation] = []
    for e in fills:
        p = e.payload
        if not p.get("avg_price") or not p.get("filled_quantity") or not e.symbol:
            continue
        minute = e.occurred_at.replace(second=0, microsecond=0)
        bars = await db.bars_between([e.symbol], "1m", minute, minute + timedelta(minutes=1))
        if bars:
            reference = Decimal(str(bars[0]["open"]))
        else:
            prior = await db.recent_bars(e.symbol, "1m", limit=1, before=e.occurred_at)
            if not prior or e.occurred_at - prior[0]["ts"] > timedelta(minutes=5):
                continue
            reference = Decimal(str(prior[0]["close"]))
        side = p.get("side") or side_of.get(p.get("intent_id")) or "buy"
        out.append(
            FillObservation(
                symbol=e.symbol,
                side=side,
                quantity=Decimal(str(p["filled_quantity"])),
                price=Decimal(str(p["avg_price"])),
                reference=reference,
                filled_at=e.occurred_at,
            )
        )
    return out


def calibrate(fills: list[FillObservation]) -> tuple[CostModel, dict[str, Any]]:
    """The cost model the fills support, and what was measured."""
    report: dict[str, Any] = {"fills": len(fills)}
    if not fills:
        report["note"] = "no fills yet: default costs"
        return CostModel.default(), report
    bps = [f.slippage_bps for f in fills]
    buys = [f.slippage_bps for f in fills if f.side == "buy"]
    sells = [f.slippage_bps for f in fills if f.side == "sell"]
    report.update(
        median_bps=median(bps),
        mean_bps=mean(bps),
        worst_bps=max(bps),
        best_bps=min(bps),
        buy_median_bps=median(buys) if buys else None,
        sell_median_bps=median(sells) if sells else None,
        by_symbol={
            s: round(median([f.slippage_bps for f in fills if f.symbol == s]), 2)
            for s in sorted({f.symbol for f in fills})
        },
    )
    if len(fills) < MIN_FILLS_TO_CALIBRATE:
        report["note"] = f"fewer than {MIN_FILLS_TO_CALIBRATE} fills: default costs"
        return CostModel.default(), report
    per_side = max(0.5, float(median(bps)))  # price improvement happens, but not on average
    model = CostModel(
        half_spread_bps=round(per_side / 2, 2),
        impact_bps=round(per_side / 2, 2),
        source=f"calibrated on {len(fills)} fills",
    )
    return model, report


def render(model: CostModel, report: dict[str, Any]) -> str:
    lines = [f"COST MODEL: {model.describe()}"]
    lines.append(f"fills observed: {report['fills']}")
    if report.get("median_bps") is not None:
        lines.append(
            f"slippage vs arrival price: median {report['median_bps']:+.2f} bps, mean {report['mean_bps']:+.2f}, "
            f"best {report['best_bps']:+.2f}, worst {report['worst_bps']:+.2f}"
        )
        if report.get("buy_median_bps") is not None:
            lines.append(f"buys: median {report['buy_median_bps']:+.2f} bps")
        if report.get("sell_median_bps") is not None:
            lines.append(f"sells: median {report['sell_median_bps']:+.2f} bps")
        lines.append(
            "by symbol: "
            + ", ".join(f"{s} {v:+.2f}" for s, v in list(report["by_symbol"].items())[:20])
        )
    if report.get("note"):
        lines.append(report["note"])
    return "\n".join(lines)
