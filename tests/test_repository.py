from __future__ import annotations

from datetime import date, datetime

import pytest

from qp.storage.repository import Repository, build_upsert
from qp.storage.schema import normalized_facts
from qp.util import utcnow


def _fact_row(value: float, available: datetime, source: str = "akshare") -> dict:
    return {
        "symbol": "00700", "source": source, "statement": "CF",
        "period_end": date(2024, 12, 31), "published_at": available,
        "available_at": available, "field": "OCF", "value": value,
        "value_native": value, "currency": "HKD", "report_currency": "CNY",
        "fx_rate": 1.08, "quality": "[]", "ingested_at": utcnow(),
    }


def test_upsert_is_idempotent_and_keeps_versions(config):
    store = Repository(config.database.url)
    store.init_schema()
    t1, t2 = datetime(2025, 3, 19, 8), datetime(2025, 10, 15, 8)
    row = _fact_row(200e9, t1)
    assert store.upsert("normalized_facts", [row]) == 1
    assert store.upsert("normalized_facts", [row]) == 1
    assert store.upsert("normalized_facts", [_fact_row(198e9, t2)]) == 1
    all_rows = store.get_all_facts("00700")
    assert len(all_rows) == 2, "same PK → update; new available_at → new version"
    assert store.known_available_at("00700", "akshare", "CF", date(2024, 12, 31), "abc") is None


def test_get_facts_filters_by_pit_clock(config):
    store = Repository(config.database.url)
    store.init_schema()
    store.upsert("normalized_facts", [
        _fact_row(200e9, datetime(2025, 3, 19, 8)),
        _fact_row(198e9, datetime(2025, 10, 15, 8)),
    ])
    assert len(store.get_facts("00700", date(2025, 6, 30))) == 1
    assert len(store.get_facts("00700", date(2025, 12, 31))) == 2
    assert len(store.get_all_facts("00700")) == 2


def test_market_cap_and_features_roundtrip(config):
    store = Repository(config.database.url)
    store.init_schema()
    store.upsert("market_caps", [
        {"symbol": "00700", "as_of": date(2025, 6, 30), "source": "fixture",
         "market_cap": 3.4e12, "currency": "HKD"},
        {"symbol": "00700", "as_of": date(2026, 6, 30), "source": "fixture",
         "market_cap": 3.8e12, "currency": "HKD"},
    ])
    assert store.get_market_cap("00700", date(2026, 1, 1)) == (3.4e12, "HKD", date(2025, 6, 30))
    assert store.get_market_cap("00700", date(2024, 1, 1)) is None
    store.upsert("derived_features", [{
        "symbol": "00700", "as_of": date(2026, 10, 27), "feature": "fcf_yield",
        "formula_version": "fcf-v1", "period_end": date(2025, 12, 31),
        "value": 0.06, "currency": "HKD", "unit": "ratio", "inputs": "{}",
        "computed_at": utcnow(),
    }])
    features = store.get_features("00700", date(2026, 10, 27))
    assert len(features) == 1 and features.iloc[0]["value"] == pytest.approx(0.06)


def test_reference_data_and_quality_events(config):
    store = Repository(config.database.url)
    store.init_schema()
    counts = store.load_reference_data(config.resolve(config.sources.fixtures_dir))
    assert counts["fx_rates"] > 0 and counts["market_caps"] > 0
    events = [{"run_id": "r1", "symbol": "00700", "severity": "WARN",
               "check": "test", "detail": "x", "as_of": date(2026, 1, 1)}]
    store.insert_quality_events("r1", events)
    store.insert_quality_events("r1", events)  # replace, never duplicate
    assert len(store.get_quality_events("r1")) == 1


def test_upsert_sql_compiles_for_postgres_and_sqlite():
    from sqlalchemy.dialects import postgresql, sqlite

    pk = [c.name for c in normalized_facts.primary_key.columns]
    row = _fact_row(1.0, datetime(2025, 3, 19, 8))
    prepared = [Repository._coerce("normalized_facts", row)]
    for dialect_name, dialect in (("postgresql", postgresql.dialect()), ("sqlite", sqlite.dialect())):
        stmt = build_upsert(normalized_facts, prepared, pk, dialect_name)
        sql = str(stmt.compile(dialect=dialect))
        assert "ON CONFLICT" in sql.upper()
        assert "DO UPDATE" in sql.upper()
