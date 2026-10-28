from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from qp.ingest.disclosure import DisclosureCalendar, SecurityMaster

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "examples" / "fixtures"


def test_security_master_currency_red_line():
    master = SecurityMaster(ROOT / "config" / "securities.csv")
    tencent = master.get("00700")
    assert tencent.report_currency == "CNY"
    assert tencent.akshare_currency == "CNY"
    assert tencent.ticker == "0700.HK"
    hsbc = master.get("00005")
    assert hsbc.report_currency == "USD"
    assert hsbc.akshare_currency == "CNY", "AkShare AMOUNT is CNY even for USD filers"
    assert master.get("12345").report_currency == "HKD"


def test_calendar_exact_dates_and_restatement_versions():
    calendar = DisclosureCalendar(FIX / "announcements.csv")
    resolved = calendar.resolve("00700", date(2024, 12, 31), "ANNUAL", source="akshare")
    assert not resolved.is_estimated
    assert resolved.published_at == datetime(2025, 10, 15, 8)
    versions = calendar.versions_for("00700", date(2024, 12, 31))
    assert len(versions) == 2
    assert versions[0].published_at < versions[1].published_at
    assert versions[1].is_restatement


def test_calendar_conservative_estimate_is_flagged():
    calendar = DisclosureCalendar(path=None, lag_days={"ANNUAL": 90})
    resolved = calendar.resolve("09999", date(2025, 12, 31), "ANNUAL")
    assert resolved.is_estimated
    assert resolved.origin == "lag_estimate"
    assert resolved.published_at == datetime(2026, 3, 31)
