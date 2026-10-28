from __future__ import annotations

from datetime import date, datetime

import pytest

from qp.clean.notes.engine import NonRecurringEngine, drafts_to_items, rules_from_aligned
from qp.clean.notes.models import ParsedNotes
from qp.clean.notes.pdf_notes import extract_note_chunks, extract_pdf_notes
from qp.clean.notes.regex_parser import RegexNoteParser, classify
from qp.domain import (
    FAIR_VALUE_GAIN,
    GOVERNMENT_GRANT,
    IMPAIRMENT_LOSS,
    LITIGATION_SETTLEMENT,
    NET_PROFIT_ATTRIBUTABLE,
    AlignedFact,
    AlignedReport,
    NonRecurringItem,
)
from qp.ingest.fx import FxRateProvider, FxRateRow

FX = FxRateProvider(
    rows=[FxRateRow("HKD", "CNY", date(2020, 1, 1), 1.09, "fixture")],
    target_currency="HKD",
)


def _fact(field: str, value: float) -> AlignedFact:
    return AlignedFact(
        symbol="00700", statement="IS", period_end=date(2025, 12, 31),
        period_type="ANNUAL", field=field, value=value, value_native=value,
        currency="HKD", report_currency="CNY", fx_rate=1.09,
        fx_as_of=date(2025, 12, 1), sources=("akshare",),
        published_at=datetime(2026, 3, 25), available_at=datetime(2026, 3, 25),
    )


def _aligned() -> AlignedReport:
    return AlignedReport(
        symbol="00700", period_end=date(2025, 12, 31), period_type="ANNUAL",
        as_of=date(2026, 10, 27),
        facts={
            NET_PROFIT_ATTRIBUTABLE: _fact(NET_PROFIT_ATTRIBUTABLE, 100.0),
            FAIR_VALUE_GAIN: _fact(FAIR_VALUE_GAIN, 8.0),
            GOVERNMENT_GRANT: _fact(GOVERNMENT_GRANT, 1.5),
            IMPAIRMENT_LOSS: _fact(IMPAIRMENT_LOSS, 3.0),
        },
    )


def test_rules_sign_conventions():
    items = rules_from_aligned(_aligned())
    by_category = {i.category: i for i in items}
    assert by_category[FAIR_VALUE_GAIN].profit_impact == 8.0
    assert by_category[IMPAIRMENT_LOSS].profit_impact == -3.0
    assert by_category[IMPAIRMENT_LOSS].amount == 3.0
    engine = NonRecurringEngine(FX, "HKD")
    result = engine.extract(_aligned(), ParsedNotes())
    # 100 - (8 + 1.5 - 3) = 93.5
    assert result.recurring_net_profit == pytest.approx(93.5)
    assert result.oneoff_profit_impact == pytest.approx(6.5)


def test_merge_dedupes_note_restatement_of_rule_item():
    engine = NonRecurringEngine(FX, "HKD")
    rule_items = rules_from_aligned(_aligned())
    note = NonRecurringItem(
        symbol="00700", period_end=date(2025, 12, 31), category=FAIR_VALUE_GAIN,
        label="投资物业公平值变动", amount=8.0 * 1.002, profit_impact=8.0 * 1.002,
        direction="gain", source_kind="llm", evidence="...", confidence=0.8,
        currency="HKD", published_at=datetime(2026, 3, 25),
        available_at=datetime(2026, 3, 25),
    )
    merged, warnings = engine.merge(rule_items, [note])
    assert len(merged) == len(rule_items)
    assert merged[[m.category for m in merged].index(FAIR_VALUE_GAIN)].corroborated


def test_merge_keeps_new_note_category_with_warning():
    engine = NonRecurringEngine(FX, "HKD")
    rule_items = rules_from_aligned(_aligned())
    note = NonRecurringItem(
        symbol="00700", period_end=date(2025, 12, 31), category=LITIGATION_SETTLEMENT,
        label="诉讼赔偿", amount=1.2, profit_impact=-1.2, direction="loss",
        source_kind="regex", evidence="...", confidence=0.55, currency="HKD",
        published_at=datetime(2026, 3, 25), available_at=datetime(2026, 3, 25),
    )
    merged, warnings = engine.merge(rule_items, [note])
    assert len(merged) == len(rule_items) + 1
    result = engine.extract(_aligned(), ParsedNotes(), prebuilt_note_items=[note])
    assert result.recurring_net_profit == pytest.approx(94.7)


def test_regex_parser_on_fixture_notes(note_chunks):
    parsed = RegexNoteParser().parse(note_chunks)
    assert len(parsed.items) >= 5
    assert all(i.amount >= 1000 for i in parsed.items)
    assert any(i.category == LITIGATION_SETTLEMENT for i in parsed.items)
    assert not any(abs(i.amount - 2026) < 1 for i in parsed.items), "years must be filtered"
    items, warnings = drafts_to_items(
        parsed.items, "00700", date(2025, 12, 31), FX, "HKD",
        datetime(2026, 3, 25), datetime(2026, 3, 25),
    )
    assert len(items) == len(parsed.items)
    assert all(i.currency == "HKD" for i in items)
    litigation = next(i for i in items if i.category == LITIGATION_SETTLEMENT)
    assert litigation.profit_impact == pytest.approx(-1_200_000 * 1000 * 1.09)


def test_extract_pdf_notes_compatibility_with_text_file(config):
    path = config.resolve(config.sources.fixtures_dir) / "hkexnews" / "00700" / "00700_2025_annual_notes.txt"
    text = extract_pdf_notes(path, keyword="非经常性损益")
    assert "--- Page 1 ---" in text and "诉讼赔偿" in text
    chunks = extract_note_chunks(path, keywords=("政府补助",), context_chars=200)
    assert chunks and chunks[0].page == 1


def test_classify_categories():
    assert classify("投资物业公允价值变动收益") == FAIR_VALUE_GAIN
    assert classify("资产减值损失") == IMPAIRMENT_LOSS
    assert classify("政府补助") == GOVERNMENT_GRANT
