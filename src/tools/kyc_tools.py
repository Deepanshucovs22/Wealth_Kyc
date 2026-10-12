"""
eKYC review tools — read-only access to the KYC application's session API.
Auto-discovered by the tool registry (src/tools/registry.py).
"""

import json

from langchain_core.tools import tool

from src.tools._kyc_client import (
    KycApiError,
    fetch_attention_sessions,
    fetch_client_profile,
    fetch_kyc_session,
    is_valid_session_id,
    search_clients,
    search_kyc_sessions,
)


@tool
def get_kyc_session(session_id: str) -> str:
    """Fetch full details for one KYC session: submitted form data, PAN/Aadhaar OCR
    extraction, the app's own field-by-field comparison, and the outcome.
    session_id looks like 'KYC-20261008-8B8742'. Use this whenever the user asks
    about, or gives you, a specific session."""
    session_id = session_id.strip()
    if not is_valid_session_id(session_id):
        return (
            f"'{session_id}' doesn't look like a valid session ID. "
            "Session IDs look like KYC-YYYYMMDD-XXXXXX, e.g. KYC-20261008-8B8742."
        )
    try:
        data = fetch_kyc_session(session_id)
    except KycApiError as e:
        return str(e)
    return json.dumps(data)


@tool
def find_kyc_sessions(query: str) -> str:
    """Find KYC sessions by applicant name or PAN number, when the user doesn't
    have a session ID. Returns matching sessions with their session_id, outcome,
    and flags — follow up with get_kyc_session for full details. Aadhaar numbers
    are not searchable (the app never indexes them for lookup, by design); if a
    user gives only an Aadhaar number, tell them you need a session ID, PAN, or
    name instead."""
    query = query.strip()
    if not query:
        return "Please provide a PAN number or applicant name to search for."
    try:
        data = search_kyc_sessions(query)
    except KycApiError as e:
        return str(e)
    return json.dumps(data)


@tool
def list_attention_kyc_sessions() -> str:
    """List KYC sessions the application itself flagged as needing a second look
    (outcome other than Verified/Pending — e.g. Mismatch, Wrong Document, OCR
    Failed, Needs Review, Incomplete, No Documents). Use this when the user asks
    what needs review, or hasn't given a specific session ID."""
    try:
        data = fetch_attention_sessions()
    except KycApiError as e:
        return str(e)
    return json.dumps(data)


@tool
def find_client(query: str) -> str:
    """Search the legacy client roster — a separate, older dataset from KYC
    sessions, covering onboarded clients (individuals, companies, HUFs, trusts)
    rather than new-account OCR submissions. Matches name, PAN, or application
    ID. Returns matching clients with client_id, client_type, pan, kra_status,
    onboarding_stage, aml_risk_rating, rm_name, branch — follow up with
    get_client_profile using the client_id for full details including bank
    accounts. Use this (not the kyc-session tools) when the user asks about a
    client_id, an onboarded client, bank account holders, UBOs, or a company /
    trust / HUF name rather than a specific KYC session."""
    query = query.strip()
    if not query:
        return "Please provide a name, PAN, client_id, or application_id to search for."
    try:
        data = search_clients(query)
    except KycApiError as e:
        return str(e)
    return json.dumps(data)


@tool
def get_client_profile(client_id: str) -> str:
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
        data = fetch_client_profile(client_id)
    except KycApiError as e:
        return str(e)
    return json.dumps(data)
