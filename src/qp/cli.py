"""Command line interface: `qp demo`, `qp run`, `qp history`, ..."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from . import __version__
from .config import load_config
from .logging_utils import get_logger, setup_logging
from .pipeline import Pipeline
from .reports import ReportWriter
from .util import to_date

logger = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qp",
        description="港股量化财务数据流水线：多源拉取 → 清洗 → FCF → 非经常性损益 → PIT 因子入库",
    )
    parser.add_argument("--version", action="version", version=f"qp {__version__}")
    parser.add_argument("--config", help="TOML config path (default config/pipeline.toml)")
    parser.add_argument("--offline", action="store_true", help="use examples/fixtures, no network")
    parser.add_argument("--db-url", help="override database URL")
    parser.add_argument("--strict", action="store_true", help="quarantine symbols with ERROR events")
    parser.add_argument("--log-level", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create/upgrade relational schema")

    p_ingest = sub.add_parser("ingest", help="fetch + clean + store raw/facts (no factor write)")
    p_ingest.add_argument("--symbols", default="00700,09988")
    p_ingest.add_argument("--as-of", default=None)

    p_run = sub.add_parser("run", help="full pipeline for one as-of date")
    p_run.add_argument("--symbols", default="00700,09988")
    p_run.add_argument("--as-of", default=None)
    p_run.add_argument("--no-store", action="store_true", help="compute but do not write features")

    p_factors = sub.add_parser("factors", help="read stored PIT factors")
    p_factors.add_argument("--symbol", required=True)
    p_factors.add_argument("--as-of", default=None)

    p_history = sub.add_parser("history", help="PIT factor replay over a date grid")
    p_history.add_argument("--symbol", required=True)
    p_history.add_argument("--start", required=True)
    p_history.add_argument("--end", required=True)
    p_history.add_argument("--freq", default="QE", help="pandas offset alias: QE / ME / YE")
    p_history.add_argument("--run", action="store_true", help="ingest fixtures first")

    p_report = sub.add_parser("report", help="print a stored run manifest")
    p_report.add_argument("--run-id", required=True)

    p_demo = sub.add_parser("demo", help="offline end-to-end demo (fixtures → factors → report)")
    p_demo.add_argument("--symbol", default="00700")
    p_demo.add_argument("--as-of", default=None)

    p_push = sub.add_parser("push", help="run pipeline and send a PushPlus WeChat report")
    p_push.add_argument("--symbols", default=os.environ.get("QP_NOTIFY_SYMBOLS", "00700,09988"))
    p_push.add_argument("--as-of", default=None, help="evaluation date (default: China local date)")
    p_push.add_argument("--slot", choices=("早报", "午报", "晚报"), default=None)
    p_push.add_argument("--dry-run", action="store_true", help="print report without sending")

    sub.add_parser("check", help="validate config and storage connectivity")
    return parser


def _config_for(args: argparse.Namespace) -> Any:
    overrides: dict[str, dict[str, Any]] = {}
    if args.db_url:
        overrides["database"] = {"url": args.db_url}
    if args.offline:
        overrides["sources"] = {"offline": True}
    if args.log_level:
        overrides["reports"] = {"log_level": args.log_level}
    return load_config(args.config, overrides=overrides or None)


def _symbols(value: str) -> list[str]:
    return [s.strip().zfill(5) for s in value.split(",") if s.strip()]


def _as_of(value: str | None) -> date:
    return to_date(value) if value else date.today()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = _config_for(args)
    setup_logging(config.reports.log_level)

    if args.command == "check":
        pipeline = Pipeline(config)
        print(json.dumps({
            "config": str(config.path or "defaults"),
            "database": config.database.url,
            "offline": config.sources.offline,
            "sources": list(config.sources.priority),
            "target_currency": config.fx.target_currency,
            "llm_enabled": config.llm.enabled,
            "reference_data": pipeline.seed_counts,
            "status": "OK",
        }, ensure_ascii=False, indent=2))
        return 0

    if args.command == "init-db":
        pipeline = Pipeline(config)
        print(f"schema ready: {config.database.url}")
        print(f"reference data: {pipeline.seed_counts}")
        return 0

    if args.command == "report":
        pipeline = Pipeline(config)
        stored = pipeline.store.get_run(args.run_id)
        if stored is None:
            print(f"run not found: {args.run_id}", file=sys.stderr)
            return 1
        print(json.dumps(stored, ensure_ascii=False, indent=2))
        return 0

    if args.command == "push":
        from .pushplus import PushPlusError, current_slot, format_summary, send_pushplus

        token = os.environ.get("QP_PUSHPLUS_TOKEN", "").strip()
        if not token and not args.dry_run:
            print("未配置 QP_PUSHPLUS_TOKEN，未运行流水线或发送推送", file=sys.stderr)
            return 1
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        as_of = to_date(args.as_of) if args.as_of else now.date()
        title = f"港股财务{args.slot or current_slot(now)} {as_of}"
        symbols = _symbols(args.symbols)
        if not symbols:
            print("股票列表不能为空", file=sys.stderr)
            return 1
        try:
            pipeline = Pipeline(config)
            summary = pipeline.run(symbols, as_of=as_of, offline=True if args.offline else None,
                                   strict=True if args.strict else None)
            content = format_summary(summary, offline=config.sources.offline)
            status = 0 if summary.status == "OK" else 1
        except Exception as exc:
            # Exception messages may contain credentials or vendor response bodies.
            content = f"日期：{as_of}\n运行状态：FAILED\n流水线运行失败，请检查本地运行环境。"
            status = 1
            print(f"流水线运行失败（{type(exc).__name__}），正在发送失败通知", file=sys.stderr)
        if args.dry_run:
            print(f"{title}\n{content}")
            return status
        try:
            send_pushplus(token, title, content)
        except PushPlusError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print("PushPlus 已接受消息；微信实际送达以 PushPlus 为准。")
        return status

    pipeline = Pipeline(config)

    if args.command == "ingest":
        from .clean.quality import QualityChecker

        as_of = _as_of(args.as_of)
        quality = QualityChecker(f"ingest-{as_of.isoformat()}", config.quality.strict)
        result = pipeline.ingest(_symbols(args.symbols), as_of, quality)
        print(json.dumps({
            "as_of": as_of.isoformat(), "reports": result.reports,
            "facts": result.facts, "note_items": result.note_items,
            "warnings": [e.detail for e in result.events if e.severity.value in {"WARN", "ERROR"}][:10],
        }, ensure_ascii=False, indent=2))
        return 0

    if args.command == "run":
        summary = pipeline.run(
            _symbols(args.symbols), as_of=_as_of(args.as_of),
            offline=True if args.offline else None,
            strict=True if args.strict else None,
            store_features=not args.no_store,
        )
        print(ReportWriter.console(summary))
        return 0 if summary.status != "FAILED" else 1

    if args.command == "demo":
        summary = pipeline.run(
            [args.symbol], as_of=_as_of(args.as_of), offline=True, strict=False,
        )
        print(ReportWriter.console(summary))
        print("\nPIT replay (look-ahead check):")
        start = date(2025, 3, 31)
        end = min(_as_of(args.as_of), date(2026, 9, 30))
        grid = pd.date_range(start, end, freq="QE").date
        print(pipeline.history(args.symbol, grid).to_string(index=False))
        return 0

    if args.command == "factors":
        as_of = _as_of(args.as_of)
        frame = pipeline.store.get_features(str(args.symbol).zfill(5), as_of)
        if frame.empty:
            snap = pipeline.compute_snapshot(args.symbol, as_of)
            print(json.dumps({
                "symbol": args.symbol, "as_of": as_of.isoformat(), "status": snap.status,
                "factors": [f.as_row() for f in snap.factors],
            }, ensure_ascii=False, indent=2))
        else:
            print(frame[["symbol", "as_of", "period_end", "feature", "value", "unit",
                         "formula_version"]].to_string(index=False))
        return 0

    if args.command == "history":
        if args.run:
            from .clean.quality import QualityChecker

            quality = QualityChecker("history-ingest", False)
            pipeline.ingest([args.symbol], _as_of(args.end), quality, offline=True)
        freq = {"Q": "QE", "M": "ME", "Y": "YE", "A": "YE"}.get(args.freq, args.freq)
        grid = pd.date_range(_as_of(args.start), _as_of(args.end), freq=freq).date
        frame = pipeline.history(args.symbol, grid)
        print(frame.to_string(index=False))
        return 0

    print(f"unknown command: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
