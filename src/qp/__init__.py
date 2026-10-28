"""qp — 港股量化财务数据流水线 (Point-In-Time safe).

架构: 多源拉取 → 标准化清洗 → FCF 复算 → 规则+LLM 非经常性损益 → PIT 因子入库。
快速开始: ``python -m qp demo`` (离线, 用 examples/fixtures 内演示数据)。
"""

from __future__ import annotations

__version__ = "0.1.0"

from .domain import (  # noqa: F401
    AlignedFact,
    AlignedReport,
    FactorValue,
    FCFResult,
    NonRecurringItem,
    NormalizedFact,
    PeriodType,
    QualityEvent,
    RawReport,
    RunSummary,
    Severity,
    Snapshot,
    StatementType,
)

__all__ = [
    "AlignedFact", "AlignedReport", "FCFResult", "FactorValue", "NonRecurringItem",
    "NormalizedFact", "PeriodType", "QualityEvent", "RawReport", "RunSummary",
    "Severity", "Snapshot", "StatementType", "__version__",
]
