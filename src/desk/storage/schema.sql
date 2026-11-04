-- Phase 1 schema on plain PostgreSQL 14 (decision D9). TimescaleDB can replace the partitioning
-- later without changing any query. Everything here is idempotent.

CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT        NOT NULL,
    type            TEXT        NOT NULL,
    schema_version  INT         NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL,
    persisted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    symbol          TEXT,
    book            TEXT,
    correlation_id  TEXT,
    causation_id    TEXT,
    source          TEXT        NOT NULL,
    payload         JSONB       NOT NULL,
    PRIMARY KEY (event_id, occurred_at)
) PARTITION BY RANGE (occurred_at);

CREATE TABLE IF NOT EXISTS events_default PARTITION OF events DEFAULT;
CREATE INDEX IF NOT EXISTS events_symbol_time ON events (symbol, occurred_at);
CREATE INDEX IF NOT EXISTS events_type_time   ON events (type, occurred_at);
CREATE INDEX IF NOT EXISTS events_correlation ON events (correlation_id);

CREATE TABLE IF NOT EXISTS bars (
    symbol       TEXT        NOT NULL,
    interval     TEXT        NOT NULL,
    ts           TIMESTAMPTZ NOT NULL,
    open         NUMERIC     NOT NULL,
    high         NUMERIC     NOT NULL,
    low          NUMERIC     NOT NULL,
    close        NUMERIC     NOT NULL,
    volume       BIGINT      NOT NULL,
    vwap         NUMERIC,
    trade_count  INT,
    source       TEXT        NOT NULL,
    ingested_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (symbol, interval, ts)
);

CREATE TABLE IF NOT EXISTS theses (
    thesis_id            TEXT PRIMARY KEY,
    symbol               TEXT        NOT NULL,
    book                 TEXT        NOT NULL,
    direction            TEXT        NOT NULL,
    status               TEXT        NOT NULL,
    cycle_id             TEXT        NOT NULL,
    narrative            TEXT        NOT NULL,
    evidence_ids         TEXT[]      NOT NULL DEFAULT '{}',
    expected_return_pct  NUMERIC     NOT NULL,
    horizon_days         INT         NOT NULL,
    conviction           NUMERIC     NOT NULL,
    quantity             NUMERIC     NOT NULL,
    entry_price          NUMERIC,
    stop_price           NUMERIC,
    target_price         NUMERIC,
    stop_pct             NUMERIC     NOT NULL,
    target_pct           NUMERIC     NOT NULL,
    invalidation         JSONB       NOT NULL DEFAULT '[]',
    size_rationale       TEXT        NOT NULL DEFAULT '',
    reason               TEXT        NOT NULL DEFAULT '',
    created_at           TIMESTAMPTZ NOT NULL,
    updated_at           TIMESTAMPTZ NOT NULL,
    closed_at            TIMESTAMPTZ,
    realized_pnl         NUMERIC
);
CREATE INDEX IF NOT EXISTS theses_symbol_status ON theses (symbol, status);

-- Structured model outputs keyed by a hash of the whole prompt (replays rerun for free)
CREATE TABLE IF NOT EXISTS llm_cache (
    key        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    model      TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    response   JSONB NOT NULL
);

-- The feature store: point-in-time features per name and session, targets kept apart
CREATE TABLE IF NOT EXISTS features_daily (
    symbol      TEXT        NOT NULL,
    session     DATE        NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL,
    features    JSONB       NOT NULL,
    targets     JSONB       NOT NULL,
    PRIMARY KEY (symbol, session)
);

-- Trained models and their out-of-sample record; one champion per kind and horizon
CREATE TABLE IF NOT EXISTS models (
    model_id        TEXT PRIMARY KEY,
    kind            TEXT        NOT NULL,
    horizon         INT         NOT NULL,
    trained_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    trained_through DATE        NOT NULL,
    path            TEXT        NOT NULL,
    metrics         JSONB       NOT NULL,
    status          TEXT        NOT NULL,
    note            TEXT        NOT NULL DEFAULT ''
);
