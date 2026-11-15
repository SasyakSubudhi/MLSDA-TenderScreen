"""Build corpus/clauses.json - the curated clause registry.

Each clause is cut out of the *normalised PDF text* between a start anchor
and an end anchor, so the registry never contains paraphrased legal text.
The registry powers the ``lookup_clause`` tool (lookup by identifier) and
the clause tags attached to every indexed chunk.

Run:  python -m tenderscreen.retrieval.build_clauses
"""
from __future__ import annotations

import json
import os
import re
import sys

from .pdftext import load_manifest, read_pages

# clause_id, doc_id, local page, start regex, end regex (exclusive), title, topics, categories
SPECS = [
    ("TENDER-1.2", "tender", 2, r"1\.2\. The tender documents may be obtained", r"1\.3\.",
     "1.2 Where the tender is published (CPPP) and the closing date", ["publication"], []),
    ("TENDER-1.7", "tender", 2, r"1\.7\. Each tenderer shall submit only one bid", r"1\.8\.",
     "1.7 One bid per bidder / joint venture / consortium", ["consortium"], ["consortium"]),
    ("TENDER-5.3", "tender", 4, r"5\.3\. The bidder shall furnish, as part of the bid, an EMD", r"5\.3\.2\.",
     "5.3 Earnest Money Deposit @ 2% of estimated value; bids without EMD rejected", ["emd"], []),
    ("TENDER-5.7", "tender", 4, r"5\.7\. Information related to the agency", r"5\.8\.",
     "5.7 Registration / PAN / GST / TIN copies", ["documents"], []),
    ("TENDER-5.9", "tender", 4, r"5\.9\. Bidders, who are bidding for this NIT", r"5\.10\.",
     "5.9 Purchase orders from CFTIs, performance certificate, turnover of Rs.5 Lakh each year, audited statements",
     ["prior turnover", "prior experience", "documents"], []),
    ("TENDER-5.10", "tender", 4, r"5\.10\. Copy of Tender Document", r"$",
     "5.10 Signed copy of the tender document marked Original", ["documents"], []),
    ("ANNEX-II-1", "tender", 13, r"1\.The bidder should be either an Original Equipment Manufacturer", r"2\.The bidder should be a company",
     "Annexure-II 1 OEM or authorised representative of an OEM", ["documents"], []),
    ("ANNEX-II-2", "tender", 13, r"2\.The bidder should be a company registered", r"3\.Either the bidder",
     "Annexure-II 2 Registered company / LLP / partnership / proprietorship", ["documents", "registration"], ["foreign"]),
    ("ANNEX-II-4-5", "tender", 13, r"4\.The bidder must have a registered office", r"6\.The bidder must be in existence",
     "Annexure-II 4-5 Registered office and service centre in KA/TN/TS/AP/KL", ["regional presence"], []),
    ("ANNEX-II-6", "tender", 13, r"6\.The bidder must be in existence", r"7\.The bidder must provide proof",
     "Annexure-II 6 At least three financial years in the desktop computer business", ["prior experience"], []),
    ("ANNEX-II-7", "tender", 13, r"7\.The bidder must provide proof of turnover", r"8\.The bidder/OEM",
     "Annexure-II 7 Turnover of at least Rs.5 Lakh in each of the last three financial years", ["prior turnover"], []),
    ("ANNEX-II-8", "tender", 13, r"8\.The bidder/OEM should have supplied", r"9\.The bidder must provide detailed",
     "Annexure-II 8 Desktops supplied to CFTIs in the last five years, with purchase orders", ["prior experience"], []),
    ("ANNEX-II-10", "tender", 13, r"10\.Compliance sheet", r"$",
     "Annexure-II 10 Compliance sheet with OEM brochure references", ["documents"], []),
    ("ANNEX-VII", "tender", 19, r"Annexure-VII DECLARATION", r"$",
     "Annexure-VII Declaration of serially numbered pages and OEM authorisation", ["documents"], []),
    ("ANNEX-VIII", "tender", 20, r"Annexure-VIII LIST OF EQUIPMENT", r"$",
     "Annexure-VIII Estimated value Rs.8.8 Lakh and EMD Rs.17,600", ["emd"], []),
    ("GFR-170", "gfr_2017", 9, r"Rule 170 Bid Security", r"The bid security may be accepted",
     "GFR 2017 Rule 170(i) - Bid Security not obtained from MSEs", ["emd"], ["mse"]),
    ("GFR-173", "gfr_2017", 10, r"The condition of prior turnover and prior experience may be relaxed for Startups", r"The bidding document should contain",
     "GFR 2017 Rule 173(i) - prior turnover/experience may be relaxed for Startups", ["prior turnover", "prior experience"], ["startup"]),
    ("DOE-2016-07", "doe_om_2016_07", 2, r"2\. Ministry of Micro, Small", r"To The Secretaries|/ /:",
     "DoE OM 25-07-2016 paras 2-3 - relaxation for MSEs and Startups", ["prior turnover", "prior experience"], ["mse", "startup"]),
    ("DOE-2016-07-REPRINT", "doe_om_2016_09", 2, r"2\. Ministry of Micro, Small & Medium Enterprises \(MSMEs\) vide", r"\[D/o Expenditure",
     "DoE OM 25-07-2016 paras 2-3 (clean reprint in DPE compendium) - MSE and Startup relaxation", ["prior turnover", "prior experience"], ["mse", "startup"]),
    ("DOE-2016-09", "doe_om_2016_09", 1, r"The undersigned is directed to refer to this Department O\.M\. of even number dated 25th July", r"3\. However",
     "DoE OM 20-09-2016 paras 1-2 - relaxation for Startups normally to be ensured", ["prior turnover", "prior experience"], ["startup"]),
]


