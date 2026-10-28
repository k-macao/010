"""Vendor DataFrame shapes → one canonical long table.

Handles three shapes seen in the wild:

* AkShare HK: long rows ``STD_ITEM_NAME / AMOUNT / REPORT_DATE / DATE_TYPE_CODE``
  (the user's draft assumed wide; that assumption is wrong — see README).
* yfinance: wide, index = English line items, columns = period-end timestamps.
* ad-hoc CSV / user draft: wide, ``报告期`` column + one column per line item.

Output columns: ``period_end (date) | period_type (str) | raw_label (str) |
raw_code (str|None) | value (float) | unit_label (str|None)``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from ..errors import SchemaDriftError
from ..util import parse_number, to_date

_LABEL_COLS = (
    "STD_ITEM_NAME", "std_item_name", "ITEM_NAME", "项目", "科目", "指标",
    "raw_label", "label", "item", "name", "科目名称",
)
_VALUE_COLS = (
    "AMOUNT", "amount", "VALUE", "value", "金额", "数值", "val", "本期金额",
)
_PERIOD_COLS = (
    "REPORT_DATE", "report_date", "报告期", "报告日期", "period_end", "date",
    "日期", "END_DATE", "STD_REPORT_DATE", "asOfDate", "Date",
)
_CODE_COLS = ("STD_ITEM_CODE", "std_item_code", "item_code", "科目代码", "code")
_TYPE_COLS = ("DATE_TYPE_CODE", "period_type", "报告类型", "type")
_UNIT_COLS = ("UNIT", "unit", "单位", "UNIT_LABEL", "unit_label")

_DATE_TYPE_MAP = {"001": "ANNUAL", "002": "INTERIM", "003": "QUARTERLY", "004": "QUARTERLY"}


def _find_col(df: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    lowered = {str(c).strip().lower(): c for c in df.columns}
    for candidate in candidates:
        if candidate in df.columns:
            return str(candidate)
        if candidate.lower() in lowered:
            return str(lowered[candidate.lower()])
    return None


def _looks_like_dates(values: Any, threshold: float = 0.6) -> bool:
    items = list(values)
    if not items:
        return False
    # Pure integer column names are positions, not epoch nanoseconds.
    if all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in items):
        return False
    parsed = pd.to_datetime(pd.Series(items, dtype="object"), errors="coerce", format="mixed")
    return float(parsed.notna().mean()) >= threshold


def _infer_period_type(period_end: date, fiscal_year_end: str | None, date_type_code: str | None) -> str:
    if date_type_code:
        code = str(date_type_code).strip()
        if code in _DATE_TYPE_MAP:
            return _DATE_TYPE_MAP[code]
    if fiscal_year_end:
        try:
            month, day = (int(x) for x in str(fiscal_year_end).split("-"))
            if (period_end.month, period_end.day) == (month, day):
                return "ANNUAL"
        except (ValueError, TypeError):
            pass
        return "INTERIM" if period_end.month in (6, 9, 12, 3) else "QUARTERLY"
    return "ANNUAL" if period_end.month == 12 and period_end.day == 31 else "INTERIM"


def to_long(
    df: pd.DataFrame,
    default_period_end: date | None = None,
    default_period_type: str | None = None,
    fiscal_year_end: str | None = None,
) -> pd.DataFrame:
    """Convert any supported vendor shape into the canonical long table."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["period_end", "period_type", "raw_label", "raw_code", "value", "unit_label"])
    if not isinstance(df, pd.DataFrame):
        raise SchemaDriftError(f"expected DataFrame, got {type(df).__name__}")

    frame = df.copy()
    # yfinance orientation: index = line items, columns = period-end timestamps.
    # (The inverse orientation appears in some CSV exports: index = dates.)
    if _find_col(frame, _LABEL_COLS) is None and _find_col(frame, _PERIOD_COLS) is None:
        if _looks_like_dates(frame.columns) and not _looks_like_dates(frame.index):
            frame.index.name = "raw_label"
            frame = frame.reset_index()
        elif _looks_like_dates(frame.index) and not _looks_like_dates(frame.columns):
            frame = frame.T
            frame.index.name = "raw_label"
            frame = frame.reset_index()

    label_col = _find_col(frame, _LABEL_COLS)
    value_col = _find_col(frame, _VALUE_COLS)
    period_col = _find_col(frame, _PERIOD_COLS)
    code_col = _find_col(frame, _CODE_COLS)
    type_col = _find_col(frame, _TYPE_COLS)
    unit_col = _find_col(frame, _UNIT_COLS)

    records: list[dict[str, Any]] = []

    if label_col and value_col:
        # long format
        for _, row in frame.iterrows():
            value = parse_number(row[value_col])
            if value is None:
                continue
            raw_period = row[period_col] if period_col else default_period_end
            if raw_period is None or pd.isna(raw_period):
                continue
            period_end = to_date(raw_period)
            records.append({
                "period_end": period_end,
                "period_type": default_period_type or _infer_period_type(
                    period_end, fiscal_year_end, str(row[type_col]) if type_col else None
                ),
                "raw_label": str(row[label_col]),
                "raw_code": str(row[code_col]) if code_col else None,
                "value": value,
                "unit_label": str(row[unit_col]) if unit_col else None,
            })
    elif label_col and not value_col:
        # "label column + one date column per period" (yfinance exported to CSV)
        date_cols = [c for c in frame.columns if c != label_col and _looks_like_dates([c])]
        if not date_cols:
            raise SchemaDriftError("found a label column but no value/date columns")
        for col in date_cols:
            period_end = to_date(col)
            for _, row in frame.iterrows():
                value = parse_number(row[col])
                if value is None:
                    continue
                records.append({
                    "period_end": period_end,
                    "period_type": default_period_type or _infer_period_type(
                        period_end, fiscal_year_end,
                        str(row[type_col]) if type_col else None,
                    ),
                    "raw_label": str(row[label_col]),
                    "raw_code": str(row[code_col]) if code_col else None,
                    "value": value,
                    "unit_label": str(row[unit_col]) if unit_col else None,
                })
    else:
        # wide format: one column per line item
        if period_col:
            periods = [to_date(x) for x in frame[period_col]]
            value_cols = [c for c in frame.columns if c != period_col]
        elif frame.index.name in _LABEL_COLS or _looks_like_dates(frame.index):
            periods = [None] * len(frame)
            value_cols = [c for c in frame.columns if c not in (type_col, unit_col, code_col)]
        else:
            if default_period_end is None:
                raise SchemaDriftError(
                    "cannot find a period column (报告期/REPORT_DATE/period_end) in wide DataFrame"
                )
            # single-row wide table
            frame = frame.reset_index(drop=True)
            periods = [default_period_end] * len(frame)
            value_cols = [c for c in frame.columns if c not in (type_col, unit_col, code_col)]
        for pos, (_, row) in enumerate(frame.iterrows()):
            period_raw = periods[pos] if pos < len(periods) else None
            period_end = period_raw or (to_date(row.name) if period_raw is None and _looks_like_dates([row.name]) else default_period_end)
            if period_end is None:
                continue
            for col in value_cols:
                value = parse_number(row[col])
                if value is None:
                    continue
                records.append({
                    "period_end": to_date(period_end),
                    "period_type": default_period_type or _infer_period_type(
                        to_date(period_end), fiscal_year_end,
                        str(row[type_col]) if type_col else None,
                    ),
                    "raw_label": str(col),
                    "raw_code": None,
                    "value": value,
                    "unit_label": str(row[unit_col]) if unit_col else None,
                })

    out = pd.DataFrame.from_records(records, columns=[
        "period_end", "period_type", "raw_label", "raw_code", "value", "unit_label",
    ])
    if out.empty:
        return out
    return out.drop_duplicates(subset=["period_end", "raw_label", "value"]).reset_index(drop=True)
