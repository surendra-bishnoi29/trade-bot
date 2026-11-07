"""Read models for the interface, built from the journal. Reads only: the api never changes desk state.
Every trade card answers what, when, why, how it exits, what the skeptic said and what the risk officer
checked, assembled from the fill, the intent, the thesis, the cycle plan and the risk decision."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from desk.core import calendar
from desk.core.events import Event, Topics
from desk.core.notices import summarize
from desk.storage.db import Database

EVENT_COLS = (
    "event_id, type, schema_version, occurred_at, ingested_at, symbol, book, correlation_id, "
    "causation_id, source, payload"
)

JOURNAL_TYPES = [
    Topics.ORDER_FILLED,
    Topics.ORDER_SUBMITTED,
    Topics.ORDER_REJECTED,
    Topics.RISK_APPROVED,
    Topics.RISK_REJECTED,
    Topics.RISK_KILL_SWITCH,
    Topics.WATCHER_ALERT,
    Topics.CYCLE_STARTED,
    Topics.CYCLE_COMPLETED,
    Topics.CYCLE_FAILED,
    Topics.THESIS_CREATED,
    Topics.THESIS_UPDATED,
    Topics.THESIS_INVALIDATED,
    Topics.THESIS_CLOSED,
    Topics.SYSTEM_HEALTH,
    Topics.SYSTEM_RESTARTED,
    Topics.CONSOLE_QUESTION,
    Topics.CONSOLE_ANSWER,
]


def num(v: Any) -> Any:
    """Numbers travel through the journal as JSON strings (Decimal); give the interface floats."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return v
    return v


def pct(a: Any, b: Any) -> float | None:
    """(a - b) / b in percent, or None."""
    try:
        a, b = float(a), float(b)
        return round((a - b) / b * 100, 2) if b else None
    except (TypeError, ValueError):
        return None


