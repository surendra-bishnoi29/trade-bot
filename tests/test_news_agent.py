import asyncio
from datetime import timedelta
from types import SimpleNamespace

from desk.agents.news_agent import NewsAgent, NewsScore, NewsScorer
from desk.bus.memory import InMemoryBus
from desk.core.events import Topics
from desk.core.payloads import NewsArticleRaw, ResearchRequest
from tests.conftest import make_event


class FakeMessages:
    def __init__(self) -> None:
        self.calls = 0

    async def parse(self, **kwargs):
        self.calls += 1
        assert kwargs["output_format"] is NewsScore and kwargs["model"] == "claude-haiku-5-5"
        symbol = kwargs["messages"][0]["content"].split("\n")[0].split()[1]
        score = NewsScore(
            relevance=0.9 if symbol == "AAPL" else 0.2,
            polarity=0.5,
            confidence=0.8,
            event_type="product",
            impact="medium",
            horizon="days",
            one_line="new product",
        )
        return SimpleNamespace(
            parsed_output=score,
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=300, output_tokens=60),
        )


class FakeClient:
    def __init__(self) -> None:
        self.messages = FakeMessages()


class FakeMarket:
    def __init__(self, clock) -> None:
        self.clock = clock
        self.articles = [
            NewsArticleRaw(
                article_id="a1",
                published_at=clock.now() - timedelta(hours=1),
                source="benzinga",
                title="Apple launches thing",
                symbols=["AAPL", "MSFT", "TSLA"],
            ),
            NewsArticleRaw(
                article_id="a2",
                published_at=clock.now() - timedelta(hours=2),
                source="benzinga",
                title="Unrelated",
                symbols=["XYZ"],
            ),
        ]

    async def news(self, symbols, start, limit=50):
        return [a for a in self.articles if not symbols or set(a.symbols) & set(symbols)]


async def test_ingest_scores_each_universe_symbol_once(clock):
    bus = InMemoryBus(clock)
    client = FakeClient()
    agent = NewsAgent(
        bus,
        clock,
        db=None,
        market=FakeMarket(clock),
        scorer=NewsScorer(client=client),
        universe=["AAPL", "MSFT"],
    )  # type: ignore[arg-type]
    n = await agent.ingest(["AAPL", "MSFT"], clock.now() - timedelta(hours=6))
    assert n == 2 and client.messages.calls == 2
    raw = [e for e in bus.log if e.type == Topics.NEWS_ARTICLE_RAW]
    scored = [e for e in bus.log if e.type == Topics.NEWS_ARTICLE_SCORED]
    assert [e.payload["article_id"] for e in raw] == ["a1"] and sorted(
        e.symbol for e in scored
    ) == ["AAPL", "MSFT"]
    assert all(e.causation_id == raw[0].event_id for e in scored)
    assert (
        scored[0].occurred_at == raw[0].occurred_at
    )  # knowledge of the article is dated at publication
    assert (
        await agent.ingest(["AAPL", "MSFT"], clock.now() - timedelta(hours=6)) == 0
    )  # nothing new
    assert client.messages.calls == 2


async def test_scan_request_scores_only_the_requested_symbol(clock):
    bus = InMemoryBus(clock)
    client = FakeClient()
    agent = NewsAgent(
        bus,
        clock,
        db=None,
        market=FakeMarket(clock),
        scorer=NewsScorer(client=client),
        universe=["AAPL"],
    )  # type: ignore[arg-type]
    agent.start()
    task = asyncio.create_task(bus.run())
    request = make_event(
        clock,
        type=Topics.RESEARCH_REQUEST,
        symbol="AAPL",
        payload=ResearchRequest(request_id="r1", kind="news_scan", symbol="AAPL").model_dump(
            mode="json"
        ),
    )
    await bus.publish(request)
    await bus.wait_idle()
    scored = [e for e in bus.log if e.type == Topics.NEWS_ARTICLE_SCORED]
    assert [e.symbol for e in scored] == ["AAPL"] and scored[0].payload[
        "prompt_version"
    ] == "news-v1"
    await bus.stop()
    await task
