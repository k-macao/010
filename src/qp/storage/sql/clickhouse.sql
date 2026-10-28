-- ClickHouse factor mirror. ReplacingMergeTree makes re-runs idempotent; the view
-- resolves the newest version per key (equivalent to PostgreSQL's pit_facts view).

CREATE DATABASE IF NOT EXISTS quant;

CREATE TABLE IF NOT EXISTS quant.factors
(
    symbol          LowCardinality(String),
    as_of           Date,
    period_end      Date,
    feature         LowCardinality(String),
    value           Float64,
    currency        LowCardinality(String),
    unit            LowCardinality(String),
    formula_version LowCardinality(String),
    run_id          String,
    computed_at     DateTime,
    version         DateTime
)
ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(as_of)
ORDER BY (as_of, feature, symbol, formula_version, period_end);

CREATE TABLE IF NOT EXISTS quant.normalized_facts
(
    symbol          LowCardinality(String),
    source          LowCardinality(String),
    statement       LowCardinality(String),
    period_end      Date,
    published_at    DateTime,
    available_at    DateTime,
    field           LowCardinality(String),
    value           Float64,
    value_native    Float64,
    currency        LowCardinality(String),
    report_currency LowCardinality(String),
    fx_rate         Float64,
    fx_as_of        Date,
    quality         String,
    run_id          String,
    version         DateTime
)
ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYY(period_end)
ORDER BY (symbol, period_end, field, source, available_at);

CREATE OR REPLACE VIEW quant.pit_factors AS
SELECT
    symbol, as_of, period_end, feature, formula_version,
    argMax(value, version)           AS value,
    argMax(currency, version)        AS currency,
    argMax(unit, version)            AS unit
FROM quant.factors
GROUP BY symbol, as_of, period_end, feature, formula_version;
