"""An in-memory thesis store with the same four methods as the database, for replays and tests:
a replay must never write theses into the live journal."""

from __future__ import annotations

from datetime import datetime

from desk.core.payloads import ThesisPayload

OPEN_STATUSES = ("proposed", "pending_entry", "active", "closing")
UPDATABLE = {
    "status",
    "entry_price",
    "stop_price",
    "target_price",
    "quantity",
    "reason",
    "closed_at",
    "realized_pnl",
}


class MemoryTheses:
    def __init__(self) -> None:
        self.rows: dict[str, ThesisPayload] = {}

    async def insert_thesis(self, thesis: ThesisPayload) -> None:
        self.rows.setdefault(thesis.thesis_id, thesis)

    async def update_thesis(self, thesis_id: str, now: datetime, **fields) -> ThesisPayload | None:
        unknown = set(fields) - UPDATABLE
        if unknown:
            raise ValueError(f"cannot update thesis fields {sorted(unknown)}")
        current = self.rows.get(thesis_id)
        if current is None:
            return None
        data = current.model_dump()
        data.update(fields)
        data["updated_at"] = now
        updated = ThesisPayload(**data)
        self.rows[thesis_id] = updated
        return updated

    async def open_theses(self) -> list[ThesisPayload]:
        rows = [t for t in self.rows.values() if t.status in OPEN_STATUSES]
        return sorted(rows, key=lambda t: t.created_at)

    async def recent_theses(self, limit: int = 50) -> list[ThesisPayload]:
        rows = sorted(self.rows.values(), key=lambda t: t.created_at, reverse=True)
        return rows[:limit]
