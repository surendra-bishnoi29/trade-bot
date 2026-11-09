"""Assembly: build the desk from settings and run it as one asyncio process."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import redis.asyncio as aioredis
from alpaca.data.enums import DataFeed

from desk.agents.back_office import BackOffice
from desk.agents.base import BaseAgent
from desk.agents.brief import DailyBriefAgent
from desk.agents.commands import CommandAgent
from desk.agents.cycle import CycleScheduler, SwingCycle
from desk.agents.data_agent import DataAgent
from desk.agents.execution import ExecutionAgent
from desk.agents.filings import FilingAnalyst
from desk.agents.forecaster import ForecastAnalyst
from desk.agents.fundamentals import FundamentalAnalyst
from desk.agents.head_trader import HeadTrader
from desk.agents.intraday import IntradayBook
from desk.agents.macro import MacroStrategist
from desk.agents.market_feeds import MarketFeeds
from desk.agents.monitor import Monitor
from desk.agents.news_agent import NewsAgent, NewsScorer
from desk.agents.prices import PriceCache
from desk.agents.quant_screen import QuantScreen
from desk.agents.research import NewsAnalyst, RegimeStrategist, SectorSpecialist
from desk.agents.reviewer import ReviewerAgent
from desk.agents.risk_officer import RiskLimits, RiskOfficer
from desk.agents.swing_strategy import SwingStrategy
from desk.agents.telegram import TelegramNotifier
from desk.agents.watcher import WatcherAgent
from desk.bus.base import Bus
from desk.bus.memory import InMemoryBus
from desk.bus.redis_streams import RedisStreamsBus
from desk.console import ConsoleAgent
from desk.core import calendar, sdnotify
from desk.core.clock import LiveClock
from desk.core.config import Settings
from desk.core.events import Topics
from desk.core.payloads import Heartbeat, KillSwitch, SystemRestarted
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
    commands: CommandAgent | None = None
    brief: DailyBriefAgent | None = None
    screen: QuantScreen | None = None
    forecaster: ForecastAnalyst | None = None
    reviewer: ReviewerAgent | None = None
    intraday: IntradayBook | None = None
    feeds: MarketFeeds | None = None
    lines: Any = None
    sentinel: Any = None
    predictor: Any = None
    news_analyst: NewsAnalyst | None = None
    filings: FilingAnalyst | None = None
    macro: MacroStrategist | None = None
    fundamentals: FundamentalAnalyst | None = None
    telegram: TelegramNotifier | None = None
    ops: Ops | None = None
    data: DataAgent | None = None
    watcher: WatcherAgent | None = None
    sim: Any = None  # the simulated broker's filler when DESK_BROKER=sim
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
        elif settings.require_redis:
            raise SystemExit(
                f"redis is required in production and not reachable at {settings.redis_url}"
            )
        else:
            bus = InMemoryBus(clock)
            log.warning(
                "bus: redis not reachable at %s, using the in-memory bus for this process",
                settings.redis_url,
            )

        market = AlpacaMarketData(key, secret)
        trading: Any
        sim: Any = None
        if settings.broker == "sim":
            from desk.replay.broker import SimulatedBroker

            sim = trading = SimulatedBroker(
                clock,
                settings.paper_capital,
                allow_short=True,
                crypto_fee_pct=settings.sim_crypto_fee_pct,
            )
            log.warning(
                "BROKER: simulated in this process, %s starting cash; fills at the next live bar",
                settings.paper_capital,
            )
        else:
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

        from desk.funnel.markets import markets as funnel_markets

        lab = settings.lab_enabled
        all_markets = funnel_markets(universe, set(settings.lab_markets) if lab else None)
        risk_universe = list(universe)
        if lab:
            for m in all_markets.values():
                if m.traded:
                    risk_universe += [s for s in m.all_symbols if s not in risk_universe]
        risk = RiskOfficer(
            bus,
            clock,
            RiskLimits.from_settings(settings, risk_universe),
            back_office.view,
            price_of,
            price_age_of,
        )
        risk.start()
        if await risk.restore(db):
            log.warning("kill switch restored from the journal: %s", risk.state.kill_reason)
        execution = ExecutionAgent(bus, clock, trading)
        execution.start()
        extra_sources: list = []
        if settings.rss_news:
            from desk.data.rss import RssNews

            extra_sources.append(RssNews())
        news = NewsAgent(
            bus, clock, db, market, NewsScorer(), universe, extra_sources=extra_sources
        )
        await news.remember_stored()
        news.start()
        head = HeadTrader(bus, clock, db, settings, back_office, risk=risk)
        head.start()
        strategy = SwingStrategy(bus, clock, db)
        await strategy.load()
        if not lab:
            strategy.start()  # the lab desk's lines engine owns every exit instead
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
        desk.brief = DailyBriefAgent(bus, clock, db, head, back_office)
        desk.brief.start()
        console.commands = desk.console_commands()
        desk.commands = CommandAgent(bus, clock, console.commands, head)
        desk.commands.start()
        if sim is not None:
            from desk.lab.sim import SimFiller

            desk.sim = SimFiller(bus, clock, sim)
            desk.sim.start()
        if settings.telegram_bot_token and settings.telegram_chat_id:
            desk.telegram = TelegramNotifier(
                bus,
                clock,
                settings.telegram_bot_token.get_secret_value(),
                settings.telegram_chat_id,
            )
            desk.telegram.start()
            log.info("telegram notifier on")
        if live:
            RegimeStrategist(bus, clock).start()
            SectorSpecialist(bus, clock).start()
            desk.news_analyst = NewsAnalyst(bus, clock, db)
            warmed = await desk.news_analyst.warm()
            desk.news_analyst.start()
            log.info("news analyst warmed with %d scored articles", warmed)
            if settings.sec_user_agent:
                from desk.data.edgar import Edgar

                desk.filings = FilingAnalyst(
                    bus, clock, db, Edgar(settings.sec_user_agent), universe
                )
                await desk.filings.warm()
            else:
                log.info("filing analyst off: set DESK_SEC_USER_AGENT to poll EDGAR")
            if settings.fred_api_key:
                from desk.data.fred import Fred

                desk.macro = MacroStrategist(
                    bus, clock, db, Fred(settings.fred_api_key.get_secret_value())
                )
            else:
                log.info("macro strategist off: set DESK_FRED_API_KEY (free) to read FRED")
            if settings.finnhub_api_key:
                from desk.data.finnhub import Finnhub

                desk.fundamentals = FundamentalAnalyst(
                    bus, clock, db, Finnhub(settings.finnhub_api_key.get_secret_value()), universe
                )
            else:
                log.info("fundamental and events analysts off: set DESK_FINNHUB_API_KEY (free)")
            desk.screen = QuantScreen(bus, clock, db, universe, back_office, head=head, cycle=cycle)
            desk.forecaster = ForecastAnalyst(
                bus, clock, db, universe, horizon=settings.forecast_horizon
            )
            desk.forecaster.start()
            desk.reviewer = ReviewerAgent(bus, clock, db, head, narrative=settings.review_narrative)
            desk.reviewer.start()
            if settings.intraday_enabled:
                desk.intraday = IntradayBook(
                    bus,
                    clock,
                    db,
                    universe,
                    back_office,
                    risk,
                    strategy=settings.intraday_strategy,
                    position_pct=settings.intraday_position_pct,
                    watchlist_size=settings.intraday_watchlist_size,
                    max_positions=settings.intraday_max_positions,
                )
                desk.intraday.start()
                log.warning(
                    "intraday book ON (%s): paper orders under the book's limits, flat before the close",
                    settings.intraday_strategy,
                )
            else:
                log.info(
                    "intraday book off (DESK_INTRADAY_ENABLED=false): the baselines do not beat costs yet"
                )
            desk.data = DataAgent(bus, clock, db, market, universe)
            if settings.watch_markets or lab:
                from desk.data.watch import WatchData

                others = [m for m in all_markets.values() if m.key != "stocks"]
                desk.feeds = MarketFeeds(
                    bus, clock, db, WatchData(key, secret), others, emit_bars=lab
                )
            if lab:
                from desk.lab.lines import LinesAgent
                from desk.lab.predictor import Predictor
                from desk.lab.sentinel import Sentinel

                lab_markets = [m for m in all_markets.values() if m.traded]
                desk.lines = LinesAgent(bus, clock, db, trading, back_office)
                desk.lines.start()
                desk.sentinel = Sentinel(
                    bus,
                    clock,
                    db,
                    lab_markets,
                    review_minutes=settings.sentinel_review_minutes,
                    has_position=lambda s: (
                        s in desk.lines.lines or s in desk.lines.pending_symbols()
                    ),
                )
                desk.predictor = Predictor(
                    bus,
                    clock,
                    db,
                    all_markets,
                    desk.sentinel,
                    back_office,
                    risk,
                    min_confidence=settings.predictor_min_confidence,
                    max_calls_per_hour=settings.predictor_max_calls_per_hour,
                    position_pct=settings.lab_position_pct,
                    min_first_profit_pct=settings.lab_min_first_profit_pct,
                    min_loss_pct=settings.lab_min_loss_pct,
                )
                desk.predictor.start()
                log.warning(
                    "LAB DESK ON: sentinel, predictor and lines on %s; the swing cycle is off",
                    ", ".join(m.key for m in lab_markets),
                )
            feed = DataFeed.SIP if settings.alpaca_feed == "sip" else DataFeed.IEX
            stream = settings.live_stream if settings.live_stream is not None else not lab
            if stream:
                live_bars: Any = AlpacaLiveBars(key, secret, feed=feed)
            else:
                from desk.data.watch import PolledBars, WatchData

                live_bars = PolledBars(
                    WatchData(key, secret),
                    clock,
                    # two minutes of grace so the session's last bar, which closes at the bell, arrives
                    is_open=lambda at: (
                        calendar.is_open(at) or calendar.is_open(at - timedelta(minutes=2))
                    ),
                )
                log.warning(
                    "live stock bars by REST poll every 20s: the one stream this login allows "
                    "is left to the main desk (DESK_LIVE_STREAM=true to use the stream here)"
                )
            desk.watcher = WatcherAgent(bus, clock, db, live_bars, universe)
            if not lab:
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

        async def brief_cmd(arg: str) -> str:
            assert self.brief is not None
            return DailyBriefAgent.render(await self.brief.write())

        return {
            "brief": brief_cmd,
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
        if self.intraday is not None:
            self.tasks.append(asyncio.create_task(self.intraday.run_forever(), name="intraday"))
        if self.feeds is not None:
            self.tasks.append(asyncio.create_task(self.feeds.run_forever(), name="market_feeds"))
        if self.lines is not None:
            await self.lines.restore()
        if self.sentinel is not None:
            self.tasks.append(asyncio.create_task(self.sentinel.run_forever(), name="sentinel"))
            self.tasks.append(asyncio.create_task(self.watcher.stale_loop(), name="watcher_stale"))
        if self.data is not None:
            self.tasks.append(asyncio.create_task(self.news.run_forever(), name="news"))
        if self.scheduler is not None:
            self.tasks.append(asyncio.create_task(self.scheduler.run_forever(), name="scheduler"))
        if self.screen is not None:
            self.tasks.append(asyncio.create_task(self.screen.run_forever(), name="quant_screen"))
        if self.news_analyst is not None:
            self.tasks.append(
                asyncio.create_task(self.news_analyst.flush_forever(), name="news_analyst")
            )
        if self.filings is not None:
            self.tasks.append(asyncio.create_task(self.filings.run_forever(), name="filings"))
        if self.macro is not None:
            self.tasks.append(asyncio.create_task(self.macro.run_forever(), name="macro"))
        if self.fundamentals is not None:
            self.tasks.append(
                asyncio.create_task(self.fundamentals.run_forever(), name="fundamentals")
            )
        self.ops = Ops(self.bus, self.clock)
        await self.ops.announce_start(self)
        self.tasks.append(asyncio.create_task(self.ops.heartbeat(self), name="heartbeat"))
        interval = sdnotify.watchdog_interval()
        if interval is not None:
            self.tasks.append(asyncio.create_task(self.ops.watchdog(interval), name="watchdog"))
        for task in self.tasks:
            task.add_done_callback(_report_crash)
        sdnotify.ready()

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


class Ops(BaseAgent):
    """Operational events: the start announcement, heartbeats, and systemd watchdog pings."""

    name = "ops"
    topics = ()

    def __init__(self, bus: Bus, clock: LiveClock) -> None:
        super().__init__(bus, clock)
        self._morning_sent: date | None = None

    async def announce_start(self, desk: Desk) -> None:
        now = self.clock.now()
        last_bar = await desk.db.latest_bar_ts("1m")
        gap = (now - last_bar).total_seconds() if last_bar is not None else None
        actions = []
        if desk.data is not None:
            actions.append("backfill the bars missed while away")
        actions.append("reconcile orders and positions with the broker")
        actions.append("restore the risk state from the journal")
        if desk.scheduler is not None:
            actions.append("run the last scheduled cycle if the journal shows it never ran")
        await self.emit(
            Topics.SYSTEM_RESTARTED,
            SystemRestarted(
                started_at=now,
                last_bar_at=last_bar,
                gap_seconds=gap,
                during_session=calendar.is_open(now),
                actions=actions,
            ),
        )

    def snapshot(self, desk: Desk) -> Heartbeat:
        now = self.clock.now()
        last = max(desk.prices.last_at.values(), default=None)
        feed_age = (now - last).total_seconds() if last is not None else None
        next_cycle = ""
        if desk.scheduler is not None:
            at, trigger = desk.scheduler.next_run(now)
            next_cycle = f"{trigger} at {at:%H:%M} UTC"
        degraded = calendar.is_open(now) and (feed_age is None or feed_age > 600)
        morning = False
        if calendar.is_session(now.date()) and now.hour >= 12 and self._morning_sent != now.date():
            self._morning_sent = now.date()
            morning = True
        posture = desk.risk.status()
        return Heartbeat(
            status="degraded" if degraded else "ok",
            feed_age_seconds=feed_age,
            kill_switch=desk.risk.state.kill_switch,
            open_theses=len(desk.strategy.theses),
            next_cycle=next_cycle,
            morning=morning,
            drawdown=posture.get("drawdown"),
            day_pnl=posture.get("day_pnl"),
            size_scale=posture.get("size_scale"),
        )

    async def heartbeat(self, desk: Desk) -> None:
        while True:
            try:
                beat = self.snapshot(desk)
                await self.emit(Topics.SYSTEM_HEARTBEAT, beat)
                sdnotify.status(
                    f"{beat.status}; kill switch {'on' if beat.kill_switch else 'off'}; next {beat.next_cycle}"
                )
            except Exception as exc:  # noqa: BLE001 - a heartbeat must never stop
                log.warning("heartbeat failed: %s", exc)
            await self.clock.sleep(max(30.0, desk.settings.heartbeat_minutes * 60.0))

    async def watchdog(self, interval: float) -> None:
        while True:
            sdnotify.watchdog()
            await self.clock.sleep(interval)
