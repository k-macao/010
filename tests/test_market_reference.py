from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from sqlalchemy import create_engine, select

from qp.ingest.disclosure import SecurityMaster
from qp.ingest.market_reference import capture_reference_data, persist_first_observations
from qp.storage.schema import fx_rates, market_caps, metadata
from scripts.production_preflight import PreflightError, check_reference_data

NOW = datetime(2026, 10, 28, 12, tzinfo=ZoneInfo("Asia/Shanghai"))


class FakeAk:
    def currency_boc_sina(self, symbol, start_date, end_date):
        assert symbol == "港币"
        assert start_date == "20261021" and end_date == "20261028"
        return pd.DataFrame({"日期": ["2026-10-27", "2026-10-28", "2026-10-29"],
                             "央行中间价": [86.0, 87.0, 1.0]})


class FakeTicker:
    def __init__(self, name, now=NOW):
        self.name = name
        self.info = {"marketCap": 1000, "currency": "HKD", "financialCurrency": "CNY",
                     "regularMarketTime": int((now - timedelta(hours=1)).timestamp())}

    def history(self, period, auto_adjust):
        assert self.name in {"HKD=X", "CNYHKD=X"}
        assert period == "10d" and auto_adjust is False
        values = [7.8, 7.81, 1.0] if self.name == "HKD=X" else [100 / 86, 100 / 87, 1.0]
        return pd.DataFrame({"Close": values},
                            index=pd.to_datetime(["2026-10-27", "2026-10-28", "2026-10-29"]))


class FakeYf:
    def Ticker(self, name):
        return FakeTicker(name)


def test_real_source_capture_uses_first_seen_next_day_and_market_currency():
    result = capture_reference_data(["00700"], SecurityMaster(), NOW, FakeAk(), FakeYf())
    fx = {row["quote"]: row for row in result.fx_rows}
    assert fx["CNY"]["rate"] == pytest.approx(100 / 87)
    assert fx["USD"]["rate"] == pytest.approx(7.81)
    assert all(r["as_of"] == date(2026, 10, 29) for r in result.fx_rows + result.market_rows)
    assert result.market_rows[0]["currency"] == "HKD"  # not financialCurrency
    assert result.market_rows[0]["market_cap"] == 1000
    assert not result.warnings


def test_failures_are_isolated_and_do_not_log_vendor_details():
    class BadAk:
        def currency_boc_sina(self, **kwargs):
            raise RuntimeError("secret-token")

    class BadYf:
        def Ticker(self, name):
            raise RuntimeError("secret-token")

    result = capture_reference_data(["00700"], SecurityMaster(), NOW, BadAk(), BadYf())
    assert not result.fx_rows and not result.market_rows
    assert len(result.warnings) == 4
    assert "secret-token" not in str(result.warnings)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0, -1, None, True])
def test_invalid_market_caps_never_written(value):
    class BadTicker(FakeTicker):
        def __init__(self, name):
            super().__init__(name)
            self.info["marketCap"] = value

    class Client:
        def Ticker(self, name):
            return BadTicker(name)

    result = capture_reference_data(["00700"], SecurityMaster(), NOW, FakeAk(), Client())
    assert not result.market_rows
    assert any("00700" in warning for warning in result.warnings)


@pytest.mark.parametrize("market_time", [None, int((NOW + timedelta(hours=1)).timestamp()),
                                            int((NOW - timedelta(days=8)).timestamp())])
def test_missing_future_or_stale_market_timestamp_never_written(market_time):
    class BadTicker(FakeTicker):
        def __init__(self, name):
            super().__init__(name)
            self.info["regularMarketTime"] = market_time

    class Client:
        def Ticker(self, name):
            return BadTicker(name)

    result = capture_reference_data(["00700"], SecurityMaster(), NOW, FakeAk(), Client())
    assert not result.market_rows


def test_frozen_first_observation_and_no_historical_leak():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    result = capture_reference_data(["00700"], SecurityMaster(), NOW, FakeAk(), FakeYf())
    assert persist_first_observations(engine, result) == (2, 1)
    result.market_rows[0]["market_cap"] = 2000
    result.fx_rows[0]["rate"] = 100
    assert persist_first_observations(engine, result) == (0, 0)
    with engine.connect() as conn:
        assert conn.execute(select(market_caps.c.market_cap)).scalar_one() == 1000
        stored = conn.execute(select(fx_rates.c.rate).where(fx_rates.c.quote == "CNY")).scalar_one()
        assert stored == pytest.approx(100 / 87)
        # Until the next day, newly captured rows are not visible to the preflight.
        with pytest.raises(PreflightError, match="市值"):
            check_reference_data(conn, ["00700"], NOW.date())
        check_reference_data(conn, ["00700"], date(2026, 10, 29))
    engine.dispose()


