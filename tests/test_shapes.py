from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from qp.errors import SchemaDriftError
from qp.ingest.shapes import to_long
from qp.util import parse_number


def test_akshare_long_shape():
    df = pd.DataFrame([
        {"REPORT_DATE": "2025-12-31 00:00:00", "DATE_TYPE_CODE": "001",
         "STD_ITEM_NAME": "经营活动产生的现金流量净额", "STD_ITEM_CODE": "x1", "AMOUNT": 240.0},
        {"REPORT_DATE": "2025-06-30 00:00:00", "DATE_TYPE_CODE": "002",
         "STD_ITEM_NAME": "经营活动产生的现金流量净额", "STD_ITEM_CODE": "x1", "AMOUNT": 100.0},
    ])
    out = to_long(df)
    assert len(out) == 2
    assert set(out["period_type"]) == {"ANNUAL", "INTERIM"}
    assert out.loc[0, "raw_label"] == "经营活动产生的现金流量净额"
    assert out.loc[0, "raw_code"] == "x1"


def test_user_sketch_wide_shape():
    df = pd.DataFrame({
        "报告期": ["2024-12-31", "2025-12-31"],
        "经营活动产生的现金流量净额": [200.0, 240.0],
        "购建固定资产、无形资产和其他长期资产支付的现金": [30.0, 35.0],
    })
    out = to_long(df)
    assert len(out) == 4
    assert set(out["raw_label"]) == {
        "经营活动产生的现金流量净额", "购建固定资产、无形资产和其他长期资产支付的现金",
    }


def test_yfinance_transposed_shape():
    df = pd.DataFrame(
        [[240.0, 200.0], [-35.0, -30.0]],
        index=["Operating Cash Flow", "Capital Expenditure"],
        columns=[datetime(2025, 12, 31), datetime(2024, 12, 31)],
    )
    out = to_long(df)
    assert len(out) == 4
    assert out["value"].min() == -35.0
    assert {str(x) for x in out["period_end"]} == {"2025-12-31", "2024-12-31"}


def test_label_column_plus_date_columns():
    df = pd.DataFrame({
        "raw_label": ["Operating Cash Flow", "Capital Expenditure"],
        "2025-12-31": [240.0, -35.0],
        "2024-12-31": [200.0, -30.0],
    })
    out = to_long(df)
    assert len(out) == 4
    assert out.query("raw_label == 'Capital Expenditure'")["value"].tolist() == [-35.0, -30.0]


def test_wide_without_period_requires_default():
    df = pd.DataFrame({"OCF": [1.0], "CAPEX": [0.2]})
    with pytest.raises(SchemaDriftError):
        to_long(df)
    assert len(to_long(df, default_period_end=datetime(2025, 12, 31))) == 2


@pytest.mark.parametrize("raw,expected", [
    ("(1,234)", -1234.0), ("1.2亿", 1.2e8), ("—", None), ("", None),
    ("1 234.5", 1234.5), (42, 42.0), ("N/A", None),
])
def test_parse_number(raw, expected):
    assert parse_number(raw) == expected
