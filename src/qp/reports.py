"""Run manifests, human-readable console output, and optional alerting."""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Any

from .config import Config
from .domain import RunSummary, Severity
from .logging_utils import get_logger
from .util import utcnow

logger = get_logger(__name__)


class ReportWriter:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.output_dir = config.resolve(config.reports.output_dir)

    def write(self, summary: RunSummary) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / f"{summary.run_id}.json"
        path.write_text(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
        (self.output_dir / "latest.md").write_text(self.markdown(summary), encoding="utf-8")
        self.maybe_alert(summary)
        return path

    @staticmethod
    def console(summary: RunSummary) -> str:
        lines = [
            f"run_id   : {summary.run_id}",
            f"status   : {summary.status}   as_of={summary.as_of}",
            f"counts   : {json.dumps(summary.counts, ensure_ascii=False)}",
        ]
        errors = [e for e in summary.events if e.severity is Severity.ERROR]
        warns = [e for e in summary.events if e.severity is Severity.WARN]
        lines.append(f"quality  : {len(errors)} ERROR / {len(warns)} WARN / "
                     f"{len(summary.events) - len(errors) - len(warns)} INFO")
        for event in (errors + warns)[:8]:
            lines.append(f"  - [{event.severity.value}] {event.symbol} {event.check}: {event.detail[:150]}")
        for snapshot in summary.snapshots:
            lines.append(f"\n{snapshot.symbol}  status={snapshot.status}  "
                         f"period_end={snapshot.period_end}")
            if snapshot.fcf:
                lines.append(f"  OCF={snapshot.fcf.ocf:,.2f}  CapEx={snapshot.fcf.capex:,.2f}  "
                             f"Standard_FCF={snapshot.fcf.standard_fcf:,.2f} {snapshot.fcf.currency}")
            for factor in snapshot.factors:
                lines.append(f"  {factor.feature:<28} {factor.value:>18,.6f} {factor.unit}")
        if summary.report_path:
            lines.append(f"\nreport   : {summary.report_path}")
        return "\n".join(lines)

    @staticmethod
    def markdown(summary: RunSummary) -> str:
        lines = [f"# Pipeline run {summary.run_id}", "",
                 f"- status: `{summary.status}`",
                 f"- as_of: `{summary.as_of}`",
                 f"- symbols: {', '.join(summary.symbols)}", "",
                 "## Counts", ""]
        for key, value in summary.counts.items():
            lines.append(f"- {key}: {value}")
        lines += ["", "## Factors", "", "| symbol | as_of | period_end | feature | value | unit |",
                  "|---|---|---|---|---:|---|"]
        for snapshot in summary.snapshots:
            for factor in snapshot.factors:
                lines.append(f"| {snapshot.symbol} | {snapshot.as_of} | {snapshot.period_end} | "
                             f"{factor.feature} | {factor.value:.6g} | {factor.unit} |")
        lines += ["", "## Quality events", "", "| severity | check | symbol | detail |",
                  "|---|---|---|---|"]
        for event in summary.events:
            if event.severity is Severity.INFO:
                continue
            lines.append(f"| {event.severity.value} | {event.check} | {event.symbol} | "
                         f"{event.detail.replace('|', '/')[:200]} |")
        return "\n".join(lines) + "\n"

    def maybe_alert(self, summary: RunSummary) -> None:
        url = os.environ.get(self.config.quality.alert_webhook_url_env, "").strip()
        errors = [e for e in summary.events if e.severity is Severity.ERROR]
        if not url or (not errors and summary.status == "OK"):
            return
        payload: dict[str, Any] = {
            "run_id": summary.run_id, "status": summary.status,
            "as_of": summary.as_of.isoformat(), "errors": len(errors),
            "counts": summary.counts, "ts": utcnow().isoformat(),
        }
        try:
            request = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(request, timeout=5):
                pass
        except Exception as exc:
            logger.warning("alert webhook failed", extra={"context": {"error": str(exc)}})
