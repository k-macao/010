from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from qp.pit import detect_restatements, is_lookahead_free, select_latest_versions, visible_at


def _fact(day: str, available: str, value: float, field: str = "OCF", source: str = "akshare") -> dict:
    return {
        "symbol": "00700", "source": source, "statement": "CF",
        "period_end": date(2024, 12, 31), "field": field, "value": value,
        "value_native": value, "published_at": datetime.fromisoformat(day),
        "available_at": datetime.fromisoformat(available),
        "content_hash": f"h{int(value)}",
    }


def test_visible_at_excludes_future_versions():
    df = pd.DataFrame([
        _fact("2025-03-19T08:00:00", "2025-03-19T08:00:00", 200e9),
        _fact("2025-10-15T08:00:00", "2025-10-15T08:00:00", 198e9),
    ])
    before = visible_at(df, date(2025, 6, 30), lag_days=1)
    after = visible_at(df, date(2025, 12, 31), lag_days=1)
    assert len(before) == 1 and before.iloc[0]["value"] == 200e9
    assert len(after) == 2
    assert is_lookahead_free(df, date(2025, 6, 30), lag_days=1)


def test_select_latest_versions_and_restatement_detection():
    df = pd.DataFrame([
        _fact("2025-03-19T08:00:00", "2025-03-19T08:00:00", 200e9),
        _fact("2025-10-15T08:00:00", "2025-10-15T08:00:00", 198e9),
        _fact("2026-03-25T08:00:00", "2026-03-25T08:00:00", 240e9, field="CAPEX"),
    ])
    latest = select_latest_versions(visible_at(df, date(2026, 6, 30)))
    assert len(latest) == 2
    assert latest.set_index("field").loc["OCF", "value"] == 198e9
    events = detect_restatements(df)
    assert len(events) == 1
    assert events[0].old_value == 200e9 and events[0].new_value == 198e9


def test_lag_delays_availability():
    df = pd.DataFrame([_fact("2025-03-19T08:00:00", "2025-03-19T08:00:00", 200e9)])
    assert len(visible_at(df, date(2025, 3, 19), lag_days=1)) == 0
    assert len(visible_at(df, date(2025, 3, 20), lag_days=1)) == 1
