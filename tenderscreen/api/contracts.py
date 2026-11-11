"""Data contract for the bid-summary interface (requirements F1, F2, D1).

Validation happens in four layers at the ingestion boundary, *before* a run
is created or any agent starts:

  1. schema      types, required fields, no unknown fields (pydantic)
  2. range       numeric bounds, string lengths, identifier patterns
  3. categorical enumerations (category, EMD status, supplier class, tender)
  4. cross-field rules that involve several fields (see CROSS_FIELD_RULES)

A request failing any layer is quarantined with its field-level errors.
"""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..agents.conditions import FINANCIAL_YEARS, TENDER_CLOSING_DATE

SCHEMA_VERSION = "1.0"
SUPPORTED_TENDERS = ("TENDER/2024-25/370",)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Bidder(_Strict):
    legal_name: str = Field(min_length=2, max_length=200, description="Registered name of the bidder (free text)")
    declared_category: Literal["large_enterprise", "mse", "startup", "consortium", "foreign", "not_stated"]
    country_of_incorporation: str = Field(pattern=r"^[A-Z]{2}$", description="ISO 3166-1 alpha-2")
    date_of_incorporation: date
    manufactures_own_goods: bool = Field(description="False if the bidder is trading goods made by others")
    udyam_registration: Optional[str] = Field(None, pattern=r"^UDYAM-[A-Z]{2}-\d{2}-\d{7}$")
    nsic_registration: Optional[str] = Field(None, pattern=r"^[A-Za-z0-9/-]{4,40}$")
    dpiit_recognition: Optional[str] = Field(None, pattern=r"^DIPP\d{3,8}$")
    consortium_members: list[str] = Field(default_factory=list, max_length=10)


class Financials(_Strict):
    annual_turnover_inr: dict[str, float] = Field(
        description="Audited turnover in INR for each of the five financial years 2019-20 .. 2023-24")


class Experience(_Strict):
    similar_orders_count: int = Field(ge=0, le=10000, description="Desktop-computer orders from CFTIs/Govt, last 5 FYs")


class Emd(_Strict):
    status: Literal["paid", "exemption_claimed", "not_paid"]
    amount_inr: float = Field(ge=0, le=1_000_000_000)
    transaction_ref: Optional[str] = Field(None, pattern=r"^[A-Za-z0-9-]{4,40}$")


class Documents(_Strict):
    audited_financial_statements: bool = False
    ca_certificate: bool = False
    purchase_orders: bool = False
    user_certificates: bool = False
    digitally_signed_tender: bool = False
    blacklisting_declaration: bool = False
    mii_self_declaration: bool = False
    msme_certificate: bool = False
    startup_recognition_certificate: bool = False


class BidSummary(_Strict):
    schema_version: Literal["1.0"]
    bid_id: str = Field(pattern=r"^[A-Za-z0-9._-]{3,40}$")
    tender_id: Literal["TENDER/2024-25/370"]
    bidder: Bidder
    financials: Financials
    experience: Experience
    emd: Emd
    local_content_percent: float = Field(ge=0, le=100)
    supplier_class: Literal["class_I", "class_II", "non_local"]
    documents: Documents
    remarks: Optional[str] = Field(None, max_length=2000, description="Free text from the bidder (untrusted)")


CROSS_FIELD_RULES = [
    "financials.annual_turnover_inr must contain exactly the five years 2019-20 .. 2023-24, each 0 .. 1e13",
    "bidder.date_of_incorporation must be after 1850-01-01 and not after the tender closing date (2025-01-07)",
    "declared_category=startup requires bidder.dpiit_recognition",
    "declared_category=mse requires bidder.udyam_registration or bidder.nsic_registration",
    "declared_category=consortium requires at least two consortium_members; other categories must have none",
    "declared_category=foreign if and only if country_of_incorporation is not IN",
    "emd.status=paid requires emd.amount_inr > 0 and emd.transaction_ref; other statuses require amount_inr = 0",
    "supplier_class class_I requires local_content_percent >= 50; class_II requires 20 <= value < 50; "
    "non_local requires value < 20 (Make in India Order 2017, as revised 16-09-2020)",
]


