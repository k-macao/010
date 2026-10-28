from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest

from qp.cli import main
from qp.pushplus import PushPlusError, current_slot, send_pushplus

ROOT = Path(__file__).resolve().parents[1]


def test_pushplus_posts_json_token_in_body_only():
    captured = {}

    def fake_open(request, timeout):
        captured.update(url=request.full_url, data=json.loads(request.data), timeout=timeout)
        return BytesIO(b'{"code":200,"msg":"success"}')

    with patch("urllib.request.urlopen", fake_open):
        send_pushplus("secret-token", "早报", "内容")
    assert captured["url"] == "https://www.pushplus.plus/send"
    assert captured["data"] == {"token": "secret-token", "title": "早报", "content": "内容", "template": "txt"}
    assert captured["timeout"] == 15


@pytest.mark.parametrize("response", [b'{"code":500,"msg":"secret-token"}', b'not json'])
def test_pushplus_rejection_does_not_leak_response(response):
    with patch("urllib.request.urlopen", return_value=BytesIO(response)):
        with pytest.raises(PushPlusError) as exc:
            send_pushplus("secret-token", "a", "b")
    assert "secret-token" not in str(exc.value)


def test_network_error_does_not_leak_token():
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("secret-token")):
        with pytest.raises(PushPlusError) as exc:
            send_pushplus("secret-token", "a", "b")
    assert "secret-token" not in str(exc.value)


def test_slot_uses_china_time():
    assert current_slot(datetime(2026, 1, 1, 0, tzinfo=timezone.utc)) == "早报"
    assert current_slot(datetime(2026, 1, 1, 4, tzinfo=timezone.utc)) == "午报"
    assert current_slot(datetime(2026, 1, 1, 10, tzinfo=timezone.utc)) == "晚报"


def test_cli_no_token_exits_before_pipeline(monkeypatch, capsys):
    monkeypatch.delenv("QP_PUSHPLUS_TOKEN", raising=False)
    with patch("qp.cli.Pipeline", side_effect=AssertionError("should not run")):
        assert main(["push"]) == 1
    assert "QP_PUSHPLUS_TOKEN" in capsys.readouterr().err


def test_cli_offline_dry_run_and_delivery(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("QP_PUSHPLUS_TOKEN", "secret-token")
    args = ["--offline", "--db-url", f"sqlite:///{tmp_path / 'db.sqlite'}", "push",
            "--symbols", "00700", "--slot", "早报", "--as-of", "2026-10-27"]
    with patch("qp.pushplus.send_pushplus") as send:
        assert main(args + ["--dry-run"]) == 0
        send.assert_not_called()
        output = capsys.readouterr().out
        assert "离线演示" in output and "标准FCF" in output
        assert main(args) == 0
        token, title, content = send.call_args.args
        assert token == "secret-token" and "早报" in title
        assert "合成数据" in content and "FCF收益率" in content
        assert "secret-token" not in capsys.readouterr().out


def test_cli_sends_failure_alert_and_nonzero(monkeypatch, capsys):
    monkeypatch.setenv("QP_PUSHPLUS_TOKEN", "secret-token")
    with patch("qp.cli.Pipeline", side_effect=RuntimeError("secret-token")), \
         patch("qp.pushplus.send_pushplus") as send:
        assert main(["push"]) == 1
    assert "FAILED" in send.call_args.args[2]
    assert "secret-token" not in send.call_args.args[2]
    assert "secret-token" not in capsys.readouterr().err


def test_cli_delivery_failure_is_nonzero(monkeypatch, capsys):
    monkeypatch.setenv("QP_PUSHPLUS_TOKEN", "secret-token")
    with patch("qp.cli.Pipeline", side_effect=RuntimeError("oops")), \
         patch("qp.pushplus.send_pushplus", side_effect=PushPlusError("推送失败")):
        assert main(["push"]) == 1
    assert "推送失败" in capsys.readouterr().err
