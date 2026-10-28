from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine

from qp.config import load_config
from qp.storage.schema import fx_rates, market_caps, metadata
from scripts.production_preflight import PreflightError, check_reference_data, main

TODAY = date(2026, 10, 28)


@pytest.fixture
def conn():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(fx_rates.insert(), [{"base": "HKD", "quote": "CNY", "as_of": TODAY,
                                                "source": "official", "rate": 1.1}])
        connection.execute(market_caps.insert(), [{"symbol": "00700", "as_of": TODAY,
                                                   "source": "market", "currency": "HKD",
                                                   "market_cap": 100}])
        yield connection
    engine.dispose()


def test_production_reference_data(conn):
    check_reference_data(conn, ["00700"], TODAY)


def test_demo_source_disallowed_even_if_newer_real_rate_exists(conn):
    conn.execute(fx_rates.insert(), {"base": "HKD", "quote": "CNY", "as_of": TODAY - timedelta(days=10),
                                     "source": "fixture", "rate": 1.0})
    with pytest.raises(PreflightError, match="演示"):
        check_reference_data(conn, ["00700"], TODAY)


def test_future_and_stale_market_caps_do_not_pass(conn):
    with pytest.raises(PreflightError, match="市值"):
        check_reference_data(conn, ["00700"], TODAY - timedelta(days=1))
    with pytest.raises(PreflightError, match="市值"):
        check_reference_data(conn, ["00700"], TODAY + timedelta(days=8))


def test_missing_currency_fx(conn):
    conn.execute(market_caps.update().values(currency="USD"))
    with pytest.raises(PreflightError, match="USD"):
        check_reference_data(conn, ["00700"], TODAY)


def test_preflight_refuses_sqlite_without_printing_connection_string(monkeypatch, capsys):
    monkeypatch.setenv("QP_DATABASE_URL", "sqlite:///secret-credentials.db")
    assert main() == 1
    assert "secret-credentials" not in capsys.readouterr().err


def test_github_config_excludes_demo_reference_data(monkeypatch):
    monkeypatch.delenv("QP_CONFIG", raising=False)
    cfg = load_config("config/github-actions.toml", apply_env=False)
    assert cfg.fx.fallback_rates == {}
    assert "examples/fixtures" not in str(cfg.resolve(cfg.sources.fixtures_dir))
    assert "examples/fixtures" not in str(cfg.resolve(cfg.fx.rates_file))
    assert cfg.pit.backfill_mode is False and cfg.quality.strict is True


@pytest.mark.parametrize(("code", "expected"), [
    ("28P01", "身份验证"),
    ("3D000", "数据库不存在"),
    ("42P01", "缺少所需表或字段"),
    ("42703", "缺少所需表或字段"),
    ("42501", "权限不足"),
    ("08001", "连接失败"),
])
def test_diagnostics_from_sqlstate_hide_credentials(code, expected):
    from sqlalchemy.exc import DBAPIError

    from scripts.production_preflight import diagnose_database_error

    class FakeDriverError(Exception):
        pgcode = code

    exc = DBAPIError("SQL containing secret", {"password": "secret"}, FakeDriverError("secret"))
    message = diagnose_database_error(exc)
    assert expected in message
    assert "secret" not in message


@pytest.mark.parametrize(("error", "expected"), [
    ("could not translate host name secret", "主机名无法解析"),
    ("connection timed out secret", "连接超时"),
    ("password authentication failed for user secret", "身份验证"),
    ("SSL certificate verify failed secret", "TLS/SSL"),
    ("unknown secret", "未知原因"),
])
def test_diagnostics_from_driver_text_hide_credentials(error, expected):
    from sqlalchemy.exc import DBAPIError

    from scripts.production_preflight import diagnose_database_error

    exc = DBAPIError("secret SQL", {"password": "secret"}, Exception(error))
    message = diagnose_database_error(exc)
    assert expected in message
    assert "secret" not in message


def test_preflight_main_logs_only_safe_diagnostic(monkeypatch, capsys):
    from sqlalchemy.exc import DBAPIError

    monkeypatch.setenv("QP_DATABASE_URL", "postgresql://user:secret@example.invalid/qpdata")
    exc = DBAPIError("sql secret", {}, Exception("could not translate host name secret"))

    def fail(*args, **kwargs):
        raise exc

    monkeypatch.setattr("scripts.production_preflight.create_engine", fail)
    assert main() == 1
    error = capsys.readouterr().err
    assert "主机名无法解析" in error
    assert "secret" not in error and "example.invalid" not in error


@pytest.mark.parametrize(("driver_message", "expected"), [
    ("FATAL: Tenant or user not found", "连接池未找到项目或用户"),
    ("FATAL: Max clients reached", "连接数已满"),
])
def test_pooler_errors_are_sanitized(driver_message, expected):
    from sqlalchemy.exc import DBAPIError

    from scripts.production_preflight import diagnose_database_error

    exc = DBAPIError("SQL", {}, Exception(driver_message + " secret-token"))
    result = diagnose_database_error(exc)
    assert expected in result
    assert "secret-token" not in result


def test_malformed_url_port_reports_parse_stage_without_sensitive_details(monkeypatch, capsys):
    monkeypatch.setenv("QP_DATABASE_URL", "postgresql://user:secret@example.invalid:secret/qpdata")
    assert main() == 1
    error = capsys.readouterr().err
    assert "连接地址格式无效" in error
    assert "阶段：url-parse" in error and "类别：value" in error
    assert "secret" not in error and "example.invalid" not in error


def test_unknown_connect_failure_has_fixed_stage_and_category(monkeypatch, capsys):
    from sqlalchemy.exc import OperationalError

    monkeypatch.setenv("QP_DATABASE_URL", "postgresql://user:secret@example.invalid/qpdata")

    class Engine:
        def connect(self):
            raise OperationalError("secret SQL", {}, Exception("secret-token"))

        def dispose(self):
            pass

    monkeypatch.setattr("scripts.production_preflight.create_engine", lambda *args, **kwargs: Engine())
    assert main() == 1
    error = capsys.readouterr().err
    assert "阶段：connection" in error and "类别：db-operational" in error
    assert "secret" not in error and "example.invalid" not in error


def test_unknown_query_failure_has_fixed_stage_and_category(monkeypatch, capsys):
    from sqlalchemy.exc import ProgrammingError

    monkeypatch.setenv("QP_DATABASE_URL", "postgresql://user:secret@example.invalid/qpdata")

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Engine:
        def connect(self):
            return Conn()

        def dispose(self):
            pass

    def fail(*args, **kwargs):
        raise ProgrammingError("secret SQL", {}, Exception("secret-token"))

    monkeypatch.setattr("scripts.production_preflight.create_engine", lambda *args, **kwargs: Engine())
    monkeypatch.setattr("scripts.production_preflight.check_reference_data", fail)
    assert main() == 1
    error = capsys.readouterr().err
    assert "阶段：reference-query" in error and "类别：db-programming" in error
    assert "secret" not in error and "example.invalid" not in error
