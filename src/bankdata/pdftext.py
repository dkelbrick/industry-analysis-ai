"""Cached page-level text and word extraction for report PDFs."""
from __future__ import annotations

import json
from pathlib import Path

import pdfplumber

from .config import DATA

CACHE = DATA / "raw" / "pdf_text"


def page_texts(pdf_path: Path) -> list[str]:
    """Plain text per page, cached as JSON next to raw data."""
    out = CACHE / (pdf_path.stem + ".json")
    if out.exists():
        return json.loads(out.read_text(encoding="utf-8"))
    import pypdfium2 as pdfium  # fast text layer, used only for locating pages

    texts = []
    doc = pdfium.PdfDocument(str(pdf_path))
    for i in range(len(doc)):
        try:
            texts.append(doc[i].get_textpage().get_text_range())
        except Exception:  # noqa: BLE001 - malformed pages should not stop the run
            texts.append("")
    doc.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(texts), encoding="utf-8")
    return texts


def page_words(pdf_path: Path, page_index: int) -> tuple[list[dict], float]:
    """Words with coordinates for one page, and the page width."""
    with pdfplumber.open(pdf_path) as pdf:
        p = pdf.pages[page_index]
        words = p.extract_words(keep_blank_chars=False, use_text_flow=False, x_tolerance=1.5, y_tolerance=2)
        return words, float(p.width)
