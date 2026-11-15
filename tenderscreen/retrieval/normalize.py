"""Text clean-up applied to PDF text before chunking.

The GFR PDF uses letter-spaced headings ("R e s e r v e d  I t e m s"), and
all four PDFs have hard line breaks inside sentences. Normalising once, at
ingestion, means the text the agent quotes and the text the verifier checks
are byte-for-byte the same string.
"""
from __future__ import annotations

import re

_LETTER_SPACED = re.compile(r"\b(?:[A-Za-z] ){2,}[A-Za-z]\b")
_WS = re.compile(r"\s+")


def _join_letter_spaced(match: re.Match) -> str:
    return match.group(0).replace(" ", "")


def normalize_text(text: str) -> str:
    text = text.replace("\u00a0", " ").replace("\u2019", "'").replace("\u2018", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"').replace("\u2013", "-").replace("\u2014", "-")
    text = text.replace("\uf0b7", " ")  # PDF bullet glyph
    # join letter-spaced words BEFORE collapsing whitespace: in "R e s e r v e d  I t e m s"
    # the double space is the only thing that separates the two words
    text = _LETTER_SPACED.sub(_join_letter_spaced, text)
    text = _WS.sub(" ", text)
    return text.strip()


def canon(text: str) -> str:
    """Canonical form used when checking that a quote occurs in a passage."""
    return _WS.sub(" ", normalize_text(text)).strip().lower()


_ABBREV = re.compile(r"(?:\b(?:Rs|No|Nos|Rule|Govt|Dept|Ltd|Pvt|viz|i\.e|e\.g|Sl|S|para|Para|Cl|Annex|OM|vide|Dr|Mr|Ms|Sh)\.)$")


def sentences(text: str) -> list[tuple[int, int]]:
    """Split ``text`` into sentence spans (start, end) without breaking on
    common abbreviations such as "Rs." or "No.". Spans index into ``text``
    itself, so a quote taken from a span is always an exact substring."""
    spans: list[tuple[int, int]] = []
    start = 0
    for m in re.finditer(r"[.;](?=\s+[A-Z(0-9\[])", text):
        end = m.end()
        if _ABBREV.search(text[max(0, end - 8):end]):
            continue
        # don't split "Rs.24,00,000" style or numbered list "1." at sentence start
        if re.search(r"(?:^|\s)\(?\d{1,2}\.$", text[max(0, end - 4):end]):
            continue
        spans.append((start, end))
        start = end
        while start < len(text) and text[start] == " ":
            start += 1
    if start < len(text):
        spans.append((start, len(text)))
    return [s for s in spans if s[1] - s[0] > 1]
