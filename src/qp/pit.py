"""Point-In-Time primitives. The single most important file for backtest correctness.

Rules enforced here:

* A fact is usable only when ``available_at <= as_of_cutoff``. For a restatement
  discovered today, ``available_at`` is today, not the original filing date.
* Never use the fiscal ``period_end`` as the availability timestamp.
* Among versions visible at ``as_of``, take the latest one (restatement wins).
* FX and market cap use the same clock (see storage/repository + factors).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable

import pandas as pd

from .util import as_of_cutoff, to_datetime


@dataclass(frozen=True)
class RestatementEvent:
    symbol: str
    source: str
    statement: str
    period_end: date
    field: str
    old_value: float
    new_value: float
    old_available_at: datetime
    new_available_at: datetime


def visible_at(df: pd.DataFrame, as_of: date | datetime, lag_days: int = 0, column: str = "available_at") -> pd.DataFrame:
    """Filter rows to those knowable at ``as_of`` (end of day UTC minus lag)."""
    if df.empty:
        return df
    cutoff = as_of_cutoff(as_of, lag_days)
    available = pd.to_datetime(df[column], errors="coerce", utc=True).dt.tz_localize(None)
    return df.loc[available.notna() & (available <= cutoff)].copy()


def select_latest_versions(df: pd.DataFrame, keys: Iterable[str] | None = None) -> pd.DataFrame:
    """Latest version per (source, statement, period_end, field) among visible rows."""
    if df.empty:
        return df
    keys = list(keys or ["source", "statement", "period_end", "field"])
    ordered = df.copy()
    ordered["_ts"] = pd.to_datetime(ordered["available_at"], errors="coerce", utc=True).dt.tz_localize(None)
    ordered["_published"] = pd.to_datetime(ordered["published_at"], errors="coerce", utc=True).dt.tz_localize(None)
    ordered = ordered.sort_values(["_ts", "_published"], na_position="first")
    latest = ordered.groupby(keys, as_index=False, dropna=False).tail(1).copy()
    return latest.drop(columns=["_ts", "_published"])


def detect_restatements(df: pd.DataFrame) -> list[RestatementEvent]:
    """Compare all archived versions with what was visible earlier."""
    events: list[RestatementEvent] = []
    if df.empty or "value" not in df.columns:
        return events
    for _, group in df.groupby(["symbol", "source", "statement", "period_end", "field"], dropna=False):
        versions = group.sort_values("available_at")
        previous: pd.Series | None = None
        for _, row in versions.iterrows():
            if previous is not None and float(row["value"]) != float(previous["value"]):
                events.append(RestatementEvent(
                    symbol=str(row["symbol"]), source=str(row["source"]),
                    statement=str(row["statement"]), period_end=pd.Timestamp(row["period_end"]).date(),
                    field=str(row["field"]), old_value=float(previous["value"]),
                    new_value=float(row["value"]),
                    old_available_at=to_datetime(previous["available_at"]),
                    new_available_at=to_datetime(row["available_at"]),
                ))
            previous = row
    return events


def is_lookahead_free(df: pd.DataFrame, as_of: date | datetime, lag_days: int = 0) -> bool:
    visible = visible_at(df, as_of, lag_days)
    if visible.empty:
        return True
    available = pd.to_datetime(visible["available_at"], errors="coerce", utc=True).dt.tz_localize(None)
    return bool((available <= as_of_cutoff(as_of, lag_days)).all())
