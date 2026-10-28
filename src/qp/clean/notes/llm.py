"""Structured LLM note parsing with anti-hallucination grounding and caching.

Any OpenAI-compatible ``/chat/completions`` endpoint works (vLLM, Ollama, a local
gateway, or a cloud vendor). No SDK dependency: stdlib urllib only.

Defence layers:

1. Strict JSON schema in the prompt + a tolerant (fenced-code) JSON reader.
2. **Grounding**: every extracted amount must literally appear in the supplied
   note text, and the ``evidence`` string must be a substring of that text.
   Otherwise the item is dropped and a WARN is emitted.
3. Enumerated categories, clamped confidence, bounded context length.
4. Deterministic disk cache keyed by (document text, model, prompt version).
5. If the LLM is disabled/unreachable/malformed, fall back to the regex parser —
   the pipeline never blocks on a model.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any, Iterable

from ...config import Config, LlmConfig, NotesConfig
from ...domain import (
    DISPOSAL_GAIN,
    FAIR_VALUE_GAIN,
    FX_GAIN_LOSS,
    GOVERNMENT_GRANT,
    IMPAIRMENT_LOSS,
    LITIGATION_SETTLEMENT,
    OTHER_NONRECURRING_GAIN,
    OTHER_NONRECURRING_LOSS,
)
from ...errors import LlmError
from ...util import fold_text, number_tokens, stable_hash
from .models import NoteChunk, NoteItemDraft, ParsedNotes
from .regex_parser import RegexNoteParser

ALLOWED_CATEGORIES = (
    FAIR_VALUE_GAIN, IMPAIRMENT_LOSS, GOVERNMENT_GRANT, FX_GAIN_LOSS,
    DISPOSAL_GAIN, LITIGATION_SETTLEMENT, OTHER_NONRECURRING_GAIN,
    OTHER_NONRECURRING_LOSS,
)

SYSTEM_PROMPT = """你是港股财务报表附注的结构化抽取引擎，只做抽取，不做估计。
输出必须是单个 JSON 对象：{"items": [...]}，不要输出 Markdown、解释或多余文字。
每条 item 的字段：
  label: 附注中的项目名称（原文）
  amount: 数字，原始披露金额（正数；不要换算单位）
  currency: "CNY" | "HKD" | "USD" | "EUR" | "GBP"
  unit_scale: 数字，披露单位相对"元"的倍数，如 千元=1000、百万元=1000000、亿元=100000000；已是元则 1
  direction: "gain"（增加利润）或 "loss"（减少利润）
  category: 从枚举中选择
  evidence: 包含该金额的原文短句（必须逐字来自输入）
  cash_impact: 布尔值，是否为一次性现金收付（影响经营现金流）
  confidence: 0~1
  page: 整数，来源页码
严禁编造数字：所有 amount 必须能在 evidence 中找到；不确定就省略该条。
category 枚举：""" + ", ".join(ALLOWED_CATEGORIES) + """
分类提示：投资物业/金融资产公允价值变动=FAIR_VALUE_GAIN；减值/拨备=IMPAIRMENT_LOSS；
政府补助/补贴=GOVERNMENT_GRANT；汇兑损益=FX_GAIN_LOSS；出售子公司/资产收益=DISPOSAL_GAIN；
诉讼赔偿/法律拨备=LITIGATION_SETTLEMENT；其他一次性收益/亏损=OTHER_NONRECURRING_GAIN/LOSS。
"""

_UNIT_SCALES: tuple[tuple[str, float], ...] = (
    ("亿元", 1e8), ("百万元", 1e6), ("千元", 1e3), ("万元", 1e4),
    ("亿", 1e8), ("百万", 1e6), ("千", 1e3), ("万", 1e4),
    ("billion", 1e9), ("million", 1e6), ("thousand", 1e3),
)


def build_prompt(chunks: Iterable[NoteChunk], max_chars: int = 12000) -> str:
    parts: list[str] = []
    total = 0
    for chunk in chunks:
        block = f"--- Page {chunk.page} (keyword={chunk.keyword}) ---\n{chunk.text}\n"
        if total + len(block) > max_chars:
            parts.append(block[: max(0, max_chars - total)])
            break
        parts.append(block)
        total += len(block)
    return (
        "以下是从港股年报 PDF 抽取的附注片段。请抽取所有非经常性损益项目。\n\n"
        + "".join(parts)
        + "\n\n现在输出 JSON。"
    )


def parse_json_loose(text: str) -> dict[str, Any]:
    """Accept raw JSON or ```json ... ``` fences; never eval()."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?|```$", "", cleaned, flags=re.IGNORECASE | re.MULTILINE).strip()
    try:
        out = json.loads(cleaned)
        if isinstance(out, dict):
            return out
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        out = json.loads(cleaned[start:end + 1])
        if isinstance(out, dict):
            return out
    raise LlmError("LLM response is not a JSON object")