def test_timezone_is_required():
    with pytest.raises(ValueError, match="timezone-aware"):
        capture_reference_data(["00700"], SecurityMaster(), datetime(2026, 10, 28), FakeAk(), FakeYf())


def test_scheduled_collector_writes_without_retroactive_rows(tmp_path, monkeypatch, capsys):
    import sys
    from types import SimpleNamespace

    from scripts import refresh_market_reference

    path = tmp_path / "reference.sqlite"
    url = f"sqlite:///{path}"
    setup = create_engine(url)
    metadata.create_all(setup)
    setup.dispose()
    monkeypatch.setenv("QP_DATABASE_URL", "postgresql+psycopg2://user:secret@example.invalid/prod")
    monkeypatch.setenv("QP_NOTIFY_SYMBOLS", "00700")
    monkeypatch.setitem(sys.modules, "akshare", FakeAk())
    monkeypatch.setitem(sys.modules, "yfinance", FakeYf())
    monkeypatch.setattr(refresh_market_reference, "create_engine", lambda *args, **kwargs: create_engine(url))
    monkeypatch.setattr(refresh_market_reference, "datetime", SimpleNamespace(now=lambda tz: NOW))
    assert refresh_market_reference.main() == 0
    output = capsys.readouterr()
    assert "secret" not in output.out + output.err
    assert "新增汇率 2 条、市值 1 条" in output.out
    assert refresh_market_reference.main() == 0
    assert "新增汇率 0 条、市值 0 条" in capsys.readouterr().out
    db = create_engine(url)
    with db.connect() as conn:
        assert conn.execute(select(market_caps.c.as_of)).scalar_one() == date(2026, 10, 29)
        assert all(day == date(2026, 10, 29) for day in conn.execute(select(fx_rates.c.as_of)).scalars())
    db.dispose()


def test_collector_never_uses_sqlite_from_environment(monkeypatch, capsys):
    from scripts import refresh_market_reference

    monkeypatch.setenv("QP_DATABASE_URL", "sqlite:///secret.sqlite")
    assert refresh_market_reference.main() == 1
    assert "secret" not in capsys.readouterr().err


def test_akshare_is_only_advisory_and_yahoo_primary():
    class OfflineAk:
        def currency_boc_sina(self, **kwargs):
            raise RuntimeError("unavailable")

    result = capture_reference_data(["00700"], SecurityMaster(), NOW, OfflineAk(), FakeYf())
    fx = {row["quote"]: row for row in result.fx_rows}
    assert set(fx) == {"CNY", "USD"}
    assert fx["CNY"]["source"] == "yfinance_observed"
    assert any("交叉校验" in warning for warning in result.warnings)


def test_divergent_same_day_cross_check_quarantines_cny_rate():
    class DivergentAk(FakeAk):
        def currency_boc_sina(self, symbol, start_date, end_date):
            return pd.DataFrame({"日期": ["2026-10-28"], "央行中间价": [50.0]})

    result = capture_reference_data(["00700"], SecurityMaster(), NOW, DivergentAk(), FakeYf())
    assert [row["quote"] for row in result.fx_rows] == ["USD"]
    assert any("差异超过 3%" in warning for warning in result.warnings)


def test_collector_does_not_write_to_demo_contaminated_store(tmp_path, monkeypatch, capsys):
    import sys
    from types import SimpleNamespace

    from scripts import refresh_market_reference

    path = tmp_path / "reference.sqlite"
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(fx_rates.insert(), {"base": "HKD", "quote": "CNY", "as_of": NOW.date(),
                                        "source": "fixture", "rate": 1.1})
    engine.dispose()
    monkeypatch.setenv("QP_DATABASE_URL", "postgresql+psycopg2://user:secret@example.invalid/prod")
    monkeypatch.setenv("QP_NOTIFY_SYMBOLS", "00700")
    monkeypatch.setitem(sys.modules, "akshare", FakeAk())
    monkeypatch.setitem(sys.modules, "yfinance", FakeYf())
    monkeypatch.setattr(refresh_market_reference, "create_engine", lambda *args, **kwargs: create_engine(url))
    monkeypatch.setattr(refresh_market_reference, "datetime", SimpleNamespace(now=lambda tz: NOW))
    assert refresh_market_reference.main() == 1
    assert "secret" not in capsys.readouterr().err
    engine = create_engine(url)
    with engine.connect() as conn:
        assert len(conn.execute(select(fx_rates)).all()) == 1
        assert not conn.execute(select(market_caps)).all()
    engine.dispose()
