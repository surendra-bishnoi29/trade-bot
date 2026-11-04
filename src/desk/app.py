"""Assembly: build the desk from settings and run it as one asyncio process."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

import redis.asyncio as aioredis

from desk.agents.back_office import BackOffice
from desk.agents.cycle import CycleScheduler, SwingCycle
from desk.agents.data_agent import DataAgent
from desk.agents.execution import ExecutionAgent
from desk.agents.head_trader import HeadTrader
from desk.agents.monitor import Monitor
from desk.agents.news_agent import NewsAgent, NewsScorer
from desk.agents.prices import PriceCache
from desk.agents.risk_officer import RiskLimits, RiskOfficer
from desk.agents.swing_strategy import SwingStrategy
from desk.agents.watcher import WatcherAgent
from desk.bus.base import Bus
from desk.bus.memory import InMemoryBus
from desk.bus.redis_streams import RedisStreamsBus
from desk.console import ConsoleAgent
from desk.core.clock import LiveClock
from desk.core.config import Settings
from desk.core.events import Topics
from desk.core.payloads import KillSwitch
from desk.data.alpaca import AlpacaLiveBars, AlpacaMarketData, AlpacaTrading
from desk.storage.db import Database
from desk.storage.persister import Persister

log = logging.getLogger(__name__)


async def _redis_reachable(url: str) -> bool:
    try:
        r = aioredis.from_url(url)
        try:
            return bool(await asyncio.wait_for(r.ping(), timeout=1.0))
        finally:
            await r.aclose()
    except Exception:  # noqa: BLE001 - unreachable means "use the in-memory bus"
        return False


@dataclass
class Desk:
    settings: Settings
    clock: LiveClock
    bus: Bus
    db: Database
    back_office: BackOffice
    prices: PriceCache
    risk: RiskOfficer
    execution: ExecutionAgent
    news: NewsAgent
    head: HeadTrader
    console: ConsoleAgent
    strategy: SwingStrategy
    cycle: SwingCycle
    scheduler: CycleScheduler | None = None
    data: DataAgent | None = None
    watcher: WatcherAgent | None = None
    tasks: list[asyncio.Task[None]] = field(default_factory=list)

    @classmethod
    async def build(cls, settings: Settings, *, live: bool) -> Desk:
        key = settings.alpaca_api_key.get_secret_value() if settings.alpaca_api_key else ""
        secret = settings.alpaca_secret_key.get_secret_value() if settings.alpaca_secret_key else ""
        if not key or not secret:
            raise SystemExit("ALPACA_API_KEY and ALPACA_SECRET_KEY are required in .env")
        clock = LiveClock()
        db = Database(settings.database_url)
        await db.open()
        await db.migrate()
        if await _redis_reachable(settings.redis_url):
            bus: Bus = RedisStreamsBus(settings.redis_url, clock)
            log.info("bus: redis streams at %s", settings.redis_url)
        else:
            bus = InMemoryBus(clock)
            log.warning(
                "bus: redis not reachable at %s, using the in-memory bus for this process",
                settings.redis_url,
            )

        market = AlpacaMarketData(key, secret)
        trading = AlpacaTrading(key, secret, paper=settings.alpaca_paper)
        universe = list(settings.core_universe)

        Persister(bus, db).start()
        back_office = BackOffice(bus, clock, trading, settings.paper_capital, db=db)
        back_office.start()
        prices = PriceCache(bus, clock)
        prices.start()
        await prices.seed(db, universe)

        def price_of(symbol: str):
            return back_office.price_of(symbol) or prices.price_of(symbol)

        def price_age_of(symbol: str):
            """Freshness is judged on the newest source we have for the symbol: the broker snapshot
            for held names, the bar feed for everything."""
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
        if await risk.restore(db):
            log.warning("kill switch restored from the journal: %s", risk.state.kill_reason)
        execution = ExecutionAgent(bus, clock, trading)
        execution.start()
        news = NewsAgent(bus, clock, db, market, NewsScorer(), universe)
        await news.remember_stored()
        news.start()
        head = HeadTrader(bus, clock, db, settings, back_office, risk=risk)
        head.start()
        strategy = SwingStrategy(bus, clock, db)
        await strategy.load()
        strategy.start()
        Monitor(bus, clock, len(universe)).start()
        cycle = SwingCycle(bus, clock, db, settings, head, back_office, risk, strategy)
        console = ConsoleAgent(bus, clock, head)
        desk = cls(
            settings,
            clock,
            bus,
            db,
            back_office,
            prices,
            risk,
            execution,
            news,
            head,
            console,
            strategy,
            cycle,
        )
        console.commands = desk.console_commands()
        if live:
            desk.data = DataAgent(bus, clock, db, market, universe)
            desk.watcher = WatcherAgent(bus, clock, db, AlpacaLiveBars(key, secret), universe)
            desk.scheduler = CycleScheduler(bus, clock, cycle, strategy, settings)
            desk.scheduler.start()
            head.scheduler = desk.scheduler
            desk.scheduler.on_result = lambda r: print(
                "\n[scheduled cycle]\n" + r.render() + "\n", flush=True
            )
        return desk

    def console_commands(self) -> dict:
        async def cycle_cmd(arg: str) -> str:
            result = await self.cycle.run("console", dry_run=arg.strip() in ("dry", "plan"))
            return result.render()

        async def plan_cmd(arg: str) -> str:
            return await cycle_cmd("dry")

        async def kill_cmd(arg: str) -> str:
            await self.console.emit(
                Topics.RISK_KILL_SWITCH,
                KillSwitch(engaged=True, reason=arg.strip() or "manual", source="console"),
            )
            return "kill switch engaged; only closing orders pass until /resume"

        async def resume_cmd(arg: str) -> str:
            await self.console.emit(
                Topics.RISK_KILL_SWITCH,
                KillSwitch(engaged=False, reason=arg.strip() or "manual resume", source="console"),
            )
            return "kill switch released"

        async def theses_cmd(arg: str) -> str:
            rows = await self.db.recent_theses(limit=20)
            if not rows:
                return "no theses yet"
            return "\n".join(
                f"{t.created_at:%m-%d} {t.thesis_id} {t.symbol} {t.status} qty={t.quantity} entry={t.entry_price} "
                f"stop={t.stop_price} target={t.target_price} pnl={t.realized_pnl} {t.reason}"
                for t in rows
            )

        async def status_cmd(arg: str) -> str:
            s = self.back_office.snapshot
            k = self.risk.state
            return (
                f"equity {s.equity} cash {s.cash} positions {len(s.positions)} | open theses {len(self.strategy.theses)} | "
                f"kill switch {'ENGAGED: ' + k.kill_reason if k.kill_switch else 'off'} | orders today {k.orders_today}"
                + (
                    f" | next cycle {self.scheduler.next_run(self.clock.now())[1]} at {self.scheduler.next_run(self.clock.now())[0].strftime('%H:%M UTC')}"
                    if self.scheduler
                    else ""
                )
            )

        return {
            "cycle": cycle_cmd,
            "plan": plan_cmd,
            "kill": kill_cmd,
            "resume": resume_cmd,
            "theses": theses_cmd,
            "status": status_cmd,
        }

    async def start(self) -> None:
        self.tasks.append(asyncio.create_task(self.bus.run(), name="bus"))
        await asyncio.sleep(0.5)
        await self.back_office.refresh()
        snap = self.back_office.snapshot
        notes = await self.strategy.reconcile(
            {s: (p.qty, p.avg_entry) for s, p in snap.positions.items()}
        )
        if notes:
            log.info("theses reconciled: %s", "; ".join(notes))
        self.tasks.append(asyncio.create_task(self.back_office.run_forever(), name="back_office"))
        if self.data is not None:
            self.tasks.append(asyncio.create_task(self.data.run_forever(), name="data"))
        if self.watcher is not None:
            self.tasks.append(asyncio.create_task(self.watcher.run_forever(), name="watcher"))
            self.tasks.append(asyncio.create_task(self.watcher.stale_loop(), name="watcher_stale"))
        if self.data is not None:
            self.tasks.append(asyncio.create_task(self.news.run_forever(), name="news"))
        if self.scheduler is not None:
            self.tasks.append(asyncio.create_task(self.scheduler.run_forever(), name="scheduler"))
        for task in self.tasks:
            task.add_done_callback(_report_crash)

    async def stop(self) -> None:
        if self.watcher is not None:
            with contextlib.suppress(Exception):
                await self.watcher.live.stop()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.bus.close()
        await self.db.close()


def _report_crash(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("task %s crashed: %s: %s", task.get_name(), type(exc).__name__, exc)