# a clause whose text is a reprint of another document is labelled with the document it reproduces
SOURCE_OVERRIDES = {"DOE-2016-07-REPRINT": "DoE OM 25-07-2016 (reprinted in DPE compendium)"}


def build(corpus_dir: str) -> list[dict]:
    manifest = load_manifest(corpus_dir)
    docs = {d["doc_id"]: d for d in manifest["documents"]}
    page_cache: dict[str, dict[int, object]] = {}
    clauses = []
    for cid, doc_id, page_no, start_re, end_re, title, topics, cats in SPECS:
        if doc_id not in page_cache:
            page_cache[doc_id] = {p.page: p for p in read_pages(corpus_dir, docs[doc_id])}
        page = page_cache[doc_id][page_no]
        text = page.text
        m = re.search(start_re, text)
        if not m:
            raise SystemExit(f"{cid}: start anchor not found on page {page_no}")
        rest = text[m.start():]
        e = re.search(end_re, rest[1:])
        body = rest[: e.start() + 1].strip() if e and end_re != r"$" else rest.strip()
        body = body[:1500].rsplit(" ", 1)[0] if len(body) > 1500 else body
        doc = docs[doc_id]
        clauses.append({
            "clause_id": cid,
            "doc_id": doc_id,
            "family": doc["family"],
            "title": title,
            "page": page_no,
            "original_page": page.original_page,
            "url": doc["url"],
            "source": SOURCE_OVERRIDES.get(cid, doc["short"]),
            "topics": topics,
            "categories": cats,
            "text": body,
        })
    return clauses


def main() -> None:
    corpus_dir = sys.argv[1] if len(sys.argv) > 1 else "corpus"
    clauses = build(corpus_dir)
    with open(os.path.join(corpus_dir, "clauses.json"), "w", encoding="utf-8") as fh:
        json.dump({"clauses": clauses}, fh, indent=2, ensure_ascii=False)
    for c in clauses:
        print(f"{c['clause_id']:15s} p{c['page']:<3d} {len(c['text']):5d} chars  {c['text'][:90]}")


if __name__ == "__main__":
    main()
