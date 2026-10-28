from __future__ import annotations

from datetime import date, datetime

from qp.clean.align import MultiSourceAligner
from qp.clean.normalize import Normalizer
from qp.clean.quality import QualityChecker
from qp.domain import AlignedReport, FCFResult, Severity
from qp.ingest.fx import FxRateProvider


def _event_checker() -> QualityChecker:
    return QualityChecker("run-1", strict=False)


def test_ingest_events_for_estimated_disclosure_and_schema_drift(raw_cf_report):
    raw_cf_report.metadata["disclosure_estimated"] = True
    result = Normalizer(fx=FxRateProvider(fallback_rates={"CNY": 1.09})).normalize(raw_cf_report)
    events = _event_checker().check_ingest(raw_cf_report, result, date(2026, 10, 27))
    checks = {e.check for e in events}
    assert "estimated_disclosure_date" in checks
    assert any(e.severity is Severity.WARN for e in events)


def test_schema_drift_produces_error():
    class BadResult:
        rows_in = 20
        facts = []
        warnings = ["schema drift suspected ..."]
        unmapped_labels = ["x"] * 20
        mapped_ratio = 0.0

    import pandas as pd

    from qp.domain import PeriodType, RawReport, StatementType

    report = RawReport(
        "00700", "akshare", StatementType.CF, date(2025, 12, 31), PeriodType.ANNUAL,
        "CNY", 1, "", datetime(2026, 3, 25), datetime(2026, 3, 25), pd.DataFrame(),
    )
    events = _event_checker().check_ingest(report, BadResult(), date(2026, 10, 27))
    assert any(e.severity is Severity.ERROR and e.check == "schema_drift" for e in events)


def _aligned_with_conflict() -> tuple[AlignedReport, object]:
    import pandas as pd

    base = {
        "symbol": "00700", "source": "hkexnews", "statement": "CF",
        "period_end": "2025-12-31", "period_type": "ANNUAL", "field": "OCF",
        "value": 100.0, "value_native": 100.0, "currency": "HKD",
        "report_currency": "CNY", "fx_rate": 1.09, "fx_as_of": "2025-12-01",
        "published_at": datetime(2026, 3, 25), "available_at": datetime(2026, 3, 25),
        "quality": "[]",
    }
    df = pd.DataFrame([base, {**base, "source": "yfinance", "value": 150.0}])
    aligner = MultiSourceAligner()
    result = aligner.align(df, "00700", date(2025, 12, 31), date(2026, 10, 27))
    return result.report, result


def test_alignment_and_snapshot_gates():
    aligned, align_result = _aligned_with_conflict()
    checker = _event_checker()
    events = checker.check_alignment(aligned, align_result, date(2026, 10, 27))
    assert any(e.check == "cross_source_conflict" for e in events)
    fcf = FCFResult("00700", date(2025, 12, 31), "HKD", 100.0, 20.0, 20.0,
                    80.0, 80.0 / 1.09, 1.09, date(2025, 12, 1), 80.0, 0.0, "auto",
                    quality=["negative_ocf"])
    events += checker.check_snapshot(aligned, fcf, date(2026, 10, 27), None)
    # CAPEX is absent from this synthetic report → ERROR (required-field gate).
    assert QualityChecker.has_errors(events) is True
    assert any(e.check == "missing_required_field" for e in events)
    assert checker.counts(events)["WARN"] >= 2


def test_strict_quarantine_produces_error():
    aligned, align_result = _aligned_with_conflict()
    checker = QualityChecker("r", strict=True)
    events = checker.check_alignment(aligned, align_result, date(2026, 10, 27))
    assert QualityChecker.has_errors(events)
