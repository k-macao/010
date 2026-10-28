"""yfinance fallback adapter: structurally uniform English fields, PIT-weak.

yfinance gives ``ticker.cashflow / income_stmt / balance_sheet`` with index = line
items and columns = period ends, values in the filer's ``financialCurrency``.
It has NO filing date, NO restatement history, and only ~4 periods. Use it to fill
missing fields, never as the disclosure clock.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..domain import PeriodType, RawReport, StatementType
from ..errors import SourceError
from ..util import to_date
from .disclosure import DisclosureCalendar, SecurityMaster
from .shapes import to_long

_ATTRS = {
    StatementType.CF: "cashflow",
    StatementType.IS: "income_stmt",
    StatementType.BS: "balance_sheet",
}


class YFinanceHKAdapter:
    name = "yfinance"

    def __init__(self, master: SecurityMaster, calendar: DisclosureCalendar, client: Any = None) -> None:
        self.master = master
        self.calendar = calendar
        self._client = client

    def _ticker(self, symbol: str) -> Any:
        security = self.master.get(symbol)
        if self._client is not None:
            return self._client.Ticker(security.ticker)
        try:
            import yfinance as yf  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise SourceError("yfinance not installed: pip install '.[sources]'") from exc
        return yf.Ticker(security.ticker)

    def fetch(self, symbol: str, statements: Iterable[StatementType] | None = None) -> list[RawReport]:
        security = self.master.get(symbol)
        ticker = self._ticker(symbol)
        try:
            currency = str(ticker.info.get("financialCurrency") or security.report_currency).upper()
        except Exception:
            currency = security.report_currency.upper()
        reports: list[RawReport] = []
        for statement in (statements or StatementType):
            statement = StatementType(statement)
            frame = getattr(ticker, _ATTRS[statement], None)
            if frame is None or getattr(frame, "empty", True):
                continue
            long = to_long(frame, fiscal_year_end=security.fiscal_year_end)
            if long.empty:
                continue
            for period_end, group in long.groupby("period_end", sort=True):
                period_end = to_date(period_end)
                period_type = PeriodType(str(group["period_type"].iloc[0]).upper())
                disclosure = self.calendar.resolve(symbol, period_end, period_type.value, source="yfinance")
                reports.append(RawReport(
                    symbol=str(symbol).zfill(5),
                    source=self.name,
                    statement=statement,
                    period_end=period_end,
                    period_type=period_type,
                    currency=currency,
                    unit_scale=1.0,
                    unit_label="元(原币)",
                    published_at=disclosure.published_at,
                    available_at=disclosure.published_at,
                    data=group[["raw_label", "raw_code", "value"]].reset_index(drop=True),
                    metadata={
                        "ticker": security.ticker,
                        "disclosure_origin": disclosure.origin,
                        "disclosure_estimated": disclosure.is_estimated,
                        "pit_warning": "yfinance has no filing-date/restatement history",
                    },
                ))
        return reports
