"""The head trader: an Opus 5.5 agent with read-only tools over the desk, plus two actions that go
through the team: a news scan and an order proposal that the risk officer decides on."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
from anthropic import beta_async_tool
from ulid import ULID

from desk.agents.back_office import BackOffice
from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core import calendar
from desk.core.clock import Clock
from desk.core.config import Settings
from desk.core.events import Event, Topics
from desk.core.payloads import Challenge, CyclePlan, DailyBrief, OrderIntent, ResearchRequest
from desk.storage.db import Database

log = logging.getLogger(__name__)
PROMPTS = Path(__file__).resolve().parents[3] / "prompts"
PROMPT_PATH = PROMPTS / "head_trader" / "v1.md"
PLAN_PROMPT_PATH = PROMPTS / "head_trader" / "plan_v1.md"
SKEPTIC_PROMPT_PATH = PROMPTS / "skeptic" / "v1.md"
BRIEF_PROMPT_PATH = PROMPTS / "head_trader" / "brief_v1.md"
FALLBACK_HEADERS = {"anthropic-beta": "server-side-fallback-2026-07-01"}
FALLBACK_BODY = {"fallbacks": "default"}


def _fmt(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


class HeadTrader(BaseAgent):
    name = "head_trader"
    topics = (Topics.NEWS_ARTICLE_SCORED, Topics.RISK_APPROVED, Topics.RISK_REJECTED)

    def __init__(
        self,
        bus: Bus,
        clock: Clock,
        db: Database,
        settings: Settings,
        back_office: BackOffice | None,
        *,
        client: anthropic.AsyncAnthropic | None = None,
        model: str = "claude-opus-5-5",
        risk=None,
    ) -> None:
        super().__init__(bus, clock)
        self.risk = risk
        self.scheduler = None  # set by the app when the schedule runs in this process
        self.db = db
        self.settings = settings
        self.back_office = back_office
        self._client = client
        self.cache = None  # an LLMCache during replays; live cycles are never cached
        self.material_rows: list = []  # the scorecard rows behind the material, set by the cycle
        self.model = model
        self.static_prompt = PROMPT_PATH.read_text()
        self.plan_prompt = PLAN_PROMPT_PATH.read_text()
        self.skeptic_prompt = SKEPTIC_PROMPT_PATH.read_text()
        self.brief_prompt = BRIEF_PROMPT_PATH.read_text()
        self._scores: dict[str, asyncio.Queue[Event]] = {}
        self._decisions: dict[str, asyncio.Future[Event]] = {}
        self.last_usage: dict[str, int] = {}

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic()
        return self._client

    # ---- bus side: events the tools wait for ----

    async def handle(self, event: Event) -> None:
        if event.type == Topics.NEWS_ARTICLE_SCORED and event.symbol in self._scores:
            await self._scores[event.symbol].put(event)
        elif event.type in (Topics.RISK_APPROVED, Topics.RISK_REJECTED):
            fut = self._decisions.get(str(event.payload.get("intent_id")))
            if fut is not None and not fut.done():
                fut.set_result(event)

    # ---- prompt layers ----

    def daily_layer(self) -> str:
        s = self.settings
        return (
            f"Desk configuration for {self.clock.now().date().isoformat()}: autonomy level {s.autonomy_level} "
            f"(0 research only, 1 autonomous paper, 2 live with approval, 3 live within limits); "
            f"broker account {'paper' if s.alpaca_paper else 'LIVE'}; "
            f"core universe {', '.join(s.core_universe)}."
        )

    def context_line(self) -> str:
        now = self.clock.now()
        state = "open" if calendar.is_open(now) else "closed"
        return f"[now {now.strftime('%Y-%m-%d %H:%M')} UTC, market {state}]"

    # ---- queries the tools wrap (plain methods, testable without an LLM) ----

    async def q_portfolio(self) -> str:
        if self.back_office is None:
            events = await self.db.recent_events(
                [Topics.PORTFOLIO_RECONCILED], self.clock.now() - timedelta(days=7), limit=1
            )
            return _fmt(events[0].payload) if events else "no portfolio snapshot recorded yet"
        snap = self.back_office.snapshot
        return _fmt(
            {
                "as_of": snap.as_of,
                "equity": snap.equity,
                "cash": snap.cash,
                "buying_power": snap.buying_power,
                "positions": [p.model_dump(mode="json") for p in snap.positions.values()],
            }
        )

    async def q_bars(self, symbol: str, interval: str = "1d", n: int = 10) -> str:
        rows = await self.db.recent_bars(symbol.upper(), interval, limit=min(max(n, 1), 120))
        if not rows:
            return f"no {interval} bars stored for {symbol.upper()}"

        def stamp(ts) -> str:
            ts = ts.astimezone(UTC)
            return (
                f"session {ts.date().isoformat()}"
                if interval == "1d"
                else ts.strftime("%Y-%m-%d %H:%M UTC")
            )

        return "\n".join(
            f"{stamp(r['ts'])} o={r['open']} h={r['high']} l={r['low']} c={r['close']} v={r['volume']}"
            for r in rows
        )

    async def q_news(self, symbol: str, hours: int = 24) -> str:
        since = self.clock.now() - timedelta(hours=min(max(hours, 1), 168))
        raw = await self.db.recent_events(
            [Topics.NEWS_ARTICLE_RAW], since, limit=200, until=self.clock.now()
        )
        scored = await self.db.recent_events(
            [Topics.NEWS_ARTICLE_SCORED],
            since,
            symbol=symbol.upper(),
            limit=200,
            until=self.clock.now(),
        )
        scores = {e.payload["article_id"]: e.payload for e in scored}
        lines = []
        shown: set[str] = set()
        for e in raw:
            p = e.payload
            if p["article_id"] in shown:
                continue
            shown.add(p["article_id"])
            if symbol.upper() not in p.get("symbols", []):
                continue
            s = scores.get(p["article_id"])
            tag = (
                f" | score polarity={s['polarity']} impact={s['impact']} type={s['event_type']} relevance={s['relevance']}"
                if s
                else " | not scored yet"
            )
            lines.append(
                f"{p['published_at']} [{p['article_id']}] {p['source']}: {p['title']}{tag}"
            )
        return (
            "\n".join(lines[:30])
            if lines
            else f"no news stored for {symbol.upper()} in the last {hours} hours"
        )

    async def q_alerts(self, hours: int = 6) -> str:
        since = self.clock.now() - timedelta(hours=min(max(hours, 1), 48))
        events = await self.db.recent_events(
            [Topics.WATCHER_ALERT], since, limit=50, until=self.clock.now()
        )
        if not events:
            return "no watcher alerts in the window"
        return "\n".join(
            f"{e.occurred_at.isoformat()} [{e.event_id}] {e.symbol} {e.payload['kind']} {e.payload['severity']} {_fmt(e.payload.get('details'))}"
            for e in events
        )

    async def q_journal(self, hours: int = 24) -> str:
        since = self.clock.now() - timedelta(hours=min(max(hours, 1), 168))
        types = [
            Topics.ORDER_INTENT,
            Topics.RISK_APPROVED,
            Topics.RISK_REJECTED,
            Topics.ORDER_SUBMITTED,
            Topics.ORDER_FILLED,
            Topics.ORDER_REJECTED,
            Topics.ORDER_CANCELED,
            Topics.CONSOLE_QUESTION,
            Topics.CONSOLE_ANSWER,
            Topics.RISK_KILL_SWITCH,
        ]
        events = await self.db.recent_events(types, since, limit=60, until=self.clock.now())
        if not events:
            return "nothing in the journal for the window"
        out = []
        for e in events:
            brief = (
                e.payload.get("text") or e.payload.get("reason") or e.payload.get("status") or ""
            )
            ids = " ".join(
                f"{k}={e.payload[k]}" for k in ("intent_id", "broker_order_id") if e.payload.get(k)
            )
            if e.payload.get("side"):
                ids += f" side={e.payload['side']}"
            if e.payload.get("filled_quantity") not in (None, "0", 0):
                ids += f" filled={e.payload['filled_quantity']}@{e.payload.get('avg_price')}"
            when = e.occurred_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            out.append(
                f"{when} event={e.event_id} {e.type} {e.symbol or ''} {ids} {str(brief)[:160]}".replace(
                    "  ", " "
                )
            )
        return "\n".join(out)

    async def q_cycles(self, hours: int = 24) -> str:
        since = self.clock.now() - timedelta(hours=min(max(hours, 1), 168))
        events = await self.db.recent_events(
            [Topics.CYCLE_PLAN], since, limit=10, until=self.clock.now()
        )
        if not events:
            return "no cycles in the window"
        out = []
        for e in events:
            p = e.payload
            plan, challenge = p["plan"], p.get("challenge") or {}
            when = e.occurred_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
            acts = ", ".join(
                f"{a['symbol']}:{a['action']}" + (f" ({a['note']})" if a.get("note") else "")
                for a in p.get("final_actions", [])
            )
            blocking = "; ".join(
                f"{o['symbol']}: {o['argument'][:160]}"
                for o in challenge.get("objections", [])
                if o.get("severity") == "blocking"
            )
            out.append(
                f"{when} cycle {p['cycle_id']} trigger={p['trigger']} dry_run={p['dry_run']}\n"
                f"  market view: {plan['market_view'][:400]}\n  actions: {acts or 'none'}\n"
                f"  skeptic: {challenge.get('overall', '')[:300]}"
                + (f"\n  blocking objections: {blocking}" if blocking else "")
            )
        return "\n".join(out)

    def q_risk_state(self) -> str:
        if self.risk is None:
            return "risk officer state not available in this process"
        s, lim = self.risk.state, self.risk.limits
        switch = f"ENGAGED: {s.kill_reason}" if s.kill_switch else "off"
        schedule = "no scheduler in this process (cycles run only on request)"
        if self.scheduler is not None:
            at, trigger = self.scheduler.next_run(self.clock.now())
            every = self.settings.cycle_every_minutes
            schedule = (
                f"next cycle {trigger} at {at.astimezone(UTC).strftime('%Y-%m-%d %H:%M UTC')}"
                + (f", plus every {every} minutes while the market is open" if every else "")
            )
        return (
            f"{schedule}; kill switch {switch}; orders today {s.orders_today} of {lim.max_orders_per_day}; "
            f"max order {float(lim.max_order_notional_pct) * 100:.0f}% of equity; max position {float(lim.max_position_pct) * 100:.0f}% of equity; "
            f"shorts {'allowed' if lim.allow_short else 'not allowed'}; orders while closed {'allowed' if lim.allow_when_closed else 'blocked'}"
        )

    async def a_scan_news(self, symbol: str) -> str:
        symbol = symbol.upper()
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._scores[symbol] = queue
        try:
            request = ResearchRequest(request_id=str(ULID()), kind="news_scan", symbol=symbol)
            await self.emit(Topics.RESEARCH_REQUEST, request, symbol=symbol)
            got: list[Event] = []
            try:
                while True:
                    got.append(await asyncio.wait_for(queue.get(), timeout=8.0 if got else 30.0))
            except TimeoutError:
                pass
        finally:
            self._scores.pop(symbol, None)
        if not got:
            return f"the news agent returned no new scores for {symbol}; use get_news for what is already stored"
        return "\n".join(
            f"[{e.payload['article_id']}] polarity={e.payload['polarity']} impact={e.payload['impact']} type={e.payload['event_type']} relevance={e.payload['relevance']} confidence={e.payload['confidence']}"
            for e in got
        )

    async def a_propose_order(self, symbol: str, side: str, quantity: int, rationale: str) -> str:
        if self.settings.autonomy_level < 1:
            return "autonomy level 0: the desk only researches; no orders"
        if side not in ("buy", "sell"):
            return "side must be buy or sell"
        intent = OrderIntent(
            intent_id=str(ULID()),
            symbol=symbol.upper(),
            book="swing",
            side=side,
            quantity=Decimal(int(quantity)),
            rationale_event_ids=[],
        )  # type: ignore[arg-type]
        fut: asyncio.Future[Event] = asyncio.get_running_loop().create_future()
        self._decisions[intent.intent_id] = fut
        try:
            await self.emit(Topics.ORDER_INTENT, intent, symbol=intent.symbol, book="swing")
            try:
                decision = await asyncio.wait_for(fut, timeout=15.0)
            except TimeoutError:
                return f"order intent {intent.intent_id} published; the risk officer has not answered within 15 seconds"
        finally:
            self._decisions.pop(intent.intent_id, None)
        p = decision.payload
        failed = [r for r in p["rules"] if not r["passed"]]
        verdict = f"risk officer {p['decision']} intent {intent.intent_id}: {p['reason']}"
        if failed:
            verdict += " | failed rules: " + "; ".join(
                f"{r['rule']} ({r['detail']})" for r in failed
            )
        else:
            account = "paper" if self.settings.alpaca_paper else "LIVE"
            verdict += f" | approved quantity {p['approved_quantity']}; the execution trader is sending it to the {account} account"
        return verdict

    # ---- tools: thin typed wrappers the model sees ----

    def tools(self) -> list[Any]:
        head = self

        @beta_async_tool
        async def get_portfolio() -> str:
            """Current equity, cash, buying power and open positions from the back office."""
            return await head.q_portfolio()

        @beta_async_tool
        async def get_bars(symbol: str, interval: str = "1d", n: int = 10) -> str:
            """Recent completed bars for a symbol from the desk's database.

            Args:
                symbol: Ticker, for example AAPL.
                interval: "1d" for daily bars or "1m" for minute bars.
                n: How many of the most recent bars to return, at most 120.
            """
            return await head.q_bars(symbol, interval, n)

        @beta_async_tool
        async def get_news(symbol: str, hours: int = 24) -> str:
            """Recent news for a symbol with the news agent's scores, from the desk's database.

            Args:
                symbol: Ticker.
                hours: Look-back window in hours, at most 168.
            """
            return await head.q_news(symbol, hours)

        @beta_async_tool
        async def get_alerts(hours: int = 6) -> str:
            """Watcher alerts (abnormal moves, volume spikes, halts, stale data) in the last hours.

            Args:
                hours: Look-back window in hours, at most 48.
            """
            return await head.q_alerts(hours)

        @beta_async_tool
        async def get_journal(hours: int = 24) -> str:
            """Orders, risk decisions, fills and console exchanges in the last hours.

            Args:
                hours: Look-back window in hours, at most 168.
            """
            return await head.q_journal(hours)

        @beta_async_tool
        async def get_cycles(hours: int = 24) -> str:
            """What the autonomous swing cycles decided recently: market view, actions with reasons, the skeptic's verdict.

            Args:
                hours: Look-back window in hours, at most 168.
            """
            return await head.q_cycles(hours)

        @beta_async_tool
        async def get_risk_state() -> str:
            """The risk officer's live state: whether the kill switch is engaged, orders used today, and the limits."""
            return head.q_risk_state()

        @beta_async_tool
        async def scan_news(symbol: str) -> str:
            """Ask the news agent to fetch and score the last 24 hours of news for a symbol right now. Takes up to 30 seconds.

            Args:
                symbol: Ticker.
            """
            return await head.a_scan_news(symbol)

        @beta_async_tool
        async def propose_order(symbol: str, side: str, quantity: int, rationale: str) -> str:
            """Propose a market order. The risk officer approves or rejects it; execution happens only on approval. Use only when the person asked for an order.

            Args:
                symbol: Ticker.
                side: "buy" or "sell".
                quantity: Whole number of shares.
                rationale: One sentence on why, citing evidence ids.
            """
            return await head.a_propose_order(symbol, side, quantity, rationale)

        return [
            get_portfolio,
            get_bars,
            get_news,
            get_alerts,
            get_journal,
            get_cycles,
            get_risk_state,
            scan_news,
            propose_order,
        ]

    # ---- the conversation ----

    async def ask(
        self,
        question: str,
        *,
        max_iterations: int = 8,
        on_text: Callable[[str], Awaitable[None]] | None = None,
        on_progress: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> str:
        """Answer a question with the tools. Text streams to on_text as it is written; on_progress hears
        which tools run between turns. Returns the final answer."""
        runner = self.client.beta.messages.tool_runner(
            model=self.model,
            max_tokens=4000,
            system=[
                {
                    "type": "text",
                    "text": self.static_prompt,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": self.daily_layer(),
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            tools=self.tools(),
            messages=[{"role": "user", "content": f"{self.context_line()} {question}"}],
            max_iterations=max_iterations,
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default"},
            stream=True,
        )
        final: Any = None
        async for stream in runner:
            async for event in stream:
                if event.type == "text" and on_text is not None and event.text:
                    await on_text(event.text)
            final = await stream.get_final_message()
            tools_used = [b.name for b in final.content if b.type == "tool_use"]
            if tools_used and on_progress is not None:
                await on_progress("tool", ", ".join(tools_used))
        if final is None:
            return "(no answer)"
        usage = final.usage
        self.last_usage = {
            "input": usage.input_tokens,
            "output": usage.output_tokens,
            "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        }
        if final.stop_reason == "refusal":
            return "The request was declined by the model's safety system: " + str(
                getattr(final, "stop_details", "")
            )
        return "".join(b.text for b in final.content if b.type == "text").strip() or "(no answer)"

    # ---- the planning cycle: structured outputs, no tool loop ----

    def _usage(self, response: Any) -> None:
        usage = response.usage
        self.last_usage = {
            "input": usage.input_tokens,
            "output": usage.output_tokens,
            "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        }

    def _cache_key(self, kind: str, *parts: str) -> str:
        from desk.storage.llm_cache import cache_key

        prompt = {
            "plan": self.static_prompt + self.plan_prompt,
            "challenge": self.skeptic_prompt,
            "brief": self.static_prompt + self.brief_prompt,
        }[kind]
        return cache_key(kind, self.model, prompt, *parts)

    async def plan(self, material: str, limits: str) -> CyclePlan:
        key = self._cache_key("plan", self.daily_layer(), material, limits)
        if self.cache is not None:
            hit = await self.cache.get(key)
            if hit is not None:
                self.last_usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
                return CyclePlan.model_validate(hit)
        plan = await self._plan_llm(material, limits)
        if self.cache is not None:
            await self.cache.put(key, "plan", self.model, plan.model_dump(mode="json"))
        return plan

    async def _plan_llm(self, material: str, limits: str) -> CyclePlan:
        response = await self.client.messages.parse(
            model=self.model,
            max_tokens=8000,
            system=[
                {
                    "type": "text",
                    "text": self.static_prompt + "\n\n" + self.plan_prompt,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": self.daily_layer(),
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            messages=[
                {
                    "role": "user",
                    "content": f"{self.context_line()}\nLIMITS\n{limits}\n\nMATERIAL\n{material}\n\nProduce the plan.",
                }
            ],
            output_format=CyclePlan,
            output_config={"effort": "high"},
            extra_headers=FALLBACK_HEADERS,
            extra_body=FALLBACK_BODY,
        )
        self._usage(response)
        if response.stop_reason == "refusal" or response.parsed_output is None:
            return CyclePlan(
                market_view="The model declined to plan this cycle.",
                risk_note=str(getattr(response, "stop_details", "")),
                decisions=[],
            )
        return response.parsed_output

    async def challenge(self, material: str, plan: CyclePlan) -> Challenge:
        key = self._cache_key("challenge", self.daily_layer(), material, plan.model_dump_json())
        if self.cache is not None:
            hit = await self.cache.get(key)
            if hit is not None:
                self.last_usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
                return Challenge.model_validate(hit)
        challenge = await self._challenge_llm(material, plan)
        if self.cache is not None:
            await self.cache.put(key, "challenge", self.model, challenge.model_dump(mode="json"))
        return challenge

    async def _challenge_llm(self, material: str, plan: CyclePlan) -> Challenge:
        response = await self.client.messages.parse(
            model=self.model,
            max_tokens=4000,
            system=[
                {
                    "type": "text",
                    "text": self.skeptic_prompt,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": self.daily_layer(),
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            messages=[
                {
                    "role": "user",
                    "content": f"{self.context_line()}\nMATERIAL\n{material}\n\nTHE HEAD TRADER'S PLAN\n{plan.model_dump_json(indent=1)}\n\nChallenge it.",
                }
            ],
            output_format=Challenge,
            output_config={"effort": "medium"},
            extra_headers=FALLBACK_HEADERS,
            extra_body=FALLBACK_BODY,
        )
        self._usage(response)
        if response.stop_reason == "refusal" or response.parsed_output is None:
            return Challenge(
                overall="The skeptic declined; every proposed trade is treated as blocked.",
                objections=[],
            )
        return response.parsed_output

    # ---- the daily brief: structured output, no tool loop ----

    async def brief(self, material: str) -> DailyBrief:
        key = self._cache_key("brief", self.daily_layer(), material)
        if self.cache is not None:
            hit = await self.cache.get(key)
            if hit is not None:
                self.last_usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
                return DailyBrief.model_validate(hit)
        response = await self.client.messages.parse(
            model=self.model,
            max_tokens=4000,
            system=[
                {
                    "type": "text",
                    "text": self.static_prompt + "\n\n" + self.brief_prompt,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": self.daily_layer(),
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            messages=[
                {
                    "role": "user",
                    "content": f"{self.context_line()}\n{material}\n\nWrite the brief.",
                }
            ],
            output_format=DailyBrief,
            output_config={"effort": "medium"},
            extra_headers=FALLBACK_HEADERS,
            extra_body=FALLBACK_BODY,
        )
        self._usage(response)
        if response.stop_reason == "refusal" or response.parsed_output is None:
            brief = DailyBrief(
                headline="The model declined to write today's brief.",
                summary=str(getattr(response, "stop_details", "")),
                trades=[],
                book="",
                incidents="",
                tomorrow="",
            )
        else:
            brief = response.parsed_output
        if self.cache is not None:
            await self.cache.put(key, "brief", self.model, brief.model_dump(mode="json"))
        return brief
