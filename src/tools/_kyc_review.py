"""
Field comparison, outcome and flag derivation for KYC intake sessions.

The KYC application computed these at request time and returned them from its
API; they are NOT stored in the database (kyc_intake.kyc_session has no
outcome/flags column, and there is no comparison table). Reading the database
directly therefore means re-deriving them here, from the submitted form data
(kyc_intake.kyc_form_data) and the OCR extraction (kyc_intake.kyc_ocr_result).

These rules mirror the application's observed output, but they are a
re-implementation rather than the application's own code — if the application
changes its logic, this will need the same change. Anything it cannot
determine is reported honestly ("Not read", "OCR failed") rather than guessed.

Which identifiers appear on which card drives most of the matrix:
  PAN card     — name, date of birth, PAN number (no address, no Aadhaar)
  Aadhaar card — name, date of birth, Aadhaar number, address (no PAN)
"""

import re
from typing import Any, Dict, List, Optional

# Per-field verdicts, mirroring the application's vocabulary.
MATCH = "Match"
MISMATCH = "Mismatch"
NOT_READ = "Not read"
NOT_ON_CARD = "Not on card"
OCR_FAILED = "OCR failed"
NO_DOCUMENT = "No document"
NA = "n/a"

# Session outcomes, mirroring the application's vocabulary.
OUTCOME_VERIFIED = "Verified"
OUTCOME_MISMATCH = "Mismatch"
OUTCOME_NEEDS_REVIEW = "Needs Review"
OUTCOME_OCR_FAILED = "OCR Failed"
OUTCOME_NO_DOCUMENTS = "No Documents"

# Fraction of the form's address tokens that must appear in the OCR address for
# a Match. OCR addresses routinely carry extra lines the form omits (a street
# the applicant left out, a differently-punctuated PIN), so this is deliberately
# a containment check rather than equality.
_ADDRESS_MATCH_THRESHOLD = 0.7


def _norm_text(value: Optional[str]) -> str:
    """Uppercase, strip punctuation, collapse whitespace — for name comparison."""
    if not value:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 ]", " ", value)).strip().upper()


def _tokens(value: Optional[str]) -> set:
    """Lowercased alphanumeric tokens — for address comparison."""
    if not value:
        return set()
    return {t for t in re.split(r"[^A-Za-z0-9]+", value.lower()) if t}


def _last4(value: Optional[str]) -> str:
    """Trailing 4 digits of a masked Aadhaar ('XXXX XXXX 9012' -> '9012')."""
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    return digits[-4:] if len(digits) >= 4 else ""


def _compare_name(form: Dict[str, Any], ocr: Dict[str, Any]) -> str:
    form_value, ocr_value = _norm_text(form.get("full_name")), _norm_text(ocr.get("extracted_name"))
    if not ocr_value:
        return NOT_READ
    return MATCH if form_value == ocr_value else MISMATCH


def _compare_dob(form: Dict[str, Any], ocr: Dict[str, Any]) -> str:
    form_value = str(form.get("date_of_birth") or "")
    ocr_value = str(ocr.get("extracted_dob") or "")
    if ocr_value:
        return MATCH if form_value == ocr_value else MISMATCH
    # Some cards print only a year of birth; fall back to comparing that.
    ocr_year = ocr.get("extracted_yob")
    if ocr_year and form_value:
        return MATCH if form_value[:4] == str(ocr_year) else MISMATCH
    return NOT_READ


def _compare_pan(form: Dict[str, Any], ocr: Dict[str, Any]) -> str:
    form_value = (form.get("pan") or "").strip().upper()
    ocr_value = (ocr.get("extracted_pan") or "").strip().upper()
    if not ocr_value:
        return NOT_READ
    return MATCH if form_value == ocr_value else MISMATCH


def _compare_aadhaar(form: Dict[str, Any], ocr: Dict[str, Any]) -> str:
    """Compare on the masked last 4 only — the full number is never stored."""
    form_value, ocr_value = _last4(form.get("aadhaar_masked")), _last4(ocr.get("extracted_aadhaar_masked"))
    if not ocr_value:
        return NOT_READ
    return MATCH if form_value == ocr_value else MISMATCH


