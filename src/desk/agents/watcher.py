"""The real-time watcher: live minute bars in, bar events and alerts out."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Any

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core import calendar
from desk.core.clock import Clock
from desk.core.events import Topics
from desk.core.payloads import MarketBar, WatcherAlert
from desk.data.alpaca import AlpacaLiveBars
from desk.storage.db import Database

log = logging.getLogger(__name__)


@dataclass
class WatcherConfig:
    move_pct: Decimal = Decimal("1.0")  # abnormal move: percent change over the last five bars
    move_window: int = 5
    volume_mult: Decimal = Decimal(5)  # volume spike: multiple of the trailing median
    volume_lookback: int = 20
    stale_after: timedelta = timedelta(minutes=10)  # per symbol, informational on the thin IEX feed
    feed_stale_after: timedelta = timedelta(minutes=3)  # no bar from any symbol: the safety signal


@dataclass
class WatcherState:
    """Pure alert logic, so it can be tested without a websocket."""

    config: WatcherConfig = field(default_factory=WatcherConfig)
    bars: dict[str, deque[MarketBar]] = field(default_factory=dict)
    last_seen: dict[str, datetime] = field(default_factory=dict)
    stale_flagged: set[str] = field(default_factory=set)
    started_at: datetime | None = None
    last_any_bar: datetime | None = None
    feed_flagged: bool = False

    def on_bar(self, bar: MarketBar, now: datetime) -> list[WatcherAlert]:
        history = self.bars.setdefault(bar.symbol, deque(maxlen=self.config.volume_lookback + 1))
        alerts: list[WatcherAlert] = []
        if len(history) >= self.config.move_window:
            base = history[-self.config.move_window].close
            if base > 0:
                change = (bar.close / base - 1) * 100
                if abs(change) >= self.config.move_pct:
                    alerts.append(
                        WatcherAlert(
                            symbol=bar.symbol,
                            kind="abnormal_move",
                            severity="critical"
                            if abs(change) >= self.config.move_pct * 3
                            else "warning",
                            details={
                                "pct_change": str(round(change, 3)),
                                "bars": self.config.move_window,
                                "close": str(bar.close),
                            },
                        )
                    )
        if len(history) >= 5:
            typical = Decimal(median(b.volume for b in history))
            if typical > 0 and bar.volume > typical * self.config.volume_mult:
                alerts.append(
                    WatcherAlert(
                        symbol=bar.symbol,
                        kind="volume_spike",
                        severity="warning",
                        details={
                            "volume": bar.volume,
                            "median": str(typical),
                            "multiple": str(round(Decimal(bar.volume) / typical, 1)),
                        },
                    )
                )
        history.append(bar)
        self.last_seen[bar.symbol] = now
        self.last_any_bar = now
        self.stale_flagged.discard(bar.symbol)
        return alerts

    def stale_checks(
        self, symbols: list[str], now: datetime, market_open: bool, floor: datetime | None = None
    ) -> list[WatcherAlert]:
        """Per-symbol staleness (informational: quiet names print no IEX bars for minutes) and feed-level
        staleness (no bar from any symbol: the signal the monitor acts on). Time before the watcher
        started, or before the session opened, never counts."""
        if self.started_at is None:
            self.started_at = now
        if not market_open:
            return []
        alerts: list[WatcherAlert] = []
        for symbol in symbols:
            last = self.last_seen.get(symbol)
            reference = last or self.started_at
            if floor is not None and floor > reference:
                reference = floor
            if now - reference > self.config.stale_after and symbol not in self.stale_flagged:
                self.stale_flagged.add(symbol)
                alerts.append(
                    WatcherAlert(
                        symbol=symbol,
                        kind="stale_data",
                        severity="info",
                        details={
                            "last_seen": last.isoformat() if last else None,
                            "stale_after_s": int(self.config.stale_after.total_seconds()),
                        },
                    )
                )
        feed_reference = self.last_any_bar or self.started_at
        if floor is not None and floor > feed_reference:
            feed_reference = floor
        if now - feed_reference > self.config.feed_stale_after:
            if not self.feed_flagged:
                self.feed_flagged = True
                alerts.append(
                    WatcherAlert(
                        symbol="FEED",
                        kind="feed_stale",
                        severity="critical",
                        details={
                            "last_bar": self.last_any_bar.isoformat()
                            if self.last_any_bar
                            else None,
                            "feed_stale_after_s": int(self.config.feed_stale_after.total_seconds()),
                        },
                    )
                )
        elif self.feed_flagged:
            self.feed_flagged = False
            alerts.append(
                WatcherAlert(symbol="FEED", kind="feed_recovered", severity="info", details={})
            )
        return alerts


class WatcherAgent(BaseAgent):
    name = "watcher"
    topics = ()

    def __init__(
        self,
        bus: Bus,
        clock: Clock,
        db: Database,
        live: AlpacaLiveBars | Any,  # anything with run(symbols, on_bar) and stop(): stream or poll
        symbols: list[str],
        config: WatcherConfig | None = None,
    ) -> None:
        super().__init__(bus, clock)
        self.db = db
        self.live = live
        self.symbols = symbols[:30]  # the free IEX stream allows thirty symbols
        self.state = WatcherState(config or WatcherConfig())

    async def on_bar(self, bar: MarketBar) -> None:
        now = self.clock.now()
        await self.db.insert_bars([bar], now)
        event = await self.emit(
            Topics.MARKET_BAR_1M, bar, symbol=bar.symbol, book="intraday", occurred_at=bar.closes_at
        )
        for alert in self.state.on_bar(bar, now):
            await self.emit(
                Topics.WATCHER_ALERT, alert, symbol=bar.symbol, book="intraday", caused_by=event
            )

    async def stale_loop(self, interval_s: float = 60.0) -> None:
        while True:
            await self.clock.sleep(interval_s)
            now = self.clock.now()
            market_open = calendar.is_open(now)
            floor = calendar.session_bounds(calendar.session_of(now))[0] if market_open else None
            for alert in self.state.stale_checks(self.symbols, now, market_open, floor=floor):
                await self.emit(Topics.WATCHER_ALERT, alert, symbol=alert.symbol, book="intraday")

    async def run_forever(self) -> None:
        log.info("watcher on %d symbols (%s)", len(self.symbols), type(self.live).__name__)
        await self.live.run(self.symbols, self.on_bar)