class StateReader:
    def __init__(
        self, db: Database, *, heartbeat_minutes: int = 5, traded: set[str] | None = None
    ) -> None:
        self.db = db
        self.heartbeat_minutes = heartbeat_minutes
        self.traded = traded

    async def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self.db.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r, strict=True)) for r in await cur.fetchall()]

    async def _latest(
        self, type_: str, *, days: int = 30, where: str = "", params: tuple = ()
    ) -> dict | None:
        rows = await self._query(
            f"SELECT {EVENT_COLS} FROM events WHERE type = %s AND occurred_at >= now() - interval '{int(days)} days' "
            f"{where} ORDER BY occurred_at DESC LIMIT 1",
            (type_, *params),
        )
        return rows[0] if rows else None

    # ---- the book ----

    async def book(self) -> dict:
        snap = await self._latest(Topics.PORTFOLIO_RECONCILED, days=7)
        theses = {t.symbol: t for t in await self.db.open_theses()}
        positions = []
        p = snap["payload"] if snap else {}
        for pos in p.get("positions", []):
            symbol = pos["symbol"]
            qty = Decimal(str(pos["qty"]))
            entry = pos.get("avg_entry")
            bars = await self.db.recent_bars(symbol, "1m", limit=1)
            last = bars[0] if bars else None
            if last is not None:
                price, price_at = float(last["close"]), last["ts"]
            elif pos.get("market_value") is not None and qty:
                price, price_at = float(pos["market_value"]) / float(qty), p.get("as_of")
            else:
                price, price_at = None, None
            value = price * float(qty) if price is not None else num(pos.get("market_value"))
            pl = (
                (price - float(entry)) * float(qty)
                if price is not None and entry is not None
                else num(pos.get("unrealized_pl"))
            )
            t = theses.get(symbol)
            thesis = None
            if t is not None:
                thesis = {
                    "thesis_id": t.thesis_id,
                    "status": t.status,
                    "conviction": t.conviction,
                    "stop": num(t.stop_price),
                    "target": num(t.target_price),
                    "entry": num(t.entry_price),
                    "horizon_days": t.horizon_days,
                    "created_at": t.created_at,
                    "to_stop_pct": pct(price, t.stop_price) if t.stop_price else None,
                    "to_target_pct": pct(t.target_price, price) if t.target_price else None,
                    "narrative": t.narrative,
                }
            positions.append(
                {
                    "symbol": symbol,
                    "qty": num(qty),
                    "avg_entry": num(entry),
                    "price": price,
                    "price_at": price_at,
                    "market_value": value,
                    "unrealized_pl": pl,
                    "unrealized_pct": pct(price, entry) if price is not None and entry else None,
                    "thesis": thesis,
                }
            )
        return {
            "as_of": p.get("as_of"),
            "broker": p.get("broker"),
            "equity": num(p.get("equity")),
            "cash": num(p.get("cash")),
            "buying_power": num(p.get("buying_power")),
            "gross_exposure": sum(x["market_value"] or 0 for x in positions),
            "positions": positions,
            "open_theses": [t.model_dump(mode="json") for t in theses.values()],
        }

    # ---- trades with their reasons ----

    async def trades(self, limit: int = 50) -> list[dict]:
        fills = await self._query(
            f"SELECT {EVENT_COLS} FROM events WHERE type = %s AND occurred_at >= now() - interval '90 days' "
            "ORDER BY occurred_at DESC LIMIT %s",
            (Topics.ORDER_FILLED, limit * 3),
        )
        seen: set[str] = set()
        cards: list[dict] = []
        for fill in fills:
            intent_id = fill["payload"].get("intent_id")
            if not intent_id or intent_id in seen:
                continue
            seen.add(intent_id)
            cards.append(await self._trade_card(fill, intent_id))
            if len(cards) >= limit:
                break
        return cards

    async def _trade_card(self, fill: dict, intent_id: str) -> dict:
        p = fill["payload"]
        symbol = fill["symbol"]
        intent = await self._latest(
            Topics.ORDER_INTENT,
            days=90,
            where="AND payload->>'intent_id' = %s",
            params=(intent_id,),
        )
        ip = intent["payload"] if intent else {}
        risk_rows = await self._query(
            f"SELECT {EVENT_COLS} FROM events WHERE type IN (%s, %s) AND payload->>'intent_id' = %s "
            "ORDER BY occurred_at DESC LIMIT 1",
            (Topics.RISK_APPROVED, Topics.RISK_REJECTED, intent_id),
        )
        risk = risk_rows[0] if risk_rows else None
        thesis = None
        if ip.get("thesis_id"):
            rows = await self._query(
                "SELECT * FROM theses WHERE thesis_id = %s", (ip["thesis_id"],)
            )
            thesis = rows[0] if rows else None
        plan = decision = objection = None
        corr = intent["correlation_id"] if intent else None
        if corr:
            plan_ev = await self._latest(
                Topics.CYCLE_PLAN, days=90, where="AND correlation_id = %s", params=(corr,)
            )
            if plan_ev:
                pp = plan_ev["payload"]
                plan = {
                    "trigger": pp.get("trigger"),
                    "market_view": (pp.get("plan") or {}).get("market_view"),
                    "risk_note": (pp.get("plan") or {}).get("risk_note"),
                    "at": plan_ev["occurred_at"],
                }
                decisions = (pp.get("plan") or {}).get("decisions") or []
                decision = next(
                    (d for d in decisions if str(d.get("symbol", "")).upper() == symbol), None
                )
                challenge = pp.get("challenge") or {}
                objection = next(
                    (
                        o
                        for o in challenge.get("objections") or []
                        if str(o.get("symbol", "")).upper() == symbol
                    ),
                    None,
                )
                if objection is None and challenge.get("overall"):
                    objection = {"severity": "none", "argument": challenge["overall"]}
        evidence_ids = (thesis or {}).get("evidence_ids") or ip.get("rationale_event_ids") or []
        side = p.get("side") or ip.get("side") or ""
        qty = num(p.get("filled_quantity")) or num(ip.get("quantity"))
        price = num(p.get("avg_price"))
        if plan:
            trigger = plan["trigger"]
        elif ip.get("urgency") == "now":
            trigger = "thesis monitor"
        else:
            trigger = "head trader"
        return {
            "trade_id": intent_id,
            "time": fill["occurred_at"],
            "symbol": symbol,
            "side": side,
            "quantity": qty,
            "price": price,
            "notional": round(qty * price, 2) if qty and price else None,
            "book": ip.get("book"),
            "what": f"{side.upper()} {qty:g} {symbol}" + (f" at {price:,.2f}" if price else ""),
            "when": {"time": fill["occurred_at"], "trigger": trigger, "cycle_id": corr},
            "why": (thesis or {}).get("narrative") or (decision or {}).get("thesis") or "",
            "evidence": await self._evidence(list(evidence_ids)[:12]),
            "exit_plan": (
                {
                    "stop": num(thesis.get("stop_price")),
                    "target": num(thesis.get("target_price")),
                    "stop_pct": num(thesis.get("stop_pct")),
                    "target_pct": num(thesis.get("target_pct")),
                    "horizon_days": thesis.get("horizon_days"),
                    "invalidation": thesis.get("invalidation") or [],
                }
                if thesis
                else None
            ),
            "skeptic": objection,
            "risk": (
                {
                    "decision": "approved" if risk["type"] == Topics.RISK_APPROVED else "rejected",
                    "reason": risk["payload"].get("reason"),
                    "rules": risk["payload"].get("rules") or [],
                }
                if risk
                else None
            ),
            "thesis": (
                {
                    "thesis_id": thesis["thesis_id"],
                    "status": thesis["status"],
                    "conviction": num(thesis.get("conviction")),
                    "expected_return_pct": num(thesis.get("expected_return_pct")),
                    "realized_pnl": num(thesis.get("realized_pnl")),
                    "closed_at": thesis.get("closed_at"),
                    "reason": thesis.get("reason"),
                }
                if thesis
                else None
            ),
            "plan": plan,
        }

    async def _evidence(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        out: list[dict] = []
        articles = await self._query(
            f"SELECT {EVENT_COLS} FROM events WHERE type = %s AND payload->>'article_id' = ANY(%s)",
            (Topics.NEWS_ARTICLE_RAW, ids),
        )
        scores = {
            r["payload"]["article_id"]: r["payload"]
            for r in await self._query(
                f"SELECT {EVENT_COLS} FROM events WHERE type = %s AND payload->>'article_id' = ANY(%s)",
                (Topics.NEWS_ARTICLE_SCORED, ids),
            )
        }
        seen: set[str] = set()
        for a in articles:
            ap = a["payload"]
            if ap["article_id"] in seen:
                continue
            seen.add(ap["article_id"])
            s = scores.get(ap["article_id"], {})
            out.append(
                {
                    "id": ap["article_id"],
                    "kind": "news",
                    "title": ap.get("title"),
                    "source": ap.get("source"),
                    "url": ap.get("url"),
                    "time": ap.get("published_at"),
                    "impact": s.get("impact"),
                    "polarity": s.get("polarity"),
                }
            )
        events = await self._query(
            f"SELECT {EVENT_COLS} FROM events WHERE event_id = ANY(%s)", (ids,)
        )
        for e in events:
            ev = Event(**e)
            out.append(
                {
                    "id": e["event_id"],
                    "kind": "event",
                    "title": summarize(ev),
                    "source": e["source"],
                    "url": None,
                    "time": e["occurred_at"],
                    "impact": None,
                    "polarity": None,
                }
            )
        return out

    # ---- cycles ----

    async def cycles(self, limit: int = 20) -> list[dict]:
        rows = await self._query(
            f"SELECT {EVENT_COLS} FROM events WHERE type = %s ORDER BY occurred_at DESC LIMIT %s",
            (Topics.CYCLE_PLAN, limit),
        )
        out = []
        for r in rows:
            p = r["payload"]
            plan = p.get("plan") or {}
            done = await self._query(
                f"SELECT {EVENT_COLS} FROM events WHERE type = %s AND payload->>'cycle_id' = %s "
                "ORDER BY occurred_at DESC LIMIT 1",
                (Topics.CYCLE_COMPLETED, p.get("cycle_id")),
            )
            out.append(
                {
                    "cycle_id": p.get("cycle_id"),
                    "time": r["occurred_at"],
                    "trigger": p.get("trigger"),
                    "dry_run": p.get("dry_run", False),
                    "market_view": plan.get("market_view"),
                    "risk_note": plan.get("risk_note"),
                    "decisions": plan.get("decisions") or [],
                    "challenge": p.get("challenge"),
                    "actions": p.get("final_actions") or [],
                    "result": done[0]["payload"].get("detail") if done else None,
                }
            )
        return out

    # ---- activity feed ----

    async def journal(self, limit: int = 100) -> list[dict]:
        rows = await self._query(
            f"SELECT {EVENT_COLS} FROM events WHERE type = ANY(%s) ORDER BY occurred_at DESC LIMIT %s",
            (JOURNAL_TYPES, limit * 2),
        )
        out = []
        for r in rows:
            if r["type"] == Topics.WATCHER_ALERT and r["payload"].get("kind") not in (
                "feed_stale",
                "feed_recovered",
            ):
                continue
            ev = Event(**r)
            out.append(
                {
                    "id": r["event_id"],
                    "time": r["occurred_at"],
                    "type": r["type"],
                    "symbol": r["symbol"],
                    "source": r["source"],
                    "text": summarize(ev),
                }
            )
            if len(out) >= limit:
                break
        return out

    # ---- health ----

    async def health(self) -> dict:
        now = datetime.now(UTC)
        beat = await self._latest(Topics.SYSTEM_HEARTBEAT, days=3)
        restarted = await self._latest(Topics.SYSTEM_RESTARTED, days=60)
        kill = await self._latest(Topics.RISK_KILL_SWITCH, days=60)
        comps = await self._query(
            "SELECT DISTINCT ON (payload->>'component') payload, occurred_at FROM events "
            "WHERE type = %s AND occurred_at >= now() - interval '2 days' "
            "ORDER BY payload->>'component', occurred_at DESC",
            (Topics.SYSTEM_HEALTH,),
        )
        last_bar = await self.db.latest_bar_ts("1m")
        beat_age = (now - beat["occurred_at"]).total_seconds() if beat else None
        bp = beat["payload"] if beat else {}
        return {
            "now": now,
            "market_open": calendar.is_open(now),
            "next_open": calendar.next_open(now),
            "bot": {
                "alive": beat_age is not None and beat_age < self.heartbeat_minutes * 60 * 3,
                "last_heartbeat": beat["occurred_at"] if beat else None,
                "status": bp.get("status"),
                "next_cycle": bp.get("next_cycle"),
                "open_theses": bp.get("open_theses"),
                "started_at": restarted["payload"].get("started_at") if restarted else None,
                "gap_seconds": restarted["payload"].get("gap_seconds") if restarted else None,
                "drawdown": bp.get("drawdown"),
                "day_pnl": bp.get("day_pnl"),
                "size_scale": bp.get("size_scale"),
            },
            "feed": {
                "last_bar": last_bar,
                "age_seconds": (now - last_bar).total_seconds() if last_bar else None,
            },
            "kill_switch": {
                "engaged": bool(kill["payload"].get("engaged", True)) if kill else False,
                "reason": kill["payload"].get("reason") if kill else "",
                "since": kill["occurred_at"] if kill else None,
            },
            "components": [
                {
                    "component": c["payload"].get("component"),
                    "status": c["payload"].get("status"),
                    "detail": c["payload"].get("detail"),
                    "at": c["occurred_at"],
                }
                for c in comps
            ],
        }

    # ---- chat ----

    async def chat(self, limit: int = 30) -> list[dict]:
        rows = await self._query(
            f"SELECT {EVENT_COLS} FROM events WHERE type IN (%s, %s) ORDER BY occurred_at DESC LIMIT %s",
            (Topics.CONSOLE_QUESTION, Topics.CONSOLE_ANSWER, limit * 2),
        )
        questions: dict[str, dict] = {}
        answers: dict[str, dict] = {}
        for r in rows:
            if r["type"] == Topics.CONSOLE_QUESTION:
                questions[r["event_id"]] = r
            elif r["causation_id"]:
                answers[r["causation_id"]] = r
        out = []
        for qid, q in questions.items():
            a = answers.get(qid)
            out.append(
                {
                    "request_id": q["payload"].get("request_id") or q["event_id"],
                    "user": q["payload"].get("user") or "console",
                    "question": q["payload"].get("text", ""),
                    "asked_at": q["occurred_at"],
                    "answer": a["payload"].get("text") if a else None,
                    "answered_at": a["occurred_at"] if a else None,
                }
            )
        out.sort(key=lambda x: x["asked_at"])
        return out[-limit:]

    async def answer(self, request_id: str) -> dict | None:
        a = await self._latest(
            Topics.CONSOLE_ANSWER,
            days=7,
            where="AND payload->>'request_id' = %s",
            params=(request_id,),
        )
        if a is None:
            return None
        return {
            "request_id": request_id,
            "answer": a["payload"].get("text"),
            "at": a["occurred_at"],
        }

    async def command_result(self, request_id: str) -> dict | None:
        r = await self._latest(
            Topics.DESK_COMMAND_RESULT,
            days=7,
            where="AND payload->>'request_id' = %s",
            params=(request_id,),
        )
        if r is None:
            return None
        return {"request_id": request_id, **r["payload"], "at": r["occurred_at"]}

    # ---- the team: last activity per agent ----

    async def agents(self) -> list[dict]:
        last = await self._query(
            f"SELECT DISTINCT ON (source) {EVENT_COLS} FROM events "
            "WHERE occurred_at >= now() - interval '2 days' ORDER BY source, occurred_at DESC"
        )
        counts = {
            r["source"]: r["n"]
            for r in await self._query(
                "SELECT source, count(*) AS n FROM events WHERE occurred_at >= now() - interval '10 minutes' "
                "GROUP BY source"
            )
        }
        out = []
        for r in last:
            name, _, version = r["source"].partition("@")
            out.append(
                {
                    "agent": name,
                    "version": version,
                    "last_type": r["type"],
                    "last_at": r["occurred_at"],
                    "last_symbol": r["symbol"],
                    "last_text": summarize(Event(**r)),
                    "events_10m": counts.get(r["source"], 0),
                }
            )
        return out

    # ---- research reports (scorecards, morning notes) ----

    async def research(self, kind: str, limit: int = 1) -> list[dict]:
        rows = await self._query(
            f"SELECT {EVENT_COLS} FROM events WHERE type = %s AND payload->>'kind' = %s "
            "ORDER BY occurred_at DESC LIMIT %s",
            (Topics.RESEARCH_REPORT, kind, limit),
        )
        return [{"id": r["event_id"], "at": r["occurred_at"], **r["payload"]} for r in rows]

    async def research_latest(self, hours: int = 24) -> list[dict]:
        rows = await self._query(
            f"SELECT DISTINCT ON (payload->>'analyst', payload->>'kind', symbol) {EVENT_COLS} FROM events "
            "WHERE type = %s AND occurred_at >= now() - make_interval(hours => %s) "
            "ORDER BY payload->>'analyst', payload->>'kind', symbol, occurred_at DESC",
            (Topics.RESEARCH_REPORT, hours),
        )
        return [
            {
                "id": r["event_id"],
                "at": r["occurred_at"],
                "analyst": r["payload"].get("analyst"),
                "kind": r["payload"].get("kind"),
                "symbol": r["symbol"] or r["payload"].get("symbol"),
                "stance": r["payload"].get("stance"),
                "conviction": r["payload"].get("conviction"),
                "summary": r["payload"].get("summary"),
                "data": r["payload"].get("data"),
            }
            for r in rows
            if r["payload"].get("kind") not in ("scorecard",)
        ]

    # ---- daily briefs ----

    async def reviews(self, limit: int = 20) -> list[dict]:
        rows = await self._query(
            f"SELECT {EVENT_COLS} FROM events WHERE type = %s ORDER BY occurred_at DESC LIMIT %s",
            (Topics.REVIEW_POSTMORTEM, limit),
        )
        return [{"id": r["event_id"], "at": r["occurred_at"], **r["payload"]} for r in rows]

    async def weekly_review(self) -> dict | None:
        row = await self._latest(Topics.REVIEW_WEEKLY, days=30)
        if not row:
            return None
        p = row["payload"]
        return {
            "id": row["event_id"],
            "at": row["occurred_at"],
            "week_ending": p.get("week_ending"),
            "markdown": p.get("markdown", ""),
            "facts": p.get("facts") or {},
        }

    async def graduation(self) -> dict:
        from desk.review.graduation import graduation

        result = await graduation(self.db, datetime.now(UTC))
        result["as_of"] = result["as_of"].isoformat()
        result["started"] = result["started"].isoformat() if result.get("started") else None
        return result

    async def analyst_scores(self) -> dict | None:
        row = await self._latest(Topics.REVIEW_ANALYST_SCORE, days=60)
        return {"id": row["event_id"], "at": row["occurred_at"], **row["payload"]} if row else None

    # ---- the funnel ----

    def _markets(self):
        from desk.core.config import DEFAULT_UNIVERSE
        from desk.funnel.markets import markets

        return markets(list(DEFAULT_UNIVERSE), self.traded)

    @staticmethod
    def _at(at: str | None) -> datetime | None:
        if not at:
            return None
        t = datetime.fromisoformat(at)
        return t if t.tzinfo else t.replace(tzinfo=UTC)

    async def funnel(self, market: str, at: str | None = None) -> dict:
        from desk.funnel.build import build_funnel

        return await build_funnel(
            self.db, self._markets()[market], datetime.now(UTC), at=self._at(at)
        )

    async def funnel_detail(self, market: str, symbol: str, at: str | None = None) -> dict:
        from desk.funnel.build import symbol_detail

        m = self._markets()[market]
        if symbol not in m.all_symbols:
            raise KeyError(symbol)
        return await symbol_detail(self.db, m, symbol, datetime.now(UTC), at=self._at(at))

    async def funnel_markets(self) -> list[dict]:
        out = []
        for m in self._markets().values():
            rows = await self._query(
                "SELECT max(ts) AS last, array_agg(DISTINCT (ts AT TIME ZONE 'UTC')::date) AS days FROM bars "
                "WHERE interval = '1m' AND symbol = ANY(%s) AND ts >= now() - interval '21 days'",
                (list(m.all_symbols),),
            )
            out.append(
                {
                    "key": m.key,
                    "label": m.label,
                    "traded": m.traded,
                    "note": m.note,
                    "instruments": len(m.symbols),
                    "last_bar": rows[0]["last"] if rows else None,
                    "has_data": bool(rows and rows[0]["last"]),
                    "days": sorted(str(d) for d in (rows[0]["days"] or [])) if rows else [],
                }
            )
        return out

    async def briefs(self, limit: int = 5) -> list[dict]:
        rows = await self._query(
            f"SELECT {EVENT_COLS} FROM events WHERE type = %s ORDER BY occurred_at DESC LIMIT %s",
            (Topics.DESK_BRIEF, limit),
        )
        return [
            {
                "id": r["event_id"],
                "day": r["payload"].get("day"),
                "generated_at": r["payload"].get("generated_at"),
                "brief": r["payload"].get("brief"),
            }
            for r in rows
        ]

    # ---- bars for charts ----

    async def bars(self, symbol: str, interval: str, n: int) -> list[dict]:
        rows = await self.db.recent_bars(symbol.upper(), interval, limit=n)  # oldest first
        return [
            {
                "ts": r["ts"],
                "open": num(r["open"]),
                "high": num(r["high"]),
                "low": num(r["low"]),
                "close": num(r["close"]),
                "volume": r["volume"],
            }
            for r in rows
        ]