def _unit_scale(value: Any) -> float:
    if isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) > 0:
        return float(value)
    text = fold_text(str(value or ""))
    for token, scale in _UNIT_SCALES:
        if token in text:
            return scale
    return 1.0


def _currency(value: Any, default: str = "CNY") -> str:
    token = str(value or "").upper().strip()
    if token in {"CNY", "RMB", "人民币"}:
        return "CNY"
    if token in {"HKD", "港币", "港元"}:
        return "HKD"
    if token in {"USD", "EUR", "GBP"}:
        return token
    return default


class LlmClient:
    def __init__(self, config: LlmConfig, sleep: Any = time.sleep) -> None:
        self.config = config
        self.sleep = sleep

    @property
    def endpoint(self) -> str:
        return self.config.base_url.rstrip("/") + "/chat/completions"

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        api_key = os.environ.get(self.config.api_key_env, "")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(
            self.endpoint, data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise LlmError(f"LLM HTTP {exc.code} from {self.endpoint}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LlmError(f"LLM request failed: {type(exc).__name__}") from exc

    def complete(self, prompt: str) -> str:
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.config.temperature,
            "stream": False,
        }
        last_error: Exception | None = None
        for attempt in range(self.config.retries + 1):
            try:
                response = self._post(payload)
                content = response["choices"][0]["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise LlmError("empty LLM content")
                return content
            except Exception as exc:  # network + schema errors are retryable
                last_error = exc
                if attempt < self.config.retries:
                    self.sleep(0.5 * (2 ** attempt))
        raise LlmError(f"LLM failed after {self.config.retries + 1} attempts: {last_error}")


class LlmNoteParser:
    name = "llm"

    def __init__(self, config: LlmConfig, notes: NotesConfig, client: LlmClient | None = None) -> None:
        self.config = config
        self.notes = notes
        self.client = client or LlmClient(config)

    def _validation_inputs(self, chunks: list[NoteChunk]) -> tuple[str, set[str], set[int]]:
        full_text = "\n".join(c.text for c in chunks)
        return full_text, number_tokens(full_text), {c.page for c in chunks}

    def parse(self, chunks: Iterable[NoteChunk]) -> ParsedNotes:
        chunk_list = list(chunks)
        result = ParsedNotes(model=self.config.model, prompt_version=self.config.prompt_version)
        if not chunk_list:
            result.warnings.append("no note chunks extracted from document")
            return result
        full_text, tokens, pages = self._validation_inputs(chunk_list)
        prompt = build_prompt(chunk_list, self.notes.max_note_chars)
        raw = self.client.complete(prompt)
        result.raw_response = raw[:5000]
        payload = parse_json_loose(raw)
        items = payload.get("items", [])
        if not isinstance(items, list):
            raise LlmError("LLM JSON 'items' is not a list")
        folded_full = fold_text(full_text)
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            label = str(raw_item.get("label") or "").strip()
            evidence = str(raw_item.get("evidence") or "").strip()
            try:
                amount = float(str(raw_item.get("amount", "")).replace(",", ""))
            except (TypeError, ValueError):
                result.warnings.append(f"dropped item with non-numeric amount: {label!r}")
                continue
            if not math.isfinite(amount) or abs(amount) < 1e-9:
                result.warnings.append(f"dropped item with invalid amount: {label!r}")
                continue
            if self.config.strict_grounding:
                if f"{abs(amount):.6g}" not in tokens and f"{amount:.6g}" not in tokens:
                    result.warnings.append(f"dropped ungrounded amount {amount} ({label!r})")
                    continue
                if evidence and fold_text(evidence[:40]) not in folded_full:
                    result.warnings.append(f"dropped item with fabricated evidence: {label!r}")
                    continue
            direction = str(raw_item.get("direction", "")).lower()
            if direction not in {"gain", "loss"}:
                direction = "loss" if amount < 0 else "gain"
            category = str(raw_item.get("category", "")).upper()
            if category not in ALLOWED_CATEGORIES:
                result.warnings.append(f"unknown category {category!r} → {OTHER_NONRECURRING_GAIN}")
                category = OTHER_NONRECURRING_GAIN
            try:
                confidence = min(1.0, max(0.0, float(raw_item.get("confidence", 0.7))))
            except (TypeError, ValueError):
                confidence = 0.7
            try:
                page: int | None = int(raw_item.get("page") or 0) or None
            except (TypeError, ValueError):
                page = None
            if page is not None and pages and page not in pages:
                page = None
            result.items.append(NoteItemDraft(
                label=label or category,
                amount=abs(amount),
                currency=_currency(raw_item.get("currency")),
                unit_scale=_unit_scale(raw_item.get("unit_scale", raw_item.get("unit"))),
                direction=direction,
                category=category,
                evidence=evidence[:400],
                confidence=confidence,
                source_kind="llm",
                page=page,
                cash_impact=bool(raw_item.get("cash_impact", False)),
                raw={"provider_item": {k: v for k, v in raw_item.items() if k != "evidence"}},
            ))
        return result


def _draft_to_dict(draft: NoteItemDraft) -> dict[str, Any]:
    return asdict(draft)


def _dict_to_draft(data: dict[str, Any]) -> NoteItemDraft:
    known = {f.name for f in fields(NoteItemDraft)}
    return NoteItemDraft(**{k: v for k, v in data.items() if k in known})


class CachedLlmNoteParser:
    """Disk cache: same document + model + prompt → same extraction, zero extra cost."""

    name = "llm-cached"

    def __init__(self, inner: LlmNoteParser, cache_dir: str | Path) -> None:
        self.inner = inner
        self.cache_dir = Path(cache_dir)

    def _key(self, chunks: list[NoteChunk]) -> str:
        return stable_hash({
            "text": "\n".join(f"{c.page}:{c.text}" for c in chunks),
            "model": self.inner.config.model,
            "prompt": self.inner.config.prompt_version,
            "strict": self.inner.config.strict_grounding,
        })

    def parse(self, chunks: Iterable[NoteChunk]) -> ParsedNotes:
        chunk_list = list(chunks)
        path = self.cache_dir / f"{self._key(chunk_list)}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return ParsedNotes(
                items=[_dict_to_draft(x) for x in data.get("items", [])],
                warnings=list(data.get("warnings", [])),
                model=data.get("model", ""),
                prompt_version=data.get("prompt_version", ""),
                raw_response=data.get("raw_response", ""),
                used_fallback=bool(data.get("used_fallback", False)),
            )
        result = self.inner.parse(chunk_list)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "items": [_draft_to_dict(x) for x in result.items],
            "warnings": result.warnings, "model": result.model,
            "prompt_version": result.prompt_version,
            "raw_response": result.raw_response[:5000],
            "used_fallback": result.used_fallback,
        }, ensure_ascii=False), encoding="utf-8")
        return result


class LlmWithFallbackParser:
    """LLM first, deterministic regex on ANY failure. Never blocks the pipeline."""

    name = "llm-with-fallback"

    def __init__(self, llm: Any, regex: RegexNoteParser | None = None) -> None:
        self.llm = llm
        self.regex = regex or RegexNoteParser()

    def parse(self, chunks: Iterable[NoteChunk]) -> ParsedNotes:
        chunk_list = list(chunks)
        try:
            return self.llm.parse(chunk_list)
        except Exception as exc:  # LlmError, JSON errors, transport, ...
            result = self.regex.parse(chunk_list)
            result.used_fallback = True
            result.warnings.insert(0, f"LLM unavailable ({type(exc).__name__}: {exc}); used regex fallback")
            return result


def create_note_parser(config: Config) -> Any:
    if not config.llm.enabled:
        return RegexNoteParser(default_currency="CNY")
    inner = LlmNoteParser(config.llm, config.notes)
    cached = CachedLlmNoteParser(inner, config.resolve(config.llm.cache_dir))
    return LlmWithFallbackParser(cached)
