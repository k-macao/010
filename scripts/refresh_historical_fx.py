"""Backfill official HKMA daily CNY/HKD observations into the production FX table.

HKMA publishes HKD per unit of foreign currency. The API's ``cny`` value
therefore directly matches our (base=HKD, quote=CNY) rate convention.
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import date, datetime, timedelta
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from qp.ingest.market_reference import (
    CaptureResult,
    persist_first_observations,
    require_no_demo_reference_rows,
)

API_URL = (
    "https://api.hkma.gov.hk/public/market-data-and-statistics/"
    "monthly-statistical-bulletin/er-ir/er-eeri-daily"
)
PAGE_SIZE = 1000
LOOKBACK_DAYS = 550


def fetch_hkma_cny_rows(start: date, end: date) -> list[dict[str, object]]:
    """Fetch and validate official daily HKMA CNY/HKD rows in a bounded window."""
    rows: list[dict[str, object]] = []
    offset = 0
    while True:
        query = urlencode({
            "choose": "end_of_day", "from": start.isoformat(), "to": end.isoformat(),
            "offset": offset, "pagesize": PAGE_SIZE,
        })
        with urlopen(f"{API_URL}?{query}", timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        records = payload.get("result", {}).get("records", [])
        if not isinstance(records, list):
            raise ValueError("invalid HKMA response")
        for record in records:
            if not isinstance(record, dict):
                continue
            try:
                as_of = date.fromisoformat(str(record["end_of_day"]))
                rate = float(record["cny"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if start <= as_of <= end and math.isfinite(rate) and rate > 0:
                rows.append({
                    "base": "HKD", "quote": "CNY", "as_of": as_of,
                    "source": "hkma_eeri_daily", "rate": rate,
                })
        if len(records) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    # Guard against upstream schema/API changes returning no useful CNY observations.
    if not rows:
        raise ValueError("no valid HKMA CNY observations")
    unique = {(row["base"], row["quote"], row["as_of"], row["source"]): row for row in rows}
    return list(unique.values())


def main() -> int:
    url = os.environ.get("QP_DATABASE_URL", "")
    engine = None
    try:
        if not url or make_url(url).get_backend_name() != "postgresql":
            raise ValueError("PostgreSQL URL required")
        engine = create_engine(url, connect_args={"connect_timeout": 10})
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
            require_no_demo_reference_rows(conn)
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        rows = fetch_hkma_cny_rows(today - timedelta(days=LOOKBACK_DAYS), today)
        _, added = persist_first_observations(engine, CaptureResult(fx_rows=rows))
        print(f"HKMA 历史 CNY/HKD：获取 {len(rows)} 条，新增 {added} 条（重复记录保留）。")
        return 0
    except Exception as exc:
        # Emit fixed diagnostics only; vendor payloads and DB URLs may be sensitive.
        label = "数据库连接或参考表检查失败" if isinstance(exc, (ValueError,)) else "HKMA 汇率同步失败"
        if isinstance(exc, URLError):
            label = "HKMA 数据源暂不可用"
        print(f"{label}，未确认历史汇率已补齐。", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                print("数据库资源清理失败", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
