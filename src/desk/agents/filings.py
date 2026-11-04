"""The filing analyst: polls EDGAR for the universe's new filings, records each one as a filing.new event
and publishes a research report with the impact its items imply. Deterministic: the item codes of an
8-K say what it is about; reading the document is the head trader's call."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core.clock import Clock
from desk.core.events import Topics
from desk.core.sectors import REFERENCES
from desk.data.edgar import Edgar, Filing, impact_for
from desk.storage.db import Database

log = logging.getLogger(__name__)


class FilingAnalyst(BaseAgent):
    name = "filings"
    topics = ()

    def __init__(
        self,
        bus: Bus,
        clock: Clock,
        db: Database | None,
        edgar: Edgar,
        universe: list[str],
        *,
        poll_s: float = 1800.0,
        lookback: timedelta = timedelta(days=3),
    ) -> None:
        super().__init__(bus, clock)
        self.db = db
        self.edgar = edgar
        self.symbols = [s for s in universe if s not in REFERENCES]
        self.poll_s = poll_s
        self.lookback = lookback
        self.ciks: dict[str, str] = {}
        self.seen: set[str] = set()

    async def warm(self) -> int:
        if self.db is None:
            return 0
        since = self.clock.now() - timedelta(days=10)
        events = await self.db.recent_events([Topics.FILING_NEW], since, limit=2000)
        for e in events:
            self.seen.add(str(e.payload.get("accession")))
        return len(events)

    def report_for(self, f: Filing) -> dict[str, Any]:
        impact, about = impact_for(f.form, f.items)
        return {
            "analyst": "filings",
            "kind": "filing",
            "symbol": f.symbol,
            "stance": "neutral",
            "conviction": {"low": 0.2, "medium": 0.5, "high": 0.8}[impact],
            "horizon": "days",
            "summary": (
                f"{f.form} filed {f.filed_at:%Y-%m-%d %H:%M} UTC: {about} ({impact} impact)"
                + (f". {f.description}" if f.description else "")
            ),
            "data": {
                "form": f.form,
                "items": f.items,
                "impact": impact,
                "about": about,
                "accession": f.accession,
                "url": f.url,
            },
        }

    async def poll_once(self) -> int:
        if not self.ciks:
            self.ciks = await self.edgar.tickers()
        now = self.clock.now()
        new = 0
        for symbol in self.symbols:
            cik = self.ciks.get(symbol)
            if cik is None:
                continue
            try:
                filings = await self.edgar.submissions(symbol, cik)
            except Exception as exc:  # noqa: BLE001 - one company failing must not stop the poll
                log.warning("edgar %s: %s", symbol, exc)
                continue
            for f in filings:
                if f.accession in self.seen or now - f.filed_at > self.lookback:
                    continue
                self.seen.add(f.accession)
                new += 1
                created = await self.emit(
                    Topics.FILING_NEW,
                    {
                        "symbol": f.symbol,
                        "cik": f.cik,
                        "accession": f.accession,
                        "form": f.form,
                        "filed_at": f.filed_at,
                        "items": f.items,
                        "document": f.document,
                        "description": f.description,
                        "url": f.url,
                    },
                    symbol=f.symbol,
                    book="swing",
                )
                await self.emit(
                    Topics.RESEARCH_REPORT,
                    self.report_for(f),
                    symbol=f.symbol,
                    book="swing",
                    caused_by=created,
                )
            await asyncio.sleep(0.15)  # the SEC's fair-access limit is 10 requests a second
        if new:
            log.info("edgar: %d new filings", new)
        return new

    async def run_forever(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception as exc:  # noqa: BLE001 - keep polling through any failure
                log.warning("edgar poll failed: %s", exc)
            waited = 0.0
            while waited < self.poll_s:
                await self.clock.sleep(60.0)
                waited += 60.0
