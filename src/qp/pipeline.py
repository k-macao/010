"""Pipeline orchestrator: the four stages from the design, wired end to end.

    ingest()           Step 1 多源拉取 → 字段/币种/单位标准化 → 原始快照入库
    ingest() (notes)   Step 3 PDF/TXT 附注 → LLM/正则 → 非经常性损益明细入库
    compute_snapshot() Step 2 FCF 硬编码复算 + Step 3 规则层合并 + PIT 对齐
                       Step 4 因子计算（PIT 汇率 + PIT 市值）
    run()              入库 + 质量门禁 + 监控报告 + ClickHouse 镜像
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .clean.align import MultiSourceAligner, choose_period
from .clean.fcf import FCFEngine
from .clean.field_map import FieldMatcher
from .clean.normalize import Normalizer
from .clean.notes.engine import NonRecurringEngine, drafts_to_items
from .clean.notes.llm import create_note_parser
from .clean.notes.models import ParsedNotes
from .clean.notes.pdf_notes import extract_note_chunks, infer_period_end
from .clean.quality import QualityChecker
from .config import Config
from .domain import (
    NonRecurringItem,
    QualityEvent,
    RawReport,
    RunSummary,
    Severity,
    Snapshot,
)
from .errors import MissingFieldError, QPError
from .factors import FactorEngine
from .ingest.disclosure import DisclosureCalendar, SecurityMaster
from .ingest.fx import FxRateProvider
from .ingest.hub import FetchEvent, SourceHub
from .ingest.shapes import to_long
from .logging_utils import get_logger
from .pit import detect_restatements, select_latest_versions, visible_at
from .reports import ReportWriter
from .storage.clickhouse import ClickHouseMirror
from .storage.repository import Repository
from .util import json_dumps, stable_hash, to_date, to_datetime, utcnow

logger = get_logger(__name__)


@dataclass
class IngestResult:
    reports: int = 0
    facts: int = 0
    note_items: int = 0
    events: list[QualityEvent] = field(default_factory=list)
    fetch_events: list[FetchEvent] = field(default_factory=list)


def _fetch_to_quality(events: list[FetchEvent], symbol: str, run_id: str, as_of: date) -> list[QualityEvent]:
    severity = {"INFO": Severity.INFO, "WARN": Severity.WARN, "ERROR": Severity.ERROR}
    return [
        QualityEvent(run_id=run_id, symbol=symbol, severity=severity.get(e.level, Severity.INFO),
                     check=f"source_{e.level.lower()}", detail=f"[{e.source}] {e.message}",
                     as_of=as_of)
        for e in events
    ]


def rows_to_items(df: pd.DataFrame) -> list[NonRecurringItem]:
    items: list[NonRecurringItem] = []
    for row in df.to_dict(orient="records"):
        items.append(NonRecurringItem(
            symbol=str(row["symbol"]),
            period_end=to_date(row["period_end"]),
            category=str(row["category"]),
            label=str(row["label"]),
            amount=float(row["amount"]),
            profit_impact=float(row["profit_impact"]),
            direction=str(row["direction"]),
            source_kind=str(row["source_kind"]),
            evidence=str(row.get("evidence") or ""),
            confidence=float(row.get("confidence") or 0),
            currency=str(row.get("currency") or ""),
            published_at=to_datetime(row["published_at"]),
            available_at=to_datetime(row["available_at"]),
            cash_impact=bool(row.get("cash_impact")),
            page=int(row["page"]) if row.get("page") is not None and not pd.isna(row.get("page")) else None,
            raw_amount=float(row["raw_amount"]) if row.get("raw_amount") is not None and not pd.isna(row.get("raw_amount")) else None,
            raw_currency=str(row.get("raw_currency") or ""),
            corroborated=bool(row.get("corroborated")),
        ))
    return items


class Pipeline:
    def __init__(
        self,
        config: Config,
        store: Repository | None = None,
        hub: SourceHub | None = None,
        fx: FxRateProvider | None = None,
        note_parser: Any = None,
        now_fn: Any = utcnow,
    ) -> None:
        self.config = config
        self.now_fn = now_fn
        self.store = store or Repository(config.database.url, config.database.echo)
        self.store.init_schema()
        self.master = SecurityMaster(config.resolve(config.sources.securities_file))
        self.calendar = DisclosureCalendar(config.resolve(config.sources.fixtures_dir) / "announcements.csv")
        self.hub = hub or SourceHub(config, self.master, self.calendar)
        self.fx = fx or FxRateProvider.from_csv(
            config.resolve(config.fx.rates_file),
            target_currency=config.fx.target_currency,
            fallback_rates=config.fx.fallback_rates,
            max_age_days=config.fx.max_age_days,
        )
        self.fx.add_rows(self.store.all_fx_rows())
        self.matcher = FieldMatcher()
        self.normalizer = Normalizer(self.matcher, self.fx, config.fx.target_currency)
        self.aligner = MultiSourceAligner(
            config.sources.priority, config.align.conflict_rel_tolerance,
            config.align.quarantine_on_conflict,
        )
        self.fcf_engine = FCFEngine(config.fcf.capex_sign_policy, config.fcf.formula_version)
        self.factor_engine = FactorEngine(self.fx, config.fx.target_currency, config.fcf.formula_version)
        self.note_engine = NonRecurringEngine(self.fx, config.fx.target_currency)
        self.note_parser = note_parser or create_note_parser(config)
        self.mirror = ClickHouseMirror(config)
        self.reporter = ReportWriter(config)
        self.seed_counts = self.store.load_reference_data(config.resolve(config.sources.fixtures_dir))
        self.fx.add_rows(self.store.all_fx_rows())

    # ------------------------------------------------------------------ stage 1
    def ingest(
        self,
        symbols: Iterable[str],
        as_of: date,
        quality: QualityChecker,
        offline: bool | None = None,
    ) -> IngestResult:
        as_of = to_date(as_of)
        offline = self.config.sources.offline if offline is None else offline
        now = self.now_fn()
        result = IngestResult()
        for symbol in symbols:
            symbol = str(symbol).zfill(5)
            reports, fetch_events = self.hub.fetch(symbol, offline=offline)
            result.fetch_events.extend(fetch_events)
            result.events.extend(_fetch_to_quality(fetch_events, symbol, quality.run_id, as_of))
            self.normalizer.fiscal_year_end = self.master.get(symbol).fiscal_year_end
            for report in reports:
                normalized_report = self._snapshot_policy(report, now, offline)
                long = to_long(
                    normalized_report.data,
                    default_period_end=normalized_report.period_end,
                    default_period_type=normalized_report.period_type.value,
                    fiscal_year_end=self.normalizer.fiscal_year_end,
                )
                content_hash = stable_hash({
                    "key": normalized_report.key,
                    "rows": long.to_json(orient="records", force_ascii=False),
                })
                known = self.store.known_available_at(
                    symbol, report.source, report.statement.value, report.period_end, content_hash
                )
                if known is not None:
                    normalized_report.available_at = known  # idempotent: keep historic clock
                result.reports += self.store.upsert("raw_reports", [{
                    "symbol": symbol,
                    "source": report.source,
                    "statement": report.statement.value,
                    "period_end": report.period_end,
                    "published_at": report.published_at,
                    "available_at": normalized_report.available_at,
                    "period_type": report.period_type.value,
                    "currency": report.currency,
                    "unit_scale": report.unit_scale,
                    "unit_label": report.unit_label,
                    "content_hash": content_hash,
                    "payload": long.to_json(orient="records", force_ascii=False),
                    "metadata": json_dumps(report.metadata),
                    "run_id": quality.run_id,
                    "ingested_at": now,
                }])
                norm = self.normalizer.normalize(normalized_report, ingested_at=now)
                result.facts += self.store.upsert("normalized_facts", [
                    {**fact.as_row(), "run_id": quality.run_id} for fact in norm.facts
                ])
                result.events.extend(quality.check_ingest(normalized_report, norm, as_of))
            result.note_items += self._ingest_notes(symbol, reports, as_of, quality, result.events, offline)
        return result

    def _snapshot_policy(self, report: RawReport, now: datetime, offline: bool) -> RawReport:
        """Conservative PIT clock for late/restated vendor snapshots.

        A vendor that silently replaces history (AkShare does) gives us no filing
        date for the change. We therefore treat *first sighting* as availability,
        unless we are explicitly replaying a backfill (offline fixture mode).
        """
        if not self.config.pit.backfill_mode and not offline:
            report.available_at = max(report.available_at, now)
        return report

    # ------------------------------------------------------------------ stage 3
    def _ingest_notes(
        self,
        symbol: str,
        reports: list[RawReport],
        as_of: date,
        quality: QualityChecker,
        events: list[QualityEvent],
        offline: bool,
    ) -> int:
        total = 0
        documents = self.hub.note_documents(symbol, offline=offline)
        fallback_period = max((r.period_end for r in reports), default=None)
        for document in documents:
            period_str = infer_period_end(document)
            period_end = to_date(period_str) if period_str else fallback_period
            if period_end is None:
                events.append(QualityEvent(
                    run_id=quality.run_id, symbol=symbol, severity=Severity.WARN,
                    check="note_period_unknown", detail=f"cannot infer period for {document}",
                    as_of=as_of,
                ))
                continue
            chunks = extract_note_chunks(
                document, keywords=self.config.notes.keywords,
                context_chars=self.config.notes.context_chars,
                max_chunks=self.config.notes.max_chunks,
            )
            parsed = self.note_parser.parse(chunks)
            disclosure = self.calendar.resolve(symbol, period_end, "ANNUAL", source="hkexnews")
            available_at = disclosure.published_at
            items, warnings = drafts_to_items(
                parsed.items, symbol, period_end, self.fx, self.config.fx.target_currency,
                disclosure.published_at, available_at,
            )
            total += self.store.upsert("nonrecurring_items", [
                {**item.as_row(), "run_id": quality.run_id} for item in items
            ])
            for warning in warnings + parsed.warnings:
                events.append(QualityEvent(
                    run_id=quality.run_id, symbol=symbol, severity=Severity.WARN,
                    check="note_parsing", detail=warning, period_end=period_end, as_of=as_of,
                ))
            if parsed.used_fallback:
                events.append(QualityEvent(
                    run_id=quality.run_id, symbol=symbol, severity=Severity.WARN,
                    check="llm_fallback", detail="LLM unavailable; regex fallback used for notes",
                    period_end=period_end, as_of=as_of,
                ))
            events.append(QualityEvent(
                run_id=quality.run_id, symbol=symbol, severity=Severity.INFO,
                check="notes_extracted",
                detail=f"{Path(document).name}: {len(chunks)} chunks, {len(items)} items, "
                       f"parser={self.note_parser.name}",
                period_end=period_end, as_of=as_of,
            ))
        return total

    # -------------------------------------------------------------- stage 2 + 4
    def compute_snapshot(
        self,
        symbol: str,
        as_of: date,
        quality: QualityChecker | None = None,
        strict: bool | None = None,
    ) -> Snapshot:
        symbol = str(symbol).zfill(5)
        as_of = to_date(as_of)
        quality = quality or QualityChecker("adhoc", self.config.quality.strict)
        strict = self.config.quality.strict if strict is None else strict
        all_facts = self.store.get_all_facts(symbol)
        restatements = detect_restatements(all_facts)
        restated_fields = {r.field for r in restatements}
        facts = select_latest_versions(visible_at(all_facts, as_of, self.config.pit.lag_days))
        period_end, reason = choose_period(
            facts, as_of, max_staleness_days=self.config.pit.max_staleness_days
        )
        events: list[QualityEvent] = []
        for restatement in restatements:
            if to_date(restatement.new_available_at) <= as_of:
                events.append(QualityEvent(
                    run_id=quality.run_id, symbol=symbol, severity=Severity.INFO,
                    check="restatement_detected",
                    detail=f"{restatement.source}/{restatement.field}: "
                           f"{restatement.old_value:.4g} → {restatement.new_value:.4g} "
                           f"@ {restatement.new_available_at}",
                    period_end=restatement.period_end, as_of=as_of,
                ))
        if period_end is None:
            events.append(QualityEvent(
                run_id=quality.run_id, symbol=symbol, severity=Severity.WARN,
                check="no_data", detail=reason, as_of=as_of,
            ))
            return Snapshot(symbol, as_of, "NO_DATA", events=events)

        align_result = self.aligner.align(facts, symbol, period_end, as_of, restated_fields)
        aligned = align_result.report
        events.extend(quality.check_alignment(aligned, align_result, as_of))
        note_df = self.store.get_nonrecurring(symbol, as_of, period_end, self.config.pit.lag_days)
        note_items = rows_to_items(note_df)
        nonrecurring = self.note_engine.extract(
            aligned, ParsedNotes(), prebuilt_note_items=note_items,
        )
        for warning in nonrecurring.warnings:
            events.append(QualityEvent(
                run_id=quality.run_id, symbol=symbol, severity=Severity.WARN,
                check="nonrecurring", detail=warning, period_end=period_end, as_of=as_of,
            ))
        try:
            fcf = self.fcf_engine.compute(aligned, nonrecurring.cash_items)
        except MissingFieldError as exc:
            events.append(QualityEvent(
                run_id=quality.run_id, symbol=symbol, severity=Severity.ERROR,
                check="fcf_computation", detail=str(exc), period_end=period_end, as_of=as_of,
            ))
            return Snapshot(symbol, as_of, "QUARANTINED", period_end=period_end,
                            aligned=aligned, items=nonrecurring.items, events=events)

        market_cap = self.store.get_market_cap(symbol, as_of)
        factors, warnings = self.factor_engine.compute(
            aligned, fcf, nonrecurring, as_of, market_cap
        )
        for warning in warnings:
            events.append(QualityEvent(
                run_id=quality.run_id, symbol=symbol, severity=Severity.WARN,
                check="factor_fx", detail=warning, period_end=period_end, as_of=as_of,
            ))
        events.extend(quality.check_snapshot(aligned, fcf, as_of, market_cap))
        status = "OK"
        if quality.has_errors(events) and (strict or self.config.quality.fail_on_error):
            factors = []
            status = "QUARANTINED"
        return Snapshot(
            symbol=symbol, as_of=as_of, status=status, period_end=period_end,
            aligned=aligned, fcf=fcf, items=nonrecurring.items, factors=factors, events=events,
        )

    # ------------------------------------------------------------------ run
    def run(
        self,
        symbols: Iterable[str],
        as_of: date | None = None,
        offline: bool | None = None,
        strict: bool | None = None,
        store_features: bool = True,
    ) -> RunSummary:
        as_of = to_date(as_of or date.today())
        symbols = [str(s).zfill(5) for s in symbols]
        started = self.now_fn()
        run_id = f"run-{started:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        strict = self.config.quality.strict if strict is None else strict
        quality = QualityChecker(run_id, strict, self.config.pit.max_staleness_days)
        self.store.init_schema()
        try:
            ingested = self.ingest(symbols, as_of, quality, offline=offline)
            snapshots = [self.compute_snapshot(symbol, as_of, quality, strict) for symbol in symbols]
            events = ingested.events + [e for s in snapshots for e in s.events]
            feature_rows = [
                {**f.as_row(), "computed_at": started, "run_id": run_id}
                for s in snapshots for f in s.factors
            ]
            if store_features:
                self.store.upsert("derived_features", feature_rows)
                self.mirror.push_features([f for s in snapshots for f in s.factors], run_id)
            counts = {
                "raw_reports": ingested.reports,
                "normalized_facts": ingested.facts,
                "note_items": ingested.note_items,
                "features": len(feature_rows),
                "market_caps": self.seed_counts.get("market_caps", 0),
                "fx_rates": self.seed_counts.get("fx_rates", 0),
                **{f"events_{k.lower()}": v for k, v in quality.counts(events).items()},
            }
            status = "OK" if all(s.status == "OK" for s in snapshots) else "PARTIAL"
            if any(s.status == "QUARANTINED" for s in snapshots):
                status = "QUARANTINED"
            summary = RunSummary(
                run_id=run_id, started_at=started, finished_at=self.now_fn(), as_of=as_of,
                status=status, symbols=symbols, counts=counts, events=events, snapshots=snapshots,
            )
            self.store.insert_quality_events(run_id, [e.as_row() for e in events])
            self.store.save_run(summary)
            summary.report_path = str(self.reporter.write(summary))
            self.store.save_run(summary)
            return summary
        except QPError as exc:
            logger.error("pipeline aborted", extra={"context": {"run_id": run_id, "error": str(exc)}})
            summary = RunSummary(run_id, started, self.now_fn(), as_of, "FAILED",
                                 symbols, {}, [], [])
            self.store.save_run(summary)
            raise

    # -------------------------------------------------------------- utilities
    def history(self, symbol: str, dates: Iterable[date]) -> pd.DataFrame:
        """Read-only PIT factor replay: the easiest way to catch look-ahead bias."""
        quality = QualityChecker("history", False, self.config.pit.max_staleness_days)
        rows: list[dict[str, Any]] = []
        for day in dates:
            snapshot = self.compute_snapshot(symbol, to_date(day), quality, strict=False)
            rows.append({
                "as_of": snapshot.as_of.isoformat(),
                "status": snapshot.status,
                "period_end": snapshot.period_end.isoformat() if snapshot.period_end else None,
                "standard_fcf": snapshot.factor("standard_fcf"),
                "fcf_yield": snapshot.factor("fcf_yield"),
                "recurring_net_profit": snapshot.factor("recurring_net_profit"),
                "ocf": snapshot.fcf.ocf if snapshot.fcf else None,
                "capex": snapshot.fcf.capex if snapshot.fcf else None,
            })
        return pd.DataFrame(rows)
