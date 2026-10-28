-- Production PostgreSQL schema for qp-hk-pipeline.
-- The application creates the same tables via SQLAlchemy (`qp init-db`); this file
-- exists for DBA-managed deployments, partitioning and indexes. All timestamps are
-- stored as TIMESTAMPTZ by the application in UTC (naive UTC on SQLite).

CREATE TABLE IF NOT EXISTS raw_reports (
    symbol        TEXT        NOT NULL,
    source        TEXT        NOT NULL,
    statement     TEXT        NOT NULL,
    period_end    DATE        NOT NULL,
    published_at  TIMESTAMPTZ NOT NULL,
    available_at  TIMESTAMPTZ NOT NULL,
    period_type   TEXT        NOT NULL DEFAULT 'ANNUAL',
    currency      TEXT        NOT NULL,
    unit_scale    DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    unit_label    TEXT,
    content_hash  TEXT        NOT NULL,
    payload       TEXT,
    metadata      TEXT,
    run_id        TEXT,
    ingested_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (symbol, source, statement, period_end, published_at, available_at)
);
CREATE INDEX IF NOT EXISTS ix_raw_reports_hash ON raw_reports (content_hash);
CREATE INDEX IF NOT EXISTS ix_raw_reports_pit  ON raw_reports (symbol, available_at);

CREATE TABLE IF NOT EXISTS normalized_facts (
    symbol          TEXT        NOT NULL,
    source          TEXT        NOT NULL,
    statement       TEXT        NOT NULL,
    period_end      DATE        NOT NULL,
    published_at    TIMESTAMPTZ NOT NULL,
    available_at    TIMESTAMPTZ NOT NULL,
    field           TEXT        NOT NULL,
    period_type     TEXT,
    value           DOUBLE PRECISION,
    value_native    DOUBLE PRECISION,
    currency        TEXT,
    report_currency TEXT,
    raw_value       DOUBLE PRECISION,
    raw_label       TEXT,
    unit_scale      DOUBLE PRECISION DEFAULT 1.0,
    fx_rate         DOUBLE PRECISION DEFAULT 1.0,
    fx_as_of        DATE,
    fx_source       TEXT,
    sign            TEXT,
    quality         TEXT,
    content_hash    TEXT,
    ingested_at     TIMESTAMPTZ,
    run_id          TEXT,
    PRIMARY KEY (symbol, source, statement, period_end, published_at, available_at, field)
);
CREATE INDEX IF NOT EXISTS ix_facts_pit ON normalized_facts (symbol, available_at, period_end, field);

CREATE TABLE IF NOT EXISTS nonrecurring_items (
    symbol        TEXT        NOT NULL,
    period_end    DATE        NOT NULL,
    published_at  TIMESTAMPTZ NOT NULL,
    available_at  TIMESTAMPTZ NOT NULL,
    category      TEXT        NOT NULL,
    label         TEXT        NOT NULL,
    source_kind   TEXT        NOT NULL,
    amount        DOUBLE PRECISION,
    profit_impact DOUBLE PRECISION,
    direction     TEXT,
    evidence      TEXT,
    confidence    DOUBLE PRECISION,
    currency      TEXT,
    cash_impact   BOOLEAN     DEFAULT FALSE,
    corroborated  BOOLEAN     DEFAULT FALSE,
    page          INTEGER,
    raw_amount    DOUBLE PRECISION,
    raw_currency  TEXT,
    run_id        TEXT,
    PRIMARY KEY (symbol, period_end, published_at, available_at, category, label, source_kind)
);

CREATE TABLE IF NOT EXISTS fx_rates (
    base   TEXT NOT NULL,
    quote  TEXT NOT NULL,
    as_of  DATE NOT NULL,
    source TEXT NOT NULL,
    rate   DOUBLE PRECISION NOT NULL CHECK (rate > 0),
    PRIMARY KEY (base, quote, as_of, source)
);

CREATE TABLE IF NOT EXISTS market_caps (
    symbol     TEXT NOT NULL,
    as_of      DATE NOT NULL,
    source     TEXT NOT NULL,
    market_cap DOUBLE PRECISION NOT NULL,
    currency   TEXT NOT NULL,
    PRIMARY KEY (symbol, as_of, source)
);

CREATE TABLE IF NOT EXISTS derived_features (
    symbol          TEXT NOT NULL,
    as_of           DATE NOT NULL,
    feature         TEXT NOT NULL,
    formula_version TEXT NOT NULL,
    period_end      DATE,
    value           DOUBLE PRECISION,
    currency        TEXT,
    unit            TEXT,
    inputs          TEXT,
    computed_at     TIMESTAMPTZ,
    run_id          TEXT,
    PRIMARY KEY (symbol, as_of, feature, formula_version)
);
CREATE INDEX IF NOT EXISTS ix_features_lookup ON derived_features (as_of, feature, symbol);

CREATE TABLE IF NOT EXISTS quality_events (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT,
    symbol      TEXT,
    severity    TEXT,
    check_name  TEXT,
    detail      TEXT,
    period_end  DATE,
    as_of       DATE,
    created_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_quality_run ON quality_events (run_id, severity);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    as_of       DATE,
    status      TEXT,
    config_hash TEXT,
    summary     TEXT
);

-- PIT view: for each (symbol, source, statement, period, field), the newest version
-- visible at a given as_of. Backtests MUST join through this view (or its equivalent
-- window function), never directly against normalized_facts.
CREATE OR REPLACE VIEW pit_facts AS
SELECT DISTINCT ON (symbol, source, statement, period_end, field)
       symbol, source, statement, period_end, field, value, value_native, currency,
       report_currency, published_at, available_at, fx_rate, fx_as_of, quality
FROM normalized_facts
ORDER BY symbol, source, statement, period_end, field, available_at DESC, published_at DESC;
