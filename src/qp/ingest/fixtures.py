"""Offline adapter: deterministic vendor-like snapshots from CSV fixtures.

Used by ``qp demo``, CI, and backfill replay. It is a real adapter (same contract as
AkShare/yfinance/HKEXnews), so all downstream cleaning code is exercised offline.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Iterable

import pandas as pd

from ..domain import PeriodType, RawReport, StatementType
from ..errors import SourceError
from ..util import to_date, to_datetime
from .base import unit_scale_from_label

_STATEMENT_SUFFIX = {
    "cf": StatementType.CF, "cashflow": StatementType.CF, "cash_flow": StatementType.CF,
    "is": StatementType.IS, "income": StatementType.IS, "income_stmt": StatementType.IS,
    "bs": StatementType.BS, "balance": StatementType.BS, "balance_sheet": StatementType.BS,
}

_SKIP_PREFIXES = ("fx_rate", "market_cap", "announcement", "securit")
_VERSION_COLS = ("period_end", "published_at", "available_at", "currency", "unit_scale", "unit_label")
_META_COLS = _VERSION_COLS + ("source", "statement", "period_type")


class FixtureAdapter:
    """Reads ``<fixtures_dir>/<source>/<symbol>_<statement>.csv``.

    Required columns: ``raw_label, value, period_end``.
    Optional: ``published_at, available_at, currency, unit_scale, unit_label,
    period_type, source, statement, raw_code``. Multiple versions of one period are
    expressed as repeated blocks with different ``available_at`` (restatement replay).
    """

    name = "fixture"

    def __init__(self, fixtures_dir: str | Path) -> None:
        self.root = Path(fixtures_dir)

    def _statement(self, path: Path, frame: pd.DataFrame) -> StatementType:
        if "statement" in frame.columns and str(frame["statement"].iloc[0]).strip():
            return StatementType.parse(str(frame["statement"].iloc[0]))
        suffix = path.stem.rsplit("_", 1)[-1].lower()
        if suffix not in _STATEMENT_SUFFIX:
            raise SourceError(f"cannot infer statement type from {path.name}; add a 'statement' column")
        return _STATEMENT_SUFFIX[suffix]

    def _files(self, symbol: str) -> list[Path]:
        if not self.root.exists():
            return []
        out: list[Path] = []
        for path in sorted(self.root.glob("**/*.csv")):
            name = path.name.lower()
            if name.startswith(_SKIP_PREFIXES):
                continue
            if symbol in name or symbol in str(path.parent):
                out.append(path)
        return out

    def fetch(self, symbol: str, statements: Iterable[StatementType] | None = None) -> list[RawReport]:
        wanted = set(statements or StatementType)
        reports: list[RawReport] = []
        for path in self._files(symbol):
            frame = pd.read_csv(path, dtype=str, keep_default_na=False)
            if frame.empty:
                continue
            statement = self._statement(path, frame)
            if statement not in wanted:
                continue
            # Accept vendor-style long headers in fixtures too, so the same shape
            # normalization code is exercised offline.
            frame = frame.rename(columns={
                "STD_ITEM_NAME": "raw_label", "项目": "raw_label", "科目": "raw_label",
                "item": "raw_label", "label": "raw_label",
                "AMOUNT": "value", "金额": "value", "amount": "value",
                "REPORT_DATE": "period_end", "报告期": "period_end", "date": "period_end",
                "STD_ITEM_CODE": "raw_code",
            })
            for column in ("raw_label", "value", "period_end"):
                if column not in frame.columns:
                    raise SourceError(f"{path.name}: missing required column {column!r}")
            for column in _VERSION_COLS:
                if column not in frame.columns:
                    frame[column] = ""
            source = (
                str(frame["source"].iloc[0]) if "source" in frame.columns and str(frame["source"].iloc[0]).strip()
                else (path.parent.name if path.parent != self.root else "fixture")
            )
            group_cols = list(_VERSION_COLS)
            if "period_type" in frame.columns:
                group_cols.append("period_type")
            for key, group in frame.groupby(group_cols, dropna=False, sort=True):
                meta = dict(zip(group_cols, key if isinstance(key, tuple) else (key,)))
                period_end = to_date(meta["period_end"])
                published_at = to_datetime(meta["published_at"] or period_end.isoformat())
                available_at = to_datetime(meta["available_at"] or published_at.isoformat())
                currency = (meta["currency"] or "HKD").upper()
                unit_label = meta["unit_label"] or ""
                scale = float(meta["unit_scale"] or unit_scale_from_label(unit_label) or 1.0)
                payload = group.drop(columns=[c for c in _META_COLS if c in group.columns], errors="ignore")
                reports.append(RawReport(
                    symbol=symbol,
                    source=source,
                    statement=statement,
                    period_end=period_end,
                    period_type=PeriodType(str(meta.get("period_type") or "ANNUAL").upper()),
                    currency=currency,
                    unit_scale=scale,
                    unit_label=unit_label,
                    published_at=published_at,
                    available_at=available_at,
                    data=payload.reset_index(drop=True),
                    metadata={"fixture": str(path), "origin": "offline"},
                ))
        return reports


def load_fx_rows(path: str | Path) -> list[dict[str, str]]:
    p = Path(path)
    return pd.read_csv(p, dtype=str).to_dict(orient="records") if p.exists() else []


def load_market_caps(path: str | Path) -> list[dict[str, str]]:
    p = Path(path)
    return pd.read_csv(p, dtype=str).to_dict(orient="records") if p.exists() else []


def fixture_periods(fixtures_dir: str | Path, symbol: str) -> list[date]:
    return sorted({r.period_end for r in FixtureAdapter(fixtures_dir).fetch(symbol)})
