from __future__ import annotations

from pathlib import Path

import pytest

from qp.config import load_config
from qp.errors import ConfigError

ROOT = Path(__file__).resolve().parents[1]


def test_load_default_config(tmp_path):
    cfg = load_config(ROOT / "config" / "pipeline.toml")
    assert cfg.fx.target_currency == "HKD"
    assert cfg.sources.priority[0] == "hkexnews"
    assert cfg.resolve("examples/fixtures") == ROOT / "examples" / "fixtures"


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("QP_DATABASE_URL", f"sqlite:///{tmp_path}/x.db")
    monkeypatch.setenv("QP_OFFLINE", "1")
    monkeypatch.setenv("QP_TARGET_CURRENCY", "USD")
    monkeypatch.setenv("QP_LLM_ENABLED", "true")
    monkeypatch.setenv("QP_STRICT", "1")
    cfg = load_config(ROOT / "config" / "pipeline.toml")
    assert cfg.database.url.endswith("x.db")
    assert cfg.sources.offline is True
    assert cfg.fx.target_currency == "USD"
    assert cfg.llm.enabled is True
    assert cfg.quality.strict is True


def test_cli_overrides_win_over_env(monkeypatch):
    monkeypatch.setenv("QP_DATABASE_URL", "sqlite:////tmp/env.db")
    cfg = load_config(ROOT / "config" / "pipeline.toml",
                      overrides={"database": {"url": "sqlite:////tmp/cli.db"}})
    assert cfg.database.url == "sqlite:////tmp/cli.db"


def test_unknown_key_and_bad_values(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text("[fcf]\ncapex_sign_policy = 'nonsense'\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
    path.write_text("[fcf]\nunknown_key = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
