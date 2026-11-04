"""The swing cycle: scorecard, head trader plan, skeptic challenge, theses, orders. And its schedule."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from ulid import ULID

from desk.agents.back_office import BackOffice
from desk.agents.base import BaseAgent
from desk.agents.head_trader import HeadTrader
from desk.agents.research import research_digest
from desk.agents.reviewer import analyst_record
from desk.agents.risk_officer import RiskOfficer
from desk.agents.scorecard import ScoreRow, build_scorecard, render_scorecard
from desk.agents.swing_strategy import SwingStrategy
from desk.bus.base import Bus
from desk.core import calendar
from desk.core.clock import Clock
from desk.core.config import Settings
from desk.core.events import Event, Topics
from desk.core.payloads import (
    Challenge,
    ChallengePayload,
    CycleEvent,
    CyclePlan,
    CyclePlanPayload,
    OrderIntent,
    ThesisPayload,
)
from desk.storage.db import Database

log = logging.getLogger(__name__)


@dataclass
class Action:
    symbol: str
    action: str  # buy, sell, hold, none
    quantity: int = 0
    note: str = ""
    thesis_id: str | None = None
    decision_index: int | None = None

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "action": self.action,
            "quantity": self.quantity,
            "note": self.note,
            "thesis_id": self.thesis_id,
        }


@dataclass
class CycleResult:
    cycle_id: str
    trigger: str
    dry_run: bool
    scorecard: str
    plan: CyclePlan
    challenge: Challenge
    actions: list[Action]
    failed: str | None = None
    usage: dict = field(default_factory=dict)

    def render(self) -> str:
        lines = [f"cycle {self.cycle_id} ({self.trigger}{', dry run' if self.dry_run else ''})"]
        if self.failed:
            lines.append(f"FAILED: {self.failed}")
            return "\n".join(lines)
        lines += [
            "",
            "MARKET VIEW: " + self.plan.market_view,
            "RISK NOTE: " + self.plan.risk_note,
            "",
            "DECISIONS",
        ]
        for i, d in enumerate(self.plan.decisions):
            final = next((a for a in self.actions if a.decision_index == i), None)
            verdict = (
                f" -> {final.action} {final.quantity or ''} {final.note}".rstrip() if final else ""
            )
            lines.append(
                f"- {d.symbol} {d.action} qty={d.quantity} conv={d.conviction:.2f} exp={d.expected_return_pct:+.1f}% / {d.horizon_days}d stop={d.stop_pct}% target={d.target_pct}%{verdict}"
            )
            lines.append(f"    {d.thesis}")
        lines += ["", "SKEPTIC: " + self.challenge.overall]
        for o in self.challenge.objections:
            lines.append(f"- [{o.severity}] {o.symbol}: {o.argument}")
        acted = [a for a in self.actions if a.action in ("buy", "sell")]
        lines += [
            "",
            "ORDERS: "
            + (
                ", ".join(f"{a.action} {a.quantity} {a.symbol}" for a in acted) if acted else "none"
            ),
        ]
        return "\n".join(lines)


def merge_plan(
    plan: CyclePlan,
    challenge: Challenge,
    rows: list[ScoreRow],
    open_theses: list[ThesisPayload],
    equity: Decimal,
    settings: Settings,
    kill_switch: bool,
) -> list[Action]:
    """Deterministic merge of the plan with the skeptic's objections and the desk's limits."""
    blocking = {o.symbol.upper() for o in challenge.objections if o.severity == "blocking"}
    concerns = {o.symbol.upper() for o in challenge.objections if o.severity == "concern"}
    by_symbol = {r.symbol: r for r in rows}
    open_symbols = {t.symbol for t in open_theses}
    held = {r.symbol for r in rows if r.position_qty}
    budget = equity * settings.position_pct
    slots = max(
        0, min(settings.max_new_per_cycle, settings.max_open_positions - len(open_symbols | held))
    )
    actions: list[Action] = []
    for i, d in enumerate(plan.decisions):
        symbol = d.symbol.upper()
        row = by_symbol.get(symbol)
        a = Action(symbol=symbol, action="none", decision_index=i)
        if d.action == "buy":
            if kill_switch:
                a.note = "kill switch engaged"
            elif symbol in blocking:
                a.note = "blocked by the skeptic"
            elif symbol not in settings.core_universe:
                a.note = "not in the universe"
            elif symbol in held or symbol in open_symbols:
                a.action, a.note = "hold", "already held"
            elif d.conviction < 0.4:
                a.note = "conviction below 0.4"
            elif symbol in concerns and d.conviction < settings.min_conviction_with_concern:
                a.note = (
                    f"skeptic concern and conviction {d.conviction:.2f} below "
                    f"{settings.min_conviction_with_concern}"
                )
            elif not (2 <= d.stop_pct <= 8) or d.target_pct < 1.5 * d.stop_pct:
                a.note = "stop or target outside the rules"
            elif len(d.evidence_ids) < 2:
                a.note = "fewer than two evidence ids"
            elif row is None or row.last_close is None:
                a.note = "no price on the scorecard"
            elif slots <= 0:
                a.note = "no free position slot this cycle"
            else:
                qty = math.floor(float(budget) / float(row.last_close))
                if qty < 1:
                    a.note = f"one share exceeds the {budget:.0f} USD budget"
                else:
                    a.action, a.quantity, a.note = (
                        "buy",
                        qty,
                        f"{qty} shares at about {row.last_close} within a {budget:.0f} USD budget",
                    )
                    slots -= 1
        elif d.action == "sell":
            if symbol in blocking:
                a.action, a.note = "hold", "sell blocked by the skeptic"
            elif symbol not in held:
                a.note = "nothing held to sell"
            else:
                a.action, a.quantity, a.note = (
                    "sell",
                    int(by_symbol[symbol].position_qty),
                    "closing the position",
                )
                thesis = next((t for t in open_theses if t.symbol == symbol), None)
                a.thesis_id = thesis.thesis_id if thesis else None
        elif d.action == "hold":
            a.action, a.note = "hold", "held" if symbol in held else "nothing held"
        else:
            a.note = "passed"
        actions.append(a)
    return actions


class SwingCycle(BaseAgent):
    name = "cycle"
    topics = ()

    def __init__(
        self,
        bus: Bus,
        clock: Clock,
        db: Database,
        settings: Settings,
        head: HeadTrader,
        back_office: BackOffice,
        risk: RiskOfficer,
        strategy: SwingStrategy,
        theses=None,
    ) -> None:
        super().__init__(bus, clock)
        self.db = db
        self.store = theses or db  # where theses live: the database, or memory in a replay
        self.settings = settings
        self.head = head
        self.back_office = back_office
        self.risk = risk
        self.strategy = strategy
        self.running = False

    async def _material(self, rows: list[ScoreRow], open_theses: list[ThesisPayload]) -> str:
        table = render_scorecard(rows, top_n=10)
        candidates = [r.symbol for r in rows if r.rank <= 10 or r.position_qty]
        news = []
        for s in candidates:
            text = await self.head.q_news(s, hours=48)
            news.append(f"## {s}\n" + "\n".join(text.splitlines()[:8]))
        theses = (
            "\n".join(
                f"- {t.thesis_id} {t.symbol} {t.status} qty={t.quantity} entry={t.entry_price} stop={t.stop_price} target={t.target_price} horizon={t.horizon_days}d since {t.created_at.date()}: {t.narrative[:160]}"
                for t in open_theses
            )
            or "none"
        )
        return (
            f"SCORECARD (rank by composite of 20-day return, 5-day return, news and low volatility)\n{table}\n\n"
            f"NEWS (last 48 hours, with scores)\n" + "\n".join(news) + "\n\n"
            f"ALERTS (last 24 hours)\n{await self.head.q_alerts(24)}\n\n"
            f"PORTFOLIO\n{await self.head.q_portfolio()}\n\n"
            f"OPEN THESES\n{theses}\n\n"
            f"RESEARCH TEAM (latest reports; their ids are evidence you can cite)\n"
            f"{await research_digest(self.db, self.clock.now(), candidates)}\n\n"
            f"ANALYST TRACK RECORD (the reviewer's scores of each analyst's calls against what prices did; weigh their reports accordingly)\n"
            f"{await analyst_record(self.db, self.clock.now())}"
        )

    def _limits(self, rows: list[ScoreRow], open_theses: list[ThesisPayload]) -> str:
        s = self.settings
        equity = self.back_office.snapshot.equity
        scale = self.risk.size_scale() if hasattr(self.risk, "size_scale") else Decimal(1)
        budget = equity * s.position_pct * scale
        held = {r.symbol for r in rows if r.position_qty} | {t.symbol for t in open_theses}
        slots = max(0, min(s.max_new_per_cycle, s.max_open_positions - len(held)))
        if scale == 0:
            slots = 0
        posture = (
            " (HALVED: drawdown de-risk)"
            if scale == Decimal("0.5")
            else " (NO NEW RISK: drawdown halt, daily loss limit or kill switch)"
            if scale == 0
            else ""
        )
        suggested = ", ".join(
            f"{r.symbol}={math.floor(float(budget) / float(r.last_close))}"
            for r in rows
            if r.rank <= 10 and r.last_close
        )
        return (
            f"equity {equity:.0f} USD; budget per new position {float(s.position_pct) * 100:.0f}% = {budget:.0f} USD{posture}; "
            f"max open positions {s.max_open_positions}; currently open {len(held)}; new positions allowed this cycle {slots}; "
            f"longs only; whole shares; suggested quantity per buy: {suggested or 'n/a'}; "
            f"kill switch {'ENGAGED: no new buys' if self.risk.state.kill_switch else 'off'}"
        )

    async def run(self, trigger: str, *, dry_run: bool = False) -> CycleResult:
        if self.running:
            raise RuntimeError("a cycle is already running")
        self.running = True
        cycle_id = str(ULID())
        try:
            return await self._run(cycle_id, trigger, dry_run)
        finally:
            self.running = False

    async def _run(self, cycle_id: str, trigger: str, dry_run: bool) -> CycleResult:
        started = await self.emit(
            Topics.CYCLE_STARTED,
            CycleEvent(
                cycle_id=cycle_id, book="swing", stage="planning", status="started", detail=trigger
            ),
            book="swing",
            correlation_id=cycle_id,
        )
        try:
            self.head.current_cycle_id = cycle_id
            await self._stage(
                cycle_id, "scorecard", "ranking the universe and reading the book", started
            )
            await self.back_office.refresh()
            snap = self.back_office.snapshot
            positions = {s: (p.qty, p.avg_entry) for s, p in snap.positions.items()}
            rows = await build_scorecard(
                self.db, list(self.settings.core_universe), positions, self.clock.now()
            )
            open_theses = await self.store.open_theses()
            scorecard = render_scorecard(rows, top_n=10)
            await self.emit(
                Topics.RESEARCH_REPORT,
                {
                    "analyst": "quant_screen",
                    "cycle_id": cycle_id,
                    "table": scorecard,
                    "top": [r.symbol for r in rows[:10]],
                },
                book="swing",
                caused_by=started,
            )
            await self._stage(
                cycle_id,
                "material",
                "collecting the news, alerts and theses behind the top names",
                started,
            )
            material = await self._material(rows, open_theses)
            limits = self._limits(rows, open_theses)
            self.head.material_rows = rows
            plan = await self.head.plan(material, limits)
            usage = {"plan": dict(self.head.last_usage)}
            challenge = await self.head.challenge(material, plan)
            usage["challenge"] = dict(self.head.last_usage)
            await self._stage(
                cycle_id,
                "merge",
                "applying the desk's rules to the plan and the objections",
                started,
            )
            actions = merge_plan(
                plan,
                challenge,
                rows,
                open_theses,
                snap.equity,
                self.settings,
                self.risk.state.kill_switch,
            )
            plan_event = await self.emit(
                Topics.CYCLE_PLAN,
                CyclePlanPayload(
                    cycle_id=cycle_id,
                    trigger=trigger,
                    dry_run=dry_run,
                    plan=plan,
                    challenge=challenge,
                    final_actions=[a.as_dict() for a in actions],
                    prompt_versions=self.head.prompt_versions(),
                ),
                book="swing",
                caused_by=started,
            )
            await self.emit(
                Topics.THESIS_CHALLENGE,
                ChallengePayload(cycle_id=cycle_id, challenge=challenge),
                book="swing",
                caused_by=plan_event,
            )
            if not dry_run:
                orders = sum(a.action in ("buy", "sell") for a in actions)
                await self._stage(
                    cycle_id,
                    "act",
                    f"{orders} orders to the risk officer" if orders else "nothing to do",
                    plan_event,
                )
                await self._act(actions, plan, rows, cycle_id, plan_event)
            await self.emit(
                Topics.CYCLE_COMPLETED,
                CycleEvent(
                    cycle_id=cycle_id,
                    book="swing",
                    stage="done",
                    status="completed",
                    detail=f"{sum(a.action in ('buy', 'sell') for a in actions)} orders",
                ),
                book="swing",
                caused_by=plan_event,
            )
            return CycleResult(
                cycle_id, trigger, dry_run, scorecard, plan, challenge, actions, usage=usage
            )
        except Exception as exc:
            log.exception("cycle %s failed", cycle_id)
            await self.emit(
                Topics.CYCLE_FAILED,
                CycleEvent(
                    cycle_id=cycle_id,
                    book="swing",
                    stage="planning",
                    status="failed",
                    detail=f"{type(exc).__name__}: {exc}"[:300],
                ),
                book="swing",
                caused_by=started,
            )
            return CycleResult(
                cycle_id,
                trigger,
                dry_run,
                "",
                CyclePlan(market_view="", risk_note="", decisions=[]),
                Challenge(overall="", objections=[]),
                [],
                failed=f"{type(exc).__name__}: {exc}",
            )

    async def _stage(self, cycle_id: str, stage: str, detail: str, caused_by: Event) -> None:
        await self.emit(
            Topics.CYCLE_STAGE,
            CycleEvent(
                cycle_id=cycle_id, book="swing", stage=stage, status="started", detail=detail
            ),
            book="swing",
            caused_by=caused_by,
        )

    async def _act(
        self,
        actions: list[Action],
        plan: CyclePlan,
        rows: list[ScoreRow],
        cycle_id: str,
        caused_by: Event,
    ) -> None:
        now = self.clock.now()
        for a in actions:
            if a.action == "buy" and a.decision_index is not None:
                d = plan.decisions[a.decision_index]
                known = await self.db.events_exist(d.evidence_ids)
                if len(known) < 2:
                    a.action, a.quantity = "none", 0
                    a.note = f"only {len(known)} of {len(d.evidence_ids)} evidence ids are known events or articles"
                    continue
                thesis = ThesisPayload(
                    thesis_id=str(ULID()),
                    symbol=a.symbol,
                    book="swing",
                    direction="long",
                    status="pending_entry",
                    cycle_id=cycle_id,
                    narrative=d.thesis,
                    evidence_ids=d.evidence_ids,
                    expected_return_pct=d.expected_return_pct,
                    horizon_days=max(1, min(d.horizon_days, 20)),
                    conviction=d.conviction,
                    quantity=Decimal(a.quantity),
                    stop_pct=d.stop_pct,
                    target_pct=d.target_pct,
                    invalidation=d.invalidation,
                    size_rationale=a.note,
                    created_at=now,
                    updated_at=now,
                )
                await self.store.insert_thesis(thesis)
                a.thesis_id = thesis.thesis_id
                created = await self.emit(
                    Topics.THESIS_CREATED,
                    thesis,
                    symbol=a.symbol,
                    book="swing",
                    caused_by=caused_by,
                )
                intent = OrderIntent(
                    intent_id=str(ULID()),
                    symbol=a.symbol,
                    book="swing",
                    side="buy",
                    quantity=Decimal(a.quantity),
                    thesis_id=thesis.thesis_id,
                    rationale_event_ids=d.evidence_ids,
                )
                await self.emit(
                    Topics.ORDER_INTENT, intent, symbol=a.symbol, book="swing", caused_by=created
                )
            elif a.action == "sell":
                evidence: list[str] = []
                if a.thesis_id:
                    updated = await self.store.update_thesis(
                        a.thesis_id, now, status="closing", reason="head trader decided to exit"
                    )
                    if updated:
                        await self.emit(
                            Topics.THESIS_UPDATED,
                            updated,
                            symbol=a.symbol,
                            book="swing",
                            caused_by=caused_by,
                        )
                        evidence = updated.evidence_ids
                    self.strategy.exiting.add(a.thesis_id)
                intent = OrderIntent(
                    intent_id=str(ULID()),
                    symbol=a.symbol,
                    book="swing",
                    side="sell",
                    quantity=Decimal(a.quantity),
                    thesis_id=a.thesis_id,
                    rationale_event_ids=evidence,
                )
                await self.emit(
                    Topics.ORDER_INTENT, intent, symbol=a.symbol, book="swing", caused_by=caused_by
                )


class CycleScheduler(BaseAgent):
    """Runs the cycle pre-market, mid-session and after the close, and on high-impact news for a held name."""

    name = "scheduler"
    topics = (Topics.NEWS_ARTICLE_SCORED,)

    def __init__(
        self, bus: Bus, clock: Clock, cycle: SwingCycle, strategy: SwingStrategy, settings: Settings
    ) -> None:
        super().__init__(bus, clock)
        self.cycle = cycle
        self.strategy = strategy
        self.settings = settings
        self._last_news_trigger: datetime | None = None
        self.last_cycle_at: datetime | None = None
        self.on_result: Callable[[CycleResult], None] | None = None

    def next_run(self, now: datetime) -> tuple[datetime, str]:
        candidates: list[tuple[datetime, str]] = []
        for day in calendar.last_sessions(now.date() + timedelta(days=7), 12):
            if day < now.date():
                continue
            open_at, close_at = calendar.session_bounds(day)
            candidates.append((open_at - timedelta(minutes=60), "pre_market"))
            if self.settings.cycle_mid_session:
                candidates.append((open_at + timedelta(hours=3), "mid_session"))
            candidates.append((close_at + timedelta(minutes=10), "post_close"))
        every = self.settings.cycle_every_minutes
        if every > 0 and calendar.is_open(now):
            base = self.last_cycle_at or now
            at = base + timedelta(minutes=every)
            if at <= now:
                at = now + timedelta(seconds=1)
            if calendar.is_open(at - timedelta(seconds=1)):
                candidates.append((at, f"every_{every}m"))
        future = sorted(c for c in candidates if c[0] > now)
        return future[0]

    SLICE_SECONDS = 60.0
    MAX_LATE_HOURS = 10  # a cycle missed longer ago than this is not worth running late

    def missed_slot(self, now: datetime) -> tuple[datetime, str] | None:
        """The most recent scheduled slot that has passed, if it passed within MAX_LATE_HOURS."""
        candidates: list[tuple[datetime, str]] = []
        for day in calendar.last_sessions(now.date(), 3):
            open_at, close_at = calendar.session_bounds(day)
            candidates.append((open_at - timedelta(minutes=60), "pre_market"))
            if self.settings.cycle_mid_session:
                candidates.append((open_at + timedelta(hours=3), "mid_session"))
            candidates.append((close_at + timedelta(minutes=10), "post_close"))
        past = sorted((c for c in candidates if c[0] <= now), reverse=True)
        if not past or now - past[0][0] > timedelta(hours=self.MAX_LATE_HOURS):
            return None
        return past[0]

    @staticmethod
    def may_run_late(trigger: str, now: datetime) -> bool:
        """When a missed cycle is still worth running: a post-close review any time, a pre-market cycle
        any time before that session's close, a mid-session cycle only while the market is open."""
        if trigger == "post_close":
            return True
        if trigger == "pre_market":
            return calendar.is_session(now.date()) and now < calendar.session_bounds(now.date())[1]
        return calendar.is_open(now)

    async def catch_up(self) -> bool:
        """After a start: run the most recent scheduled cycle if the journal shows it never happened."""
        now = self.clock.now()
        slot = self.missed_slot(now)
        if slot is None:
            return False
        at, trigger = slot
        plans = await self.cycle.db.recent_events([Topics.CYCLE_PLAN], at, limit=50)
        if any(e.payload.get("trigger") == trigger for e in plans):
            return False
        if not self.may_run_late(trigger, now):
            log.warning(
                "missed cycle %s due at %s: market closed now, skipping", trigger, at.isoformat()
            )
            return False
        log.warning("missed cycle %s due at %s: running it now", trigger, at.isoformat())
        await self._run(trigger)
        return True

    async def run_forever(self) -> None:
        """Sleeps in short slices and re-reads the wall clock, so a machine that was asleep or a long
        stall cannot skip a cycle silently: a due cycle runs as soon as the desk is awake."""
        try:
            await self.catch_up()
        except Exception as exc:  # noqa: BLE001 - catching up must not stop the schedule
            log.error("catch-up failed: %s", exc)
        at, trigger = self.next_run(self.clock.now())
        log.info("next cycle: %s at %s", trigger, at.isoformat())
        while True:
            now = self.clock.now()
            if now < at:
                await self.clock.sleep(
                    min(self.SLICE_SECONDS, max(1.0, (at - now).total_seconds()))
                )
                continue
            late = (now - at).total_seconds()
            if late > 15 * 60 and not self.may_run_late(trigger, now):
                log.warning(
                    "cycle %s is %.0f minutes late and the market is closed: skipped",
                    trigger,
                    late / 60,
                )
            else:
                if late > 15 * 60:
                    log.warning(
                        "cycle %s is %.0f minutes late (the desk was away): running now",
                        trigger,
                        late / 60,
                    )
                await self._run(trigger)
            at, trigger = self.next_run(self.clock.now())
            log.info("next cycle: %s at %s", trigger, at.isoformat())

    async def _run(self, trigger: str) -> None:
        self.last_cycle_at = self.clock.now()
        try:
            result = await self.cycle.run(trigger)
            log.info("cycle %s: %s", trigger, result.render().splitlines()[-1])
            if self.on_result is not None:
                self.on_result(result)
        except Exception as exc:  # noqa: BLE001 - the schedule must survive a failed cycle
            log.error("scheduled cycle failed: %s", exc)

    async def handle(self, event: Event) -> None:
        if (
            not self.settings.cycle_news_trigger
            or event.payload.get("impact") != "high"
            or not event.symbol
        ):
            return
        if not self.strategy.by_symbol(event.symbol):
            return
        now = self.clock.now()
        if self._last_news_trigger and now - self._last_news_trigger < timedelta(minutes=10):
            return
        self._last_news_trigger = now
        if self.cycle.running:
            return
        log.info("high-impact news on held %s: running a cycle", event.symbol)
        result = await self.cycle.run(f"news:{event.symbol}")
        if self.on_result is not None:
            self.on_result(result)
