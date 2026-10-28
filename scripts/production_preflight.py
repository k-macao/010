"""Fail closed before the scheduled job can use demo/reference data."""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import DBAPIError, NoSuchModuleError

from qp.storage.schema import fx_rates, market_caps


class PreflightError(Exception):
    pass


def diagnose_database_error(exc: Exception) -> str:
    """Classify failures using fixed messages only; never return exception text or SQL."""
    if isinstance(exc, NoSuchModuleError):
        return "PostgreSQL 驱动不可用，请检查连接地址的驱动名称及依赖"
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "数据库连接超时或被拒绝，请检查网络、端口及防火墙"
    if not isinstance(exc, DBAPIError):
        return "数据库不可访问或表结构不兼容（未知原因）"

    original = exc.orig
    code = getattr(original, "pgcode", None) or getattr(original, "sqlstate", None) or ""
    if code in {"28000", "28P01"}:
        return "数据库身份验证失败，请核对用户名、密码及访问规则"
    if code == "3D000":
        return "数据库不存在，请核对连接地址中的数据库名"
    if code in {"42P01", "42703", "3F000"}:
        return "数据库缺少所需表或字段，请核对项目表结构和 schema"
    if code == "42501":
        return "数据库权限不足，请检查连接账户的表和 schema 权限"
    if code.startswith("08"):
        return "数据库连接失败，请检查网络、端口及防火墙"

    # libpq connection errors often have no SQLSTATE. Scan only the wrapped driver
    # error and emit predefined text, not the error itself (which can contain secrets).
    message = str(original).lower()
    if any(s in message for s in ("password authentication failed", "authentication failed",
                                   "no pg_hba.conf entry", "role does not exist")):
        return "数据库身份验证或访问规则失败，请核对用户名、密码及访问规则"
    if "database" in message and "does not exist" in message:
        return "数据库不存在，请核对连接地址中的数据库名"
    if any(s in message for s in ("could not translate host name", "name or service not known",
                                   "temporary failure in name resolution", "nodename nor servname")):
        return "数据库主机名无法解析，请检查 DNS 和网络"
    if "ssl" in message or "certificate verify failed" in message:
        return "数据库 TLS/SSL 连接失败，请核对服务端及客户端 TLS 配置"
    if any(s in message for s in ("connection refused", "connection timed out", "timeout expired",
                                   "network is unreachable", "no route to host", "could not connect to server")):
        return "数据库连接超时或被拒绝，请检查网络、端口及防火墙"
    return "数据库不可访问或表结构不兼容（未知原因）"


def check_reference_data(conn: Connection, symbols: list[str], day: date) -> None:
    # Repository lookups do not filter by source, so even one demo row could win.
    for table in (fx_rates, market_caps):
        bad = conn.execute(select(func.count()).select_from(table).where(
            func.lower(table.c.source).in_(["fixture", "demo", "synthetic"])
        )).scalar_one()
        if bad:
            raise PreflightError("数据库包含演示参考数据，请移除或使用隔离的生产数据库")

    for symbol in symbols:
        row = conn.execute(select(market_caps.c.currency, market_caps.c.as_of, market_caps.c.market_cap)
                           .where(market_caps.c.symbol == symbol, market_caps.c.as_of <= day)
                           .order_by(market_caps.c.as_of.desc(), market_caps.c.source.desc()).limit(1)).first()
        if row is None or row.as_of < day - timedelta(days=7) or row.market_cap <= 0:
            raise PreflightError(f"{symbol} 缺少最近 7 天的有效市值")
        currencies = {"CNY", row.currency.upper()} - {"HKD"}
        for currency in currencies:
            rate = conn.execute(select(fx_rates.c.as_of, fx_rates.c.rate)
                                .where(fx_rates.c.base == "HKD", fx_rates.c.quote == currency,
                                       fx_rates.c.as_of <= day)
                                .order_by(fx_rates.c.as_of.desc(), fx_rates.c.source.desc()).limit(1)).first()
            if rate is None or rate.as_of < day - timedelta(days=45) or rate.rate <= 0:
                raise PreflightError(f"缺少最近 45 天的 {currency}/HKD 有效汇率")


def main() -> int:
    url = os.environ.get("QP_DATABASE_URL", "")
    symbols = [s.strip().zfill(5) for s in os.environ.get("QP_NOTIFY_SYMBOLS", "00700,09988").split(",") if s.strip()]
    try:
        if not url or make_url(url).get_backend_name() != "postgresql":
            raise PreflightError("须配置 QP_DATABASE_URL PostgreSQL 密钥")
        if not symbols or any(not (s.isascii() and s.isdigit() and len(s) == 5) for s in symbols):
            raise PreflightError("QP_NOTIFY_SYMBOLS 必须是逗号分隔的港股代码")
        engine = create_engine(url, connect_args={"connect_timeout": 10})
        try:
            with engine.connect() as conn:
                check_reference_data(conn, symbols, datetime.now(ZoneInfo("Asia/Shanghai")).date())
        finally:
            engine.dispose()
    except PreflightError as exc:
        print(f"生产预检失败：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Only a fixed diagnostic is printed; URLs, SQL and driver text may contain secrets.
        print(f"生产预检失败：{diagnose_database_error(exc)}", file=sys.stderr)
        return 1
    print("生产参考数据预检通过；仍需核验数据真实性与公告日。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
