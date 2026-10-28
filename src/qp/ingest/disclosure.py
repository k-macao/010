"""Security master + disclosure calendar: the two things vendors do NOT give you.

AkShare / yfinance omit both the *reporting currency* and the *announcement date*.
Using fiscal period_end as the disclosure date is the classic HK look-ahead bug,
so we resolve it from HKEXnews announcements and only fall back to a conservative
lag estimate, flagging it as such in the quality report.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from ..util import to_date, to_datetime
from .base import DisclosureRecord

DEFAULT_LAG_DAYS = {"ANNUAL": 90, "INTERIM": 60, "QUARTERLY": 45}


@dataclass(frozen=True)
class SecurityInfo:
    symbol: str
    name: str
    report_currency: str
    fiscal_year_end: str
    market_currency: str = "HKD"
    yfinance_ticker: str = ""
    akshare_currency: str = ""

    @property
    def ticker(self) -> str:
        return self.yfinance_ticker or f"{self.symbol[-4:]}.HK"


class SecurityMaster:
    def __init__(self, path: str | Path | None = None) -> None:
        self._items: dict[str, SecurityInfo] = {}
        if path and Path(path).exists():
            with Path(path).open("r", encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    info = SecurityInfo(
                        symbol=str(row["symbol"]).zfill(5),
                        name=row.get("name", ""),
                        report_currency=row.get("report_currency", "HKD").upper(),
                        fiscal_year_end=row.get("fiscal_year_end", "12-31"),
                        market_currency=row.get("market_currency", "HKD").upper(),
                        yfinance_ticker=row.get("yfinance_ticker", ""),
                        akshare_currency=row.get("akshare_currency", "").upper(),
                    )
                    self._items[info.symbol] = info

    def get(self, symbol: str) -> SecurityInfo:
        key = str(symbol).zfill(5)
        return self._items.get(key, SecurityInfo(key, "", "HKD", "12-31"))

    def all(self) -> list[SecurityInfo]:
        return list(self._items.values())


@dataclass(frozen=True)
class Announcement:
    symbol: str
    period_end: date
    period_type: str
    published_at: datetime
    source: str
    title: str = ""
    url: str = ""
    is_restatement: bool = False


class DisclosureCalendar:
    """Exact announcement dates when known, conservative estimate + WARN otherwise.

    CSV columns: symbol,period_end,period_type,published_at,source,title,url,is_restatement
    Rows are PIT-safe: a restatement gets its OWN published_at.
    """

    def __init__(self, path: str | Path | None = None, lag_days: dict[str, int] | None = None) -> None:
        self._rows: list[Announcement] = []
        self.lag_days = dict(DEFAULT_LAG_DAYS)
        if lag_days:
            self.lag_days.update(lag_days)
        if path and Path(path).exists():
            with Path(path).open("r", encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    if not row.get("symbol") or not row.get("period_end"):
                        continue
                    self._rows.append(Announcement(
                        symbol=str(row["symbol"]).zfill(5),
                        period_end=to_date(row["period_end"]),
                        period_type=str(row.get("period_type", "ANNUAL")).upper(),
                        published_at=to_datetime(row["published_at"]),
                        source=row.get("source", "hkexnews"),
                        title=row.get("title", ""),
                        url=row.get("url", ""),
                        is_restatement=str(row.get("is_restatement", "")).lower() in {"1", "true", "yes"},
                    ))

    def resolve(
        self,
        symbol: str,
        period_end: date,
        period_type: str = "ANNUAL",
        source: str | None = None,
    ) -> DisclosureRecord:
        candidates = [
            row for row in self._rows
            if row.symbol == str(symbol).zfill(5)
            and row.period_end == period_end
            and (source is None or row.source in {source, "hkexnews", "all"})
        ]
        if candidates:
            chosen = max(candidates, key=lambda r: r.published_at)
            return DisclosureRecord(
                published_at=chosen.published_at, is_estimated=False, origin=chosen.source,
                title=chosen.title, url=chosen.url, is_restatement=chosen.is_restatement,
            )
        lag = self.lag_days.get(period_type.upper(), 90)
        return DisclosureRecord(
            published_at=to_datetime(period_end) + timedelta(days=lag),
            is_estimated=True, origin="lag_estimate",
        )

    def add(self, rows: Iterable[Announcement]) -> None:
        """Merge newly discovered HKEXnews filings into the in-memory calendar."""
        seen = {(r.symbol, r.period_end, r.period_type, r.published_at, r.source) for r in self._rows}
        for row in rows:
            key = (row.symbol, row.period_end, row.period_type, row.published_at, row.source)
            if key not in seen:
                self._rows.append(row)
                seen.add(key)

    def versions_for(self, symbol: str, period_end: date) -> list[Announcement]:
        return sorted(
            (r for r in self._rows if r.symbol == str(symbol).zfill(5) and r.period_end == period_end),
            key=lambda r: r.published_at,
        )

    @classmethod
    def from_rows(cls, rows: list[dict[str, Any]], path: str | Path | None = None) -> "DisclosureCalendar":
        obj = cls(path=None)
        for row in rows:
            obj._rows.append(Announcement(
                symbol=str(row["symbol"]).zfill(5),
                period_end=to_date(row["period_end"]),
                period_type=str(row.get("period_type", "ANNUAL")).upper(),
                published_at=to_datetime(row["published_at"]),
                source=row.get("source", "hkexnews"),
                title=row.get("title", ""),
                url=row.get("url", ""),
                is_restatement=bool(row.get("is_restatement", False)),
            ))
        return obj
