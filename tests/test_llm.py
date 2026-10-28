from __future__ import annotations

import json

import pytest

from qp.clean.notes.llm import (
    SYSTEM_PROMPT,
    CachedLlmNoteParser,
    LlmClient,
    LlmNoteParser,
    LlmWithFallbackParser,
    build_prompt,
    create_note_parser,
    parse_json_loose,
)
from qp.clean.notes.models import NoteChunk
from qp.clean.notes.regex_parser import RegexNoteParser
from qp.config import Config, NotesConfig
from qp.errors import LlmError

CHUNKS = [NoteChunk(
    page=12, keyword="非经常性损益",
    text="附注 12 非经常性损益明细（人民币千元）：投资物业公允价值变动收益 8,000,000 千元；"
         "诉讼赔偿支出 1,200,000 千元；政府补助 1,500,000 千元。",
    document="annual.pdf",
)]


class FakeClient(LlmClient):
    def __init__(self, config, response=None, error=None):
        super().__init__(config, sleep=lambda _: None)
        self.response = response
        self.error = error
        self.calls = 0

    def _post(self, payload):
        self.calls += 1
        if self.error:
            raise self.error
        return {"choices": [{"message": {"content": self.response}}]}


def _config(tmp_path=None, enabled=True, strict=True) -> Config:
    cfg = Config()
    cfg.llm.enabled = enabled
    cfg.llm.strict_grounding = strict
    cfg.llm.retries = 0
    cfg.llm.model = "test-model"
    cfg.llm.base_url = "http://localhost:1/v1"
    if tmp_path:
        cfg.base_dir = tmp_path
        cfg.llm.cache_dir = str(tmp_path / "cache")
    return cfg


def _response(items) -> str:
    return json.dumps({"items": items}, ensure_ascii=False)


def test_prompt_contains_grounding_instruction():
    prompt = build_prompt(CHUNKS)
    assert "Page 12" in prompt and "JSON" in prompt
    assert "严禁编造数字" in SYSTEM_PROMPT


def test_parse_json_loose_handles_fences():
    assert parse_json_loose('```json\n{"items": []}\n```') == {"items": []}
    with pytest.raises(LlmError):
        parse_json_loose("totally not json")


def test_llm_drops_hallucinated_amounts():
    items = [
        {"label": "投资物业公允价值变动收益", "amount": 8000000, "currency": "CNY",
         "unit_scale": 1000, "direction": "gain", "category": "FAIR_VALUE_GAIN",
         "evidence": "投资物业公允价值变动收益 8,000,000 千元", "confidence": 0.9, "page": 12},
        {"label": "虚构的补贴", "amount": 9999999, "currency": "CNY",
         "unit_scale": 1000, "direction": "gain", "category": "GOVERNMENT_GRANT",
         "evidence": "政府补助 1,500,000 千元", "confidence": 0.9, "page": 12},
    ]
    client = FakeClient(_config().llm, response=_response(items))
    parser = LlmNoteParser(_config().llm, NotesConfig(), client=client)
    parsed = parser.parse(CHUNKS)
    assert len(parsed.items) == 1
    assert parsed.items[0].amount == 8000000
    assert parsed.items[0].unit_scale == 1000
    assert any("ungrounded" in w for w in parsed.warnings)


def test_llm_validates_category_and_confidence():
    items = [{
        "label": "诉讼赔偿", "amount": 1200000, "currency": "RMB", "unit": "千元",
        "direction": "loss", "category": "MADE_UP_CATEGORY",
        "evidence": "诉讼赔偿支出 1,200,000 千元", "confidence": 42, "page": 99,
    }]
    parsed = LlmNoteParser(_config().llm, NotesConfig(),
                           client=FakeClient(_config().llm, response=_response(items))).parse(CHUNKS)
    item = parsed.items[0]
    assert item.category == "OTHER_NONRECURRING_GAIN"
    assert item.currency == "CNY"
    assert item.unit_scale == 1000
    assert item.confidence == 1.0
    assert item.page is None  # page 99 is not in the supplied chunks


def test_llm_failure_falls_back_to_regex():
    client = FakeClient(_config().llm, error=RuntimeError("connection refused"))
    inner = LlmNoteParser(_config().llm, NotesConfig(), client=client)
    parser = LlmWithFallbackParser(inner, RegexNoteParser())
    parsed = parser.parse(CHUNKS)
    assert parsed.used_fallback
    assert parsed.items
    assert any("LLM unavailable" in w for w in parsed.warnings)


def test_cache_avoids_second_call(tmp_path):
    items = [{
        "label": "政府补助", "amount": 1500000, "currency": "CNY", "unit_scale": 1000,
        "direction": "gain", "category": "GOVERNMENT_GRANT",
        "evidence": "政府补助 1,500,000 千元", "confidence": 0.9, "page": 12,
    }]
    cfg = _config(tmp_path)
    client = FakeClient(cfg.llm, response=_response(items))
    parser = CachedLlmNoteParser(LlmNoteParser(cfg.llm, cfg.notes, client=client), cfg.llm.cache_dir)
    first = parser.parse(CHUNKS)
    second = parser.parse(CHUNKS)
    assert client.calls == 1
    assert first.items[0].amount == second.items[0].amount


def test_create_note_parser_respects_disabled_llm(tmp_path):
    cfg = _config(tmp_path, enabled=False)
    assert isinstance(create_note_parser(cfg), RegexNoteParser)
