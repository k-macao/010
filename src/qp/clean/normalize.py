"""Stage 1+2: field mapping, unit normalization, currency conversion, PIT metadata.

Output is a list of :class:`NormalizedFact`. Two values are stored per fact:

* ``value_native`` — report currency, base units (元), no 千/百万 suffix.
* ``value`` — target currency using a **deterministic period-end FX rate**. This
  makes cross-source comparison possible without leaking future FX. The factor
  layer re-prices FCF with the PIT FX of the evaluation date (see factors.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd

from ..domain import NormalizedFact, RawReport, StatementType
from ..errors import MissingFieldError
from ..ingest.fx import FxRateProvider
from ..ingest.shapes import to_long
from ..util import json_dumps, stable_hash, to_date, utcnow
from .field_map import FieldMatcher, MatchResult


@dataclass
class NormalizeResult:
    facts: list[NormalizedFact] = field(default_factory=list)
    unmapped_labels: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rows_in: int = 0

    @property
    def mapped_ratio(self) -> float:
        return len(self.facts) / self.rows_in if self.rows_in else 1.0


class Normalizer:
    def __init__(
        self,
        matcher: FieldMatcher | None = None,
        fx: FxRateProvider | None = None,
        target_currency: str = "HKD",
        fiscal_year_end: str = "12-31",
    ) -> None:
        self.matcher = matcher or FieldMatcher()
        self.fx = fx or FxRateProvider(target_currency=target_currency)
        self.target_currency = target_currency.upper()
        self.fiscal_year_end = fiscal_year_end

    def _match(self, row: pd.Series, statement: StatementType) -> MatchResult | None:
        code = row.get("raw_code") if hasattr(row, "get") else None
        hit = self.matcher.match_by_code(code, statement)
        return hit or self.matcher.match(str(row["raw_label"]), statement)

    def normalize(
        self,
        report: RawReport,
        ingested_at: datetime | None = None,
    ) -> NormalizeResult:
        ingested_at = ingested_at or utcnow()
        long = to_long(
            report.data,
            default_period_end=report.period_end,
            default_period_type=report.period_type.value,
            fiscal_year_end=self.fiscal_year_end,
        )
        result = NormalizeResult(rows_in=len(long))
        if long.empty:
            return result

        # FX at the fiscal period end: deterministic, PIT-safe (never a future rate).
        fx_rate = 1.0
        fx_as_of: date | None = None
        fx_source = "identity"
        fx_flags: list[str] = []
        if report.currency.upper() != self.target_currency:
            try:
                quote = self.fx.rate(report.currency, report.period_end, base=self.target_currency)
                fx_rate, fx_as_of, fx_source = quote.rate, quote.as_of, quote.source
                if quote.is_fallback:
                    fx_flags.append("fx_fallback")
                    result.warnings.append(
                        f"FX fallback for {report.currency}->{self.target_currency} "
                        f"at {report.period_end}: rate={quote.rate}"
                    )
                elif quote.age_days > self.fx.max_age_days:
                    fx_flags.append("fx_stale")
            except Exception as exc:  # MissingFxRate: keep the fact, but flag as ERROR
                fx_flags.append("fx_missing")
                result.warnings.append(f"FX missing for {report.currency}: {exc}")

        grouped: dict[str, list[tuple[pd.Series, MatchResult]]] = {}
        for _, row in long.iterrows():
            hit = self._match(row, report.statement)
            if hit is None:
                result.unmapped_labels.append(str(row["raw_label"]))
                continue
            if hit.ambiguous:
                result.warnings.append(f"ambiguous label {row['raw_label']!r} → {hit.field}")
            grouped.setdefault(hit.field, []).append((row, hit))

        for field_name, rows in grouped.items():
            spec = rows[0][1].spec
            labels = [str(r["raw_label"]) for r, _ in rows]
            quality = list(fx_flags)
            if spec.aggregation == "sum" and len(rows) > 1:
                raw_value = float(sum(float(r["value"]) for r, _ in rows))
            else:
                raw_value = float(rows[0][0]["value"])
                if len({label for label in labels}) > 1:
                    quality.append("duplicate_label")
                    result.warnings.append(
                        f"{report.symbol} {report.period_end} {field_name}: "
                        f"multiple labels {labels}, kept {labels[0]!r}"
                    )
            if spec.sign == "as_loss":
                # Vendors disagree on whether a loss is negative or a positive expense.
                # We store the magnitude; the profit impact is applied in the engine.
                raw_value = abs(raw_value)
            if rows[0][1].ambiguous:
                quality.append("ambiguous_match")

            value_native = raw_value * report.unit_scale
            fact = NormalizedFact(
                symbol=report.symbol,
                source=report.source,
                statement=report.statement.value,
                period_end=to_date(report.period_end),
                period_type=report.period_type.value,
                published_at=report.published_at,
                available_at=report.available_at,
                field=field_name,
                value=value_native * fx_rate,
                value_native=value_native,
                currency=self.target_currency,
                report_currency=report.currency.upper(),
                raw_value=raw_value,
                raw_label=rows[0][0]["raw_label"],
                unit_scale=report.unit_scale,
                fx_rate=fx_rate,
                fx_as_of=fx_as_of,
                fx_source=fx_source,
                sign=spec.sign,
                quality=json_dumps(sorted(set(quality))),
                content_hash=stable_hash({
                    "symbol": report.symbol, "source": report.source,
                    "statement": report.statement.value,
                    "period_end": report.period_end.isoformat(),
                    "available_at": report.available_at.isoformat(),
                    "field": field_name, "raw_value": raw_value,
                }),
                ingested_at=ingested_at,
            )
            result.facts.append(fact)

        if result.mapped_ratio < 0.1 and result.rows_in > 5:
            result.warnings.append(
                f"schema drift suspected: only {result.mapped_ratio:.0%} of "
                f"{report.source}/{report.statement.value} rows mapped"
            )
        return result

    @staticmethod
    def require(facts: list[NormalizedFact], fields: list[str]) -> None:
        present = {f.field for f in facts}
        missing = [f for f in fields if f not in present]
        if missing:
            raise MissingFieldError(f"missing canonical fields: {missing}")
