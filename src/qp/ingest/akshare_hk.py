"""AkShare (东方财富) HK financial statements adapter.

⚠️ Two differences from the naive sketch, both verified against the live API:

1. Signature is ``stock_financial_hk_report_em(stock="00700", symbol="现金流量表",
   indicator="年度")`` — NOT ``(symbol=<code>, indicator=<statement>)``. We try the
   real signature first and fall back to the legacy one on TypeError.
2. It returns a LONG table (``STD_ITEM_NAME / AMOUNT / REPORT_DATE /
   DATE_TYPE_CODE``), not wide. ``AMOUNT`` is in CNY *yuan* for ALL HK issuers:
   Eastmoney already converts from the filer's reporting currency using an
   opaque period-end rate. See README "币种红线". Hence ``currency`` comes from the
   security master's ``akshare_currency`` column (default CNY), NOT from
   ``report_currency``.
3. It exposes neither a currency column nor an announcement date. Disclosure time
   is resolved by :class:`DisclosureCalendar` (HKEXnews or conservative lag).

The adapter only returns the *current* snapshot; restatement history is built by
archiving today's snapshot under its own ``available_at`` (see pipeline snapshot policy).
"""

from __future__ import annotations

from typing import Any, Iterable

import pandas as pd

from ..domain import PeriodType, RawReport, StatementType
from ..errors import SchemaDriftError, SourceError
from ..util import to_date
from .disclosure import DisclosureCalendar, SecurityMaster
from .shapes import to_long

_INDICATORS = {
    StatementType.BS: "资产负债表",
    StatementType.IS: "利润表",
    StatementType.CF: "现金流量表",
}


def call_akshare(ak: Any, symbol: str, statement: StatementType, indicator: str = "年度") -> pd.DataFrame:
    func = getattr(ak, "stock_financial_hk_report_em", None)
    if func is None:
        raise SourceError("akshare has no stock_financial_hk_report_em; upgrade akshare")
    code = str(symbol).zfill(5)
    try:
        return func(stock=code, symbol=_INDICATORS[statement], indicator=indicator)
    except TypeError:
        # Legacy/alternate signature seen in older examples.
        return func(symbol=code, indicator=_INDICATORS[statement])


class AkShareHKAdapter:
    name = "akshare"

    def __init__(
        self,
        master: SecurityMaster,
        calendar: DisclosureCalendar,
        client: Any = None,
        indicator: str = "年度",
    ) -> None:
        self.master = master
        self.calendar = calendar
        self._client = client
        self.indicator = indicator

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import akshare as ak  # type: ignore
            except ImportError as exc:  # pragma: no cover - depends on extras
                raise SourceError("akshare not installed: pip install '.[sources]'") from exc
            self._client = ak
        return self._client

    def fetch(self, symbol: str, statements: Iterable[StatementType] | None = None) -> list[RawReport]:
        security = self.master.get(symbol)
        currency = (security.akshare_currency or "CNY").upper()
        reports: list[RawReport] = []
        for statement in (statements or StatementType):
            raw = call_akshare(self.client, symbol, StatementType(statement), self.indicator)
            long = to_long(raw, fiscal_year_end=security.fiscal_year_end)
            if long.empty:
                continue
            if not {"raw_label", "value"} <= set(long.columns):
                raise SchemaDriftError("akshare HK payload changed: no STD_ITEM_NAME/AMOUNT")
            for period_end, group in long.groupby("period_end", sort=True):
                period_end = to_date(period_end)
                period_type = PeriodType(str(group["period_type"].iloc[0]).upper())
                disclosure = self.calendar.resolve(symbol, period_end, period_type.value, source="akshare")
                reports.append(RawReport(
                    symbol=str(symbol).zfill(5),
                    source=self.name,
                    statement=StatementType(statement),
                    period_end=period_end,
                    period_type=period_type,
                    currency=currency,
                    unit_scale=1.0,
                    unit_label="元(CNY, 东财已折算)",
                    published_at=disclosure.published_at,
                    available_at=disclosure.published_at,
                    data=group[["raw_label", "raw_code", "value"]].reset_index(drop=True),
                    metadata={
                        "indicator": self.indicator,
                        "disclosure_origin": disclosure.origin,
                        "disclosure_estimated": disclosure.is_estimated,
                        "vendor_currency_note": "Eastmoney converts all HK filers to CNY",
                    },
                ))
        return reports
