"""The forecast analyst: after every quant screen, the champion model predicts each name's forward
return distribution and publishes it as a report with its own out-of-sample track record, so the
head trader knows how much to trust it. No champion, no report."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from desk.agents.base import BaseAgent
from desk.bus.base import Bus
from desk.core.clock import Clock
from desk.core.events import Event, Topics
from desk.features.daily import build_frame, latest_rows
from desk.forecast.model import SwingForecaster, load
from desk.forecast.train import load_bars
from desk.storage.db import Database

log = logging.getLogger(__name__)


def forecast_report(
    symbol: str, row: dict[str, Any], model_row: dict[str, Any], horizon: int
) -> dict[str, Any]:
    pooled = (model_row.get("metrics") or {}).get("pooled") or {}
    ic, hit, n = pooled.get("ic"), pooled.get("hit"), pooled.get("n", 0)
    p = float(row["p_up"])
    exp = float(row["exp_ret"])
    skill = min(1.0, max(0.2, (ic or 0.0) * 5))
    if p >= 0.58:
        stance = "bullish"
    elif p <= 0.42:
        stance = "bearish"
    else:
        stance = "neutral"
    conviction = round(min(1.0, abs(p - 0.5) * 2) * skill, 2)
    record = (
        f"out-of-sample rank correlation {ic:+.2f}, hit rate {hit:.0%} over {n} predictions"
        if ic is not None and hit is not None
        else "no out-of-sample record"
    )
    return {
        "analyst": "forecaster",
        "kind": "forecast",
        "symbol": symbol,
        "stance": stance,
        "conviction": conviction,
        "horizon": f"{horizon} sessions",
        "summary": (
            f"expected {horizon}-session return {exp * 100:+.1f}% (10th to 90th percentile "
            f"{float(row['q10']) * 100:+.1f}% to {float(row['q90']) * 100:+.1f}%), p(up) {p:.0%}; "
            f"model {model_row.get('model_id')}, {record}."
        ),
        "data": {
            "exp_ret": exp,
            "p_up": p,
            "q10": float(row["q10"]),
            "q90": float(row["q90"]),
            "model_id": model_row.get("model_id"),
            "horizon_sessions": horizon,
            "ic": ic,
            "hit": hit,
            "n": n,
            "session": str(row["session"]),
        },
    }


class ForecastAnalyst(BaseAgent):
    name = "forecaster"
    topics = (Topics.RESEARCH_REPORT,)

    def __init__(
        self,
        bus: Bus,
        clock: Clock,
        db: Database,
        universe: list[str],
        *,
        horizon: int = 5,
        min_interval: timedelta = timedelta(minutes=45),
    ) -> None:
        super().__init__(bus, clock)
        self.db = db
        self.universe = universe
        self.horizon = horizon
        self.min_interval = min_interval  # daily features barely move within an hour
        self.last_run: datetime | None = None
        self.model: SwingForecaster | None = None
        self.model_row: dict[str, Any] | None = None
        self._warned = False

    async def ensure_model(self) -> bool:
        row = await self.db.champion("swing", self.horizon)
        if row is None:
            if not self._warned:
                log.info("forecaster idle: no champion model yet (run `desk train`)")
                self._warned = True
            return False
        if self.model_row is None or row["model_id"] != self.model_row["model_id"]:
            try:
                self.model = load(row["path"])
                self.model_row = row
                log.info("forecaster loaded champion %s", row["model_id"])
            except Exception as exc:  # noqa: BLE001 - a missing file must not stop the desk
                log.error("forecaster cannot load %s: %s", row["path"], exc)
                return False
        return self.model is not None

    async def predict_now(self) -> list[dict[str, Any]]:
        if not await self.ensure_model() or self.model is None or self.model_row is None:
            return []
        now = self.clock.now()
        bars = await load_bars(self.db, self.universe, 120, as_of=now)
        rows = latest_rows(build_frame(bars), now.date())
        if rows.empty:
            return []
        pred = self.model.predict(rows)
        return [
            forecast_report(str(r["symbol"]), r, self.model_row, self.horizon)
            for r in pred.to_dict("records")
        ]

    async def handle(self, event: Event) -> None:
        if event.payload.get("kind") != "scorecard":
            return
        now = self.clock.now()
        if self.last_run is not None and now - self.last_run < self.min_interval:
            return
        try:
            reports = await self.predict_now()
        except Exception as exc:  # noqa: BLE001
            log.error("forecast failed: %s", exc)
            return
        if reports:
            self.last_run = now
        for r in reports:
            await self.emit(
                Topics.RESEARCH_REPORT, r, symbol=r["symbol"], book="swing", caused_by=event
            )
        if reports:
            log.info("forecaster: %d names", len(reports))
