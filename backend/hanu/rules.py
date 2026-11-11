"""Hanu's written rules for a submitted bill.

The rules return "approve" or "needs_review" plus the reasons. They never return a
denial: only a person can deny a bill (see service.decide).

Change a rule here, bump RULES_VERSION, and every case records which version judged it.
"""
from dataclasses import dataclass
from datetime import date
from typing import Optional

RULES_VERSION = "2026-11-11.1"

COVERED_CATEGORIES = {"consultation", "lab_test"}
MAX_BILL_AGE_DAYS = 60
TOTAL_TOLERANCE_KOBO = 100  # line items and stated total may differ by up to 1 naira


@dataclass
class Context:
    extraction: Optional[dict]
    extraction_error: Optional[str]
    member_found: bool
    member_hanu_key: str  # normalised Hanu ID of the member who sent the bill
    extracted_hanu_key: str  # normalised Hanu ID read off the bill, "" if none
    clinic_is_partner: Optional[bool]  # None when the clinic name matched no clinic
    total_kobo: Optional[int]
    line_items_kobo: int
    balance_kobo: int
    duplicate_of: Optional[int]
    today: date
    min_confidence: float
    auto_approve_cap_kobo: int


def naira(kobo: int) -> str:
    return f"₦{kobo / 100:,.2f}".removesuffix(".00")


def evaluate(ctx: Context) -> dict:
    flags: list[dict] = []

    def flag(code: str, message: str) -> None:
        flags.append({"code": code, "message": message})

    if not ctx.member_found:
        flag("member_not_found", "No member has the Hanu ID this bill was sent with.")

    ext = ctx.extraction
    if ext is None:
        flag("unreadable", f"The bill reader could not process this image ({ctx.extraction_error}).")
    elif not ext["is_clinic_bill"]:
        flag("not_a_bill", "The image does not look like a clinic bill.")
    else:
        if ext["contains_instructions"]:
            flag("instructions_on_bill", "The bill contains text addressed to the system or reviewer.")

        low = [k for k, v in ext["confidence"].items() if not ctx.min_confidence <= v <= 1]
        if low:
            flag("low_confidence", "Hard to read: " + ", ".join(sorted(low)).replace("_", " ") + ".")

        if ctx.clinic_is_partner is None:
            flag("clinic_not_matched", f"Clinic \"{ext['clinic_name']}\" is not in the clinic list.")
        elif not ctx.clinic_is_partner:
            flag("clinic_not_partner", f"\"{ext['clinic_name']}\" is not a partner clinic.")

        if ctx.extracted_hanu_key and ctx.extracted_hanu_key != ctx.member_hanu_key:
            flag("hanu_id_mismatch", "The Hanu ID on the bill is not the sender's.")

        if not ext["line_items"]:
            flag("no_line_items", "No line items could be read.")
        uncovered = sorted({i["category"] for i in ext["line_items"]} - COVERED_CATEGORIES)
        if uncovered:
            flag("uncovered_service", "Includes services Hanu does not pay for: "
                 + ", ".join(uncovered).replace("_", " ") + ".")

        if ctx.total_kobo is None or ctx.total_kobo <= 0:
            flag("no_total", "The bill states no readable total.")
        else:
            if abs(ctx.total_kobo - ctx.line_items_kobo) > TOTAL_TOLERANCE_KOBO:
                flag("total_mismatch", f"Line items add up to {naira(ctx.line_items_kobo)} "
                     f"but the bill says {naira(ctx.total_kobo)}.")
            if ctx.member_found and ctx.total_kobo > ctx.balance_kobo:
                flag("shortfall", f"Balance is {naira(ctx.balance_kobo)}, "
                     f"{naira(ctx.total_kobo - ctx.balance_kobo)} short of the bill.")
            if ctx.total_kobo > ctx.auto_approve_cap_kobo:
                flag("over_auto_cap", f"Above the {naira(ctx.auto_approve_cap_kobo)} "
                     "limit for approval without review.")

        age = _age_days(ext["bill_date"], ctx.today)
        if age is None:
            flag("no_date", "The bill has no readable date.")
        elif age < 0:
            flag("future_date", "The bill is dated in the future.")
        elif age > MAX_BILL_AGE_DAYS:
            flag("old_bill", f"The bill is {age} days old.")

    if ctx.duplicate_of is not None:
        flag("possible_duplicate", f"Same clinic, date and total as case {ctx.duplicate_of}.")

    return {
        "version": RULES_VERSION,
        "recommendation": "needs_review" if flags else "approve",
        "flags": flags,
    }


def _age_days(bill_date: Optional[str], today: date) -> Optional[int]:
    try:
        return (today - date.fromisoformat(bill_date)).days
    except (TypeError, ValueError):
        return None
