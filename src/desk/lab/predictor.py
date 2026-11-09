"""The predictor: the scored brain behind each instrument's eye. A sentinel trigger wakes it; it
reads what the desk knows about that instrument and answers with a call: direction, confidence,
horizon and the lines. A skeptic checks the call. An acted call becomes an order intent with the
maximum-loss line attached; the lines engine owns it from the fill and scores it at the close.
Every call, acted or not, is an event, so the record is complete."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field
from ulid import ULID

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core import calendar, prompts
from desk.core.clock import Clock
from desk.core.events import Event, Topics, parse_payload
from desk.core.payloads import Call, OrderIntent, PredictionCall, SentinelTrigger
from desk.funnel.build import symbol_detail
from desk.funnel.markets import NAMES, Market
from desk.storage.db import Database

log = logging.getLogger(__name__)

FALLBACK_HEADERS = {"anthropic-beta": "server-side-fallback-2026-07-01"}
FALLBACK_BODY = {"fallbacks": "default"}
BOOK_OF_MARKET = {"stocks": "swing", "commodities": "commodities", "crypto": "crypto"}
ROUND_TRIP_PCT = {"stocks": 0.1, "commodities": 0.1, "crypto": 0.5}


class Objection(BaseModel):
    severity: Literal["none", "concern", "blocking"]
    argument: str = Field(description="three sentences at most, with the numbers")


async def record_of(db: Database, symbol: str, now: datetime, days: int = 60) -> dict[str, Any]:
    """How the predictor's calls on this instrument turned out so far."""
    events = await db.recent_events(
        [Topics.PREDICTION_SCORE], now - timedelta(days=days), symbol=symbol, limit=500, until=now
    )
    scored = [e.payload for e in events if e.payload.get("hit") is not None]
    hits = sum(1 for p in scored if p["hit"])
    avg = sum(float(p.get("realized_pct") or 0) for p in scored) / len(scored) if scored else None
    return {
        "n": len(scored),
        "hit": round(hits / len(scored), 3) if scored else None,
        "avg_pct": round(avg, 3) if avg is not None else None,
        "unfilled": sum(1 for e in events if e.payload.get("reason") == "unfilled"),
    }


def render_record(r: dict[str, Any]) -> str:
    if not r["n"]:
        return "no scored calls yet on this instrument"
    return f"{r['n']} scored calls, {r['hit']:.0%} reached their first profit line, average {r['avg_pct']:+.2f}% after costs"


