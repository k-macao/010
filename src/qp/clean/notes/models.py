"""Intermediate types shared by PDF extraction, LLM parsing and the rule engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class NoteChunk:
    page: int
    keyword: str
    text: str
    document: str = ""


@dataclass
class NoteItemDraft:
    label: str
    amount: float                 # raw magnitude as printed (before unit_scale / FX)
    currency: str
    unit_scale: float
    direction: str                # gain | loss
    category: str                 # canonical field name
    evidence: str
    confidence: float
    source_kind: str              # llm | regex | xbrl
    page: int | None = None
    cash_impact: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedNotes:
    items: list[NoteItemDraft] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    model: str = ""
    prompt_version: str = ""
    raw_response: str = ""
    used_fallback: bool = False
