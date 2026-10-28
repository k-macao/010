"""Fetch current free reference snapshots before the scheduled production preflight."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from qp.config import load_config
from qp.ingest.disclosure import SecurityMaster
from qp.ingest.market_reference import (
    capture_reference_data,
    persist_first_observations,
    require_no_demo_reference_rows,
)


def main() -> int:
    url = os.environ.get("QP_DATABASE_URL", "")
    symbols = [s.strip().zfill(5) for s in os.environ.get("QP_NOTIFY_SYMBOLS", "00700,09988").split(",") if s.strip()]
    engine = None
    try:
        if not url or make_url(url).get_backend_name() != "postgresql":
            print("参考数据同步失败：须配置 PostgreSQL 数据库密钥", file=sys.stderr)
            return 1
        if not symbols or any(not (s.isascii() and s.isdigit() and len(s) == 5) for s in symbols):
            print("参考数据同步失败：股票代码无效", file=sys.stderr)
            return 1
        import akshare as ak
        import yfinance as yf

        cfg = load_config("config/github-actions.toml")
        master = SecurityMaster(cfg.resolve(cfg.sources.securities_file))
        engine = create_engine(url, connect_args={"connect_timeout": 10})
        # Confirm connectivity before issuing any vendor requests.
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
            require_no_demo_reference_rows(conn)
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        result = capture_reference_data(symbols, master, now, ak, yf)
        fx_count, mc_count = persist_first_observations(engine, result)
        print(f"首次观察快照：新增汇率 {fx_count} 条、市值 {mc_count} 条；次日才可用。")
        for warning in result.warnings:
            print(f"参考数据警告：{warning}", file=sys.stderr)
        # Missing or stale snapshots are handled by preflight, including existing
        # database data. No fallback, exception text or credentials are emitted.
        return 0
    except Exception:
        print("参考数据同步失败：请检查数据库连接、表结构、权限和采集依赖", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                # Dispose failures may include URL or driver text; never log them.
                print("数据库资源清理失败", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