def cross_field_errors(b: BidSummary) -> list[dict]:
    errs: list[dict] = []

    def err(field: str, msg: str) -> None:
        errs.append({"field": field, "error": "cross_field", "message": msg})

    years = b.financials.annual_turnover_inr
    if sorted(years) != FINANCIAL_YEARS:
        missing = [y for y in FINANCIAL_YEARS if y not in years]
        extra = [y for y in years if y not in FINANCIAL_YEARS]
        err("financials.annual_turnover_inr", f"expected years {FINANCIAL_YEARS}; missing {missing}, unexpected {extra}")
    for y, v in years.items():
        if not (0 <= v <= 1e13):
            errs.append({"field": f"financials.annual_turnover_inr.{y}", "error": "range",
                         "message": "turnover must be between 0 and 1e13 INR"})
    inc = b.bidder.date_of_incorporation
    if inc < date(1850, 1, 1) or inc > TENDER_CLOSING_DATE:
        err("bidder.date_of_incorporation", "must be between 1850-01-01 and the tender closing date 2025-01-07")
    cat = b.bidder.declared_category
    if cat == "startup" and not b.bidder.dpiit_recognition:
        err("bidder.dpiit_recognition", "required when declared_category is startup")
    if cat == "mse" and not (b.bidder.udyam_registration or b.bidder.nsic_registration):
        err("bidder.udyam_registration", "Udyam or NSIC registration required when declared_category is mse")
    if cat == "consortium" and len(b.bidder.consortium_members) < 2:
        err("bidder.consortium_members", "a consortium needs at least two members")
    if cat != "consortium" and b.bidder.consortium_members:
        err("bidder.consortium_members", "only a consortium may list members")
    if (cat == "foreign") != (b.bidder.country_of_incorporation != "IN"):
        err("bidder.country_of_incorporation", "declared_category foreign must match a non-IN country of incorporation")
    if b.emd.status == "paid":
        if b.emd.amount_inr <= 0:
            err("emd.amount_inr", "must be > 0 when emd.status is paid")
        if not b.emd.transaction_ref:
            err("emd.transaction_ref", "required when emd.status is paid")
    elif b.emd.amount_inr != 0:
        err("emd.amount_inr", "must be 0 unless emd.status is paid")
    lc, cls = b.local_content_percent, b.supplier_class
    if (cls == "class_I" and lc < 50) or (cls == "class_II" and not 20 <= lc < 50) or (cls == "non_local" and lc >= 20):
        err("supplier_class", f"{cls} is inconsistent with local_content_percent={lc:g}")
    return errs


def pydantic_errors(exc: ValidationError) -> list[dict]:
    out = []
    for e in exc.errors():
        kind = {"missing": "schema", "extra_forbidden": "schema", "literal_error": "categorical"}.get(e["type"])
        if kind is None:
            kind = "range" if any(t in e["type"] for t in ("greater", "less", "too_long", "too_short", "pattern"))\
                else "type"
        out.append({"field": ".".join(str(x) for x in e["loc"]), "error": kind, "message": e["msg"]})
    return out


def validate_bid(raw) -> tuple[BidSummary | None, list[dict]]:
    if not isinstance(raw, dict):
        return None, [{"field": "$", "error": "schema", "message": "body must be a JSON object"}]
    try:
        bid = BidSummary.model_validate(raw)
    except ValidationError as exc:
        return None, pydantic_errors(exc)
    errs = cross_field_errors(bid)
    return (bid if not errs else None), errs


def json_schema() -> dict:
    schema = BidSummary.model_json_schema()
    schema["$id"] = "https://tenderscreen.local/contracts/bid_summary/1.0"
    schema["title"] = "BidSummary v1.0"
    schema["x-cross-field-rules"] = CROSS_FIELD_RULES
    return schema


class FeedbackIn(_Strict):
    satisfactory: bool
    reason: str = Field(min_length=3, max_length=500)
