"""PDF/HTML/text note extraction (PyMuPDF → pdfplumber fallback → plain text).

The design sketch returned whole pages containing a keyword. That blows up the LLM
context on a 300-page annual report; here each keyword contributes a bounded,
deduplicated context window with its page number preserved as provenance.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from ...errors import NotesExtractionError
from ...util import fold_text
from .models import NoteChunk

DEFAULT_KEYWORDS = (
    "非经常性损益", "其他收益", "其他收入", "公允价值变动", "政府补助",
    "汇兑", "诉讼", "赔偿", "non-recurring", "other gains", "fair value",
)


def _pages_pymupdf(path: Path) -> list[str]:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        try:
            import pymupdf as fitz  # type: ignore
        except ImportError:
            raise NotesExtractionError("PDF extraction needs PyMuPDF: pip install '.[sources]'")
    with fitz.open(str(path)) as doc:
        return [page.get_text() for page in doc]


def _pages_pdfplumber(path: Path) -> list[str]:
    try:
        import pdfplumber  # type: ignore
    except ImportError as exc:
        raise NotesExtractionError(
            "No PDF backend available. Install pymupdf or pdfplumber: pip install '.[sources]'"
        ) from exc
    with pdfplumber.open(str(path)) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


def page_texts(path: str | Path) -> list[str]:
    p = Path(path)
    if not p.exists():
        raise NotesExtractionError(f"document not found: {p}")
    if p.suffix.lower() in {".txt", ".md", ".html", ".htm"}:
        return [p.read_text(encoding="utf-8", errors="replace")]
    try:
        return _pages_pymupdf(p)
    except NotesExtractionError as exc:
        if "PyMuPDF" not in str(exc):
            raise
        return _pages_pdfplumber(p)


def keyword_windows(text: str, keywords: Iterable[str], context_chars: int = 700) -> list[tuple[str, str]]:
    """Return (keyword, window) pairs, merging overlapping windows."""
    folded = fold_text(text)
    if len(folded) != len(text):
        folded = text.lower()  # rare NFKC length change: fall back to literal search
    hits: list[tuple[int, int, str]] = []
    for keyword in keywords:
        needle = fold_text(keyword)
        if not needle:
            continue
        for match in re.finditer(re.escape(needle), folded, flags=re.IGNORECASE):
            start = max(0, match.start() - context_chars // 3)
            end = min(len(text), match.end() + context_chars)
            hits.append((start, end, keyword))
    if not hits:
        return []
    hits.sort()
    merged: list[tuple[int, int, str]] = []
    for start, end, keyword in hits:
        if merged and start <= merged[-1][1]:
            prev_start, prev_end, prev_keyword = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end), f"{prev_keyword},{keyword}")
        else:
            merged.append((start, end, keyword))
    return [(kw, text[start:end]) for start, end, kw in merged]


def extract_note_chunks(
    path: str | Path,
    keywords: Iterable[str] = DEFAULT_KEYWORDS,
    context_chars: int = 700,
    max_chunks: int = 20,
) -> list[NoteChunk]:
    p = Path(path)
    chunks: list[NoteChunk] = []
    for page_no, text in enumerate(page_texts(p), start=1):
        if not text.strip():
            continue
        for keyword, window in keyword_windows(text, keywords, context_chars):
            chunks.append(NoteChunk(page_no, keyword, window, document=str(p)))
            if len(chunks) >= max_chunks:
                return chunks
    return chunks


def extract_pdf_notes(
    pdf_path: str | Path,
    keyword: str = "非经常性损益",
    context_chars: int = 700,
) -> str:
    """Compatibility wrapper for the sketch's ``extract_pdf_notes`` (returns str).

    ``--- Page N ---`` markers are preserved so the LLM can cite provenance.
    """
    chunks = extract_note_chunks(pdf_path, keywords=(keyword,), context_chars=context_chars)
    return "\n".join(f"--- Page {chunk.page} ---\n{chunk.text}" for chunk in chunks)


def infer_period_end(path: str | Path) -> str | None:
    match = re.search(r"(20\d{2})[-_]?(\d{2})?[-_]?(\d{2})?", Path(path).stem)
    if not match:
        return None
    year = int(match.group(1))
    return f"{year}-12-31"
