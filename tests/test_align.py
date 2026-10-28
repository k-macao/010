from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from qp.clean.align import MultiSourceAligner, choose_period


def _row(source: str, field: str, value: float, period: str = "2025-12-31") -> dict:
    return {
        "symbol": "00700", "source": source, "statement": "CF",
        "period_end": period, "period_type": "ANNUAL", "field": field,
        "value": value, "value_native": value, "currency": "HKD",
        "report_currency": "CNY", "fx_rate": 1.09, "fx_as_of": "2025-12-01",
        "published_at": datetime(2026, 3, 25, 8), "available_at": datetime(2026, 3, 25, 8),
        "quality": "[]",
    }


def test_priority_and_tolerance():
    df = pd.DataFrame([
        _row("akshare", "OCF", 100.0),
        _row("hkexnews", "OCF", 100.5),   # 0.5% → within tolerance
    ])
    result = MultiSourceAligner(priority=("hkexnews", "akshare", "yfinance"),
                                conflict_rel_tolerance=0.01).align(
        df, "00700", date(2025, 12, 31), date(2026, 10, 27)
    )
    fact = result.report.get("OCF")
    assert fact == 100.5
    assert result.conflicts == []
    assert result.report.facts["OCF"].sources[0] == "hkexnews"


def test_large_conflict_flagged_and_quarantined():
    df = pd.DataFrame([
        _row("hkexnews", "OCF", 100.0),
        _row("yfinance", "OCF", 130.0),
    ])
    loose = MultiSourceAligner().align(df, "00700", date(2025, 12, 31), date(2026, 10, 27))
    assert loose.conflicts and loose.report.get("OCF") == 100.0
    strict = MultiSourceAligner(quarantine_on_conflict=True).align(
        df, "00700", date(2025, 12, 31), date(2026, 10, 27)
    )
    assert strict.report.get("OCF") is None
    assert strict.quarantined_fields == ["OCF"]


def test_capex_sign_convention_is_not_a_conflict():
    df = pd.DataFrame([
        _row("hkexnews", "CAPEX", -35.0),
        _row("akshare", "CAPEX", 35.0),
    ])
    result = MultiSourceAligner().align(df, "00700", date(2025, 12, 31), date(2026, 10, 27))
    assert result.conflicts == []
    assert result.report.get("CAPEX") == -35.0


def test_fallback_source_fills_missing_field():
    df = pd.DataFrame([
        _row("hkexnews", "OCF", 100.0),
        _row("yfinance", "REVENUE", 500.0),
    ])
    result = MultiSourceAligner().align(df, "00700", date(2025, 12, 31), date(2026, 10, 27))
    assert result.report.get("REVENUE") == 500.0
    assert "REVENUE" in result.fallback_fields


def test_choose_period_prefers_latest_complete_and_checks_staleness():
    df = pd.DataFrame([
        _row("akshare", "OCF", 100.0, "2024-12-31"),
        _row("akshare", "CAPEX", 20.0, "2024-12-31"),
        _row("akshare", "OCF", 120.0, "2025-12-31"),  # incomplete: no CAPEX
    ])
    period, reason = choose_period(df, date(2026, 10, 27), max_staleness_days=800)
    assert period == date(2024, 12, 31) and reason == ""
    period, reason = choose_period(df, date(2030, 1, 1), max_staleness_days=550)
    assert period is None and "stale" in reason
