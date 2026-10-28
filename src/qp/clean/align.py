"""Stage 1 (continued): cross-source field alignment with lineage and conflict gates.

Priority example: HKEXnews (official XBRL) → AkShare (broad coverage) → yfinance
(uniform English fields). Missing fields are filled from lower-priority sources;
disagreements beyond ``conflict_rel_tolerance`` are recorded (and quarantined in
strict mode) instead of being silently averaged.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date
from typing import Iterable

import pandas as pd

from ..domain import CAPEX, OCF, TOTAL_DEBT, AlignedFact, AlignedReport
from ..util import normalize_label, rel_diff, to_date, to_datetime


@dataclass
class AlignResult:
    report: AlignedReport
    conflicts: list[dict[str, object]] = dc_field(default_factory=list)
    fallback_fields: list[str] = dc_field(default_factory=list)
    quarantined_fields: list[str] = dc_field(default_factory=list)


class MultiSourceAligner:
    def __init__(
        self,
        priority: Iterable[str] = ("hkexnews", "akshare", "yfinance"),
        conflict_rel_tolerance: float = 0.01,
        quarantine_on_conflict: bool = False,
    ) -> None:
        self.priority = tuple(priority)
        self.tolerance = conflict_rel_tolerance
        self.quarantine = quarantine_on_conflict

    def _rank(self, source: str) -> int:
        try:
            return self.priority.index(source)
        except ValueError:
            return len(self.priority) + 1

    def align(
        self,
        facts: pd.DataFrame,
        symbol: str,
        period_end: date,
        as_of: date,
        restated_fields: set[str] | None = None,
    ) -> AlignResult:
        period_end = to_date(period_end)
        subset = facts.copy()
        if subset.empty:
            return AlignResult(AlignedReport(symbol, period_end, "ANNUAL", to_date(as_of)))
        subset["period_end"] = subset["period_end"].astype(str)
        subset = subset[subset["period_end"] == period_end.isoformat()]
        # Older persisted rows may contain the historical mapper's invalid
        # TOTAL_DEBT <- total-liabilities alias. Do not compare those with real
        # debt values after the mapping correction; new ingests map liabilities
        # to their own canonical field.
        if "raw_label" in subset.columns:
            normalized = subset["raw_label"].fillna("").map(normalize_label)
            liability_only = normalized.isin({"总负债", "负债总额", "负债总计"}) | normalized.str.startswith(
                "totalliabilities"
            )
            subset = subset[~((subset["field"] == TOTAL_DEBT) & liability_only)]
        if subset.empty:
            return AlignResult(AlignedReport(symbol, period_end, "ANNUAL", to_date(as_of)))

        period_type = str(subset["period_type"].iloc[0]) if "period_type" in subset else "ANNUAL"
        result = AlignResult(AlignedReport(symbol, period_end, period_type, to_date(as_of)))
        restated_fields = restated_fields or set()

        for field_name, group in subset.groupby("field", sort=True):
            group = group.copy()
            group["_rank"] = group["source"].map(self._rank)
            group = group.sort_values(["_rank", "source"])
            primary = group.iloc[0]
            conflicts: list[str] = []
            for _, row in group.iloc[1:].iterrows():
                try:
                    # CapEx is a sign-convention minefield: some vendors report the
                    # cash outflow as negative, others as a positive expense. Compare
                    # magnitudes so a pure sign flip is not mistaken for a conflict.
                    left, right = float(primary["value"]), float(row["value"])
                    if field_name == CAPEX:
                        left, right = abs(left), abs(right)
                    diff = rel_diff(left, right)
                except (TypeError, ValueError):
                    continue
                if diff > self.tolerance:
                    conflicts.append(
                        f"{row['source']}={float(row['value']):.4g} vs "
                        f"{primary['source']}={float(primary['value']):.4g} (Δ={diff:.2%})"
                    )
            conflict = bool(conflicts)
            if conflict:
                result.conflicts.append({
                    "field": field_name, "period_end": period_end.isoformat(),
                    "primary": primary["source"], "detail": "; ".join(conflicts),
                })
            if self._rank(str(primary["source"])) > 0:
                result.fallback_fields.append(str(field_name))
            value: float | None = float(primary["value"])
            quality: list[str] = []
            if conflict:
                quality.append("cross_source_conflict")
            if field_name in restated_fields:
                quality.append("restated")
            if self.quarantine and conflict:
                value = None
                result.quarantined_fields.append(str(field_name))
                quality.append("quarantined")
            str_quality = str(primary.get("quality", "") or "")
            if "fx_" in str_quality:
                quality.extend(x for x in str_quality.strip("[]\"").split(",") if x.strip().startswith("fx_"))

            result.report.facts[str(field_name)] = AlignedFact(
                symbol=symbol,
                statement=str(primary["statement"]),
                period_end=period_end,
                period_type=period_type,
                field=str(field_name),
                value=value,
                value_native=float(primary["value_native"]) if "value_native" in primary else None,
                currency=str(primary.get("currency", "")),
                report_currency=str(primary.get("report_currency", "")),
                fx_rate=float(primary.get("fx_rate", 1.0) or 1.0),
                fx_as_of=to_date(primary["fx_as_of"]) if primary.get("fx_as_of") else None,
                sources=tuple(str(s) for s in group["source"].tolist()),
                published_at=to_datetime(primary["published_at"]),
                available_at=to_datetime(primary["available_at"]),
                conflict=conflict,
                conflict_detail="; ".join(conflicts),
                restated=field_name in restated_fields,
                quality=tuple(sorted(set(quality))),
            )
        return result


def choose_period(
    facts: pd.DataFrame,
    as_of: date,
    required: tuple[str, ...] = (OCF, CAPEX),
    max_staleness_days: int = 550,
) -> tuple[date | None, str]:
    """Latest fiscal period with complete required fields and acceptable staleness."""
    if facts.empty:
        return None, "no facts visible at as_of"
    frame = facts.copy()
    frame["period_end"] = frame["period_end"].astype(str)
    for period_end, group in sorted(frame.groupby("period_end"), reverse=True):
        fields = set(group["field"].astype(str))
        if not set(required) <= fields:
            continue
        day = to_date(period_end)
        if (to_date(as_of) - day).days > max_staleness_days:
            return None, f"latest complete period {period_end} is stale (> {max_staleness_days}d)"
        if any(group.loc[group["field"] == f, "value"].isna().all() for f in required):
            continue
        return day, ""
    return None, "no period with complete OCF/CAPEX fields"
