"""Deterministic note parser — the always-available fallback when the LLM is off.

It is intentionally conservative: only lines that contain BOTH a non-recurring
keyword and a number produce an item. Everything else lands in ``warnings`` so a
human can review it; nothing is hallucinated.
"""

from __future__ import annotations

import re
from typing import Iterable

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
from ...util import fold_text, parse_number
from .models import NoteChunk, NoteItemDraft, ParsedNotes

CATEGORY_PATTERNS: tuple[tuple[str, str], ...] = (
    (FAIR_VALUE_GAIN, r"公允|公平|fair value|revaluation"),
    (IMPAIRMENT_LOSS, r"减值|減值|impairment|撥備|拨备"),
    (GOVERNMENT_GRANT, r"政府|补贴|补助|government grant|subsid"),
    (FX_GAIN_LOSS, r"汇兑|匯兌|外汇|foreign exchange|exchange (gain|loss)"),
    (DISPOSAL_GAIN, r"处置|出售|disposal|sale of|disposal of"),
    (LITIGATION_SETTLEMENT, r"诉讼|诉讼赔偿|litigation|settlement|legal provision"),
    (OTHER_NONRECURRING_LOSS, r"其他亏损|其他损失|other losses"),
    (OTHER_NONRECURRING_GAIN, r"非经常性|其他收益|其他收入|other (gains|income)|one-off|non-recurring"),
)

_AMOUNT_RE = re.compile(
    r"(?P<num>\(?-?\d[\d,]*(?:\.\d+)?\)?)\s*"
    r"(?P<unit>亿元|百万元|千元|万元|亿|万|thousand|million|billion)?",
    flags=re.IGNORECASE,
)
_UNIT_SCALES = {
    "亿": 1e8, "亿元": 1e8, "百万元": 1e6, "百万": 1e6, "million": 1e6,
    "billion": 1e9, "千元": 1e3, "万元": 1e4, "万": 1e4, "thousand": 1e3,
}
_CURRENCY_RE = re.compile(r"\b(HKD|CNY|RMB|USD|EUR|GBP)\b|港[币幣]|港[元圆]|人民币|人民幣|美元")


def classify(text: str) -> str:
    folded = fold_text(text)
    for category, pattern in CATEGORY_PATTERNS:
        if re.search(pattern, folded, flags=re.IGNORECASE):
            return category
    return OTHER_NONRECURRING_GAIN


def detect_currency(text: str, default: str = "CNY") -> str:
    folded = fold_text(text)
    match = _CURRENCY_RE.search(folded)
    if not match:
        return default
    token = match.group(0)
    if token.upper() in {"HKD", "CNY", "RMB", "USD", "EUR", "GBP"}:
        return "CNY" if token.upper() == "RMB" else token.upper()
    if "港" in token:
        return "HKD"
    if "人民" in token:
        return "CNY"
    if "美" in token:
        return "USD"
    return default


def detect_direction(text: str, raw_number: float) -> str:
    folded = fold_text(text)
    loss_words = r"亏损|虧損|损失|損失|支出|拨备|撥備|减值|減值|赔偿|賠償|费用|费用|loss|expense|impairment"
    gain_words = r"收益|收入|增益|利得|gain|income"
    has_loss = bool(re.search(loss_words, folded))
    has_gain = bool(re.search(gain_words, folded))
    if has_loss and not has_gain:
        return "loss"
    if has_gain and not has_loss:
        return "gain"
    return "loss" if raw_number < 0 else "gain"


class RegexNoteParser:
    name = "regex"

    def __init__(self, default_currency: str = "CNY") -> None:
        self.default_currency = default_currency

    def parse(self, chunks: Iterable[NoteChunk]) -> ParsedNotes:
        result = ParsedNotes(model="regex", prompt_version="regex-v1")
        seen: set[tuple[int, str, float]] = set()
        for chunk in chunks:
            for line in chunk.text.splitlines():
                line = line.strip()
                if len(line) < 6 or not re.search(r"\d", line):
                    continue
                if not re.search(
                    r"公允|公平|减值|減值|政府|补助|補貼|汇兑|匯兌|处置|出售|诉讼|賠償|赔偿|"
                    r"其他收益|其他收入|非经常|非經常|one-off|non-recurring|other gains|fair value|impairment|grant|disposal",
                    fold_text(line), flags=re.IGNORECASE,
                ):
                    continue
                for match in _AMOUNT_RE.finditer(line):
                    raw_num, unit = match.group("num"), (match.group("unit") or "").lower()
                    tail = line[match.end():match.end() + 2]
                    if "%" in tail or "％" in tail:
                        continue  # percentages and ratios are not amounts
                    amount = parse_number(raw_num)
                    if amount is None or abs(amount) < 1e-9:
                        continue
                    # Bare 1900–2100 integers are years/page numbers, not money.
                    if not unit and amount.is_integer() and 1900 <= abs(amount) <= 2100:
                        continue
                    # Small bare integers are note numbers / page numbers, not amounts.
                    if not unit and abs(amount) < 1000:
                        continue
                    scale = _UNIT_SCALES.get(unit, 1.0)
                    magnitude = abs(amount) * scale
                    key = (chunk.page, line[:60], round(magnitude, 2))
                    if key in seen:
                        continue
                    seen.add(key)
                    result.items.append(NoteItemDraft(
                        label=line[:60], amount=magnitude,
                        currency=detect_currency(line, self.default_currency),
                        unit_scale=1.0,
                        direction=detect_direction(line, amount),
                        category=classify(line),
                        evidence=line[:240],
                        confidence=0.55,
                        source_kind="regex",
                        page=chunk.page,
                        raw={"token": raw_num, "unit": unit},
                    ))
        if not result.items:
            result.warnings.append("regex parser found no non-recurring amounts in the extracted notes")
        return result
