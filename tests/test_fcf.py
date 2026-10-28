from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from qp.clean.fcf import FCFEngine, calculate_standard_fcf, find_columns
from qp.domain import CAPEX, OCF, AlignedFact, AlignedReport, NonRecurringItem


def test_user_sketch_function_with_bs_trap_column():
    df = pd.DataFrame({
        "报告期": ["2024-12-31", "2025-12-31"],
        "固定资产": [130e9, 150e9],   # decoy from the balance sheet
        "经营活动产生的现金流量净额": [200e9, 240e9],
        "购建固定资产、无形资产和其他长期资产支付的现金": [30e9, 35e9],
    })
    ocf_col, capex_col = find_columns(df)
    assert ocf_col == "经营活动产生的现金流量净额"
    assert "购建" in capex_col
    out = calculate_standard_fcf(df)
    assert out["Standard_FCF"].tolist() == [170e9, 205e9]
    assert out["CapEx_Magnitude"].tolist() == [30e9, 35e9]


def test_signed_and_negative_capex():
    df = pd.DataFrame({
        "报告期": ["2025-12-31"],
        "经营活动产生的现金流量净额": [240e9],
        "Capital Expenditure": [-35e9],   # yfinance / XBRL sign convention
    })
    out = calculate_standard_fcf(df)
    assert out["Standard_FCF"].iloc[0] == 205e9
    signed = calculate_standard_fcf(df, capex_sign_policy="signed")
    assert signed["Standard_FCF"].iloc[0] == 205e9


def test_missing_fields_raise_clear_error():
    df = pd.DataFrame({"报告期": ["2025-12-31"], "固定资产": [150e9]})
    with pytest.raises(ValueError, match="未能在现金流量表中匹配到关键字段"):
        calculate_standard_fcf(df)


def _aligned(capex_raw: float = 35e9) -> AlignedReport:
    def fact(field, value, native):
        return AlignedFact(
            symbol="00700", statement="CF", period_end=date(2025, 12, 31),
            period_type="ANNUAL", field=field, value=value, value_native=native,
            currency="HKD", report_currency="CNY", fx_rate=1.09,
            fx_as_of=date(2025, 12, 1), sources=("hkexnews",),
            published_at=datetime(2026, 3, 25), available_at=datetime(2026, 3, 25),
        )
    return AlignedReport(
        symbol="00700", period_end=date(2025, 12, 31), period_type="ANNUAL",
        as_of=date(2026, 10, 27),
        facts={OCF: fact(OCF, 261.6e9, 240e9), CAPEX: fact(CAPEX, capex_raw, capex_raw / 1.09)},
    )


def test_engine_sign_policies():
    engine = FCFEngine("auto")
    result = engine.compute(_aligned(capex_raw=-38.15e9))
    assert result.capex == pytest.approx(38.15e9)
    assert result.standard_fcf == pytest.approx(223.45e9)
    assert result.policy == "auto"
    signed = FCFEngine("signed").compute(_aligned(capex_raw=-38.15e9))
    assert signed.standard_fcf == pytest.approx(223.45e9)


def test_engine_flags_implausible_ratio():
    result = FCFEngine("auto").compute(_aligned(capex_raw=3000e9))
    assert "capex_ocf_ratio_implausible" in result.quality


def test_engine_adjusts_oneoff_cash_items():
    item = NonRecurringItem(
        symbol="00700", period_end=date(2025, 12, 31), category="LITIGATION_SETTLEMENT",
        label="一次性诉讼收款", amount=10e9, profit_impact=10e9, direction="gain",
        source_kind="llm", evidence="...", confidence=0.8, currency="HKD",
        published_at=datetime(2026, 3, 25), available_at=datetime(2026, 3, 25),
        cash_impact=True,
    )
    result = FCFEngine("auto").compute(_aligned(), [item])
    assert result.oneoff_cash_impact == pytest.approx(10e9)
    assert result.adjusted_fcf == pytest.approx(result.standard_fcf - 10e9)
