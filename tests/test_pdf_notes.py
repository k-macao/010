from __future__ import annotations

import pytest

from qp.clean.notes.pdf_notes import (
    extract_note_chunks,
    extract_pdf_notes,
    infer_period_end,
    keyword_windows,
)


def test_keyword_windows_preserve_original_offsets():
    text = "开头" + "x" * 50 + "非经常性损益明细" + "y" * 50 + "结尾"
    windows = keyword_windows(text, keywords=("非经常性损益",), context_chars=30)
    assert len(windows) == 1
    assert "非经常性损益" in windows[0][1]


def test_traditional_keyword_matches_simplified_text():
    text = "本页包含匯兌差額及政府補助等項目。"
    windows = keyword_windows(text, keywords=("汇兑", "政府补助"), context_chars=20)
    assert len(windows) >= 1


def test_infer_period_end():
    assert infer_period_end("/tmp/00700_2025_annual_report.pdf") == "2025-12-31"
    assert infer_period_end("/tmp/no-year.txt") is None


def test_pdf_backend_if_available(tmp_path):
    fitz = pytest.importorskip("fitz", reason="PyMuPDF optional")
    path = tmp_path / "notes.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Notes: non-recurring items and fair value gain 8,000,000")
    doc.save(str(path))
    doc.close()
    chunks = extract_note_chunks(path, keywords=("fair value",), context_chars=200)
    assert chunks and chunks[0].page == 1
    assert "fair value" in extract_pdf_notes(path, keyword="fair value")
