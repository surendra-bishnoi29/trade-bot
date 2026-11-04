"""Assemble a replay desk: the production agents, an in-memory bus, a driven clock, the simulated
broker and an in-memory thesis store. The journal is read, never written; model outputs go through
the cache so a rerun is free."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from desk.agents.back_office import BackOffice
from desk.agents.cycle import SwingCycle
from desk.agents.execution import ExecutionAgent
from desk.agents.head_trader import HeadTrader
from desk.agents.monitor import Monitor
from desk.agents.prices import PriceCache
from desk.agents.risk_officer import RiskLimits, RiskOfficer
from desk.agents.swing_strategy import SwingStrategy
from desk.bus.memory import InMemoryBus
from desk.core import calendar
from desk.core.clock import SimClock
from desk.core.config import Settings
from desk.replay.broker import SimulatedBroker
from desk.replay.replayer import Replayer, ReplayReport
from desk.replay.scripted import ScriptedHeadTrader
from desk.storage.db import Database
from desk.storage.llm_cache import LLMCache
from desk.storage.memory_theses import MemoryTheses


@dataclass
class ReplayDesk:
    clock: SimClock
    bus: InMemoryBus
    broker: SimulatedBroker
    head: HeadTrader
    theses: MemoryTheses
    strategy: SwingStrategy
    cycle: SwingCycle
    replayer: Replayer
    back_office: BackOffice

    async def run_day(self, day: date) -> ReplayReport:
        bus_task = asyncio.create_task(self.bus.run(), name="replay-bus")
        try:
            report = ReplayReport(day=day)
            await self.back_office.refresh()
            report.equity_start = self.broker.equity()
            report = await self.replayer.run_day(day, report)
            report.theses = [
                t.model_dump(mode="json") for t in await self.theses.recent_theses(limit=50)
            ]
            if getattr(self.head, "cache", None) is not None:
                report.llm = {"hits": self.head.cache.hits, "misses": self.head.cache.misses}
            return report
        finally:
            bus_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await bus_task
            await self.bus.close()


async def build_replay(
    settings: Settings,
    db: Database,
    day: date,
    *,
    capital: Decimal = Decimal(100000),
    llm: bool = False,
    slippage_bps: Decimal = Decimal(5),
    commission_per_share: Decimal = Decimal(0),
    feed_gap: tuple[datetime, datetime] | None = None,
) -> ReplayDesk:
    open_at, _ = calendar.session_bounds(day)
    clock = SimClock(open_at - timedelta(hours=2), driven=True)
    bus = InMemoryBus(clock)
    universe = list(settings.core_universe)
    broker = SimulatedBroker(
        clock, capital, slippage_bps=slippage_bps, commission_per_share=commission_per_share
    )
    back_office = BackOffice(bus, clock, broker, capital, db=None)  # type: ignore[arg-type]
    back_office.start()
    prices = PriceCache(bus, clock)
    prices.start()
    await prices.seed(db, universe, before=clock.now())

    def price_of(symbol: str):
        return back_office.price_of(symbol) or prices.price_of(symbol)

    def price_age_of(symbol: str):
        now = clock.now()
        ages = [prices.age_of(symbol, now)]
        if back_office.price_of(symbol) is not None:
            ages.append(now - back_office.snapshot.as_of)
        known = [a for a in ages if a is not None]
        return min(known) if known else None

    risk = RiskOfficer(
        bus,
        clock,
        RiskLimits(universe=frozenset(universe)),
        back_office.view,
        price_of,
        price_age_of,
    )
    risk.start()
    ExecutionAgent(bus, clock, broker).start()  # type: ignore[arg-type]
    theses = MemoryTheses()
    if llm:
        head = HeadTrader(bus, clock, db, settings, back_office, risk=risk)
        head.cache = LLMCache(db)
    else:
        head = ScriptedHeadTrader(bus, clock, db, settings, back_office, risk=risk)
    head.start()
    strategy = SwingStrategy(bus, clock, db, theses=theses)
    strategy.start()
    Monitor(bus, clock, len(universe)).start()
    cycle = SwingCycle(bus, clock, db, settings, head, back_office, risk, strategy, theses=theses)
    replayer = Replayer(bus, clock, db, broker, universe, cycle, feed_gap=feed_gap)
    return ReplayDesk(clock, bus, broker, head, theses, strategy, cycle, replayer, back_office)


async def run_replay(settings: Settings, day: date, **kwargs) -> ReplayReport:
    db = Database(settings.database_url)
    await db.open()
    try:
        await db.migrate()
        desk = await build_replay(settings, db, day, **kwargs)
        return await desk.run_day(day)
    finally:
        await db.close()
