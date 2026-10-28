from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from qp.clean.normalize import Normalizer
from qp.errors import MissingFxRate
from qp.ingest.fx import FxRateProvider, FxRateRow


@pytest.fixture
def fx() -> FxRateProvider:
    return FxRateProvider(
        rows=[
            FxRateRow("HKD", "CNY", date(2024, 1, 1), 1.08, "fixture"),
            FxRateRow("HKD", "CNY", date(2025, 1, 1), 1.10, "fixture"),
        ],
        target_currency="HKD", max_age_days=45,
    )


def test_fx_is_point_in_time(fx):
    assert fx.rate("CNY", date(2024, 6, 30)).rate == 1.08
    assert fx.rate("CNY", date(2026, 6, 30)).rate == 1.10
    assert fx.rate("HKD", date(2026, 6, 30)).rate == 1.0


def test_fx_future_rate_never_leaks(fx):
    with pytest.raises(MissingFxRate):
        fx.rate("USD", date(2026, 1, 1))
    fallback = FxRateProvider(rows=[], fallback_rates={"USD": 7.8})
    quote = fallback.rate("USD", date(2026, 1, 1))
    assert quote.is_fallback and quote.rate == 7.8


def test_fallback_rate_is_never_used_for_the_wrong_base():
    provider = FxRateProvider(rows=[], target_currency="HKD",
                              fallback_rates={"CNY": 1.09, "CNY_USD": 0.14})
    assert provider.rate("CNY", date(2026, 1, 1), base="HKD").rate == 1.09
    assert provider.rate("CNY", date(2026, 1, 1), base="USD").rate == 0.14
    with pytest.raises(MissingFxRate):
        provider.rate("EUR", date(2026, 1, 1), base="USD")


def test_boc_sina_direction():
    df = pd.DataFrame({"日期": ["2026-07-22"], "央行中间价": [86.636]})
    rows = FxRateProvider.from_boc_sina(df)
    assert len(rows) == 1
    assert rows[0].quote == "CNY" and rows[0].base == "HKD"
    assert rows[0].rate == pytest.approx(100 / 86.636)


def test_normalizer_unit_currency_and_field_guard(raw_cf_report, fx):
    norm = Normalizer(fx=fx, target_currency="HKD").normalize(raw_cf_report)
    fields = {f.field: f for f in norm.facts}
    assert set(fields) == {"OCF", "CAPEX"}, "bare 固定资产 must not become CAPEX"
    ocf = fields["OCF"]
    assert ocf.value_native == pytest.approx(240_000_000 * 1000)
    assert ocf.value == pytest.approx(240_000_000 * 1000 * 1.10)
    assert ocf.currency == "HKD" and ocf.report_currency == "CNY"
    assert ocf.fx_as_of == date(2025, 1, 1)
    assert "固定资产" in norm.unmapped_labels


def test_normalizer_traditional_labels(raw_cf_report, fx):
    raw_cf_report.data.loc[0, "raw_label"] = "經營業務所得現金淨額"
    result = Normalizer(fx=fx).normalize(raw_cf_report)
    assert any(f.field == "OCF" for f in result.facts)


def test_normalizer_flags_missing_fx(raw_cf_report):
    strict_fx = FxRateProvider(rows=[], strict=True)
    result = Normalizer(fx=strict_fx, target_currency="HKD").normalize(raw_cf_report)
    assert result.facts
    assert all("fx_missing" in f.quality for f in result.facts)
    assert any("FX missing" in w for w in result.warnings)
