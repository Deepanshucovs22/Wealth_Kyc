"""
eKYC review tools — read-only SQL access to the KYC database.
Auto-discovered by the tool registry (src/tools/registry.py).

Two schemas back these tools:
  kyc_intake — new-account OCR intake: sessions, submitted form data, OCR results
  kyc        — the legacy onboarded-client roster: clients, bank accounts, screening

The session outcome, flags and field-by-field comparison are not stored in the
database — the application computed them per request. _kyc_review.py re-derives
them here from the form data and OCR extraction.
"""

import json

from langchain_core.tools import tool

from src.tools._db_client import (
    KycDbError,
    fetch_all,
    fetch_one,
    is_valid_session_id,
)
from src.tools._kyc_review import review_session

# Never select kyc_document.content — it holds the raw uploaded file bytes.
_DOCUMENT_COLUMNS = "document_id, doc_type, seq, file_name, mime_type, size_bytes, sha256, uploaded_at"

# v_latest_ocr already resolves to the most recent attempt per document type.
_OCR_COLUMNS = (
    "ocr_id, session_id, doc_type, attempt, status, engine, extracted_name, "
    "extracted_dob, extracted_yob, extracted_pan, extracted_aadhaar_masked, "
    "extracted_address, field_confidence, raw_text, mean_confidence, pages, "
    "error, duration_ms, processed_at"
)

# aadhaar_hmac is deliberately excluded — it is a keyed hash of the full Aadhaar
# number and has no place in anything the agent reads or repeats.
_FORM_COLUMNS = (
    "session_id, full_name, date_of_birth, pan, aadhaar_masked, address_line1, "
    "address_line2, city, state, pincode, customer_id, mobile, email, "
    "request_type, submitted_at"
)


@tool
async def get_kyc_session(session_id: str) -> str:
    """Fetch full details for one KYC session: submitted form data, PAN/Aadhaar OCR
    extraction, a field-by-field comparison, and the outcome.
    session_id looks like 'KYC-20261008-8B8742'. Use this whenever the user asks
    about, or gives you, a specific session."""
    session_id = session_id.strip()
    if not is_valid_session_id(session_id):
        return (
            f"'{session_id}' doesn't look like a valid session ID. "
            "Session IDs look like KYC-YYYYMMDD-XXXXXX, e.g. KYC-20261008-8B8742."
        )
    try:
        session = await fetch_one(
            "SELECT * FROM kyc_intake.kyc_session WHERE session_id = $1", session_id
        )
        if not session:
            return f"No KYC session found with ID {session_id}."

        form = await fetch_one(
            f"SELECT {_FORM_COLUMNS} FROM kyc_intake.kyc_form_data WHERE session_id = $1",
            session_id,
        )
        ocr_rows = await fetch_all(
            f"SELECT {_OCR_COLUMNS} FROM kyc_intake.v_latest_ocr WHERE session_id = $1",
            session_id,
        )
        documents = await fetch_all(
            f"SELECT {_DOCUMENT_COLUMNS} FROM kyc_intake.kyc_document "
            "WHERE session_id = $1 ORDER BY doc_type, seq",
            session_id,
        )
        events = await fetch_all(
            "SELECT event, actor, detail, at FROM kyc_intake.kyc_session_event "
            "WHERE session_id = $1 ORDER BY at",
            session_id,
        )
    except KycDbError as e:
        return str(e)

    review = review_session(form or {}, ocr_rows)
    session.update({"outcome": review["outcome"], "flags": review["flags"]})

    return json.dumps(
        {
            "session": session,
            "form": form,
            "documents": documents,
            "ocr": {(row.get("doc_type") or "").upper(): row for row in ocr_rows},
            "comparison": review["comparison"],
            "derived_by": review["derived_by"],
            "events": events,
        }
    )


@tool
async def find_kyc_sessions(query: str) -> str:
    """Find KYC sessions by applicant name or PAN number, when the user doesn't
    have a session ID. Returns matching sessions with their session_id, outcome,
    and flags — follow up with get_kyc_session for full details. Aadhaar numbers
    are not searchable (the app never stores them in a searchable form, by
    design); if a user gives only an Aadhaar number, tell them you need a
    session ID, PAN, or name instead."""
    query = query.strip()
    if not query:
        return "Please provide a PAN number or applicant name to search for."
    try:
        matches = await fetch_all(
            f"SELECT {_FORM_COLUMNS} FROM kyc_intake.kyc_form_data "
            "WHERE upper(pan) = upper($1) OR full_name ILIKE '%' || $1 || '%' "
            "ORDER BY submitted_at DESC LIMIT 25",
            query,
        )
        items = []
        for form in matches:
            session_id = form["session_id"]
            session = await fetch_one(
                "SELECT session_id, status, created_at FROM kyc_intake.kyc_session "
                "WHERE session_id = $1",
                session_id,
            )
            ocr_rows = await fetch_all(
                f"SELECT {_OCR_COLUMNS} FROM kyc_intake.v_latest_ocr WHERE session_id = $1",
                session_id,
            )
            review = review_session(form, ocr_rows)
            items.append(
                {
                    "session_id": session_id,
                    "full_name": form.get("full_name"),
                    "pan": form.get("pan"),
                    "status": (session or {}).get("status"),
                    "created_at": (session or {}).get("created_at"),
                    "outcome": review["outcome"],
                    "flags": review["flags"],
                }
            )
    except KycDbError as e:
        return str(e)

    return json.dumps({"total": len(items), "items": items})


