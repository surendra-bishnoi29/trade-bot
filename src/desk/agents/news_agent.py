"""News agent: polls Alpaca news for the universe, scores each article per symbol with Haiku."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core.clock import Clock
from desk.core.events import Event, Topics, parse_payload
from desk.core.payloads import NewsArticleRaw, NewsArticleScored, ResearchRequest
from desk.data.alpaca import AlpacaMarketData
from desk.storage.db import Database

log = logging.getLogger(__name__)

PROMPT_VERSION = "news-v1"
SYSTEM_NEWS = """You score financial news for a trading desk. For the given symbol, read the headline and summary and return a structured score.

Definitions:
- relevance: 0 if the symbol is only mentioned in passing or in a list, 1 if the article is about this company's business, results, products, management, legal or regulatory situation, or a deal involving it.
- polarity: -1 clearly negative for the stock, 0 neutral or unclear, +1 clearly positive. Use the full range.
- confidence: how sure you are of the polarity given only this text.
- event_type: one of earnings, guidance, analyst, deal, product, management, legal, regulatory, capital, macro, market_move, other.
- impact: low (routine coverage), medium (could move the stock a few percent), high (material: results, guidance change, deal, regulatory decision).
- horizon: intraday for a one-day reaction, days for a swing effect, weeks for a lasting change.
- one_line: at most twenty words on why it matters, or "not about this company".
Never speculate beyond the text. A rehash of old news is low impact."""


class NewsScore(BaseModel):
    relevance: float = Field(ge=0, le=1)
    polarity: float = Field(ge=-1, le=1)
    confidence: float = Field(ge=0, le=1)
    event_type: Literal[
        "earnings",
        "guidance",
        "analyst",
        "deal",
        "product",
        "management",
        "legal",
        "regulatory",
        "capital",
        "macro",
        "market_move",
        "other",
    ]
    impact: Literal["low", "medium", "high"]
    horizon: Literal["intraday", "days", "weeks"]
    one_line: str


class NewsScorer:
    def __init__(
        self, client: anthropic.AsyncAnthropic | None = None, model: str = "claude-haiku-5-5"
    ) -> None:
        self.client = client or anthropic.AsyncAnthropic()
        self.model = model
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0

    async def score(self, article: NewsArticleRaw, symbol: str) -> NewsScore:
        user = (
            f"SYMBOL {symbol}\nHEADLINE {article.title}\nSOURCE {article.source}\n"
            f"PUBLISHED {article.published_at.isoformat()}\nSUMMARY {article.summary or '(none)'}"
        )
        response = await self.client.messages.parse(
            model=self.model,
            max_tokens=400,
            system=[{"type": "text", "text": SYSTEM_NEWS, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=NewsScore,
            output_config={"effort": "low"},
        )
        self.calls += 1
        self.tokens_in += response.usage.input_tokens
        self.tokens_out += response.usage.output_tokens
        if response.stop_reason == "refusal" or response.parsed_output is None:
            return NewsScore(
                relevance=0,
                polarity=0,
                confidence=0,
                event_type="other",
                impact="low",
                horizon="days",
                one_line="not scored",
            )
        return response.parsed_output


class NewsAgent(BaseAgent):
    name = "news"
    topics = (Topics.RESEARCH_REQUEST,)

    def __init__(
        self,
        bus: Bus,
        clock: Clock,
        db: Database,
        market: AlpacaMarketData,
        scorer: NewsScorer,
        universe: list[str],
        *,
        poll_s: float = 60.0,
        max_symbols_per_article: int = 3,
        extra_sources: list | None = None,
    ) -> None:
        super().__init__(bus, clock)
        self.db = db
        self.market = market
        self.extra_sources = list(extra_sources or [])  # anything with .news(symbols, since, limit)
        self.scorer = scorer
        self.universe = universe
        self.poll_s = poll_s
        self.max_symbols_per_article = max_symbols_per_article
        self.seen: dict[str, Event] = {}  # article_id -> raw event
        self.scored: set[tuple[str, str]] = set()  # (article_id, symbol)

    async def remember_stored(self, hours: int = 48) -> int:
        """Load article ids already in the event store, so a restart does not re-emit them."""
        if self.db is None:
            return 0
        since = self.clock.now() - timedelta(hours=hours)
        for e in await self.db.recent_events([Topics.NEWS_ARTICLE_RAW], since, limit=5000):
            self.seen.setdefault(e.payload["article_id"], e)
        for e in await self.db.recent_events([Topics.NEWS_ARTICLE_SCORED], since, limit=5000):
            if e.symbol:
                self.scored.add((e.payload["article_id"], e.symbol))
        return len(self.seen)

    async def ingest(
        self,
        symbols: list[str],
        since: datetime,
        *,
        caused_by: Event | None = None,
        limit: int = 50,
    ) -> int:
        articles = list(await self.market.news(symbols, since, limit=limit))
        for source in self.extra_sources:
            try:
                articles.extend(await source.news(symbols, since, limit=limit))
            except Exception as exc:  # noqa: BLE001 - a secondary source must never block the primary
                log.warning("news source %s failed: %s", type(source).__name__, exc)
        emitted = 0
        for article in articles:
            raw = self.seen.get(article.article_id)
            if raw is None:
                raw = await self.emit(
                    Topics.NEWS_ARTICLE_RAW,
                    article,
                    symbol=article.symbols[0] if article.symbols else None,
                    occurred_at=article.published_at,
                    caused_by=caused_by,
                )
                self.seen[article.article_id] = raw
                if len(self.seen) > 5000:
                    for key in list(self.seen)[:1000]:
                        del self.seen[key]
            targets = [s for s in article.symbols if s in self.universe][
                : self.max_symbols_per_article
            ]
            if (
                symbols
                and len(symbols) == 1
                and symbols[0] in article.symbols
                and symbols[0] not in targets
            ):
                targets.append(symbols[0])
            for symbol in targets:
                if (article.article_id, symbol) in self.scored:
                    continue
                score = await self.scorer.score(article, symbol)
                self.scored.add((article.article_id, symbol))
                await self.emit(
                    Topics.NEWS_ARTICLE_SCORED,
                    NewsArticleScored(
                        article_id=article.article_id,
                        relevance=score.relevance,
                        polarity=score.polarity,
                        confidence=score.confidence,
                        event_type=score.event_type,
                        impact=score.impact,
                        horizon=score.horizon,
                        model=self.scorer.model,
                        prompt_version=PROMPT_VERSION,
                    ),
                    symbol=symbol,
                    occurred_at=article.published_at,
                    caused_by=raw,
                )
                emitted += 1
        return emitted

    async def handle(self, event: Event) -> None:
        request = parse_payload(event)
        assert isinstance(request, ResearchRequest)
        n = await self.ingest(
            [request.symbol], self.clock.now() - timedelta(hours=24), caused_by=event, limit=20
        )
        log.info("news scan %s: %d new scores", request.symbol, n)

    async def run_forever(self) -> None:
        since = self.clock.now() - timedelta(hours=6)
        while True:
            try:
                n = await self.ingest(self.universe, since)
                if n:
                    log.info(
                        "news poll: %d new scores (%d Haiku calls so far)", n, self.scorer.calls
                    )
            except Exception as exc:  # noqa: BLE001 - a feed hiccup must not kill the loop
                log.warning("news poll failed: %s", exc)
            since = self.clock.now() - timedelta(minutes=5)
            await self.clock.sleep(self.poll_s)
