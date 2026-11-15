"""Guardrails (requirement L4).

1. Prompt-injection detection on every free-text field that can reach a
   model (legal name, remarks, consortium member names, references). Strong
   matches are rejected at the ingestion boundary and quarantined; text that
   does reach a prompt is additionally fenced as untrusted data.
2. Output filtering: the system is not authorised to *decide*. Any wording
   that issues a determination ("the bid is rejected", "we award ...") is
   removed from generated text, and the route must be one of four values.
3. Toxicity detection on inputs and outputs (lexicon based - small, fast,
   no model download; its limited coverage is documented).
"""
from __future__ import annotations

import re

INJECTION_PATTERNS = [
    (r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|any|system|your)\b[^.\n]{0,30}"
     r"\b(instructions?|rules?|prompts?|guidelines?|directions?|policy|policies)\b", "instruction_override"),
    (r"\byou\s+are\s+now\b|\bact\s+as\s+(an?\s+)?(admin|developer|system|unrestricted)", "role_hijack"),
    (r"\b(system|developer)\s+(prompt|message|mode)\b", "system_prompt_reference"),
    (r"\b(reveal|print|show|repeat|leak)\b[^.\n]{0,30}\b(prompt|instructions|system message|api key|secret)", "exfiltration"),
    (r"\b(approve|qualify|accept|pass|select|award)\b[^.\n]{0,40}\b(this|our|my)\s+(bid|bidder|offer|tender)\b"
     r"[^.\n]{0,60}\b(regardless|without|ignore|irrespective|automatically|must)\b", "verdict_manipulation"),
    (r"\b(mark|set|return|output)\b[^.\n]{0,30}\b(route|status|recommendation|verdict)\b[^.\n]{0,30}"
     r"\b(pass|met|qualified|approved|pass_to_technical_evaluation)\b", "verdict_manipulation"),
    (r"<\s*/?\s*(system|assistant|user|tool|im_start|im_end)\b", "chat_markup"),
    (r"\b(call|invoke|use)\s+the\s+(tool|function)\b|\"tool\"\s*:", "tool_invocation"),
    (r"\bjailbreak\b|\bDAN\b|do anything now", "jailbreak"),
    (r"###\s*(instruction|system)|BEGIN\s+(SYSTEM|INSTRUCTIONS)", "delimiter_attack"),
]
_INJ = [(re.compile(p, re.I), name) for p, name in INJECTION_PATTERNS]

TOXIC_TERMS = [
    "idiot", "stupid", "moron", "imbecile", "bastard", "bitch", "fuck", "shit", "asshole", "dumbass",
    "scum", "retard", "kill you", "i will kill", "go to hell", "damn you",
]
_TOX = re.compile(r"\b(" + "|".join(re.escape(t) for t in TOXIC_TERMS) + r")\w*", re.I)

FORBIDDEN_DETERMINATIONS = [
    r"\b(the\s+)?(bid|bidder|offer|firm|tenderer)\s+(is|has\s+been|stands|shall\s+be|will\s+be)\s+(hereby\s+)?"
    r"(rejected|disqualified|qualified|accepted|awarded|selected|approved)\b",
    r"\bwe\s+(hereby\s+)?(reject|disqualify|award|qualify|approve|select)\b",
    r"\b(contract|purchase\s+order|work\s+order)\s+(is|will\s+be|shall\s+be|has\s+been)\s+(hereby\s+)?awarded\b",
    r"\b(final\s+decision|it\s+is\s+decided)\b",
]
_FORBID = [re.compile(p, re.I) for p in FORBIDDEN_DETERMINATIONS]
REDACTION = "[determination removed - the Tender Committee decides]"

FREE_TEXT_FIELDS = (
    ("bidder", "legal_name"),
    ("bidder", "nsic_registration"),
    ("emd", "transaction_ref"),
    ("remarks",),
)


def detect_injection(text: str | None) -> list[str]:
    if not text:
        return []
    return sorted({name for rx, name in _INJ if rx.search(text)})


def detect_toxicity(text: str | None) -> list[str]:
    if not text:
        return []
    return sorted({m.group(1).lower() for m in _TOX.finditer(text)})


def _get(d: dict, path: tuple[str, ...]):
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def scan_bid(bid: dict) -> list[dict]:
    """Return field-level guardrail violations for every free-text field."""
    problems = []
    fields = [(path, _get(bid, path)) for path in FREE_TEXT_FIELDS]
    for i, member in enumerate(_get(bid, ("bidder", "consortium_members")) or []):
        fields.append((("bidder", "consortium_members", str(i)), member))
    for path, value in fields:
        if not isinstance(value, str):
            continue
        for kind in detect_injection(value):
            problems.append({"field": ".".join(path), "error": "prompt_injection_detected", "pattern": kind})
        for term in detect_toxicity(value):
            problems.append({"field": ".".join(path), "error": "toxic_content_detected", "term": term})
    return problems


def fence_untrusted(text: str | None, limit: int = 600) -> str:
    """Wrap untrusted bidder text so the model treats it as data."""
    if not text:
        return ""
    clean = re.sub(r"[\x00-\x08\x0b-\x1f<>`]", " ", text)[:limit]
    return f"<untrusted_bidder_text>{clean}</untrusted_bidder_text>"


def filter_output(text: str) -> tuple[str, list[str]]:
    """Remove unauthorised determinations and toxic terms from generated text."""
    flags = []
    out = text
    for rx in _FORBID:
        if rx.search(out):
            flags.append("unauthorised_determination")
            out = rx.sub(REDACTION, out)
    if detect_toxicity(out):
        flags.append("toxic_output")
        out = _TOX.sub("[removed]", out)
    return out, sorted(set(flags))
