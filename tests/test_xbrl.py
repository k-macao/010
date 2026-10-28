from __future__ import annotations

from pathlib import Path

import pytest

from qp.domain import StatementType
from qp.ingest.disclosure import DisclosureCalendar, SecurityMaster
from qp.ingest.hkexnews import HKEXnewsAdapter, infer_period_end
from qp.ingest.xbrl import parse_xbrl_instance

FIX = Path(__file__).resolve().parents[1] / "examples" / "fixtures"


def test_parse_xbrl_fixture():
    facts = parse_xbrl_instance(FIX / "hkexnews" / "00700" / "00700_2025_annual.xml")
    assert len(facts) >= 5
    by_concept = {f.concept: f for f in facts}
    ocf = by_concept["NetCashFlowsFromUsedInOperatingActivities"]
    assert ocf.value == pytest.approx(240_000_000_000)
    assert ocf.currency == "CNY"
    assert str(ocf.period_end) == "2025-12-31"
    assert ocf.canonical_label == "经营活动产生的现金流量净额"
    capex = by_concept["PaymentsToAcquirePropertyPlantAndEquipment"]
    assert capex.value == pytest.approx(-35_000_000_000)


def test_scale_and_sign_attributes(tmp_path):
    xml = """<?xml version="1.0"?>
    <xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:m="http://example.com/m">
      <xbrli:context id="c1"><xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period></xbrli:context>
      <xbrli:unit id="u"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
      <m:Cash contextRef="c1" unitRef="u" scale="6" sign="-" decimals="-6">123</m:Cash>
    </xbrli:xbrl>"""
    path = tmp_path / "x.xml"
    path.write_text(xml, encoding="utf-8")
    facts = parse_xbrl_instance(path)
    assert facts[0].value == pytest.approx(-123_000_000)
    assert facts[0].currency == "USD"


def test_hkexnews_adapter_reads_local_xbrl(config):
    adapter = HKEXnewsAdapter(
        config, SecurityMaster(config.resolve(config.sources.securities_file)),
        DisclosureCalendar(config.resolve(config.sources.fixtures_dir) / "announcements.csv"),
        local_dir=config.resolve(config.sources.fixtures_dir) / "hkexnews",
        enable_network=False,
    )
    reports = adapter.fetch("00700")
    assert reports
    assert {r.statement for r in reports} >= {StatementType.CF, StatementType.IS, StatementType.BS}
    assert all(r.source == "hkexnews" for r in reports)


def test_infer_period_end_from_filing_title():
    from datetime import datetime

    end, kind = infer_period_end("2025 Interim Report", datetime(2025, 8, 20))
    assert (str(end), kind) == ("2025-06-30", "INTERIM")
    end, kind = infer_period_end("Annual Report 2024", datetime(2025, 3, 20), fiscal_year_end="03-31")
    assert (str(end), kind) == ("2024-03-31", "ANNUAL")
