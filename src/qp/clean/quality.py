"""Stage 4 (part 1): quality gates and monitoring events.

Every gate produces a structured event rather than a print(). Events are stored in
``quality_events``, counted in the run manifest, and can be shipped to a webhook.
In strict mode an ERROR event prevents factor writes for that symbol.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

from ..domain import (
    CAPEX,
    NET_PROFIT,
    OCF,
    REVENUE,
    AlignedReport,
    FCFResult,
    QualityEvent,
    RawReport,
    Severity,
)
from ..util import to_date

SEVERITY_ORDER = {"INFO": 0, "WARN": 1, "ERROR": 2}


@dataclass
class QualityChecker:
    run_id: str
    strict: bool = False
    max_staleness_days: int = 550

    def _event(
        self,
        severity: Severity,
        check: str,
        detail: str,
        symbol: str,
        period_end: date | None = None,
        as_of: date | None = None,
    ) -> QualityEvent:
        return QualityEvent(
            run_id=self.run_id, symbol=symbol, severity=severity, check=check,
            detail=detail, period_end=period_end, as_of=as_of,
        )

    def check_ingest(
        self,
        report: RawReport,
        normalize_result: Any,
        as_of: date,
    ) -> list[QualityEvent]:
        symbol = report.symbol
        out: list[QualityEvent] = [
            self._event(Severity.INFO, "ingest",
                        f"source={report.source} statement={report.statement.value} "
                        f"rows={normalize_result.rows_in} mapped={len(normalize_result.facts)}",
                        symbol, report.period_end, to_date(as_of)),
        ]
        if report.metadata.get("disclosure_estimated"):
            out.append(self._event(
                Severity.WARN, "estimated_disclosure_date",
                f"no HKEXnews filing date for {symbol} {report.period_end}: used "
                f"period_end + lag; PIT is approximate — backfill announcements.csv",
                symbol, report.period_end, to_date(as_of),
            ))
        if report.available_at > report.published_at:
            out.append(self._event(
                Severity.INFO, "late_arriving_version",
                f"{report.source} version first seen {report.available_at.isoformat()} "
                f"after publication {report.published_at.isoformat()}",
                symbol, report.period_end, to_date(as_of),
            ))
        for warning in normalize_result.warnings:
            level = Severity.ERROR if any(k in warning for k in ("FX missing", "schema drift")) else Severity.WARN
            out.append(self._event(level, "normalization", warning, symbol, report.period_end, to_date(as_of)))
        if normalize_result.rows_in and normalize_result.mapped_ratio < 0.1:
            out.append(self._event(
                Severity.ERROR, "schema_drift",
                f"only {normalize_result.mapped_ratio:.0%} of {report.source} rows mapped; "
                f"unmapped sample={normalize_result.unmapped_labels[:5]}",
                symbol, report.period_end, to_date(as_of),
            ))
        return out

    def check_alignment(
        self,
        aligned: AlignedReport,
        align_result: Any,
        as_of: date,
    ) -> list[QualityEvent]:
        symbol = aligned.symbol
        out: list[QualityEvent] = []
        for conflict in align_result.conflicts:
            out.append(self._event(
                Severity.WARN if not self.strict else Severity.ERROR, "cross_source_conflict",
                f"field={conflict['field']}: {conflict['detail']}", symbol, aligned.period_end, to_date(as_of),
            ))
        for field_name in align_result.quarantined_fields:
            out.append(self._event(
                Severity.ERROR, "quarantined_field",
                f"{field_name} quarantined after conflict; no factor computed",
                symbol, aligned.period_end, to_date(as_of),
            ))
        for fact in aligned.facts.values():
            if fact.restated:
                out.append(self._event(
                    Severity.INFO, "restatement",
                    f"{fact.field}: latest visible version is a restatement",
                    symbol, aligned.period_end, to_date(as_of),
                ))
            if "fx_fallback" in fact.quality:
                out.append(self._event(
                    Severity.WARN, "fx_fallback",
                    f"{fact.field}: used static fallback FX rate",
                    symbol, aligned.period_end, to_date(as_of),
                ))
            if "fx_missing" in fact.quality:
                out.append(self._event(
                    Severity.ERROR, "fx_missing",
                    f"{fact.field}: no PIT FX rate available; value not converted",
                    symbol, aligned.period_end, to_date(as_of),
                ))
        return out

    def check_snapshot(
        self,
        aligned: AlignedReport,
        fcf: FCFResult | None,
        as_of: date,
        market_cap: tuple[float, str, date] | None,
    ) -> list[QualityEvent]:
        symbol = aligned.symbol
        as_of = to_date(as_of)
        out: list[QualityEvent] = []
        missing = [f for f in (OCF, CAPEX, REVENUE, NET_PROFIT) if aligned.get(f) is None]
        if OCF in missing or CAPEX in missing:
            out.append(self._event(
                Severity.ERROR, "missing_required_field",
                f"cannot compute FCF; missing={missing}", symbol, aligned.period_end, as_of,
            ))
        elif missing:
            out.append(self._event(
                Severity.WARN, "missing_optional_field",
                f"missing={missing}; dependent factors will be null", symbol, aligned.period_end, as_of,
            ))
        age = (as_of - to_date(aligned.period_end)).days
        if age > self.max_staleness_days:
            out.append(self._event(
                Severity.WARN, "stale_fundamentals",
                f"period_end {aligned.period_end} is {age} days old", symbol, aligned.period_end, as_of,
            ))
        if fcf:
            for flag in fcf.quality:
                out.append(self._event(
                    Severity.ERROR if flag == "fx_missing_in_normalization" else Severity.WARN,
                    f"fcf_{flag}", f"FCF quality flag: {flag}", symbol, aligned.period_end, as_of,
                ))
        if market_cap is None:
            out.append(self._event(
                Severity.WARN, "missing_market_cap",
                "no market cap visible at as_of; fcf_yield not computed",
                symbol, aligned.period_end, as_of,
            ))
        return out

    @staticmethod
    def has_errors(events: Iterable[QualityEvent]) -> bool:
        return any(e.severity is Severity.ERROR for e in events)

    @staticmethod
    def counts(events: Iterable[QualityEvent]) -> dict[str, int]:
        out = {"INFO": 0, "WARN": 0, "ERROR": 0}
        for event in events:
            out[event.severity.value] += 1
        return out
