"""HKEXnews (披露易): authoritative filing clock + PDF/XBRL documents.

Two independent jobs:

* **Disclosure clock** — ``titleSearchServlet.do`` returns the real ``DATE_TIME``
  (HKT) of each filing. This is the only trustworthy PIT timestamp for HK issuers.
  Endpoint shape is based on the site's own config.js (undocumented, may change),
  so all URLs/params are configurable and a local cache is supported.
* **Documents** — annual/interim report PDFs (for LLM note parsing) and XBRL
  instance documents (for structured primary-source facts). Downloaded documents
  are cached under ``raw_dir``; the adapter also works fully offline on a local
  directory, which is how the demo/CI fixtures are loaded.

Network access is injected via ``HttpFetcher``, so it is unit-testable without
touching the internet.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Protocol

from ..config import Config
from ..domain import PeriodType, RawReport, StatementType
from ..errors import SourceError
from .disclosure import DisclosureCalendar, SecurityMaster
from .xbrl import parse_xbrl_instance, xbrl_facts_to_frame


@dataclass(frozen=True)
class Filing:
    symbol: str
    news_id: str
    title: str
    published_at: datetime       # naive UTC (converted from HKT)
    url: str
    file_type: str = "PDF"
    period_end: date | None = None
    period_type: str = "ANNUAL"


class HttpFetcher(Protocol):
    def get(self, url: str, params: dict[str, Any] | None = None, max_bytes: int = 25_000_000) -> bytes:
        ...


class UrllibFetcher:
    def __init__(self, timeout: int = 30, user_agent: str = "Mozilla/5.0 (qp-hk-pipeline)") -> None:
        self.timeout = timeout
        self.user_agent = user_agent

    def get(self, url: str, params: dict[str, Any] | None = None, max_bytes: int = 25_000_000) -> bytes:
        full = url + ("?" + urllib.parse.urlencode(params) if params else "")
        request = urllib.request.Request(full, headers={
            "User-Agent": self.user_agent,
            "Referer": "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=en",
        })
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise SourceError(f"document too large (> {max_bytes} bytes): {full}")
        return payload


def hkt_to_utc_naive(value: str) -> datetime:
    """HKEXnews DATE_TIME is 'DD/MM/YYYY HH:MM' in Asia/Hong_Kong (UTC+8)."""
    dt = datetime.strptime(value.strip(), "%d/%m/%Y %H:%M")
    return (dt - timedelta(hours=8)).replace(microsecond=0)


def infer_period_end(title: str, published_at: datetime, fiscal_year_end: str = "12-31") -> tuple[date, str]:
    """Best-effort period from a filing title; the calendar CSV is authoritative."""
    years = [int(y) for y in re.findall(r"(?:19|20)\d{2}", title)]
    year = years[0] if years else published_at.year - 1
    low = title.lower()
    interim = any(word in low for word in ("interim", "half-year", "中期", "中期报告", "q2", "quarterly", "季度"))
    if interim:
        return date(year, 6, 30), "INTERIM"
    month, day = (int(x) for x in fiscal_year_end.split("-"))
    return date(year, month, day), "ANNUAL"


class HKEXnewsAdapter:
    name = "hkexnews"

    def __init__(
        self,
        config: Config,
        master: SecurityMaster,
        calendar: DisclosureCalendar,
        fetcher: HttpFetcher | None = None,
        local_dir: str | Path | None = None,
        enable_network: bool = True,
    ) -> None:
        self.config = config
        self.master = master
        self.calendar = calendar
        self.fetcher: HttpFetcher = fetcher or UrllibFetcher(config.sources.request_timeout_seconds)
        self.local_dir = Path(local_dir) if local_dir else config.resolve(config.sources.raw_dir)
        self.enable_network = enable_network
        self._stock_ids: dict[str, int] | None = None

    # ---------------------------- stock id ---------------------------------
    def _stock_id(self, symbol: str) -> int:
        if self._stock_ids is None:
            cache_path = self.local_dir / "stock_ids.json"
            if cache_path.exists():
                self._stock_ids = json.loads(cache_path.read_text(encoding="utf-8"))
            elif not self.enable_network:
                self._stock_ids = {}
            else:
                payload = self.fetcher.get(
                    self.config.sources.hkexnews_base.rstrip("/") + self.config.sources.hkexnews_stock_list_path
                )
                rows = json.loads(payload.decode("utf-8"))
                self._stock_ids = {str(row["c"]).zfill(5): int(row["i"]) for row in rows if "c" in row and "i" in row}
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(self._stock_ids), encoding="utf-8")
        code = str(symbol).zfill(5)
        if code not in self._stock_ids:
            raise SourceError(f"HKEXnews stockId not found for {code}; check the stock list cache")
        return self._stock_ids[code]

    # ---------------------------- filings ----------------------------------
    def search_filings(self, symbol: str, start: date, end: date, row_range: int = 100) -> list[Filing]:
        if not self.enable_network:
            return []
        params = {
            "market": "SEHK",
            "stockId": self._stock_id(symbol),
            "searchType": 0,
            "fromDate": start.strftime("%Y%m%d"),
            "toDate": end.strftime("%Y%m%d"),
            "sortDir": 0,
            "sortByOptions": "DateTime",
            "rowRange": row_range,
            "lang": "E",
            "t1code": self.config.sources.hkexnews_financial_category,
            "t2Gcode": -2,
            "t2code": -2,
        }
        url = self.config.sources.hkexnews_base.rstrip("/") + self.config.sources.hkexnews_search_path
        payload = self.fetcher.get(url, params=params, max_bytes=5_000_000)
        envelope = json.loads(payload.decode("utf-8"))
        rows = envelope.get("result", "[]")
        if isinstance(rows, str):
            rows = json.loads(rows)
        security = self.master.get(symbol)
        out: list[Filing] = []
        for row in rows:
            title = str(row.get("TITLE") or row.get("LONG_TEXT") or "")
            raw_time = str(row.get("DATE_TIME", ""))
            try:
                published = hkt_to_utc_naive(raw_time)
            except ValueError:
                continue  # malformed row: never invent a disclosure timestamp
            link = str(row.get("FILE_LINK", ""))
            full_url = urllib.parse.urljoin(self.config.sources.hkexnews_base, link)
            period_end, period_type = infer_period_end(title, published, security.fiscal_year_end)
            out.append(Filing(
                symbol=str(symbol).zfill(5), news_id=str(row.get("NEWS_ID", "")),
                title=title, published_at=published, url=full_url,
                file_type=str(row.get("FILE_TYPE", "PDF")),
                period_end=period_end, period_type=period_type,
            ))
        return out

    def download(self, filing: Filing) -> Path:
        dest = self.local_dir / filing.symbol / f"{filing.published_at.date().isoformat()}_{filing.news_id}.pdf"
        if dest.exists():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        payload = self.fetcher.get(filing.url, max_bytes=self.config.sources.max_document_bytes)
        dest.write_bytes(payload)
        return dest

    # ---------------------------- local docs -------------------------------
    def local_documents(self, symbol: str, suffix: str = ".pdf") -> list[Path]:
        folder = self.local_dir / str(symbol).zfill(5)
        if not folder.exists():
            return []
        return sorted(p for p in folder.iterdir() if p.suffix.lower() == suffix)

    def find_note_documents(self, symbol: str) -> list[Path]:
        return self.local_documents(symbol, ".pdf") + self.local_documents(symbol, ".txt")

    def local_xbrl(self, symbol: str) -> list[Path]:
        return self.local_documents(symbol, ".xml") + self.local_documents(symbol, ".xbrl")

    # ---------------------------- adapter API ------------------------------
    def fetch(self, symbol: str, statements: Iterable[StatementType] | None = None) -> list[RawReport]:
        """Primary-source facts from cached XBRL instance documents (offline-friendly)."""
        del statements  # XBRL documents carry all three statements in one instance
        reports: list[RawReport] = []
        security = self.master.get(symbol)
        for path in self.local_xbrl(symbol):
            facts = parse_xbrl_instance(path)
            if not facts:
                continue
            from ..clean.field_map import FieldMatcher

            matcher = FieldMatcher()
            # (period, statement) -> facts; XBRL mixes all three statements in one file.
            grouped: dict[tuple[date, StatementType], list[Any]] = {}
            for fact in facts:
                if fact.currency and fact.currency.upper() not in {"CNY", "HKD", "USD", "RMB", "EUR", "GBP"}:
                    continue  # shares, percentages, pure numbers
                hit = matcher.match_any(fact.canonical_label)
                if hit is None:
                    continue
                grouped.setdefault((fact.period_end, hit.spec.statement), []).append(fact)
            for (period_end, statement), period_facts in sorted(grouped.items(), key=lambda kv: (kv[0][0], kv[0][1].value)):
                currency = next((f.currency for f in period_facts if f.currency), security.report_currency)
                disclosure = self.calendar.resolve(symbol, period_end, "ANNUAL", source="hkexnews")
                reports.append(RawReport(
                    symbol=str(symbol).zfill(5),
                    source=self.name,
                    statement=statement,
                    period_end=period_end,
                    period_type=PeriodType.ANNUAL,
                    currency=currency.upper(),
                    unit_scale=1.0,
                    unit_label="元(XBRL)",
                    published_at=disclosure.published_at,
                    available_at=disclosure.published_at,
                    data=xbrl_facts_to_frame(period_facts),
                    metadata={"document": str(path), "xbrl": True,
                              "disclosure_origin": disclosure.origin},
                ))
        return reports
