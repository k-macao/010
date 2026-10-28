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
