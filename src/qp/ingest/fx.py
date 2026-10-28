"""Point-in-time FX rates.

Convention (explicit, because this is where quant pipelines lose money):

    rate(base, quote, as_of) = number of BASE units per 1 QUOTE unit.

With target HKD, a CNY rate of 1.09 means 1 CNY = 1.09 HKD; multiply the
CNY-denominated cash flow by 1.09 before dividing by an HKD market cap.
Only rows with ``as_of <= evaluation_date`` are eligible — no future FX.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

from ..errors import MissingFxRate
from ..util import to_date


@dataclass(frozen=True)
class FxQuote:
    base: str
    quote: str
    rate: float
    as_of: date
    source: str
    is_fallback: bool = False
    age_days: int = 0


@dataclass(frozen=True)
class FxRateRow:
    base: str
    quote: str
    as_of: date
    rate: float
    source: str


class FxRateProvider:
    """CSV/DB-backed rate table with PIT lookup and a clearly-flagged static fallback.

    If ``strict`` is True, a missing rate raises instead of silently using 1.0.
    """

    def __init__(
        self,
        rows: Iterable[FxRateRow] = (),
        target_currency: str = "HKD",
        fallback_rates: dict[str, float] | None = None,
        max_age_days: int = 10,
        strict: bool = False,
    ) -> None:
        self.rows: list[FxRateRow] = list(rows)
        self.target_currency = target_currency.upper()
        self.fallback_rates = {k.upper(): float(v) for k, v in (fallback_rates or {}).items()}
        self.max_age_days = max_age_days
        self.strict = strict

    @classmethod
    def from_csv(
        cls,
        path: str | Path,
        target_currency: str = "HKD",
        fallback_rates: dict[str, float] | None = None,
        max_age_days: int = 10,
        strict: bool = False,
    ) -> "FxRateProvider":
        rows: list[FxRateRow] = []
        p = Path(path)
        if p.exists():
            with p.open("r", encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    rows.append(FxRateRow(
                        base=row.get("base", target_currency).upper(),
                        quote=row["quote"].upper(),
                        as_of=to_date(row["as_of"]),
                        rate=float(row["rate"]),
                        source=row.get("source", "csv"),
                    ))
        return cls(rows, target_currency, fallback_rates, max_age_days, strict)

    def add_rows(self, rows: Iterable[FxRateRow]) -> None:
        self.rows.extend(rows)
        # de-duplicate deterministically: later rows win for the same key/day
        dedup: dict[tuple[str, str, str], FxRateRow] = {}
        for row in self.rows:
            dedup[(row.base, row.quote, row.as_of.isoformat())] = row
        self.rows = sorted(dedup.values(), key=lambda r: (r.base, r.quote, r.as_of))

    def as_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame([{
            "base": r.base, "quote": r.quote, "as_of": r.as_of.isoformat(),
            "rate": r.rate, "source": r.source,
        } for r in self.rows])

    def rate(self, quote: str, as_of: date | datetime, base: str | None = None) -> FxQuote:
        base = (base or self.target_currency).upper()
        quote = quote.upper()
        day = to_date(as_of)
        if base == quote:
            return FxQuote(base, quote, 1.0, day, "identity")
        eligible = [
            r for r in self.rows
            if r.base == base and r.quote == quote and r.as_of <= day and r.rate > 0
        ]
        if eligible:
            best = max(eligible, key=lambda r: (r.as_of, r.source))
            return FxQuote(base, quote, best.rate, best.as_of, best.source,
                           age_days=(day - best.as_of).days)
        # Simple keys ("CNY": 1.09) are quoted against `target_currency` only.
        # For any other base, require an explicit "CNY_USD"-style key.
        fallback = self.fallback_rates.get(f"{quote}_{base}")
        if fallback is None and base == self.target_currency:
            fallback = self.fallback_rates.get(quote)
        if fallback is None and self.strict:
            raise MissingFxRate(f"no {quote}->{base} FX rate on or before {day}")
        if fallback is None:
            raise MissingFxRate(f"no {quote}->{base} FX rate on or before {day} and no fallback configured")
        return FxQuote(base, quote, fallback, day, "fallback", is_fallback=True,
                       age_days=self.max_age_days + 1)

    def convert(self, value: float, quote: str, as_of: date | datetime, base: str | None = None) -> tuple[float, FxQuote]:
        quote_obj = self.rate(quote, as_of, base)
        return value * quote_obj.rate, quote_obj

    @staticmethod
    def from_boc_sina(df: pd.DataFrame, base: str = "HKD") -> list[FxRateRow]:
        """AkShare ``currency_boc_sina(symbol='港币')`` → our rate convention.

        BOC quotes "100 HKD = N CNY", so 1 HKD = N/100 CNY and
        1 CNY = 100/N HKD. Columns differ by akshare version; detect by name.
        """
        date_col = next((c for c in df.columns if str(c) in {"日期", "date", "Date"}), None)
        rate_col = next((c for c in df.columns if "中间价" in str(c) or "中行折算价" in str(c)
                         or str(c).lower() in {"rate", "mid", "close"}), None)
        if date_col is None or rate_col is None:
            raise MissingFxRate(f"unexpected currency_boc_sina columns: {list(df.columns)}")
        rows: list[FxRateRow] = []
        for _, row in df.iterrows():
            n = pd.to_numeric(row[rate_col], errors="coerce")
            if pd.isna(n) or float(n) <= 0:
                continue
            hkd_per_cny = 100.0 / float(n)
            rows.append(FxRateRow(base="HKD", quote="CNY", as_of=to_date(row[date_col]),
                                  rate=hkd_per_cny, source="boc_sina"))
        return rows