@tool
async def list_attention_kyc_sessions() -> str:
    """List KYC sessions that need a second look (outcome other than Verified —
    e.g. Mismatch, OCR Failed, Needs Review, No Documents). Use this when the
    user asks what needs review, or hasn't given a specific session ID."""
    try:
        sessions = await fetch_all(
            "SELECT session_id, status, created_at, created_by_username "
            "FROM kyc_intake.kyc_session ORDER BY created_at DESC LIMIT 200"
        )
        counts: dict = {}
        items = []
        for session in sessions:
            session_id = session["session_id"]
            form = await fetch_one(
                f"SELECT {_FORM_COLUMNS} FROM kyc_intake.kyc_form_data WHERE session_id = $1",
                session_id,
            )
            ocr_rows = await fetch_all(
                f"SELECT {_OCR_COLUMNS} FROM kyc_intake.v_latest_ocr WHERE session_id = $1",
                session_id,
            )
            review = review_session(form or {}, ocr_rows)
            outcome = review["outcome"]
            counts[outcome] = counts.get(outcome, 0) + 1
            if outcome != "Verified":
                items.append(
                    {
                        "session_id": session_id,
                        "status": session.get("status"),
                        "created_at": session.get("created_at"),
                        "created_by_username": session.get("created_by_username"),
                        "full_name": (form or {}).get("full_name"),
                        "pan": (form or {}).get("pan"),
                        "aadhaar_masked": (form or {}).get("aadhaar_masked"),
                        "outcome": outcome,
                        "flags": review["flags"],
                        "ocr": {
                            (row.get("doc_type") or "").upper(): {
                                "status": row.get("status"),
                                "mean_confidence": row.get("mean_confidence"),
                                "error": row.get("error"),
                            }
                            for row in ocr_rows
                        },
                    }
                )
    except KycDbError as e:
        return str(e)

    return json.dumps(
        {
            "total": len(sessions),
            "attention": len(items),
            "counts": counts,
            "items": items,
        }
    )


@tool
async def find_client(query: str) -> str:
    """Search the legacy client roster — a separate, older dataset from KYC
    sessions, covering onboarded clients (individuals, companies, HUFs, trusts)
    rather than new-account OCR submissions. Matches name, PAN, client_id, or
    application ID. Returns matching clients with client_id, client_type, pan,
    kra_status, onboarding_stage, aml_risk_rating, rm_name, branch — follow up
    with get_client_profile using the client_id for full details including bank
    accounts. Use this (not the kyc-session tools) when the user asks about a
    client_id, an onboarded client, bank account holders, UBOs, or a company /
    trust / HUF name rather than a specific KYC session."""
    query = query.strip()
    if not query:
        return "Please provide a name, PAN, client_id, or application_id to search for."
    try:
        items = await fetch_all(
            "SELECT client_id, application_id, name, client_type, pan, kra_status, "
            "onboarding_stage, aml_risk_rating, risk_category, rm_name, branch, "
            "last_updated_at FROM kyc.clients "
            "WHERE upper(client_id) = upper($1) OR upper(application_id) = upper($1) "
            "   OR upper(pan) = upper($1) OR name ILIKE '%' || $1 || '%' "
            "ORDER BY name LIMIT 25",
            query,
        )
    except KycDbError as e:
        return str(e)

    return json.dumps({"total": len(items), "returned": len(items), "items": items})


@tool
async def get_client_profile(client_id: str) -> str:
    """Full profile for one legacy client: personal/entity details, linked
    bank accounts (bank name, IFSC, masked account number, verification
    status), product accounts, KYC documents, AML screening history, UBOs
    (for companies/trusts), and risk profile. client_id looks like 'CL000044'
    — get it from find_client first if you only have a name or PAN. Aadhaar is
    stored masked (last 4 digits) at best and is often absent entirely,
    especially for non-individual clients (companies, trusts, HUFs) — if
    aadhaar_masked is null or missing, say plainly that no Aadhaar is on file;
    never invent or guess a number."""
    client_id = client_id.strip()
    if not client_id:
        return "Please provide a client_id, e.g. CL000044."
    try:
        client = await fetch_one(
            "SELECT * FROM kyc.clients WHERE upper(client_id) = upper($1)", client_id
        )
        if not client:
            return f"No client found with ID {client_id}."

        related = {}
        for key, table, order in (
            ("accounts", "kyc.accounts", "account_id"),
            ("bank_accounts", "kyc.bank_accounts", "is_primary DESC, bank_link_id"),
            ("demat", "kyc.demat_accounts", "client_id"),
            ("nominees", "kyc.nominees", "nominee_id"),
            ("ubos", "kyc.ubo_details", "ubo_id"),
            ("screening", "kyc.aml_screening", "screened_at DESC"),
            ("documents", "kyc.kyc_documents", "document_id"),
            ("risk_profile", "kyc.risk_profiles", "client_id"),
            ("fatca_crs", "kyc.fatca_crs", "client_id"),
        ):
            related[key] = await fetch_all(
                f"SELECT * FROM {table} WHERE upper(client_id) = upper($1) ORDER BY {order}",
                client_id,
            )
        # These are one-per-client; flatten so the LLM doesn't read a 1-item list.
        for key in ("demat", "risk_profile", "fatca_crs"):
            related[key] = related[key][0] if related[key] else None
    except KycDbError as e:
        return str(e)

    return json.dumps({"found": True, "client": client, **related})
