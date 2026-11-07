"""The simulated broker on live data: every minute bar the feeds emit is handed to the in-process
broker so its resting orders and stops fill at that bar, exactly as in a replay. With it the lab
desk runs end to end on one machine without a second paper account; the execution trader polls the
simulated orders as it would Alpaca's, so the journal, the funnel and the scores are the same."""

from __future__ import annotations

import logging

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core.clock import Clock
from desk.core.events import Event, Topics, parse_payload
from desk.core.payloads import MarketBar
from desk.replay.broker import SimulatedBroker

log = logging.getLogger(__name__)


class SimFiller(BaseAgent):
    name = "sim_broker"
    topics = (Topics.MARKET_BAR_1M,)

    def __init__(self, bus: Bus, clock: Clock, broker: SimulatedBroker) -> None:
        super().__init__(bus, clock)
        self.broker = broker

    async def handle(self, event: Event) -> None:
        bar = parse_payload(event)
        if not isinstance(bar, MarketBar):
            return
        for fill in self.broker.on_bar(bar):
            log.info(
                "sim broker: %s %s %s at %s (reference %s, cost %s)",
                fill.side,
                fill.qty,
                fill.symbol,
                fill.price,
                fill.reference,
                fill.cost,
            )
