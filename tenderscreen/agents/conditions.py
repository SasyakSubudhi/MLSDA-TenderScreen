"""Mandatory conditions of tender IITPKD/AR/RS/053/2018-19 and the rule engine
that evaluates a bid against *retrieved* passages.

Design rule: the language model never decides a status. Statuses are
computed here, deterministically, from (a) the bid's own fields and (b) the
passages the agent actually retrieved. Thresholds such as "Rs.5 Lakh", "three
financial years", the EMD of Rs.17,600 and the list of states are parsed out
of the retrieved tender text, not hard-coded, so a finding is only as good as
the evidence the agent found - which is exactly what the verifier re-checks.

Statuses
    MET               condition satisfied on the cited evidence
    UNMET             condition not satisfied and no applicable relaxation found
    CONFLICT          tender says unmet, but a central rule relaxes or exempts it
                      for this bidder category - two authorities disagree -> escalate
    INSUFFICIENT      a curable gap (missing document, unclear exemption basis)
                      -> seek clarification
    ESCALATE          the corpus cannot settle it (category unclear, consortium,
                      foreign bidder) -> human
    EVIDENCE_MISSING  the governing clause was not found in what was retrieved
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from ..retrieval.normalize import sentences

TENDER_ID = "IITPKD/AR/RS/053/2018-19"
FINANCIAL_YEARS = ["2015-16", "2016-17", "2017-18"]
TENDER_CLOSING_DATE = date(2019, 4, 22)
STARTUP_MAX_AGE_YEARS = 10           # DPIIT startup definition (outside corpus: used only to flag doubt)
STARTUP_MAX_TURNOVER_INR = 100_00_00_000  # Rs.100 crore, same caveat

ROUTES = ("pass_to_technical_evaluation", "seek_clarification", "reject_with_cited_condition", "escalate_to_human")
ROUTE_LABELS = {
    "pass_to_technical_evaluation": "Pass to technical evaluation",
    "seek_clarification": "Seek clarification from bidder",
    "reject_with_cited_condition": "Recommend rejection (cited condition)",
    "escalate_to_human": "Escalate to Tender Committee (human review)",
}

_WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}

CATEGORY_PATTERNS = {
    "startup": re.compile(r"start-?ups?", re.I),
    "mse": re.compile(r"micro\s*(?:&|and)\s*small\s+enterprises|\bMSEs?\b|\bNSIC\b", re.I),
}
RELAX_PATTERN = re.compile(r"\brelax(?:ed|ation)?\b|\bexcept\b|\bexempt(?:ed|ion)?\b", re.I)


@dataclass(frozen=True)
class Condition:
    id: str
    title: str
    tender_clause: str                 # registry id of the governing tender clause
    anchors: tuple[str, ...]           # regexes identifying the governing text
    tender_queries: tuple[str, ...]    # first and alternative search query
    topic: re.Pattern | None = None    # what a modifying provision must talk about
    relaxable: bool = False            # policy may relax it for some categories
    doc_requirements: tuple = field(default_factory=tuple)


CONDITIONS: tuple[Condition, ...] = (
    Condition(
        "C1_TURNOVER", "Annual turnover in each of the last three financial years", "ANNEX-II-7",
        (r"turnover of at least Rs\.\s?[\d.,]+ Lakh",),
        ("proof of turnover of at least Rs 5 Lakh during each of the last three financial years",
         "turnover of at least Rs.5 Lakh during each of the last three financial years"),
        re.compile(r"prior\s+turnover|turnover", re.I), True,
    ),
    Condition(
        "C2_EXPERIENCE", "Prior experience (years in business; supplies to CFTIs)", "ANNEX-II-6",
        (r"in existence in the business of Desktop Computers for a minimum period of last \w+ financial years",
         r"supplied Desktop Computers to Centrally Funded Technical Institutions"),
        ("bidder in existence in the business of desktop computers minimum period of last three financial years",
         "in existence in the business of Desktop Computers for a minimum period"),
        re.compile(r"prior\s+experience|experience", re.I), True,
    ),
    Condition(
        "C3_EMD", "Earnest Money Deposit (bid security)", "TENDER-5.3",
        (r"furnish, as part of the bid, an EMD", r"EMD \(INR\)"),
        ("bidder shall furnish EMD earnest money deposit 2% of estimated value",
         "furnish, as part of the bid, an EMD (Earnest Money Deposit)"),
        re.compile(r"bid\s+security|earnest\s+money|\bEMD\b", re.I), True,
    ),
    Condition(
        "C4_DOCUMENTS", "Mandatory documents enclosed with the bid", "TENDER-5.9",
        (r"enclose the audited statements", r"documentary evidence \(Purchase Order\)",
         r"Registration incorporation certificate", r"Copy of Tender Document marked",
         r"Compliance sheet for the technical specification", r"pages, serially numbered"),
        ("documents bid purchase order certificate of satisfactory performance audited statements Chartered Accountant",
         "Registration incorporation certificate compliance sheet tender document marked Original"),
        None, False,
        (
            ("audited_financial_statements", "audited statements of the three financial years", r"enclose the audited statements"),
            ("ca_certificate", "certification by a Chartered Accountant", r"certified by a Chartered Accountant"),
            ("purchase_orders", "purchase orders of supplies to CFTIs", r"documentary evidence \(Purchase Order\)"),
            ("performance_certificate", "certificate of satisfactory performance", r"certificate of satisfactory performance"),
            ("incorporation_certificate", "registration / incorporation certificate", r"Registration incorporation certificate"),
            ("signed_tender_copy", "signed tender document marked Original", r"Copy of Tender Document marked"),
            ("compliance_statement", "compliance sheet with OEM brochure references", r"Compliance sheet for the technical specification"),
            ("page_declaration", "page-numbering declaration (Annexure-VII)", r"pages, serially numbered"),
        ),
    ),
    Condition(
        "C5_REGIONAL_PRESENCE", "Registered office and service centre in the specified states", "ANNEX-II-4-5",
        (r"must have a registered office in",),
        ("bidder must have a registered office and service center in Karnataka Tamil Nadu Telangana Andhra Pradesh Kerala",
         "must have a registered office in Karnataka"),
        None, False,
    ),
)
CONDITION_BY_ID = {c.id: c for c in CONDITIONS}
CATEGORY_CONDITION = "C0_CATEGORY"


# ===================================================================== profile
def classify_bidder(bid: dict) -> dict:
    """F3: classify the bidder and extract the qualification entities."""
    b = bid["bidder"]
    docs = bid.get("documents", {})
    turnover = [float(bid["financials"]["annual_turnover_inr"][fy]) for fy in FINANCIAL_YEARS]
    inc = date.fromisoformat(b["date_of_incorporation"])
    age = (TENDER_CLOSING_DATE - inc).days / 365.25
    declared = b["declared_category"]
    reasons: list[str] = []
    has_udyam = bool(b.get("udyam_registration") or b.get("nsic_registration"))
    has_dpiit = bool(b.get("dpiit_recognition"))

    category = declared
    if declared == "startup":
        if not docs.get("startup_recognition_certificate"):
            category = "unclear"
            reasons.append("startup declared but the DPIIT recognition certificate is not enclosed")
        if age > STARTUP_MAX_AGE_YEARS:
            category = "unclear"
            reasons.append(f"declared startup is {age:.1f} years old; DPIIT recognition normally ends at "
                           f"{STARTUP_MAX_AGE_YEARS} years (definition not in corpus - human check)")
        if max(turnover) >= STARTUP_MAX_TURNOVER_INR:
            category = "unclear"
            reasons.append("declared startup reports turnover of Rs.100 crore or more in a year")
    elif declared == "mse":
        if not has_udyam:
            category = "unclear"
            reasons.append("MSE declared without Udyam/NSIC registration")
    elif declared == "large_enterprise":
        if has_dpiit or has_udyam:
            category = "unclear"
            reasons.append("declared large enterprise but also supplied startup/MSE registration")
    elif declared == "consortium":
        reasons.append("consortium bid: the tender only bars multiple bids by consortium members (clause 1.7); "
                       "it does not say how turnover/experience of members combine")
    elif declared == "foreign":
        reasons.append("foreign bidder: Annexure-II 2 requires registration under the Companies Act / LLP / "
                       "partnership / proprietorship in India")
    else:
        category = "unclear"
        reasons.append("bidder category not stated")

    return {
        "declared_category": declared,
        "category": category,
        "category_clear": category in ("large_enterprise", "mse", "startup"),
        "also_mse": declared == "startup" and has_udyam,
        "reasons": reasons,
        "entities": {
            "legal_name": b["legal_name"],
            "age_years": round(age, 1),
            "turnover_by_year": dict(zip(FINANCIAL_YEARS, turnover)),
            "minimum_year_turnover_inr": min(turnover),
            "average_turnover_inr": round(sum(turnover) / len(turnover), 2),
            "years_in_desktop_business": int(bid["experience"]["years_in_desktop_business"]),
            "cfti_supply_orders": int(bid["experience"]["cfti_supply_orders"]),
            "emd_status": bid["emd"]["status"],
            "emd_amount_inr": float(bid["emd"].get("amount_inr", 0)),
            "is_oem": bool(b.get("is_oem", False)),
            "registered_office_state": b.get("registered_office_state"),
            "service_centre_states": list(b.get("service_centre_states") or []),
            "udyam_or_nsic": has_udyam,
            "dpiit_recognition": b.get("dpiit_recognition"),
            "documents_missing": sorted(k for k, v in docs.items() if not v),
        },
    }


# ===================================================================== evidence helpers
def quote_for(text: str, pattern: str) -> str | None:
    """Return the sentence(s) of ``text`` containing ``pattern`` - always an
    exact substring of ``text``."""
    m = re.search(pattern, text, re.I)
    if not m:
        return None
    for s, e in sentences(text):
        if s <= m.start() < e:
            if e - s > 420:  # long run-on sentence: trim to a window on word boundaries
                a = max(s, text.rfind(" ", s, max(s, m.start() - 150)) + 1)
                b = text.find(" ", min(e, m.end() + 220))
                b = e if b == -1 or b > e else b
                return text[a:b].strip()
            return text[s:e].strip()
    return None


def governing_passage(cond: Condition, passages: list[dict], anchor: str | None = None) -> dict | None:
    pats = [anchor] if anchor else list(cond.anchors[:1])
    best = None
    for p in passages:
        if p["family"] != "tender":
            continue
        if all(re.search(a, p["text"], re.I) for a in pats):
            # prefer intact clause units over page windows
            if best is None or (p["kind"] == "clause" and best["kind"] != "clause"):
                best = p
    return best


def modifier_sentences(cond: Condition, category: str, passages: list[dict]) -> list[dict]:
    """Policy sentences that relax/exempt ``cond`` for ``category``."""
    if not cond.topic or category not in CATEGORY_PATTERNS:
        return []
    out = []
    for p in passages:
        if p["family"] != "policy":
            continue
        text = p["text"]
        for s, e in sentences(text):
            sent = text[s:e]
            if not (cond.topic.search(sent) and RELAX_PATTERN.search(sent) and CATEGORY_PATTERNS[category].search(sent)):
                continue
            if category == "mse" and CATEGORY_PATTERNS["startup"].search(sent):
                continue  # a startup provision mentioning "(whether MSEs or otherwise)"
            kind = ("mandatory_normally" if re.search(r"have to ensure", sent, re.I)
                    else "exemption" if re.search(r"\bexcept|exempt", sent, re.I) else "discretionary")
            quote = sent.strip()
            if len(quote) > 420:
                # long run-on sentence: centre the quote on the relaxing words ("except", "relaxed")
                quote = quote_for(text[s:e], RELAX_PATTERN.pattern) or quote[:420].rsplit(" ", 1)[0]
            out.append({"chunk_id": p["chunk_id"], "quote": quote, "kind": kind, "clause_ids": p.get("clause_ids", [])})
    # one quote per clause is enough; prefer intact clause units
    strength = {"mandatory_normally": 0, "exemption": 1, "discretionary": 2}
    def ocr_noise(q: str) -> int:  # prefer the clean reprint over the noisy scanned copy
        return sum(q.count(ch) for ch in "_|~") + q.count("--") + len(re.findall(r"\b[A-Z]\b(?! /)", q))

    out.sort(key=lambda m: (strength[m["kind"]], 0 if m["chunk_id"] in m["clause_ids"] else 1, ocr_noise(m["quote"])))
    uniq, clauses, seen = [], set(), set()
    for m in out:
        tag = (m["clause_ids"] or [m["chunk_id"]])[0]
        # near-duplicate copies (scanned copy vs clean reprint) share the same key
        key = "".join(w for w in re.findall(r"[a-z]+", m["quote"].lower()) if len(w) > 2)[:120]
        if tag in clauses or key in seen or (uniq and ocr_noise(m["quote"]) >= 3):
            continue
        clauses.add(tag)
        seen.add(key)
        uniq.append(m)
    return uniq[:3]


def parse_inr(quote: str) -> int | None:
    """Rupee amount in a quote: "Rs.24,000" -> 24000, "Rs.5 Lakh" -> 500000."""
    m = re.search(r"Rs\.?\s*([\d,]+(?:\.\d+)?)\s*(Lakh|Lakhs|Crore)?", quote, re.I)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    if unit.startswith("lakh"):
        value *= 100_000
    elif unit == "crore":
        value *= 10_000_000
    return int(round(value))


def parse_emd_amount(text: str) -> int | None:
    """Annexure-VIII table: "... Estimated Value (INR in Lakh) EMD (INR) ... 11 nos. 8.8 17600"."""
    m = re.search(r"EMD \(INR\).*?(\d+(?:\.\d+)?)\s+(\d{3,7})\s*$", text, re.S)
    return int(m.group(2)) if m else None


def parse_years(quote: str) -> int | None:
    m = re.search(r"minimum period of last (\w+) financial years", quote, re.I)
    if not m:
        return None
    w = m.group(1).lower()
    return int(w) if w.isdigit() else _WORDNUM.get(w)


def parse_states(quote: str) -> list[str]:
    m = re.search(r"registered office in ([A-Za-z /]+?)(?:\.| Certificate)", quote)
    if not m:
        return []
    parts = re.split(r"/| or ", m.group(1))
    return [p.strip() for p in parts if p.strip()]


def inr(v: float) -> str:
    """Indian digit grouping, e.g. 2400000 -> 24,00,000."""
    n = int(round(v))
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if n < 0 else "") + s


# ===================================================================== evaluation
def _ev(passage: dict, quote: str, role: str) -> dict:
    return {"chunk_id": passage["chunk_id"], "quote": quote, "role": role}


def needs_modifier_search(cond: Condition, profile: dict, raw_status: str | None, bid: dict | None = None) -> bool:
    """Step 2 of the agent: could another provision modify this one in a way
    that matters for this bid? Only if the tender alone says "not met", or
    the bidder claims an exemption from it."""
    if not cond.relaxable:
        return False
    if cond.id == "C3_EMD":
        return bool(bid) and bid["emd"]["status"] == "exemption_claimed"
    return raw_status == "UNMET"


def _relaxed_or_unmet(cond, finding, desc, profile, passages) -> dict:
    """Shared tail for conditions a central rule may relax for some bidders."""
    cat = "startup" if profile["declared_category"] == "startup" else profile["category"]
    mods = modifier_sentences(cond, cat, passages) if cat in CATEGORY_PATTERNS else []
    if mods:
        for m in mods:
            finding["evidence"].append({"chunk_id": m["chunk_id"], "quote": m["quote"], "role": "policy_relaxation"})
        who = "startups" if cat == "startup" else "Micro & Small Enterprises (MSEs)"
        finding.update(
            status="CONFLICT",
            basis=(f"{desc}: the tender condition is not met on its own terms, but central procurement policy "
                   f"relaxes this criterion for {who} subject to meeting quality and technical specifications. "
                   "The tender does not mention the relaxation. The two authorities point in different directions, "
                   "so the committee must decide."),
        )
        finding["values"]["relaxation_kinds"] = sorted({m["kind"] for m in mods})
        return finding
    if not profile["category_clear"]:
        finding.update(status="ESCALATE",
                       basis=desc + ": not met, and the bidder category is unclear, so it is not known whether any "
                                    "relaxation applies.")
        return finding
    finding.update(status="UNMET", basis=desc + ": condition not met and no applicable relaxation was found.")
    return finding


def evaluate_condition(cond: Condition, bid: dict, profile: dict, passages: list[dict]) -> dict:
    """Evaluate one condition against the retrieved passages."""
    finding = {"condition_id": cond.id, "title": cond.title, "status": None, "basis": "", "values": {},
               "evidence": [], "tender_clause": cond.tender_clause}
    if cond.id == "C4_DOCUMENTS":
        return _evaluate_documents(cond, bid, profile, passages, finding)
    if cond.id == "C3_EMD":
        return _evaluate_emd(cond, profile, passages, finding)
    if cond.id == "C2_EXPERIENCE":
        return _evaluate_experience(cond, profile, passages, finding)

    ent = profile["entities"]
    gov = governing_passage(cond, passages)
    quote = quote_for(gov["text"], cond.anchors[0]) if gov else None
    if not gov or not quote:
        finding.update(status="EVIDENCE_MISSING",
                       basis=f"The governing tender clause for '{cond.title}' was not found in the retrieved passages.")
        return finding
    finding["evidence"].append(_ev(gov, quote, "tender_condition"))

    if cond.id == "C1_TURNOVER":
        threshold = parse_inr(quote)
        low = ent["minimum_year_turnover_inr"]
        by_year = ent["turnover_by_year"]
        finding["values"] = {"threshold_inr_each_year": threshold, "turnover_by_year": by_year}
        if threshold is None:
            finding.update(status="EVIDENCE_MISSING", basis="Turnover threshold could not be read from the clause.")
            return finding
        below = [fy for fy, v in by_year.items() if v < threshold]
        desc = (f"Turnover of Rs.{inr(low)} in its weakest year against Rs.{inr(threshold)} required in each of "
                f"{', '.join(by_year)}" + (f" (below in {', '.join(below)})" if below else ""))
        if not below:
            finding.update(status="MET", basis=desc + ": condition met.")
            return finding
        return _relaxed_or_unmet(cond, finding, desc, profile, passages)

    if cond.id == "C5_REGIONAL_PRESENCE":
        states = parse_states(quote)
        office, centres = ent["registered_office_state"], ent["service_centre_states"]
        finding["values"] = {"allowed_states": states, "registered_office_state": office,
                             "service_centre_states": centres}
        if not states:
            finding.update(status="EVIDENCE_MISSING", basis="The list of states could not be read from the clause.")
            return finding
        allowed = {s.lower() for s in states}
        office_ok = (office or "").lower() in allowed
        centre_ok = any(c.lower() in allowed for c in centres)
        desc = (f"Registered office in {office or 'not stated'}; service centre(s) in "
                f"{', '.join(centres) or 'none stated'}; required in {', '.join(states)}")
        if office_ok and centre_ok:
            finding.update(status="MET", basis=desc + ": condition met.")
        else:
            missing = ([] if office_ok else ["registered office"]) + ([] if centre_ok else ["service centre"])
            finding.update(status="UNMET", basis=desc + f": no {' and no '.join(missing)} in the specified states.")
        return finding
    raise ValueError(cond.id)  # pragma: no cover


def _evaluate_experience(cond, profile, passages, finding) -> dict:
    ent = profile["entities"]
    gov_years = governing_passage(cond, passages, anchor=cond.anchors[0])
    gov_cfti = governing_passage(cond, passages, anchor=cond.anchors[1])
    q_years = quote_for(gov_years["text"], cond.anchors[0]) if gov_years else None
    q_cfti = quote_for(gov_cfti["text"], cond.anchors[1]) if gov_cfti else None
    if not q_years or not q_cfti:
        finding.update(status="EVIDENCE_MISSING",
                       basis="The tender's experience requirements were not found in the retrieved passages.")
        return finding
    finding["evidence"] += [_ev(gov_years, q_years, "tender_condition"), _ev(gov_cfti, q_cfti, "tender_condition")]
    required = parse_years(q_years)
    if required is None:
        finding.update(status="EVIDENCE_MISSING", basis="Required years of business could not be read.")
        return finding
    years, orders = ent["years_in_desktop_business"], ent["cfti_supply_orders"]
    finding["values"] = {"required_years": required, "years_in_desktop_business": years,
                         "cfti_supply_orders": orders}
    gaps = ([f"{years} year(s) in the desktop business against {required} required"] if years < required else []) + \
           (["no supply of desktop computers to a CFTI declared"] if orders < 1 else [])
    desc = (f"{years} year(s) in the desktop computer business and {orders} supply order(s) to CFTIs declared")
    if not gaps:
        finding.update(status="MET", basis=desc + f" against {required} years and at least one CFTI supply: condition met.")
        return finding
    return _relaxed_or_unmet(cond, finding, desc + " (" + "; ".join(gaps) + ")", profile, passages)


def _evaluate_emd(cond, profile, passages, finding) -> dict:
    ent = profile["entities"]
    gov = governing_passage(cond, passages, anchor=cond.anchors[0])
    table = governing_passage(cond, passages, anchor=cond.anchors[1])
    q_rule = quote_for(gov["text"], cond.anchors[0]) if gov else None
    if not gov or not q_rule or not table:
        finding.update(status="EVIDENCE_MISSING",
                       basis="The EMD clause or the EMD amount (Annexure-VIII) was not found in the retrieved passages.")
        return finding
    # Annexure-VIII is a one-row table, not prose: quote the whole (short) table so the amount stays in the quote
    m = re.search(r"Annexure-VIII LIST OF EQUIPMENT.*?EMD \(INR\).*?\d+(?:\.\d+)?\s+\d{3,7}", table["text"], re.S)
    q_amount = m.group(0) if m else (quote_for(table["text"], r"EMD \(INR\)") or table["text"])
    required = parse_emd_amount(q_amount)
    finding["evidence"] += [_ev(gov, q_rule, "tender_condition"), _ev(table, q_amount, "tender_condition")]
    finding["values"] = {"required_emd_inr": required, "emd_status": ent["emd_status"],
                         "emd_amount_inr": ent["emd_amount_inr"]}
    if required is None:
        finding.update(status="EVIDENCE_MISSING", basis="EMD amount could not be read from Annexure-VIII.")
        return finding
    status = ent["emd_status"]
    if status == "paid":
        if ent["emd_amount_inr"] >= required:
            finding.update(status="MET", basis=f"EMD of Rs.{inr(ent['emd_amount_inr'])} paid against "
                                               f"Rs.{inr(required)} required: condition met.")
        else:
            finding.update(status="UNMET", basis=f"EMD of Rs.{inr(ent['emd_amount_inr'])} paid is less than the "
                                                 f"Rs.{inr(required)} required.")
        return finding
    rej = quote_for(gov["text"], r"Bids not accompanied by the EMD")
    if rej:
        finding["evidence"].append(_ev(gov, rej, "tender_condition"))
    if status == "not_paid":
        finding.update(status="UNMET", basis=f"No EMD paid and no exemption claimed; the tender requires "
                                             f"Rs.{inr(required)} and rejects bids without EMD.")
        return finding
    # exemption claimed - the tender itself grants no exemption
    is_mse = profile["category"] == "mse" or profile.get("also_mse")
    if is_mse:
        if "msme_certificate" in ent["documents_missing"]:
            finding.update(status="INSUFFICIENT",
                           basis="EMD exemption claimed as an MSE, but the MSE/NSIC registration certificate is not "
                                 "enclosed; the bidder should furnish it.")
            return finding
        mods = modifier_sentences(cond, "mse", passages)
        if mods:
            for m in mods:
                finding["evidence"].append({"chunk_id": m["chunk_id"], "quote": m["quote"], "role": "policy_exemption"})
            finding.update(status="CONFLICT",
                           basis=f"EMD exemption claimed by a registered MSE. The tender requires Rs.{inr(required)} "
                                 "from every bidder and rejects bids without EMD, but the General Financial Rules "
                                 "exempt Micro and Small Enterprises from bid security. The tender does not mention "
                                 "the exemption, so the committee must decide.")
            finding["values"]["relaxation_kinds"] = sorted({m["kind"] for m in mods})
            return finding
        finding.update(status="UNMET", basis="EMD exemption claimed as an MSE, but neither the tender nor the "
                                             "retrieved rules provide an exemption.")
        return finding
    if profile["declared_category"] == "startup":
        finding.update(status="INSUFFICIENT",
                       basis="EMD exemption claimed as a startup. The tender grants no exemption and no startup EMD "
                             "exemption was found in the retrieved rules; the bidder should state the basis of "
                             f"exemption or furnish the Rs.{inr(required)} EMD.")
        return finding
    finding.update(status="UNMET", basis="EMD exemption claimed, but the bidder is not an MSE and the tender "
                                         f"requires Rs.{inr(required)} from every bidder.")
    return finding


def _evaluate_documents(cond, bid, profile, passages, finding) -> dict:
    docs = bid.get("documents", {})
    missing, missing_anchor = [], []
    used: dict[str, dict] = {}
    for key, label, anchor in cond.doc_requirements:
        gov = governing_passage(cond, passages, anchor=anchor)
        if not gov:
            missing_anchor.append(label)
            continue
        q = quote_for(gov["text"], anchor)
        if q and q not in used:
            used[q] = _ev(gov, q, "tender_condition")
        if not docs.get(key):
            missing.append(label)
    # an authorised representative (not the OEM) must prove the OEM authorisation (Annexure-II 1)
    if not profile["entities"]["is_oem"]:
        gov = governing_passage(cond, passages, anchor=r"authorized representative \(provide documentary proof\)")
        if gov:
            q = quote_for(gov["text"], r"authorized representative")
            if q and q not in used:
                used[q] = _ev(gov, q, "tender_condition")
            if not docs.get("oem_authorization"):
                missing.append("OEM authorisation (bidder is not the OEM)")
        else:
            missing_anchor.append("OEM authorisation")
    finding["evidence"] = list(used.values())
    finding["values"] = {"missing_documents": missing}
    if missing_anchor and not finding["evidence"]:
        finding.update(status="EVIDENCE_MISSING", basis="Document requirements were not found in the retrieved passages.")
    elif missing:
        finding.update(status="INSUFFICIENT", basis="Missing: " + "; ".join(missing) + ". These are curable by clarification.")
    else:
        finding.update(status="MET", basis="All mandatory documents are declared as enclosed.")
    if missing_anchor and finding["evidence"]:
        finding["values"]["requirements_not_located"] = missing_anchor
    return finding


def evaluate_category(profile: dict, passages: list[dict]) -> dict:
    f = {"condition_id": CATEGORY_CONDITION, "title": "Bidder category", "status": "MET", "values": {
        "declared": profile["declared_category"], "classified": profile["category"]}, "evidence": [], "basis": "",
        "tender_clause": None}
    if profile["category_clear"]:
        f["basis"] = f"Bidder classified as {profile['category'].replace('_', ' ')} from the request fields."
        return f
    f["status"] = "ESCALATE"
    f["basis"] = "Bidder category cannot be settled from the request and corpus: " + "; ".join(profile["reasons"]) + "."
    want = {"consortium": ("TENDER-1.7", r"member of consortium"),
            "foreign": ("ANNEX-II-2", r"company registered under the Companies Act")}.get(profile["declared_category"])
    if want:
        p = next((p for p in passages if want[0] in (p.get("clause_ids") or [])), None)
        if p:
            q = quote_for(p["text"], want[1])
            if q:
                f["evidence"].append(_ev(p, q, "tender_condition"))
    return f


def evaluate_all(bid: dict, profile: dict, passages: list[dict]) -> list[dict]:
    findings = [evaluate_category(profile, passages)]
    findings += [evaluate_condition(c, bid, profile, passages) for c in CONDITIONS]
    return findings


# ===================================================================== routing (F6 / F7)
def decide_route(findings: list[dict], profile: dict, incomplete: bool = False, degraded: bool = False) -> dict:
    statuses = {f["condition_id"]: f["status"] for f in findings}
    reasons = []
    if incomplete:
        reasons.append("the run did not complete within its step/tool/time limits")
    if degraded:
        reasons.append("the language model was unavailable; findings were produced by the rule-based fallback only")
    for f in findings:
        if f["status"] in ("CONFLICT", "ESCALATE", "EVIDENCE_MISSING"):
            reasons.append(f"{f['condition_id']}: {f['status'].lower().replace('_', ' ')}")
    if reasons:
        route = "escalate_to_human"
    elif any(s == "UNMET" for s in statuses.values()):
        route = "reject_with_cited_condition"
    elif any(s == "INSUFFICIENT" for s in statuses.values()):
        route = "seek_clarification"
    else:
        route = "pass_to_technical_evaluation"
    basis_ids = {
        "escalate_to_human": [k for k, v in statuses.items() if v in ("CONFLICT", "ESCALATE", "EVIDENCE_MISSING")],
        "reject_with_cited_condition": [k for k, v in statuses.items() if v == "UNMET"],
        "seek_clarification": [k for k, v in statuses.items() if v == "INSUFFICIENT"],
        "pass_to_technical_evaluation": [k for k, v in statuses.items() if v == "MET"],
    }[route]
    return {"route": route, "route_label": ROUTE_LABELS[route], "basis_conditions": basis_ids, "escalation_reasons": reasons}
