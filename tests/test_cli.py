from __future__ import annotations

from pathlib import Path

import pytest

from qp.cli import main

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cli_args(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    for key in ("QP_DATABASE_URL", "QP_OFFLINE", "QP_STRICT", "QP_LLM_ENABLED", "QP_CONFIG"):
        monkeypatch.delenv(key, raising=False)
    return ["--config", str(ROOT / "config" / "pipeline.toml"),
            "--db-url", f"sqlite:///{tmp_path / 'cli.db'}", "--offline"]


def test_check_command(cli_args, capsys):
    assert main(cli_args + ["check"]) == 0
    assert '"status": "OK"' in capsys.readouterr().out


def test_demo_command_end_to_end(cli_args, capsys):
    assert main(cli_args + ["demo", "--as-of", "2026-10-27"]) == 0
    out = capsys.readouterr().out
    assert "Standard_FCF" in out and "fcf_yield" in out and "PIT replay" in out


def test_run_and_factors_commands(cli_args, capsys):
    assert main(cli_args + ["run", "--symbols", "00700,09988", "--as-of", "2026-10-27"]) == 0
    capsys.readouterr()
    assert main(cli_args + ["factors", "--symbol", "00700", "--as-of", "2026-10-27"]) == 0
    assert "standard_fcf" in capsys.readouterr().out


def test_history_command_is_read_only(cli_args, capsys):
    assert main(cli_args + ["history", "--symbol", "00700", "--start", "2025-01-01",
                            "--end", "2026-09-30", "--freq", "QE", "--run"]) == 0
    out = capsys.readouterr().out
    assert "standard_fcf" in out and "2024-12-31" in out and "2025-12-31" in out
