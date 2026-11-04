"""PostgreSQL access: the append-only event store and the bars table."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
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


def _jsonable(value):
    """JSON cannot carry NaN or infinity; models report them as nulls."""
    import math

    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if hasattr(value, "item"):  # numpy scalars
        return _jsonable(value.item())
    return value


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
                    ON CONFLICT (symbol, interval, ts) DO UPDATE SET
                        open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                        close = EXCLUDED.close, volume = EXCLUDED.volume, vwap = EXCLUDED.vwap,
                        trade_count = EXCLUDED.trade_count, source = EXCLUDED.source,
                        ingested_at = EXCLUDED.ingested_at
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

    async def report_exists(
        self, kind: str, since: datetime, until: datetime | None = None
    ) -> bool:
        """Whether a research report of this kind was published in the window (cheap, exact)."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT 1 FROM events WHERE type = %s AND payload->>'kind' = %s AND occurred_at >= %s "
                "AND (%s::timestamptz IS NULL OR occurred_at <= %s) LIMIT 1",
                ("research.report", kind, since, until, until),
            )
            return await cur.fetchone() is not None

    async def bars_between(
        self, symbols: list[str], interval: str, start: datetime, end: datetime
    ) -> list[dict]:
        """All bars of the symbols in [start, end), oldest first; the replayer's feed."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT symbol, interval, ts, open, high, low, close, volume, vwap, trade_count, source
                FROM bars WHERE symbol = ANY(%s) AND interval = %s AND ts >= %s AND ts < %s
                ORDER BY ts, symbol
                """,
                (symbols, interval, start, end),
            )
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row, strict=True)) for row in await cur.fetchall()]

    async def llm_cache_get(self, key: str) -> dict | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT response FROM llm_cache WHERE key = %s", (key,))
            row = await cur.fetchone()
            return row[0] if row else None

    async def llm_cache_put(self, key: str, kind: str, model: str, response: dict) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO llm_cache (key, kind, model, response) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (key) DO NOTHING",
                (key, kind, model, Jsonb(response)),
            )

    # ---- feature store and model registry ----

    async def upsert_features(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(
                    """
                    INSERT INTO features_daily (symbol, session, computed_at, features, targets)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (symbol, session) DO UPDATE SET
                        computed_at = EXCLUDED.computed_at, features = EXCLUDED.features, targets = EXCLUDED.targets
                    """,
                    [
                        (
                            r["symbol"],
                            r["session"],
                            r["computed_at"],
                            Jsonb(r["features"]),
                            Jsonb(r["targets"]),
                        )
                        for r in rows
                    ],
                )
            return len(rows)

    async def features_count(self) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM features_daily")
            return int((await cur.fetchone())[0])

    async def register_model(
        self,
        model_id: str,
        kind: str,
        horizon: int,
        trained_through,
        path: str,
        metrics: dict,
        status: str,
        note: str = "",
    ) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO models (model_id, kind, horizon, trained_through, path, metrics, status, note) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (model_id) DO UPDATE SET "
                "metrics = EXCLUDED.metrics, status = EXCLUDED.status, note = EXCLUDED.note",
                (
                    model_id,
                    kind,
                    horizon,
                    trained_through,
                    path,
                    Jsonb(_jsonable(metrics)),
                    status,
                    note,
                ),
            )

    async def set_model_status(self, model_id: str, status: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "UPDATE models SET status = %s WHERE model_id = %s", (status, model_id)
            )

    async def champion(self, kind: str, horizon: int) -> dict | None:
        rows = await self.models(kind, horizon=horizon, status="champion", limit=1)
        return rows[0] if rows else None

    async def models(
        self, kind: str, *, horizon: int | None = None, status: str | None = None, limit: int = 20
    ) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT model_id, kind, horizon, trained_at, trained_through, path, metrics, status, note FROM models "
                "WHERE kind = %s AND (%s::int IS NULL OR horizon = %s) AND (%s::text IS NULL OR status = %s) "
                "ORDER BY trained_at DESC LIMIT %s",
                (kind, horizon, horizon, status, status, limit),
            )
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r, strict=True)) for r in await cur.fetchall()]

    async def bar_counts(self) -> dict[str, int]:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT interval, count(*) FROM bars GROUP BY interval")
            return {row[0]: int(row[1]) for row in await cur.fetchall()}

    # ---- events ----

    async def recent_events(
        self,
        types: list[str],
        since: datetime,
        *,
        symbol: str | None = None,
        limit: int = 50,
        until: datetime | None = None,
    ) -> list[Event]:
        """Events of the given types since a time, newest first. `until` bounds the read to what was
        known at that moment: a replay must never see the future."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT event_id, type, schema_version, occurred_at, ingested_at, symbol, book, correlation_id,
                       causation_id, source, payload
                FROM events
                WHERE type = ANY(%s) AND occurred_at >= %s AND (%s::text IS NULL OR symbol = %s)
                  AND (%s::timestamptz IS NULL OR occurred_at <= %s)
                ORDER BY occurred_at DESC LIMIT %s
                """,
                (types, since, symbol, symbol, until, until, limit),
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

    async def closed_theses(self, since: datetime, until: datetime) -> list[ThesisPayload]:
        """Theses closed in the window, oldest close first."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {self._THESIS_COLS} FROM theses WHERE status = 'closed' "
                "AND coalesce(closed_at, updated_at) >= %s AND coalesce(closed_at, updated_at) <= %s "
                "ORDER BY coalesce(closed_at, updated_at)",
                (since, until),
            )
            cols = [d.name for d in cur.description]
            return [self._thesis_from_row(cols, r) for r in await cur.fetchall()]

    # ---- operations: the weekly review and the graduation checklist ----

    async def count_events_between(
        self,
        types: list[str],
        since: datetime,
        until: datetime,
        *,
        kind: str | None = None,
        book: str | None = None,
        engaged: bool | None = None,
        status_not: str | None = None,
    ) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM events WHERE type = ANY(%s) AND occurred_at >= %s AND occurred_at < %s "
                "AND (%s::text IS NULL OR payload->>'kind' = %s) "
                "AND (%s::text IS NULL OR book = %s) "
                "AND (%s::boolean IS NULL OR coalesce((payload->>'engaged')::boolean, true) = %s) "
                "AND (%s::text IS NULL OR coalesce(payload->>'status', '') <> %s)",
                (
                    types,
                    since,
                    until,
                    kind,
                    kind,
                    book,
                    book,
                    engaged,
                    engaged,
                    status_not,
                    status_not,
                ),
            )
            return int((await cur.fetchone())[0])

    async def equity_series(
        self, since: datetime, until: datetime
    ) -> list[tuple[datetime, Decimal]]:
        """Every portfolio snapshot's equity in the window, oldest first."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT occurred_at, (payload->>'equity')::numeric FROM events WHERE type = 'portfolio.reconciled' "
                "AND occurred_at >= %s AND occurred_at <= %s AND payload->>'equity' IS NOT NULL ORDER BY occurred_at",
                (since, until),
            )
            return [(r[0], Decimal(str(r[1]))) for r in await cur.fetchall()]

    async def first_event_at(self) -> datetime | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT min(occurred_at) FROM events")
            row = await cur.fetchone()
            return row[0] if row else None

    async def sessions_seen(self, since: datetime, until: datetime) -> list[date]:
        """Dates on which the desk was alive (sent heartbeats), oldest first."""
        return await self.sessions_with_events("system.heartbeat", since, until)

    async def sessions_with_events(self, type: str, since: datetime, until: datetime) -> list[date]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT DISTINCT (occurred_at AT TIME ZONE 'UTC')::date AS d FROM events WHERE type = %s "
                "AND occurred_at >= %s AND occurred_at <= %s ORDER BY d",
                (type, since, until),
            )
            return [r[0] for r in await cur.fetchall()]

    async def restart_gaps(self, since: datetime, until: datetime) -> list[dict]:
        events = await self.recent_events(["system.restarted"], since, limit=200, until=until)
        return [
            {
                "started_at": str(e.payload.get("started_at", "")),
                "gap_seconds": e.payload.get("gap_seconds"),
                "during_session": bool(e.payload.get("during_session")),
            }
            for e in reversed(events)
        ]

    async def bar_count_per_session(
        self, symbol: str, since: datetime, until: datetime
    ) -> dict[date, int]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT (ts AT TIME ZONE 'UTC')::date AS d, count(*) FROM bars WHERE symbol = %s AND interval = '1m' "
                "AND ts >= %s AND ts <= %s GROUP BY d",
                (symbol, since, until),
            )
            return {r[0]: int(r[1]) for r in await cur.fetchall()}

    async def fills_without_approval(self, since: datetime, until: datetime) -> int:
        """Fills whose intent never got a risk approval: must be zero, always."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(DISTINCT f.payload->>'intent_id') FROM events f WHERE f.type = 'order.filled' "
                "AND f.occurred_at >= %s AND f.occurred_at <= %s AND NOT EXISTS ("
                "SELECT 1 FROM events a WHERE a.type = 'risk.approved' AND a.payload->>'intent_id' = f.payload->>'intent_id')",
                (since, until),
            )
            return int((await cur.fetchone())[0])

    async def peak_equity(self) -> Decimal | None:
        """The highest equity any portfolio snapshot ever recorded: the drawdown ladder's peak."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT max((payload->>'equity')::numeric) FROM events WHERE type = 'portfolio.reconciled'"
            )
            row = await cur.fetchone()
            return Decimal(str(row[0])) if row and row[0] is not None else None

    async def earliest_bar_ts(self, interval: str) -> datetime | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT min(ts) FROM bars WHERE interval = %s", (interval,))
            row = await cur.fetchone()
            return row[0] if row else None

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

    async def known_order_intents(self, since: datetime) -> set[str]:
        """Intent ids this journal placed itself (an order.intent or order.submitted exists). The back
        office imports broker fills only for these, so orders from another run or account never leak in."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT DISTINCT payload->>'intent_id' FROM events WHERE type = ANY(%s) AND occurred_at >= %s",
                (["order.intent", "order.submitted"], since),
            )
            return {row[0] for row in await cur.fetchall() if row[0]}

    async def terminal_order_intents(self, since: datetime) -> set[str]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT DISTINCT payload->>'intent_id' FROM events WHERE type = ANY(%s) AND occurred_at >= %s",
                (["order.filled", "order.canceled", "order.rejected"], since),
            )
            return {row[0] for row in await cur.fetchall() if row[0]}
