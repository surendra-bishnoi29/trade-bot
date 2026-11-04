"""A scripted stand-in for the head trader: no model, no cost, deterministic. It buys the best-ranked
name it does not hold when the journal holds two pieces of evidence for it, holds what it has and
passes on the rest. Everything after the plan (merge, risk officer, execution, thesis monitor) is the
real code, which is what a replay is for."""

from __future__ import annotations

from datetime import timedelta

from desk.agents.head_trader import HeadTrader
from desk.core.events import Topics
from desk.core.payloads import Challenge, CyclePlan, PlanDecision


class ScriptedHeadTrader(HeadTrader):
    def __init__(self, *args, max_new: int = 1, conviction: float = 0.6, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.max_new = max_new
        self.conviction = conviction

    async def _evidence(self, symbol: str, limit: int = 3) -> list[str]:
        now = self.clock.now()
        ids: list[str] = []
        for e in await self.db.recent_events(
            [Topics.NEWS_ARTICLE_SCORED],
            now - timedelta(days=5),
            symbol=symbol,
            limit=limit,
            until=now,
        ):
            ids.append(str(e.payload.get("article_id") or e.event_id))
        for e in await self.db.recent_events(
            [Topics.WATCHER_ALERT], now - timedelta(days=1), symbol=symbol, limit=limit, until=now
        ):
            ids.append(e.event_id)
        return ids[:limit]

    async def plan(self, material: str, limits: str) -> CyclePlan:
        rows = self.material_rows
        held = {r.symbol for r in rows if r.position_qty}
        decisions: list[PlanDecision] = []
        for r in rows:
            if r.symbol in held:
                decisions.append(
                    PlanDecision(
                        symbol=r.symbol,
                        action="hold",
                        quantity=int(r.position_qty),
                        thesis="Scripted: holding; the thesis monitor manages the exit.",
                        evidence_ids=[],
                        expected_return_pct=0.0,
                        horizon_days=10,
                        stop_pct=4.0,
                        target_pct=7.0,
                        invalidation=[],
                        conviction=0.5,
                    )
                )
        new = 0
        for r in rows:
            if r.symbol in held or r.last_close is None or new >= self.max_new:
                continue
            evidence = await self._evidence(r.symbol)
            if len(evidence) < 2:
                continue
            decisions.append(
                PlanDecision(
                    symbol=r.symbol,
                    action="buy",
                    quantity=1,
                    thesis=(
                        f"Scripted signal: rank {r.rank} on the scorecard with "
                        f"{len(evidence)} pieces of evidence in the journal."
                    ),
                    evidence_ids=evidence,
                    expected_return_pct=3.0,
                    horizon_days=10,
                    stop_pct=4.0,
                    target_pct=7.0,
                    invalidation=["scripted replay"],
                    conviction=self.conviction,
                )
            )
            new += 1
        self.last_usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        return CyclePlan(
            market_view="Scripted replay: no model was consulted.",
            risk_note="none",
            decisions=decisions,
        )

    async def challenge(self, material: str, plan: CyclePlan) -> Challenge:
        self.last_usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        return Challenge(overall="Scripted replay: no challenge.", objections=[])
