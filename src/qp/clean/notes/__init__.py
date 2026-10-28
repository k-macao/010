"""LLM/rule-assisted parsing of HK annual-report notes (非经常性损益)."""

from __future__ import annotations

from .engine import NonRecurringEngine, NonRecurringResult, rules_from_aligned  # noqa: F401
from .models import NoteChunk, NoteItemDraft, ParsedNotes  # noqa: F401

__all__ = [
    "NonRecurringEngine", "NonRecurringResult", "rules_from_aligned",
    "NoteChunk", "NoteItemDraft", "ParsedNotes",
]
