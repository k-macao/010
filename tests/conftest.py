from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from qp.config import Config
from qp.domain import PeriodType, RawReport, StatementType
from qp.pipeline import Pipeline

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples" / "fixtures"


@pytest.fixture
def config(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.base_dir = ROOT
    cfg.database.url = f"sqlite:///{tmp_path / 'quant.db'}"
    cfg.sources.offline = True
    cfg.sources.fixtures_dir = str(FIXTURES)
    cfg.sources.securities_file = str(ROOT / "config" / "securities.csv")
    cfg.fx.rates_file = str(FIXTURES / "fx_rates.csv")
    cfg.reports.output_dir = str(tmp_path / "reports")
    cfg.llm.cache_dir = str(tmp_path / "llm-cache")
    cfg.pit.backfill_mode = True
    cfg.quality.strict = False
    return cfg


@pytest.fixture
def pipeline(config: Config) -> Pipeline:
    return Pipeline(config)


@pytest.fixture
def raw_cf_report() -> RawReport:
    """One CNY-thousands cash-flow snapshot, matching the AkShare fixture shape."""
    import pandas as pd

    return RawReport(
        symbol="00700", source="akshare", statement=StatementType.CF,
        period_end=date(2025, 12, 31), period_type=PeriodType.ANNUAL,
        currency="CNY", unit_scale=1000, unit_label="人民币千元",
        published_at=datetime(2026, 3, 25, 8), available_at=datetime(2026, 3, 25, 8),
        data=pd.DataFrame([
            {"raw_label": "经营活动产生的现金流量净额", "value": 240_000_000},
            {"raw_label": "购建固定资产、无形资产和其他长期资产支付的现金", "value": 35_000_000},
            {"raw_label": "固定资产", "value": 150_000_000},  # BS stock: must not map to CAPEX
        ]),
    )


@pytest.fixture
def note_chunks():
    from qp.clean.notes.pdf_notes import extract_note_chunks

    return extract_note_chunks(FIXTURES / "hkexnews" / "00700" / "00700_2025_annual_notes.txt")
