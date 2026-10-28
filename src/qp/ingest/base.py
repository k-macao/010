"""Adapter contract + shared helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Protocol, runtime_checkable

from ..domain import PeriodType, RawReport, StatementType


@dataclass(frozen=True)
class DisclosureRecord:
    """When did a filing (or a restatement) become publicly knowable?"""

    published_at: datetime
    is_estimated: bool
    origin: str                    # hkexnews | calendar | lag_estimate | vendor
    title: str = ""
    url: str = ""
    is_restatement: bool = False


@runtime_checkable
class SourceAdapter(Protocol):
    name: str

    def fetch(self, symbol: str, statements: Iterable[StatementType]) -> list[RawReport]:
        """Return every available (statement, period, version) snapshot.

        Implementations must NOT raise on a single missing statement; return [] instead.
        Network/parsing failures raise ``SourceError`` so SourceHub can fall back.
        """
        ...


def iter_reports(reports: Iterable[RawReport]) -> Iterable[RawReport]:
    for report in reports:
        if not isinstance(report.statement, StatementType):
            report.statement = StatementType.parse(report.statement)
        if not isinstance(report.period_type, PeriodType):
            report.period_type = PeriodType(str(report.period_type).upper())
        yield report


def unit_scale_from_label(label: str | None) -> float:
    """'人民币千元' -> 1000; '百万元' -> 1e6; None/unknown -> 1."""
    if not label:
        return 1.0
    text = str(label).lower().replace(",", "")
    for token, scale in (
        ("万亿", 1e12), ("trillion", 1e12), ("十亿", 1e9), ("billion", 1e9),
        ("亿", 1e8), ("百万", 1e6), ("million", 1e6), ("千", 1e3),
        ("thousand", 1e3), ("百", 1e2), ("hundred", 1e2),
    ):
        if token in text:
            return scale
    return 1.0


def period_type_for(period_end: date, fiscal_year_end: str, date_type_code: str | None = None) -> PeriodType:
    mapping = {"001": "ANNUAL", "002": "INTERIM", "003": "QUARTERLY", "004": "QUARTERLY"}
    if date_type_code and str(date_type_code) in mapping:
        return PeriodType(mapping[str(date_type_code)])
    try:
        month, day = (int(x) for x in fiscal_year_end.split("-"))
        if (period_end.month, period_end.day) == (month, day):
            return PeriodType.ANNUAL
    except (ValueError, AttributeError):
        pass
    return PeriodType.INTERIM


def report_metadata(**kwargs: Any) -> dict[str, Any]:
    return {k: v for k, v in kwargs.items() if v is not None}
