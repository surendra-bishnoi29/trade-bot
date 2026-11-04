"""RSS headlines per symbol, no key needed: a second news source behind the same scoring pipeline.
Feeds are polled gently (one request per symbol every fifteen minutes) and parsed with the standard
library; an article's id is a hash of its link, so a story seen twice is scored once."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import urllib.request
from collections.abc import Iterable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from xml.etree import ElementTree

from desk.core.payloads import NewsArticleRaw

log = logging.getLogger(__name__)

YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"


def parse_rss(xml_text: str, symbol: str) -> list[NewsArticleRaw]:
    root = ElementTree.fromstring(xml_text)
    out: list[NewsArticleRaw] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        raw_date = item.findtext("pubDate") or ""
        try:
            published = parsedate_to_datetime(raw_date)
            if published.tzinfo is None:
                published = published.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            continue
        host = urlparse(link).hostname or "rss"
        out.append(
            NewsArticleRaw(
                article_id="rss:" + hashlib.sha1(link.encode()).hexdigest()[:16],
                published_at=published.astimezone(UTC),
                source=host.removeprefix("www."),
                url=link,
                title=title,
                summary=(item.findtext("description") or "").strip()[:1000] or None,
                symbols=[symbol],
            )
        )
    return out


class RssNews:
    def __init__(
        self,
        template: str = YAHOO,
        *,
        user_agent: str = "Mozilla/5.0 (desk research agent)",
        min_interval_s: float = 900.0,
        timeout: float = 15.0,
    ) -> None:
        self.template = template
        self.user_agent = user_agent
        self.min_interval_s = min_interval_s
        self.timeout = timeout
        self._cache: dict[str, tuple[float, list[NewsArticleRaw]]] = {}

    def _fetch(self, symbol: str) -> str:
        req = urllib.request.Request(
            self.template.format(symbol=symbol.replace(".", "-")),
            headers={"User-Agent": self.user_agent},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")

    async def news(
        self, symbols: Iterable[str], start: datetime | None, limit: int = 50
    ) -> list[NewsArticleRaw]:
        loop = asyncio.get_running_loop()
        now = loop.time()
        out: list[NewsArticleRaw] = []
        for symbol in symbols:
            cached = self._cache.get(symbol)
            if cached is not None and now - cached[0] < self.min_interval_s:
                items = cached[1]
            else:
                try:
                    items = parse_rss(await asyncio.to_thread(self._fetch, symbol), symbol)
                except Exception as exc:  # noqa: BLE001 - one feed failing must not stop the others
                    log.debug("rss %s: %s", symbol, exc)
                    items = cached[1] if cached else []
                self._cache[symbol] = (now, items)
            for a in items:
                if start is None or a.published_at >= start:
                    out.append(a)
        out.sort(key=lambda a: a.published_at, reverse=True)
        return out[:limit]
