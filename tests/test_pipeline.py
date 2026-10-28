from __future__ import annotations

from datetime import date

import pytest

from qp.clean.quality import QualityChecker
from qp.pipeline import Pipeline

AS_OF = date(2026, 10, 27)


@pytest.fixture
def ingested(pipeline: Pipeline):
    quality = QualityChecker("test-ingest", False)
    return pipeline.ingest(["00700"], AS_OF, quality, offline=True)


def test_offline_end_to_end(pipeline: Pipeline):
    summary = pipeline.run(["00700"], as_of=AS_OF, offline=True)
    assert summary.status == "OK"
    snap = summary.snapshots[0]
    assert snap.period_end == date(2025, 12, 31)
    assert snap.fcf.standard_fcf == pytest.approx(223_450_000_000, rel=1e-6)
    assert snap.fcf.ocf == pytest.approx(261_600_000_000, rel=1e-6)
    assert snap.fcf.capex == pytest.approx(38_150_000_000, rel=1e-6)
    assert snap.factor("fcf_yield") == pytest.approx(0.059882, rel=1e-4)
    assert snap.factor("recurring_net_profit") == pytest.approx(128_838_000_000, rel=1e-5)
    assert snap.factor("net_debt_to_fcf") == pytest.approx(0.4878, rel=1e-3)
    assert summary.counts["features"] > 0
    assert summary.report_path
    # Cross-source disagreement is recorded, not silently averaged.
    assert any(e.check == "cross_source_conflict" for e in summary.events)
    assert not any(e.severity.value == "ERROR" for e in summary.events)


def test_no_lookahead_before_first_disclosure(ingested, pipeline: Pipeline):
    before = pipeline.compute_snapshot("00700", date(2024, 1, 1), strict=False)
    assert before.status == "NO_DATA"
    assert before.factors == []
    after = pipeline.compute_snapshot("00700", date(2024, 6, 30), strict=False)
    assert after.period_end == date(2023, 12, 31)


def test_restatement_changes_pit_value_not_history(ingested, pipeline: Pipeline):
    before = pipeline.compute_snapshot("00700", date(2025, 6, 30), strict=False)
    after = pipeline.compute_snapshot("00700", date(2025, 12, 31), strict=False)
    assert before.period_end == after.period_end == date(2024, 12, 31)
    assert before.fcf.standard_fcf == pytest.approx(183_600_000_000, rel=1e-6)
    assert after.fcf.standard_fcf == pytest.approx(180_900_000_000, rel=1e-6)
    assert after.fcf.standard_fcf < before.fcf.standard_fcf


def test_pit_history_replay(ingested, pipeline: Pipeline):
    frame = pipeline.history("00700", [date(2025, 6, 30), date(2025, 12, 31),
                                       date(2026, 6, 30)])
    assert len(frame) == 3
    assert frame.iloc[0]["period_end"] == "2024-12-31"
    assert frame.iloc[2]["period_end"] == "2025-12-31"
    assert frame.iloc[0]["standard_fcf"] != frame.iloc[1]["standard_fcf"]


def test_rerun_is_idempotent(pipeline: Pipeline):
    first = pipeline.run(["00700"], as_of=AS_OF, offline=True)
    facts_after_first = len(pipeline.store.get_facts("00700", AS_OF, only_visible=False))
    second = pipeline.run(["00700"], as_of=AS_OF, offline=True)
    facts_after_second = len(pipeline.store.get_facts("00700", AS_OF, only_visible=False))
    assert first.snapshots[0].factor("standard_fcf") == second.snapshots[0].factor("standard_fcf")
    assert facts_after_first == facts_after_second
    assert len(pipeline.store.get_features("00700", AS_OF)) == len(first.snapshots[0].factors)


def test_strict_mode_quarantines_conflicted_fields(config):
    config.align.quarantine_on_conflict = True
    config.quality.strict = True
    strict_pipeline = Pipeline(config)
    summary = strict_pipeline.run(["00700"], as_of=AS_OF, offline=True)
    assert summary.snapshots[0].status == "QUARANTINED"
    assert summary.snapshots[0].factors == []


def test_two_symbols_offline(pipeline: Pipeline):
    summary = pipeline.run(["00700", "09988"], as_of=AS_OF, offline=True)
    assert len(summary.snapshots) == 2
    assert all(s.fcf is not None for s in summary.snapshots)
