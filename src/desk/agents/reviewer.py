"""The performance reviewer: after the post-close cycle it grades every thesis closed since the last
review (deterministic numbers, one model call for the lessons) and, once a week, scores every
analyst's calls against what prices did. Both are events: Telegram and the interface carry them,
and the head trader reads the credibility weights in every plan."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core import prompts
from desk.core.clock import Clock
from desk.core.events import Event, Topics
from desk.core.payloads import AnalystScoresPayload, TradePostmortem, WeeklyReviewPayload
from desk.review.postmortem import (
    Grade,
    grade_closed,
    render_weights,
    score_analysts,
    write_lessons,
)
from desk.review.weekly import render_weekly, weekly_facts
from desk.storage.db import Database

log = logging.getLogger(__name__)

SCORE_EVERY = timedelta(days=7)


async def latest_scores(db: Database, now: datetime) -> dict[str, Any] | None:
    events = await db.recent_events(
        [Topics.REVIEW_ANALYST_SCORE], now - timedelta(days=30), limit=1, until=now
    )
    return events[0].payload if events else None


async def analyst_record(db: Database, now: datetime) -> str:
    """The line the head trader reads about who has earned trust."""
    latest = await latest_scores(db, now)
    if not latest:
        return "not scored yet: the reviewer scores the team weekly once there is a week of calls"
    return render_weights(latest.get("weights") or {})


class ReviewerAgent(BaseAgent):
    name = "reviewer"
    topics = (Topics.CYCLE_PLAN,)

    def __init__(
        self,
        bus: Bus,
        clock: Clock,
        db: Database,
        head: Any = None,
        *,
        narrative: bool = True,
        lookback_days: int = 60,
    ) -> None:
        super().__init__(bus, clock)
        self.db = db
        self.head = head
        self.narrative = narrative
        self.lookback_days = lookback_days

    async def handle(self, event: Event) -> None:
        p = event.payload
        if p.get("trigger") != "post_close" or p.get("dry_run"):
            return
        try:
            await self.review(caused_by=event)
        except Exception as exc:  # noqa: BLE001 - a failed review must not stop anything else
            log.error("post-mortems failed: %s", exc)
        try:
            if await self.scores_due():
                await self.score(caused_by=event)
        except Exception as exc:  # noqa: BLE001
            log.error("analyst scoring failed: %s", exc)
        try:
            if self.clock.now().weekday() == 4:
                await self.weekly(caused_by=event)
        except Exception as exc:  # noqa: BLE001
            log.error("weekly review failed: %s", exc)

    async def graded(self, now: datetime) -> set[str]:
        events = await self.db.recent_events(
            [Topics.REVIEW_POSTMORTEM], now - timedelta(days=90), limit=2000, until=now
        )
        return {str(e.payload.get("thesis_id")) for e in events}

    async def review(self, caused_by: Event | None = None) -> list[Grade]:
        """Grade every thesis closed in the last 30 days that has no post-mortem yet."""
        now = self.clock.now()
        grades = await grade_closed(
            self.db, now - timedelta(days=30), now, skip=await self.graded(now)
        )
        if not grades:
            log.info("reviewer: nothing closed to grade")
            return []
        lessons = {}
        if self.narrative and self.head is not None:
            try:
                lessons = await write_lessons(self.head, grades)
            except Exception as exc:  # noqa: BLE001 - the numbers stand without the narrative
                log.error("post-mortem narrative failed: %s", exc)
        version = prompts.version("postmortem") if lessons else ""
        for g in grades:
            lesson = lessons.get(g.thesis_id)
            if lesson is not None:
                g.lesson = lesson.lesson
            payload = TradePostmortem(
                **g.as_dict(),
                process_sound=lesson.process_sound if lesson is not None else None,
                prompt_version=version,
            )
            await self.emit(
                Topics.REVIEW_POSTMORTEM,
                payload,
                symbol=g.symbol,
                book="swing",
                caused_by=caused_by,
            )
        log.info("reviewer: %d post-mortems", len(grades))
        return grades

    async def weekly(self, caused_by: Event | None = None) -> str:
        """The weekly review as an event: Markdown for the interface and Telegram, facts for later."""
        now = self.clock.now()
        facts = await weekly_facts(self.db, now)
        text = render_weekly(facts)
        payload = WeeklyReviewPayload(
            week_ending=now.date(),
            generated_at=now,
            markdown=text,
            facts={
                k: v
                for k, v in facts.items()
                if k not in ("grades", "prompts") and not isinstance(v, datetime)
            },
        )
        await self.emit(Topics.REVIEW_WEEKLY, payload, caused_by=caused_by)
        log.info("reviewer: weekly review written")
        return text

    async def scores_due(self) -> bool:
        now = self.clock.now()
        if now.weekday() == 4:  # Friday: the weekly review
            return True
        recent = await self.db.recent_events(
            [Topics.REVIEW_ANALYST_SCORE], now - SCORE_EVERY, limit=1, until=now
        )
        return not recent

    async def score(self, caused_by: Event | None = None) -> dict[str, Any]:
        now = self.clock.now()
        result = await score_analysts(self.db, now, days=self.lookback_days)
        payload = AnalystScoresPayload(
            since=result["since"],
            until=result["until"],
            days=result["days"],
            calls=result["calls"],
            scores=result["scores"],
            weights=result["weights"],
        )
        await self.emit(Topics.REVIEW_ANALYST_SCORE, payload, caused_by=caused_by)
        log.info("reviewer: analyst scores over %d days, %d calls", result["days"], result["calls"])
        return result
