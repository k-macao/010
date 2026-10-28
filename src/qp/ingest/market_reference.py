"""Capture free reference data conservatively; never fabricate historical snapshots.

The existing reference tables have only a date, not a disclosure timestamp. A
quote first seen today is therefore eligible on the next China calendar day.
Repeated captures for the same date never overwrite the first observation.
The sources may revise data, and current market cap is not historical PIT data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..storage.schema import fx_rates, market_caps
from .disclosure import SecurityInfo
from .fx import FxRateProvider


@dataclass
class CaptureResult:
    fx_rows: list[dict[str, Any]] = field(default_factory=list)
    market_rows: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _positive(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def capture_reference_data(
    symbols: list[str],
    master: Any,
    observed_at: datetime,
    ak: Any,
    yf: Any,
    max_quote_age_days: int = 7,
) -> CaptureResult:
    """Read current vendor snapshots. No historical dates are inserted or revised."""
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    observed_at = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
    today = observed_at.date()
    result = CaptureResult()
    # With a date-only schema, today's observation cannot safely be used in an
    # earlier intraday run or replay. Delay eligibility to the next calendar day.
    effective = today + timedelta(days=1)
    boc = None
    try:
        # AkShare's default date range is a fixed historical example, not today.
        frame = ak.currency_boc_sina(symbol="港币", start_date=(today - timedelta(days=max_quote_age_days)).strftime("%Y%m%d"),
                                      end_date=today.strftime("%Y%m%d"))
        rates = FxRateProvider.from_boc_sina(frame)
        eligible = [r for r in rates if r.as_of <= today and (today - r.as_of).days <= max_quote_age_days
                    and _positive(r.rate) is not None]
        if not eligible:
            raise ValueError("no eligible quote")
        boc = max(eligible, key=lambda r: r.as_of)
    except Exception:
        # Vendor exceptions can contain request URLs or credentials; don't log them.
        result.warnings.append("无法取得中行 CNY/HKD 交叉校验数据")

    # Yahoo's CNYHKD=X and HKD=X report HKD per CNY and per USD respectively.
    # AkShare's Sina endpoint currently uses HTTP, so its BOC rate is used only
    # as a sanity check, never persisted as the trusted primary quote.
    for currency, ticker_name in (("CNY", "CNYHKD=X"), ("USD", "HKD=X")):
        try:
            history = yf.Ticker(ticker_name).history(period="10d", auto_adjust=False)
            candidates: list[tuple[date, float]] = []
            for index, row in history.iterrows():
                day = index.date() if hasattr(index, "date") else date.fromisoformat(str(index)[:10])
                value = _positive(row.get("Close"))
                if value is not None and day <= today and (today - day).days <= max_quote_age_days:
                    candidates.append((day, value))
            if not candidates:
                raise ValueError("no eligible quote")
            quote_day, rate = max(candidates, key=lambda item: item[0])
            if currency == "CNY" and boc is not None:
                if boc.as_of != quote_day:
                    result.warnings.append("中行与 Yahoo CNY/HKD 报价日期不同，无法交叉校验")
                elif abs(rate - boc.rate) / max(rate, boc.rate) > 0.03:
                    result.warnings.append("中行与 Yahoo CNY/HKD 差异超过 3%，本次跳过汇率")
                    continue
            result.fx_rows.append({"base": "HKD", "quote": currency, "as_of": effective,
                                   "rate": rate, "source": "yfinance_observed"})
        except Exception:
            result.warnings.append(f"无法取得有效的 {currency}/HKD 汇率")

    for symbol in symbols:
        try:
            security: SecurityInfo = master.get(symbol)
            ticker = yf.Ticker(security.ticker)
            # Currency is the quote/market currency, not financialCurrency.
            # Yahoo's slower `info` endpoint occasionally omits marketCap or
            # regularMarketTime. Fall back to yfinance fast_info for market cap
            # and a recent daily bar for the quote date; never invent a value.
            info = ticker.info
            value = _positive(info.get("marketCap"))
            currency = str(info.get("currency") or "").strip().upper()
            market_time = info.get("regularMarketTime")
            timestamp = None
            if isinstance(market_time, datetime):
                timestamp = market_time
            elif isinstance(market_time, (int, float)) and not isinstance(market_time, bool):
                timestamp = datetime.fromtimestamp(market_time, timezone.utc)

            if value is None or currency not in {"HKD", "CNY", "USD"} or timestamp is None:
                fast = ticker.fast_info
                value = value or _positive(fast.get("market_cap"))
                currency = currency if currency in {"HKD", "CNY", "USD"} else str(
                    fast.get("currency") or ""
                ).strip().upper()
                history = ticker.history(period="10d", auto_adjust=False)
                quote_days = [
                    index.date() if hasattr(index, "date") else date.fromisoformat(str(index)[:10])
                    for index in history.index
                ]
                valid_days = [day for day in quote_days
                              if day <= today and (today - day).days <= max_quote_age_days]
                if valid_days:
                    timestamp = datetime.combine(max(valid_days), datetime.min.time(),
                                                 tzinfo=ZoneInfo("Asia/Shanghai"))

            if timestamp is None or timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError("missing market timestamp")
            age = observed_at - timestamp
            if (value is None or currency not in {"HKD", "CNY", "USD"}
                    or age < timedelta(0) or age > timedelta(days=max_quote_age_days)):
                raise ValueError("invalid or stale market cap")
            result.market_rows.append({"symbol": symbol, "as_of": effective,
                                       "source": "yfinance_observed", "market_cap": value,
                                       "currency": currency})
        except Exception:
            result.warnings.append(f"{symbol} 无法取得有效市值及行情币种")
    return result


def require_no_demo_reference_rows(conn: Any) -> None:
    """Do not write into a reference store known to contain demo observations."""
    for table in (fx_rates, market_caps):
        count = conn.execute(select(func.count()).select_from(table).where(
            func.lower(table.c.source).in_(["fixture", "demo", "synthetic"])
        )).scalar_one()
        if count:
            raise ValueError("reference store contains demo observations")


def persist_first_observations(engine: Any, result: CaptureResult) -> tuple[int, int]:
    """Atomic insert-if-absent; never rewrite an earlier observed daily snapshot."""
    dialect = engine.dialect.name
    factory: Callable[..., Any]
    if dialect == "postgresql":
        factory = pg_insert
    elif dialect == "sqlite":
        factory = sqlite_insert
    else:
        raise ValueError("reference captures require PostgreSQL or SQLite")
    counts: list[int] = []
    with engine.begin() as conn:
        for table, rows in ((fx_rates, result.fx_rows), (market_caps, result.market_rows)):
            if not rows:
                counts.append(0)
                continue
            stmt = factory(table).values(rows).on_conflict_do_nothing(
                index_elements=[c.name for c in table.primary_key.columns]
            )
            counts.append(conn.execute(stmt).rowcount)
    return counts[0], counts[1]
