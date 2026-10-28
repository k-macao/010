from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from qp.domain import StatementType
from qp.ingest.akshare_hk import AkShareHKAdapter, call_akshare
from qp.ingest.disclosure import DisclosureCalendar, SecurityMaster
from qp.ingest.fixtures import FixtureAdapter, fixture_periods
from qp.ingest.hub import SourceHub
from qp.ingest.yfinance_hk import YFinanceHKAdapter

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "examples" / "fixtures"


@pytest.fixture
def master_calendar():
    master = SecurityMaster(ROOT / "config" / "securities.csv")
    calendar = DisclosureCalendar(FIX / "announcements.csv")
    return master, calendar


def test_akshare_signature_is_the_real_one():
    class FakeAk:
        def __init__(self):
            self.calls = []

        def stock_financial_hk_report_em(self, **kwargs):
            self.calls.append(kwargs)
            return pd.DataFrame([{
                "REPORT_DATE": "2025-12-31 00:00:00", "DATE_TYPE_CODE": "001",
                "STD_ITEM_NAME": "经营活动产生的现金流量净额", "STD_ITEM_CODE": "cf1",
                "AMOUNT": 240_000_000_000.0,
            }])

    ak = FakeAk()
    frame = call_akshare(ak, "700", StatementType.CF)
    assert ak.calls[0] == {"stock": "00700", "symbol": "现金流量表", "indicator": "年度"}
    assert len(frame) == 1


def test_akshare_adapter_uses_cny_and_real_disclosure_date(master_calendar):
    master, calendar = master_calendar

    class FakeAk:
        def stock_financial_hk_report_em(self, **kwargs):
            return pd.DataFrame([{
                "REPORT_DATE": "2025-12-31 00:00:00", "DATE_TYPE_CODE": "001",
                "STD_ITEM_NAME": "经营活动产生的现金流量净额", "AMOUNT": 240_000_000_000.0,
            }])

    adapter = AkShareHKAdapter(master, calendar, client=FakeAk())
    reports = adapter.fetch("00700", [StatementType.CF])
    assert len(reports) == 1
    report = reports[0]
    # Eastmoney converts ALL HK filers to CNY; must not use security.report_currency blindly.
    assert report.currency == "CNY"
    assert report.unit_scale == 1.0
    assert report.published_at == datetime(2026, 3, 25, 8)
    assert report.metadata["disclosure_origin"] == "hkexnews"


def test_fallback_to_legacy_signature():
    class LegacyAk:
        def stock_financial_hk_report_em(self, **kwargs):
            if "stock" in kwargs:
                raise TypeError("unexpected keyword 'stock'")
            return pd.DataFrame([{"REPORT_DATE": "2025-12-31", "STD_ITEM_NAME": "x", "AMOUNT": 1.0}])

    assert len(call_akshare(LegacyAk(), "00700", StatementType.CF)) == 1


def test_yfinance_adapter_transposes_and_uses_ticker_currency(master_calendar):
    master, calendar = master_calendar

    class FakeTicker:
        info = {"financialCurrency": "CNY"}
        cashflow = pd.DataFrame(
            [[240e9, 200e9], [-35e9, -30e9]],
            index=["Operating Cash Flow", "Capital Expenditure"],
            columns=[datetime(2025, 12, 31), datetime(2024, 12, 31)],
        )
        income_stmt = pd.DataFrame()
        balance_sheet = pd.DataFrame()

    class FakeYf:
        def Ticker(self, ticker):
            assert ticker == "0700.HK"
            return FakeTicker()

    reports = YFinanceHKAdapter(master, calendar, client=FakeYf()).fetch(
        "00700", [StatementType.CF]
    )
    assert len(reports) == 2
    assert all(r.currency == "CNY" and r.unit_scale == 1.0 for r in reports)
    assert all("pit_warning" in r.metadata for r in reports)


def test_fixture_adapter_reads_all_sources_and_versions():
    adapter = FixtureAdapter(FIX)
    reports = adapter.fetch("00700")
    assert {r.source for r in reports} == {"akshare", "yfinance"}
    assert fixture_periods(FIX, "00700") == [
        date(2023, 12, 31), date(2024, 12, 31), date(2025, 12, 31),
    ]
    cf_2024 = [r for r in reports if r.statement is StatementType.CF
               and r.period_end == date(2024, 12, 31) and r.source == "akshare"]
    assert len(cf_2024) == 2, "original + restated versions must both be present"


def test_source_hub_offline_includes_xbrl(config):
    hub = SourceHub(config)
    reports, events = hub.fetch("00700", offline=True)
    assert any(r.source == "hkexnews" for r in reports)
    assert events
    assert hub.note_documents("00700", offline=True)
