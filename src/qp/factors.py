"""Stage 4 (part 2): factor computation with PIT FX and PIT market cap.

Key rule: ``FCF / market_cap`` must use the same currency AND the same clock.
FCF is re-priced from the report currency using the FX rate visible on ``as_of``
(not today's, not the vendor's opaque conversion rate), and market cap is the
latest observation with ``as_of <= evaluation_date``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from .clean.notes.engine import NonRecurringResult
from .domain import (
    CASH_AND_EQUIVALENTS,
    NET_PROFIT,
    NET_PROFIT_ATTRIBUTABLE,
    REVENUE,
    TOTAL_DEBT,
    AlignedReport,
    FactorValue,
    FCFResult,
)
from .ingest.fx import FxRateProvider
from .util import to_date


@dataclass
class FactorEngine:
    fx: FxRateProvider
    target_currency: str = "HKD"
    formula_version: str = "fcf-v1"

    def compute(
        self,
        aligned: AlignedReport,
        fcf: FCFResult,
        nonrecurring: NonRecurringResult,
        as_of: date,
        market_cap: tuple[float, str, date] | None = None,
    ) -> tuple[list[FactorValue], list[str]]:
        as_of = to_date(as_of)
        period_end = to_date(aligned.period_end)
        currency = aligned.currency or self.target_currency
        ocf_fact = aligned.facts.get("OCF")
        report_currency = ocf_fact.report_currency if ocf_fact else currency
        warnings: list[str] = []

        fx_quote = self.fx.rate(report_currency, as_of, base=self.target_currency)
        if fx_quote.is_fallback:
            warnings.append(f"PIT FX fallback {report_currency}->{self.target_currency} at {as_of}")
        fcf_as_of = fcf.standard_fcf_native * fx_quote.rate

        common = dict(
            symbol=aligned.symbol, as_of=as_of, period_end=period_end,
            currency=currency, formula_version=self.formula_version,
        )
        inputs: dict[str, Any] = {
            "ocf": fcf.ocf, "capex": fcf.capex, "capex_raw": fcf.capex_raw,
            "capex_policy": fcf.policy, "fx_rate_normalization": fcf.fx_rate,
            "fx_rate_as_of": fx_quote.rate, "fx_as_of": fx_quote.as_of.isoformat(),
            "fx_source": fx_quote.source, "report_currency": report_currency,
            "sources": sorted({s for f in aligned.facts.values() for s in f.sources}),
        }

        def add(name: str, value: float, unit: str = "currency", **extra: Any) -> None:
            factors.append(FactorValue(
                feature=name, value=float(value), unit=unit,
                inputs={**inputs, **extra}, **common,
            ))

        factors: list[FactorValue] = []
        add("standard_fcf", fcf.standard_fcf)
        add("fcf_as_of", fcf_as_of)
        add("fcf_adjusted", fcf.adjusted_fcf)
        add("capex_to_ocf", fcf.capex / abs(fcf.ocf) if fcf.ocf else float("nan"), unit="ratio")
        revenue = aligned.get(REVENUE)
        if revenue:
            add("fcf_margin", fcf.standard_fcf / revenue, unit="ratio")
        net_profit = aligned.get(NET_PROFIT_ATTRIBUTABLE) or aligned.get(NET_PROFIT)
        if net_profit:
            add("fcf_to_net_profit", fcf.standard_fcf / net_profit, unit="ratio")
        if nonrecurring.recurring_net_profit is not None:
            add("recurring_net_profit", nonrecurring.recurring_net_profit)
            if nonrecurring.recurring_net_profit:
                add("fcf_to_recurring_earnings",
                    fcf.standard_fcf / nonrecurring.recurring_net_profit, unit="ratio")
        if nonrecurring.nonrecurring_ratio is not None:
            add("nonrecurring_ratio", nonrecurring.nonrecurring_ratio, unit="ratio")

        if market_cap is not None:
            mc_value, mc_currency, mc_as_of = market_cap
            mc_quote = self.fx.rate(mc_currency, as_of, base=self.target_currency)
            mc_target = mc_value * mc_quote.rate
            if mc_quote.is_fallback:
                warnings.append(f"market cap FX fallback {mc_currency}->{self.target_currency}")
            if mc_target and mc_target > 0:
                add("fcf_yield", fcf_as_of / mc_target, unit="ratio",
                    market_cap=mc_value, market_cap_currency=mc_currency,
                    market_cap_as_of=to_date(mc_as_of).isoformat())
                add("fcf_yield_adjusted",
                    (fcf.adjusted_fcf / fcf.standard_fcf * fcf_as_of) / mc_target
                    if fcf.standard_fcf else float("nan"),
                    unit="ratio")

        cash, debt = aligned.get(CASH_AND_EQUIVALENTS), aligned.get(TOTAL_DEBT)
        if cash is not None and debt is not None and fcf.standard_fcf:
            add("net_debt_to_fcf", (debt - cash) / fcf.standard_fcf, unit="ratio")

        return factors, warnings