class Predictor(BaseAgent):
    name = "predictor"
    version = "0.1"
    topics = (Topics.SENTINEL_TRIGGER, Topics.PREDICTION_SCORE)

    def __init__(
        self,
        bus: Bus,
        clock: Clock,
        db: Database,
        markets: dict[str, Market],
        sentinel: Any,
        back_office: Any,
        risk: Any = None,
        *,
        model: str = "claude-opus-5-5",
        min_confidence: float = 0.6,
        max_calls_per_hour: int = 40,
        position_pct: Decimal = Decimal("0.01"),
        min_first_profit_pct: float = 0.5,
        min_loss_pct: float = 0.5,
        no_entry_minutes: int = 30,
        client: anthropic.AsyncAnthropic | None = None,
        cache: Any = None,
    ) -> None:
        super().__init__(bus, clock)
        self.db = db
        self.markets = markets
        self.sentinel = sentinel
        self.back_office = back_office
        self.risk = risk
        self.model = model
        self.min_confidence = min_confidence
        self.max_calls_per_hour = max_calls_per_hour
        self.position_pct = Decimal(position_pct)
        self.min_first_profit_pct = min_first_profit_pct
        self.min_loss_pct = min_loss_pct
        self.no_entry_minutes = no_entry_minutes
        self._client = client
        self.cache = cache
        self.calls_at: list[datetime] = []
        self.sem = asyncio.Semaphore(2)
        self.open_symbols: set[str] = set()
        self.last_usage: dict[str, int] = {}
        self.spent: dict[str, int] = {}  # tokens of the decision in hand

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic()
        return self._client

    # ---- the material ----

    async def material(self, t: SentinelTrigger, now: datetime) -> tuple[str, dict[str, Any]]:
        market = self.markets[t.market]
        funnel = self.sentinel.funnels.get(t.market) or {}
        row = next((r for r in funnel.get("symbols", []) if r["symbol"] == t.symbol), None)
        detail = await symbol_detail(self.db, market, t.symbol, now)
        news = await self.db.recent_events(
            [Topics.NEWS_ARTICLE_RAW],
            now - timedelta(hours=36),
            symbol=t.symbol,
            limit=8,
            until=now,
        )
        record = await record_of(self.db, t.symbol, now)
        book = BOOK_OF_MARKET[t.market]
        limits = self.risk.limits.for_book(book) if self.risk is not None else None
        shorts = bool(limits.allow_short) if limits is not None else t.market != "crypto"
        parts = [
            (
                f"TRIGGER: {t.symbol} ({NAMES.get(t.symbol, t.symbol)}, {market.label}) at {now:%H:%M} UTC: {t.reason}, {t.detail}. "
                f"Last {t.last}, reading {t.side} {t.strength:.0f} (score {t.score:+.0f}), setup {t.setup}."
            ),
        ]
        if row:
            parts.append(
                "READINGS (agent in brackets):\n"
                + "\n".join(
                    f"- {e['label']}: {e['value']} [{e['source']}, {e['verdict']}]"
                    for e in row["evidence"]
                )
            )
            parts.append(f"BOOK ROUTING: {row['book']['tag']}. " + " ".join(row["book"]["reasons"]))
            lv = row["levels"]
            parts.append(
                f"LEVELS: session low {lv['s1']}, session high {lv['r1']}, 20-day low {lv['s2']}, 20-day high {lv['r2']}, "
                f"VWAP {lv['vwap']:.4g}, prior close {lv['prior_close']}, day {row['day_pct']:+.2f}%, against 20-day average {row['vs_avg20_pct']:+.1f}%."
            )
        s = funnel.get("sentiment") or {}
        if s:
            parts.append(
                f"MARKET ({market.label}): {s['verdict']} {s['score']:+.0f}: "
                + "; ".join(f"{e['label']} {e['value']}" for e in s["evidence"])
            )
        if news:
            parts.append(
                "NEWS (newest first):\n"
                + "\n".join(
                    f"- [{e.event_id}] {e.occurred_at:%m-%d %H:%M} {e.payload.get('title', '')}"
                    for e in news
                )
            )
        if detail["trail"]:
            parts.append(
                "TRAIL TODAY:\n"
                + "\n".join(
                    f"- {x['at'][11:16]} {x['agent']}: {x['text']}" for x in detail["trail"][:12]
                )
            )
        parts.append(
            f"BOOK RULES: shorts {'allowed' if shorts else 'not allowed'}; round-trip cost {ROUND_TRIP_PCT[t.market]:.2f}%; "
            f"first profit line at least {max(self.min_first_profit_pct, 3 * ROUND_TRIP_PCT[t.market]):.2f}%; "
            f"maximum loss at least {self.min_loss_pct:.2f}%."
        )
        parts.append(f"TRACK RECORD on {t.symbol}: {render_record(record)}.")
        inputs: dict[str, Any] = {
            "trigger": {"reason": t.reason, "detail": t.detail},
            "reading": {"side": t.side, "strength": t.strength, "score": t.score, "setup": t.setup},
            "last": t.last,
            "evidence": [
                {k: e.get(k) for k in ("label", "value", "verdict", "source")}
                for e in (row or {}).get("evidence", [])
            ],
            "levels": {
                k: (row or {}).get("levels", {}).get(k)
                for k in ("s1", "r1", "s2", "r2", "vwap", "prior_close")
            }
            if row
            else {},
            "day_pct": (row or {}).get("day_pct"),
            "market": {
                "label": market.label,
                "verdict": s.get("verdict"),
                "score": s.get("score"),
                "evidence": [
                    {k: e.get(k) for k in ("label", "value", "verdict")}
                    for e in s.get("evidence", [])[:5]
                ],
            }
            if s
            else {"label": market.label},
            "news": [
                {
                    "id": e.event_id,
                    "at": e.occurred_at.isoformat(),
                    "title": str(e.payload.get("title", ""))[:160],
                }
                for e in news
            ],
            "trail": [
                {"at": x["at"], "agent": x["agent"], "text": str(x["text"])[:160]}
                for x in detail["trail"][:8]
            ],
            "rules": {
                "shorts": shorts,
                "round_trip_pct": ROUND_TRIP_PCT[t.market],
                "min_first_profit_pct": max(
                    self.min_first_profit_pct, 3 * ROUND_TRIP_PCT[t.market]
                ),
                "min_loss_pct": self.min_loss_pct,
                "min_confidence": self.min_confidence,
            },
        }
        return "\n\n".join(parts), {
            "record": record,
            "shorts": shorts,
            "row": row,
            "inputs": inputs,
        }

    # ---- the model ----

    async def _parse(
        self, system: str, user: str, output: type[BaseModel], max_tokens: int
    ) -> BaseModel | None:
        key = hashlib.sha256(f"{self.model}|{system}|{user}".encode()).hexdigest()
        if self.cache is not None:
            hit = await self.cache.get(key)
            if hit is not None:
                return output.model_validate(hit)
        response = await self.client.messages.parse(
            model=self.model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=output,
            output_config={"effort": "medium"},
            extra_headers=FALLBACK_HEADERS,
            extra_body=FALLBACK_BODY,
        )
        usage = response.usage
        self.last_usage = {"input": usage.input_tokens, "output": usage.output_tokens}
        for k, v in self.last_usage.items():
            self.spent[k] = self.spent.get(k, 0) + int(v or 0)
        if response.stop_reason == "refusal" or response.parsed_output is None:
            return None
        if self.cache is not None:
            await self.cache.put(
                key,
                output.__name__.lower(),
                self.model,
                response.parsed_output.model_dump(mode="json"),
            )
        return response.parsed_output

    def _budget_ok(self, now: datetime) -> bool:
        self.calls_at = [t for t in self.calls_at if now - t < timedelta(hours=1)]
        return len(self.calls_at) < self.max_calls_per_hour

    # ---- the decision ----

    async def handle(self, event: Event) -> None:
        if event.type == Topics.PREDICTION_SCORE:
            self.open_symbols.discard(str(event.payload.get("symbol")))
            return
        t = parse_payload(event)
        assert isinstance(t, SentinelTrigger)
        async with self.sem:
            try:
                await self.decide(t, event)
            except Exception as exc:  # noqa: BLE001 - one failed call must not stop the desk
                log.error("predictor failed on %s: %s", t.symbol, exc)

    async def decide(self, t: SentinelTrigger, caused_by: Event) -> PredictionCall | None:
        now = self.clock.now()
        if t.symbol in self.open_symbols or (
            self.risk is not None and t.symbol in getattr(self.back_office, "book_of", {})
        ):
            return None
        if t.market != "crypto" and self.no_entry_minutes > 0:
            # stocks and funds are flat before the bell: a call this late has no room to work
            if not calendar.is_open(now):
                return None
            close_at = calendar.session_bounds(calendar.session_of(now))[1]
            if now >= close_at - timedelta(minutes=self.no_entry_minutes):
                return None
        if not self._budget_ok(now):
            return await self._record(
                t, None, 0.0, False, "budget: the hourly call budget is spent", "", {}, caused_by
            )
        self.calls_at.append(now)
        text, extra = await self.material(t, now)
        self.spent = {}
        started = time.monotonic()
        seen = {"inputs": extra["inputs"]}
        call = await self._parse(
            prompts.load("predictor_call"), text + "\n\nMake the call.", Call, 1600
        )
        if call is None:
            return await self._record(
                t, None, 0.0, False, "the model declined", "", extra["record"], caused_by, **seen
            )
        assert isinstance(call, Call)
        skip = ""
        if call.direction == "none":
            skip = "the predictor sees no edge"
        elif call.confidence < self.min_confidence:
            skip = f"confidence {call.confidence:.2f} below the bar {self.min_confidence:.2f}"
        elif call.direction == "short" and not extra["shorts"]:
            skip = "shorts are not allowed in this book"
        elif call.first_profit_pct < max(self.min_first_profit_pct, 3 * ROUND_TRIP_PCT[t.market]):
            skip = f"first profit line {call.first_profit_pct:.2f}% cannot pay the costs"
        elif call.max_loss_pct < self.min_loss_pct:
            skip = f"maximum loss {call.max_loss_pct:.2f}% is inside the noise"
        objection = ""
        if not skip:
            self.calls_at.append(now)
            ob = await self._parse(
                prompts.load("predictor_skeptic"),
                f"{text}\n\nTHE CALL: {call.model_dump_json()}\n\nFind what is wrong with it.",
                Objection,
                600,
            )
            if isinstance(ob, Objection):
                objection = f"{ob.severity}: {ob.argument}"
                if ob.severity == "blocking":
                    skip = "blocked by the skeptic"
                elif ob.severity == "concern" and call.confidence < self.min_confidence + 0.1:
                    skip = f"skeptic concern and confidence {call.confidence:.2f} below {self.min_confidence + 0.1:.2f}"
        if not skip and self._size(t.market, t.last) <= 0:
            skip = f"one position's budget buys less than one unit at {t.last}"
        seen["seconds"] = round(time.monotonic() - started, 1)
        seen["tokens"] = dict(self.spent)
        rec = await self._record(
            t, call, t.last, not skip, skip, objection, extra["record"], caused_by, **seen
        )
        if rec is not None and rec.acted:
            await self._order(t, call, rec, caused_by)
        return rec

    async def _record(
        self,
        t: SentinelTrigger,
        call: Call | None,
        price: float,
        acted: bool,
        skipped: str,
        objection: str,
        record: dict[str, Any],
        caused_by: Event,
        *,
        inputs: dict[str, Any] | None = None,
        seconds: float | None = None,
        tokens: dict[str, int] | None = None,
    ) -> PredictionCall:
        payload = PredictionCall(
            call_id=str(ULID()),
            symbol=t.symbol,
            market=t.market,
            trigger=f"{t.reason}: {t.detail}",
            call=call
            or Call(
                direction="none",
                confidence=0.0,
                horizon_minutes=15,
                max_loss_pct=1,
                first_profit_pct=1,
                step_pct=0.5,
                reason=skipped,
            ),
            price=price or t.last,
            acted=acted,
            skipped_because=skipped,
            objection=objection,
            model=self.model if call is not None else "",
            prompt_version=prompts.version("predictor_call") if call is not None else "",
            record=record,
            inputs=inputs or {},
            seconds=seconds,
            tokens=tokens or {},
        )
        await self.emit(
            Topics.PREDICTION_CALL,
            payload,
            symbol=t.symbol,
            book=BOOK_OF_MARKET[t.market],
            caused_by=caused_by,
        )  # type: ignore[arg-type]
        log.info(
            "predictor %s: %s %s conf %.2f%s",
            t.symbol,
            payload.call.direction,
            "acted" if acted else "skipped",
            payload.call.confidence,
            f" ({skipped})" if skipped else "",
        )
        return payload

    def _size(self, market: str, price: float) -> Decimal:
        equity = Decimal(str(getattr(self.back_office.snapshot, "equity", 0)))
        budget = equity * self.position_pct
        if price <= 0 or budget <= 0:
            return Decimal(0)
        if market == "crypto":
            return (budget / Decimal(str(price))).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
        return Decimal(int(budget / Decimal(str(price))))

    async def _order(
        self, t: SentinelTrigger, call: Call, rec: PredictionCall, caused_by: Event
    ) -> None:
        qty = self._size(t.market, rec.price)
        if qty <= 0:
            log.warning("predictor: no size for %s at %s", t.symbol, rec.price)
            return
        sign = 1 if call.direction == "long" else -1
        # the broker takes stock and fund prices to the cent only; crypto prices are finer
        places = 4 if t.market == "crypto" else 2
        stop = Decimal(str(round(rec.price * (1 - sign * call.max_loss_pct / 100), places)))
        intent = OrderIntent(
            intent_id=str(ULID()),
            symbol=t.symbol,
            book=BOOK_OF_MARKET[t.market],  # type: ignore[arg-type]
            side="buy" if call.direction == "long" else "sell",
            quantity=qty,
            urgency="now",
            rationale_event_ids=list(call.evidence_ids)[:6],
            stop_price=stop,
            call_id=rec.call_id,
            allow_fractional=t.market == "crypto",
        )
        self.open_symbols.add(t.symbol)
        await self.emit(
            Topics.ORDER_INTENT, intent, symbol=t.symbol, book=intent.book, caused_by=caused_by
        )

    def released(self, symbol: str) -> None:
        self.open_symbols.discard(symbol)


def utcnow() -> datetime:
    return datetime.now(UTC)
