"""PostgreSQL access: the append-only event store and the bars table."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from desk.core.events import Event
from desk.core.payloads import MarketBar, ThesisPayload

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _month_bounds(t: datetime) -> tuple[datetime, datetime]:
    start = t.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


class Database:
    def __init__(self, url: str, *, min_size: int = 1, max_size: int = 5) -> None:
        # every connection speaks UTC, so timestamptz values never come back in the server's local zone
        self.pool = AsyncConnectionPool(
            url,
            open=False,
            min_size=min_size,
            max_size=max_size,
            kwargs={"options": "-c timezone=UTC"},
        )
        self._partitions: set[str] = set()

    async def open(self) -> None:
        await self.pool.open()

    async def close(self) -> None:
        await self.pool.close()

    async def migrate(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(SCHEMA_PATH.read_text())

    async def ensure_partition(self, t: datetime) -> None:
        start, end = _month_bounds(t)
        name = f"events_y{start.year}m{start.month:02d}"
        if name in self._partitions:
            return
        async with self.pool.connection() as conn:
            await conn.execute(
                f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF events "
                f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
            )
        self._partitions.add(name)

    async def insert_event(self, event: Event) -> bool:
        """Append one event. Returns False if it was already stored (idempotent on event_id)."""
        await self.ensure_partition(event.occurred_at)
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                INSERT INTO events (event_id, type, schema_version, occurred_at, ingested_at, symbol, book,
                                    correlation_id, causation_id, source, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    event.event_id,
                    event.type,
                    event.schema_version,
                    event.occurred_at,
                    event.ingested_at,
                    event.symbol,
                    event.book,
                    event.correlation_id,
                    event.causation_id,
                    event.source,
                    Jsonb(event.payload),
                ),
            )
            return cur.rowcount == 1

    async def count_events(self, *, event_id: str | None = None) -> int:
        async with self.pool.connection() as conn:
            if event_id is None:
                cur = await conn.execute("SELECT count(*) FROM events")
            else:
                cur = await conn.execute(
                    "SELECT count(*) FROM events WHERE event_id = %s", (event_id,)
                )
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    # ---- bars ----

    async def insert_bars(self, bars: list[MarketBar], ingested_at: datetime) -> int:
        if not bars:
            return 0
        rows = [
            (
                b.symbol,
                b.interval,
                b.ts,
                b.open,
                b.high,
                b.low,
                b.close,
                b.volume,
                b.vwap,
                b.trade_count,
                b.source,
                ingested_at,
            )
            for b in bars
        ]
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(
                    """
                    INSERT INTO bars (symbol, interval, ts, open, high, low, close, volume, vwap, trade_count, source, ingested_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    rows,
                )
            return len(rows)

    async def recent_bars(
        self, symbol: str, interval: str, limit: int = 30, before: datetime | None = None
    ) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT symbol, interval, ts, open, high, low, close, volume, vwap, trade_count
                FROM bars WHERE symbol = %s AND interval = %s AND (%s::timestamptz IS NULL OR ts < %s)
                ORDER BY ts DESC LIMIT %s
                """,
                (symbol, interval, before, before, limit),
            )
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row, strict=True)) for row in reversed(await cur.fetchall())]

    async def bar_counts(self) -> dict[str, int]:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT interval, count(*) FROM bars GROUP BY interval")
            return {row[0]: int(row[1]) for row in await cur.fetchall()}

    # ---- events ----

    async def recent_events(
        self, types: list[str], since: datetime, *, symbol: str | None = None, limit: int = 50
    ) -> list[Event]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT event_id, type, schema_version, occurred_at, ingested_at, symbol, book, correlation_id,
                       causation_id, source, payload
                FROM events
                WHERE type = ANY(%s) AND occurred_at >= %s AND (%s::text IS NULL OR symbol = %s)
                ORDER BY occurred_at DESC LIMIT %s
                """,
                (types, since, symbol, symbol, limit),
            )
            cols = [d.name for d in cur.description]
            return [Event(**dict(zip(cols, row, strict=True))) for row in await cur.fetchall()]

    # ---- theses ----

    _THESIS_COLS = (
        "thesis_id, symbol, book, direction, status, cycle_id, narrative, evidence_ids, expected_return_pct, "
        "horizon_days, conviction, quantity, entry_price, stop_price, target_price, stop_pct, target_pct, "
        "invalidation, size_rationale, reason, created_at, updated_at, closed_at, realized_pnl"
    )

    @staticmethod
    def _thesis_from_row(cols: list[str], row: tuple) -> ThesisPayload:
        data = dict(zip(cols, row, strict=True))
        data["evidence_ids"] = list(data.get("evidence_ids") or [])
        data["invalidation"] = list(data.get("invalidation") or [])
        for k in ("expected_return_pct", "conviction", "stop_pct", "target_pct"):
            data[k] = float(data[k])
        return ThesisPayload(**data)

    async def insert_thesis(self, thesis: ThesisPayload) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                f"INSERT INTO theses ({self._THESIS_COLS}) VALUES ({', '.join(['%s'] * 24)}) ON CONFLICT (thesis_id) DO NOTHING",
                (
                    thesis.thesis_id,
                    thesis.symbol,
                    thesis.book,
                    thesis.direction,
                    thesis.status,
                    thesis.cycle_id,
                    thesis.narrative,
                    thesis.evidence_ids,
                    thesis.expected_return_pct,
                    thesis.horizon_days,
                    thesis.conviction,
                    thesis.quantity,
                    thesis.entry_price,
                    thesis.stop_price,
                    thesis.target_price,
                    thesis.stop_pct,
                    thesis.target_pct,
                    Jsonb(thesis.invalidation),
                    thesis.size_rationale,
                    thesis.reason,
                    thesis.created_at,
                    thesis.updated_at,
                    thesis.closed_at,
                    thesis.realized_pnl,
                ),
            )

    async def update_thesis(self, thesis_id: str, now: datetime, **fields) -> ThesisPayload | None:
        allowed = {
            "status",
            "entry_price",
            "stop_price",
            "target_price",
            "quantity",
            "reason",
            "closed_at",
            "realized_pnl",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot update thesis fields {sorted(unknown)}")
        sets = ", ".join(f"{k} = %s" for k in fields) + ", updated_at = %s"
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"UPDATE theses SET {sets} WHERE thesis_id = %s RETURNING {self._THESIS_COLS}",
                (*fields.values(), now, thesis_id),
            )
            row = await cur.fetchone()
            return self._thesis_from_row([d.name for d in cur.description], row) if row else None

    async def open_theses(self) -> list[ThesisPayload]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {self._THESIS_COLS} FROM theses WHERE status IN ('proposed', 'pending_entry', 'active', 'closing') ORDER BY created_at"
            )
            cols = [d.name for d in cur.description]
            return [self._thesis_from_row(cols, r) for r in await cur.fetchall()]

    async def recent_theses(self, limit: int = 50) -> list[ThesisPayload]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {self._THESIS_COLS} FROM theses ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            cols = [d.name for d in cur.description]
            return [self._thesis_from_row(cols, r) for r in await cur.fetchall()]

    async def latest_bar_ts(self, interval: str) -> datetime | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT max(ts) FROM bars WHERE interval = %s", (interval,))
            row = await cur.fetchone()
            return row[0] if row else None

    async def events_exist(self, ids: list[str]) -> set[str]:
        """Which of these identifiers are known: event ids, or article ids carried in a payload."""
        if not ids:
            return set()
        wanted = [str(i) for i in ids]
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT event_id, payload->>'article_id' FROM events WHERE event_id = ANY(%s) OR payload->>'article_id' = ANY(%s)",
                (wanted, wanted),
            )
            found: set[str] = set()
            for event_id, article_id in await cur.fetchall():
                if event_id in wanted:
                    found.add(event_id)
                if article_id in wanted:
                    found.add(article_id)
            return found

    async def terminal_order_intents(self, since: datetime) -> set[str]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT DISTINCT payload->>'intent_id' FROM events WHERE type = ANY(%s) AND occurred_at >= %s",
                (["order.filled", "order.canceled", "order.rejected"], since),
            )
            return {row[0] for row in await cur.fetchall() if row[0]}
