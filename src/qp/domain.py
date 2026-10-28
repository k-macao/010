"""Canonical data contract.

Everything crossing a stage boundary is one of these dataclasses (or a DataFrame with
these column names). No stage is allowed to pass a raw vendor DataFrame downstream.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any

import pandas as pd


class StatementType(str, Enum):
    BS = "BS"
    IS = "IS"
    CF = "CF"

    @classmethod
    def parse(cls, value: str | "StatementType") -> "StatementType":
        if isinstance(value, cls):
            return value
        v = str(value).upper().strip()
        for alias, target in {
            "资产负债表": cls.BS, "資產負債表": cls.BS, "BALANCE_SHEET": cls.BS,
            "BALANCESHEET": cls.BS, "BS": cls.BS,
            "利润表": cls.IS, "利潤表": cls.IS, "INCOME": cls.IS,
            "INCOME_STATEMENT": cls.IS, "FINANCIALS": cls.IS, "IS": cls.IS,
            "现金流量表": cls.CF, "現金流量表": cls.CF, "CASHFLOW": cls.CF,
            "CASH_FLOW": cls.CF, "CF": cls.CF,
        }.items():
            if v == alias.upper() or v == alias:
                return target
        raise ValueError(f"unknown statement type: {value!r}")


class PeriodType(str, Enum):
    ANNUAL = "ANNUAL"
    INTERIM = "INTERIM"
    QUARTERLY = "QUARTERLY"


class Severity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


class CapexSignPolicy(str, Enum):
    AUTO = "auto"
    POSITIVE_OUTFLOW = "positive_outflow"
    NEGATIVE_OUTFLOW = "negative_outflow"
    SIGNED = "signed"


# Canonical field names -------------------------------------------------------
OCF = "OCF"
CAPEX = "CAPEX"
REVENUE = "REVENUE"
NET_PROFIT = "NET_PROFIT"
NET_PROFIT_ATTRIBUTABLE = "NET_PROFIT_ATTRIBUTABLE"
FAIR_VALUE_GAIN = "FAIR_VALUE_GAIN"
IMPAIRMENT_LOSS = "IMPAIRMENT_LOSS"
IMPAIRMENT_AND_PROVISIONS = "IMPAIRMENT_AND_PROVISIONS"
GOVERNMENT_GRANT = "GOVERNMENT_GRANT"
FX_GAIN_LOSS = "FX_GAIN_LOSS"
DISPOSAL_GAIN = "DISPOSAL_GAIN"
LITIGATION_SETTLEMENT = "LITIGATION_SETTLEMENT"
OTHER_NONRECURRING_GAIN = "OTHER_NONRECURRING_GAIN"
OTHER_NONRECURRING_LOSS = "OTHER_NONRECURRING_LOSS"
CASH_AND_EQUIVALENTS = "CASH_AND_EQUIVALENTS"
TOTAL_DEBT = "TOTAL_DEBT"
TOTAL_LIABILITIES = "TOTAL_LIABILITIES"
TOTAL_ASSETS = "TOTAL_ASSETS"
TOTAL_EQUITY = "TOTAL_EQUITY"
INVESTMENT_PROPERTY = "INVESTMENT_PROPERTY"
PPE = "PPE"

NONRECURRING_FIELDS: frozenset[str] = frozenset({
    FAIR_VALUE_GAIN, IMPAIRMENT_LOSS, GOVERNMENT_GRANT, FX_GAIN_LOSS,
    DISPOSAL_GAIN, LITIGATION_SETTLEMENT, OTHER_NONRECURRING_GAIN,
    OTHER_NONRECURRING_LOSS,
})

# ---------------------------------------------------------------------------


@dataclass
class RawReport:
    """One vendor snapshot for (symbol, source, statement, period, version).

    `published_at`  = official disclosure time (or a conservative estimate).
    `available_at`  = when THIS version became knowable to us. For a restatement
                      discovered today, that is today, never the original filing date.
                      PIT filtering always uses `available_at` (max of the two).
    """

    symbol: str
    source: str
    statement: StatementType
    period_end: date
    period_type: PeriodType
    currency: str
    unit_scale: float
    unit_label: str
    published_at: datetime
    available_at: datetime
    data: pd.DataFrame
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (
            self.symbol, self.source, self.statement.value,
            self.period_end.isoformat(), self.published_at.isoformat(),
        )


@dataclass(frozen=True)
class NormalizedFact:
    """One canonical line item, converted into `currency` base units (no 千/百万 suffix)."""

    symbol: str
    source: str
    statement: str
    period_end: date
    period_type: str
    published_at: datetime
    available_at: datetime
    field: str
    value: float                  # target currency, using the (deterministic) period-end FX
    value_native: float           # report currency base units, BEFORE FX
    currency: str
    report_currency: str
    raw_value: float
    raw_label: str
    unit_scale: float
    fx_rate: float
    fx_as_of: date | None
    fx_source: str
    sign: str
    quality: str
    content_hash: str
    ingested_at: datetime

    def as_row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "source": self.source,
            "statement": self.statement,
            "period_end": self.period_end.isoformat(),
            "period_type": self.period_type,
            "published_at": self.published_at,
            "available_at": self.available_at,
            "field": self.field,
            "value": self.value,
            "value_native": self.value_native,
            "currency": self.currency,
            "report_currency": self.report_currency,
            "raw_value": self.raw_value,
            "raw_label": self.raw_label,
            "unit_scale": self.unit_scale,
            "fx_rate": self.fx_rate,
            "fx_as_of": self.fx_as_of.isoformat() if self.fx_as_of else None,
            "fx_source": self.fx_source,
            "sign": self.sign,
            "quality": self.quality,
            "content_hash": self.content_hash,
            "ingested_at": self.ingested_at,
        }


@dataclass
class AlignedFact:
    symbol: str
    statement: str
    period_end: date
    period_type: str
    field: str
    value: float | None
    value_native: float | None
    currency: str
    report_currency: str
    fx_rate: float
    fx_as_of: date | None
    sources: tuple[str, ...]
    published_at: datetime
    available_at: datetime
    conflict: bool = False
    conflict_detail: str = ""
    restated: bool = False
    quality: tuple[str, ...] = ()

    def as_row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "statement": self.statement,
            "period_end": self.period_end.isoformat(), "period_type": self.period_type,
            "field": self.field, "value": self.value, "value_native": self.value_native,
            "currency": self.currency, "report_currency": self.report_currency,
            "fx_rate": self.fx_rate, "fx_as_of": self.fx_as_of.isoformat() if self.fx_as_of else None,
            "sources": ",".join(self.sources), "published_at": self.published_at,
            "available_at": self.available_at, "conflict": self.conflict,
            "conflict_detail": self.conflict_detail, "restated": self.restated,
            "quality": ",".join(self.quality),
        }


@dataclass
class AlignedReport:
    symbol: str
    period_end: date
    period_type: str
    as_of: date
    facts: dict[str, AlignedFact] = field(default_factory=dict)

    def get(self, field: str, default: float | None = None) -> float | None:
        fact = self.facts.get(field)
        return default if fact is None or fact.value is None else fact.value

    @property
    def currency(self) -> str:
        for fact in self.facts.values():
            return fact.currency
        return ""


@dataclass
class NonRecurringItem:
    symbol: str
    period_end: date
    category: str
    label: str
    amount: float                 # magnitude, target currency base units
    profit_impact: float          # signed contribution to reported net profit
    direction: str                # gain | loss
    source_kind: str              # rule | llm | regex | xbrl
    evidence: str
    confidence: float
    currency: str
    published_at: datetime
    available_at: datetime
    cash_impact: bool = False
    page: int | None = None
    raw_amount: float | None = None
    raw_currency: str = ""
    corroborated: bool = False

    def as_row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "period_end": self.period_end.isoformat(),
            "category": self.category, "label": self.label, "amount": self.amount,
            "profit_impact": self.profit_impact, "direction": self.direction,
            "source_kind": self.source_kind, "evidence": self.evidence,
            "confidence": self.confidence, "currency": self.currency,
            "published_at": self.published_at, "available_at": self.available_at,
            "cash_impact": int(self.cash_impact), "page": self.page,
            "raw_amount": self.raw_amount, "raw_currency": self.raw_currency,
            "corroborated": int(self.corroborated),
        }


@dataclass
class FCFResult:
    symbol: str
    period_end: date
    currency: str
    ocf: float
    capex_raw: float
    capex: float
    standard_fcf: float          # target currency (period-end FX)
    standard_fcf_native: float   # report currency, for PIT FX at evaluation time
    fx_rate: float
    fx_as_of: date | None
    adjusted_fcf: float
    oneoff_cash_impact: float
    policy: str
    quality: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "period_end": self.period_end.isoformat(),
            "currency": self.currency, "ocf": self.ocf, "capex_raw": self.capex_raw,
            "capex": self.capex, "standard_fcf": self.standard_fcf,
            "standard_fcf_native": self.standard_fcf_native,
            "fx_rate": self.fx_rate, "fx_as_of": self.fx_as_of.isoformat() if self.fx_as_of else None,
            "adjusted_fcf": self.adjusted_fcf,
            "oneoff_cash_impact": self.oneoff_cash_impact, "policy": self.policy,
            "quality": list(self.quality),
        }


@dataclass
class QualityEvent:
    run_id: str
    symbol: str
    severity: Severity
    check: str
    detail: str
    period_end: date | None = None
    as_of: date | None = None
    created_at: datetime | None = None

    def as_row(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "symbol": self.symbol,
            "severity": self.severity.value, "check": self.check, "detail": self.detail,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "created_at": self.created_at,
        }


@dataclass
class FactorValue:
    symbol: str
    as_of: date
    period_end: date
    feature: str
    value: float
    currency: str
    unit: str
    formula_version: str
    inputs: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "as_of": self.as_of.isoformat(),
            "period_end": self.period_end.isoformat(), "feature": self.feature,
            "value": self.value, "currency": self.currency, "unit": self.unit,
            "formula_version": self.formula_version,
            "inputs": json.dumps(self.inputs, ensure_ascii=False, sort_keys=True, default=str),
        }


@dataclass
class Snapshot:
    """Everything a backtest needs at one as-of date."""

    symbol: str
    as_of: date
    status: str
    period_end: date | None = None
    aligned: AlignedReport | None = None
    fcf: FCFResult | None = None
    items: list[NonRecurringItem] = field(default_factory=list)
    factors: list[FactorValue] = field(default_factory=list)
    events: list[QualityEvent] = field(default_factory=list)

    def factor(self, name: str) -> float | None:
        for f in self.factors:
            if f.feature == name:
                return f.value
        return None


@dataclass
class RunSummary:
    run_id: str
    started_at: datetime
    finished_at: datetime | None
    as_of: date
    status: str
    symbols: list[str]
    counts: dict[str, int]
    events: list[QualityEvent]
    snapshots: list[Snapshot]
    report_path: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "as_of": self.as_of.isoformat(), "status": self.status,
            "symbols": list(self.symbols), "counts": dict(self.counts),
            "events": [e.as_row() for e in self.events],
            "snapshots": [
                {
                    "symbol": s.symbol, "as_of": s.as_of.isoformat(), "status": s.status,
                    "period_end": s.period_end.isoformat() if s.period_end else None,
                    "fcf": s.fcf.as_dict() if s.fcf else None,
                    "factors": [f.as_row() for f in s.factors],
                    "nonrecurring_items": [i.as_row() for i in s.items],
                }
                for s in self.snapshots
            ],
            "report_path": self.report_path,
        }
