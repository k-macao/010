"""Source orchestration: priority, retries, per-source isolation, fallback.

A failing vendor never aborts the symbol: the error is recorded, the next source
in ``sources.priority`` is tried, and the aligner fills missing fields from it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..config import Config
from ..domain import RawReport, StatementType
from ..errors import SourceError
from ..logging_utils import get_logger
from .akshare_hk import AkShareHKAdapter
from .base import SourceAdapter
from .disclosure import Announcement, DisclosureCalendar, SecurityMaster
from .fixtures import FixtureAdapter
from .hkexnews import HKEXnewsAdapter, HttpFetcher
from .yfinance_hk import YFinanceHKAdapter

logger = get_logger(__name__)


@dataclass(frozen=True)
class FetchEvent:
    source: str
    level: str
    message: str
    statement: str = ""


class SourceHub:
    def __init__(
        self,
        config: Config,
        master: SecurityMaster | None = None,
        calendar: DisclosureCalendar | None = None,
        fetcher: HttpFetcher | None = None,
        adapters: dict[str, SourceAdapter] | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.config = config
        self.master = master or SecurityMaster(config.resolve(config.sources.securities_file))
        self.calendar = calendar or DisclosureCalendar(config.resolve(config.sources.fixtures_dir) / "announcements.csv")
        self.fetcher = fetcher
        self.sleep = sleep
        self._adapters = adapters or {}
        self._fixtures = FixtureAdapter(config.resolve(config.sources.fixtures_dir))
        self._hkexnews_local = HKEXnewsAdapter(
            config, self.master, self.calendar, fetcher=fetcher,
            local_dir=config.resolve(config.sources.fixtures_dir) / "hkexnews",
            enable_network=False,
        )

    def adapter(self, name: str) -> SourceAdapter:
        if name in self._adapters:
            return self._adapters[name]
        if name == "akshare":
            adapter: SourceAdapter = AkShareHKAdapter(self.master, self.calendar)
        elif name == "yfinance":
            adapter = YFinanceHKAdapter(self.master, self.calendar)
        elif name == "hkexnews":
            adapter = HKEXnewsAdapter(self.config, self.master, self.calendar, fetcher=self.fetcher)
        elif name == "fixture":
            adapter = self._fixtures
        else:
            raise SourceError(f"unknown source {name!r}")
        self._adapters[name] = adapter
        return adapter

    def fetch(self, symbol: str, offline: bool | None = None) -> tuple[list[RawReport], list[FetchEvent]]:
        offline = self.config.sources.offline if offline is None else offline
        if offline:
            reports = list(self._fixtures.fetch(symbol))
            events: list[FetchEvent] = []
            try:
                reports += self._hkexnews_local.fetch(symbol)
            except SourceError as exc:
                events.append(FetchEvent("hkexnews", "WARN", str(exc)))
            events.append(FetchEvent("fixture", "INFO",
                                     f"loaded {len(reports)} offline snapshots for {symbol}"))
            return reports, events

        reports, events = [], []
        for name in self.config.sources.priority:
            for attempt in range(self.config.sources.retry + 1):
                try:
                    got = self.adapter(name).fetch(symbol, list(StatementType))
                    reports.extend(got)
                    events.append(FetchEvent(name, "INFO", f"{len(got)} snapshots"))
                    break
                except Exception as exc:  # one bad vendor must not kill the run
                    message = f"{type(exc).__name__}: {exc}"
                    if attempt < self.config.sources.retry:
                        self.sleep(self.config.sources.retry_backoff_seconds * (2 ** attempt))
                        continue
                    events.append(FetchEvent(name, "ERROR", message))
                    logger.warning("source failed", extra={"context": {
                        "source": name, "symbol": symbol, "error": message,
                    }})
        if not reports:
            events.append(FetchEvent("all", "ERROR", "no source returned any snapshot"))
        return reports, events

    def refresh_calendar(self, symbol: str, lookback_days: int = 3650) -> list[Announcement]:
        """Pull HKEXnews filing timestamps into the disclosure calendar (PIT clock)."""
        adapter = self.adapter("hkexnews")
        if not isinstance(adapter, HKEXnewsAdapter):
            return []
        end = date.today()
        start = end - timedelta(days=lookback_days)
        filings = adapter.search_filings(symbol, start, end)
        rows = [
            Announcement(
                symbol=f.symbol, period_end=f.period_end or end,
                period_type=f.period_type, published_at=f.published_at,
                source="hkexnews", title=f.title, url=f.url,
            )
            for f in filings if f.period_end is not None
        ]
        self.calendar.add(rows)
        return rows

    def note_documents(self, symbol: str, offline: bool | None = None) -> list[Path]:
        offline = self.config.sources.offline if offline is None else offline
        base = self._hkexnews_local if offline else self.adapter("hkexnews")
        if isinstance(base, HKEXnewsAdapter):
            return base.find_note_documents(symbol)
        return []

    def fixtures_dir(self) -> Path:
        return self.config.resolve(self.config.sources.fixtures_dir)
