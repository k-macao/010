"""Relational schema (portable SQLite/PostgreSQL).

Design notes:

* Every fact row carries BOTH ``published_at`` (filing clock) and
  ``available_at`` (when this version became knowable to us). Restatements are
  NEW ROWS, never UPDATEs of the original — that is what makes PIT possible.
* ``available_at`` is part of the primary key: a re-discovered/re-stated version
  is archived with a new timestamp instead of overwriting history.
* ``raw_reports`` keeps the untouched vendor payload for replay/audit.
* ``derived_features`` is keyed by (symbol, as_of, feature, formula_version) so a
  formula change creates a new series rather than silently rewriting the old one.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)

metadata = MetaData()

raw_reports = Table(
    "raw_reports", metadata,
    Column("symbol", String(16), primary_key=True),
    Column("source", String(32), primary_key=True),
    Column("statement", String(4), primary_key=True),
    Column("period_end", Date, primary_key=True),
    Column("published_at", DateTime, primary_key=True),
    Column("available_at", DateTime, primary_key=True),
    Column("period_type", String(16), nullable=False, default="ANNUAL"),
    Column("currency", String(8), nullable=False),
    Column("unit_scale", Float, nullable=False, default=1.0),
    Column("unit_label", Text, default=""),
    Column("content_hash", String(64), nullable=False, index=True),
    Column("payload", Text, default=""),
    Column("metadata", Text, default=""),
    Column("run_id", String(64), default=""),
    Column("ingested_at", DateTime, nullable=False),
)

normalized_facts = Table(
    "normalized_facts", metadata,
    Column("symbol", String(16), primary_key=True),
    Column("source", String(32), primary_key=True),
    Column("statement", String(4), primary_key=True),
    Column("period_end", Date, primary_key=True),
    Column("published_at", DateTime, primary_key=True),
    Column("available_at", DateTime, primary_key=True),
    Column("field", String(64), primary_key=True),
    Column("period_type", String(16), default="ANNUAL"),
    Column("value", Float),
    Column("value_native", Float),
    Column("currency", String(8)),
    Column("report_currency", String(8)),
    Column("raw_value", Float),
    Column("raw_label", Text),
    Column("unit_scale", Float, default=1.0),
    Column("fx_rate", Float, default=1.0),
    Column("fx_as_of", Date),
    Column("fx_source", String(32), default=""),
    Column("sign", String(16), default="flow"),
    Column("quality", Text, default="[]"),
    Column("content_hash", String(64), default=""),
    Column("ingested_at", DateTime),
    Column("run_id", String(64), default=""),
)

nonrecurring_items = Table(
    "nonrecurring_items", metadata,
    Column("symbol", String(16), primary_key=True),
    Column("period_end", Date, primary_key=True),
    Column("published_at", DateTime, primary_key=True),
    Column("available_at", DateTime, primary_key=True),
    Column("category", String(64), primary_key=True),
    Column("label", String(255), primary_key=True),
    Column("source_kind", String(16), primary_key=True),
    Column("amount", Float),
    Column("profit_impact", Float),
    Column("direction", String(8)),
    Column("evidence", Text, default=""),
    Column("confidence", Float, default=0.0),
    Column("currency", String(8)),
    Column("cash_impact", Boolean, default=False),
    Column("corroborated", Boolean, default=False),
    Column("page", Integer),
    Column("raw_amount", Float),
    Column("raw_currency", String(8)),
    Column("run_id", String(64), default=""),
)

fx_rates = Table(
    "fx_rates", metadata,
    Column("base", String(8), primary_key=True),
    Column("quote", String(8), primary_key=True),
    Column("as_of", Date, primary_key=True),
    Column("source", String(32), primary_key=True),
    Column("rate", Float, nullable=False),
)

market_caps = Table(
    "market_caps", metadata,
    Column("symbol", String(16), primary_key=True),
    Column("as_of", Date, primary_key=True),
    Column("source", String(32), primary_key=True),
    Column("market_cap", Float, nullable=False),
    Column("currency", String(8), nullable=False),
)

derived_features = Table(
    "derived_features", metadata,
    Column("symbol", String(16), primary_key=True),
    Column("as_of", Date, primary_key=True),
    Column("feature", String(64), primary_key=True),
    Column("formula_version", String(32), primary_key=True),
    Column("period_end", Date),
    Column("value", Float),
    Column("currency", String(8)),
    Column("unit", String(16)),
    Column("inputs", Text, default="{}"),
    Column("computed_at", DateTime),
    Column("run_id", String(64), default=""),
)

quality_events = Table(
    "quality_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(64), index=True),
    Column("symbol", String(16)),
    Column("severity", String(8)),
    Column("check_name", String(64)),
    Column("detail", Text),
    Column("period_end", Date),
    Column("as_of", Date),
    Column("created_at", DateTime),
)

pipeline_runs = Table(
    "pipeline_runs", metadata,
    Column("run_id", String(64), primary_key=True),
    Column("started_at", DateTime),
    Column("finished_at", DateTime),
    Column("as_of", Date),
    Column("status", String(16)),
    Column("config_hash", String(64)),
    Column("summary", Text, default="{}"),
)

TABLES = {
    "raw_reports": raw_reports,
    "normalized_facts": normalized_facts,
    "nonrecurring_items": nonrecurring_items,
    "fx_rates": fx_rates,
    "market_caps": market_caps,
    "derived_features": derived_features,
    "quality_events": quality_events,
    "pipeline_runs": pipeline_runs,
}
