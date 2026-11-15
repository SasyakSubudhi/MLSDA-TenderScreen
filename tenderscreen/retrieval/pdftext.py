"""PDF -> normalised page text."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from pypdf import PdfReader

from .normalize import normalize_text


@dataclass
class Page:
    doc_id: str
    family: str
    page: int  # 1-based page in the local PDF file
    original_page: int  # page number in the official source PDF
    text: str


def load_manifest(corpus_dir: str) -> dict:
    with open(os.path.join(corpus_dir, "manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)


def read_pages(corpus_dir: str, doc: dict) -> list[Page]:
    reader = PdfReader(os.path.join(corpus_dir, doc["file"]))
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        text = normalize_text(page.extract_text() or "")
        if text:
            pages.append(Page(doc["doc_id"], doc["family"], i, i + int(doc.get("page_offset", 0)), text))
    return pages