def _compare_address(form: Dict[str, Any], ocr: Dict[str, Any]) -> str:
    ocr_tokens = _tokens(ocr.get("extracted_address"))
    if not ocr_tokens:
        return NOT_READ
    form_address = " ".join(
        str(form.get(part) or "")
        for part in ("address_line1", "address_line2", "city", "state", "pincode")
    )
    form_tokens = _tokens(form_address)
    if not form_tokens:
        return NOT_READ
    overlap = len(form_tokens & ocr_tokens) / len(form_tokens)
    return MATCH if overlap >= _ADDRESS_MATCH_THRESHOLD else MISMATCH


# field label -> (comparator, verdict when the field isn't on the PAN card,
#                 verdict when the field isn't on the Aadhaar card)
_FIELDS = [
    ("Name", _compare_name, None, None),
    ("Date of birth", _compare_dob, None, None),
    ("PAN number", _compare_pan, None, NA),
    ("Aadhaar number", _compare_aadhaar, NA, None),
    ("Address", _compare_address, NOT_ON_CARD, None),
]


def _doc_verdict(comparator, form: Dict[str, Any], ocr: Optional[Dict[str, Any]]) -> str:
    """Apply one comparator, short-circuiting on a missing or failed document."""
    if ocr is None:
        return NO_DOCUMENT
    if (ocr.get("status") or "").lower() != "success":
        return OCR_FAILED
    return comparator(form, ocr)


def build_comparison(
    form: Dict[str, Any],
    pan_ocr: Optional[Dict[str, Any]],
    aadhaar_ocr: Optional[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """Field-by-field comparison of the submitted form against each document."""
    comparison = []
    for label, comparator, pan_override, aadhaar_override in _FIELDS:
        comparison.append(
            {
                "field": label,
                "pan": pan_override or _doc_verdict(comparator, form, pan_ocr),
                "aadhaar": aadhaar_override or _doc_verdict(comparator, form, aadhaar_ocr),
            }
        )
    return comparison


def derive_outcome(comparison: List[Dict[str, str]], has_any_document: bool) -> Dict[str, Any]:
    """Reduce the comparison matrix to a session outcome plus the fields that drove it."""
    if not has_any_document:
        return {"outcome": OUTCOME_NO_DOCUMENTS, "flags": []}

    verdicts = [v for row in comparison for v in (row["pan"], row["aadhaar"])]
    real_verdicts = [v for v in verdicts if v not in (NA, NOT_ON_CARD)]

    if real_verdicts and all(v in (OCR_FAILED, NO_DOCUMENT) for v in real_verdicts):
        return {"outcome": OUTCOME_OCR_FAILED, "flags": []}

    mismatched = [row["field"] for row in comparison if MISMATCH in (row["pan"], row["aadhaar"])]
    if mismatched:
        return {"outcome": OUTCOME_MISMATCH, "flags": mismatched}

    unresolved = [
        row["field"]
        for row in comparison
        if any(v in (NOT_READ, OCR_FAILED, NO_DOCUMENT) for v in (row["pan"], row["aadhaar"]))
    ]
    if unresolved:
        return {"outcome": OUTCOME_NEEDS_REVIEW, "flags": unresolved}

    return {"outcome": OUTCOME_VERIFIED, "flags": []}


def review_session(
    form: Dict[str, Any],
    ocr_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the comparison matrix and outcome for one session's latest OCR attempt."""
    by_type = {(row.get("doc_type") or "").upper(): row for row in ocr_rows}
    pan_ocr, aadhaar_ocr = by_type.get("PAN"), by_type.get("AADHAAR")
    comparison = build_comparison(form or {}, pan_ocr, aadhaar_ocr)
    verdict = derive_outcome(comparison, has_any_document=bool(ocr_rows))
    return {
        "comparison": comparison,
        "outcome": verdict["outcome"],
        "flags": verdict["flags"],
        "derived_by": (
            "Computed by the agent from form data and OCR extraction; the KYC "
            "application does not store these fields."
        ),
    }
