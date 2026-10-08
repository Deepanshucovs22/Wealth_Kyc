"""
Covasant · WealthGate — Onboarding & KYC web application.

Every screen in the UI is driven by live queries against the `wealth_kyc`
PostgreSQL database; nothing is hard-coded in the front end.

Run:  python backend/app.py         (or: uvicorn backend.app:app --reload)
"""
from __future__ import annotations

import difflib
import hashlib
import hmac
import re
import secrets
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import psycopg2
from psycopg2 import pool
from psycopg2.extras import Json, RealDictCursor
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

try:  # works both as `python backend/app.py` and `uvicorn backend.app:app`
    from backend import auth, ocr
    from backend.config import (AADHAAR_HMAC_KEY, APP_HOST, APP_PORT, COOKIE_SECURE,
                                MAX_UPLOAD_MB, app_dsn, describe_target)
except ModuleNotFoundError:  # pragma: no cover
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from backend import auth, ocr
    from backend.config import (AADHAAR_HMAC_KEY, APP_HOST, APP_PORT, COOKIE_SECURE,
                                MAX_UPLOAD_MB, app_dsn, describe_target)

ROOT = Path(__file__).resolve().parent.parent   # project root
WEB = ROOT / "frontend"                        # static front end

# --------------------------------------------------------------------------
# Numeric columns (ocr_confidence, liveness_score, averages) arrive as
# Decimal, which FastAPI would serialise as a JSON *string*. Cast them to
# float at the driver level so the API emits real numbers. Dates and
# timestamps are left to FastAPI, which emits ISO-8601.
# --------------------------------------------------------------------------
DEC2FLOAT = psycopg2.extensions.new_type(
    psycopg2.extensions.DECIMAL.values,
    "DEC2FLOAT",
    lambda value, curs: float(value) if value is not None else None,
)
psycopg2.extensions.register_type(DEC2FLOAT)


# --------------------------------------------------------------------------
# Connection pool
# --------------------------------------------------------------------------
_pool: pool.ThreadedConnectionPool | None = None


@contextmanager
def db(readonly: bool = True) -> Iterator[RealDictCursor]:
    """Pooled cursor. Read-only by default; sign-in needs `readonly=False`
    to record sessions and audit events."""
    assert _pool is not None, "connection pool not initialised"
    conn = _pool.getconn()
    try:
        conn.set_session(readonly=readonly, autocommit=True)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SET search_path TO kyc, public")
            yield cur
    finally:
        try:
            conn.set_session(readonly=True, autocommit=True)
        except psycopg2.Error:
            pass
        _pool.putconn(conn)


@contextmanager
def db_tx() -> Iterator[RealDictCursor]:
    """One read-write transaction: commits if the block succeeds, rolls back
    if it raises. Used where several rows must land together or not at all."""
    assert _pool is not None, "connection pool not initialised"
    conn = _pool.getconn()
    try:
        conn.set_session(readonly=False, autocommit=False)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SET search_path TO kyc, public")
            yield cur
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        try:
            conn.set_session(readonly=True, autocommit=True)
        except psycopg2.Error:
            pass
        _pool.putconn(conn)


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def one(cur) -> dict | None:
    r = cur.fetchone()
    return dict(r) if r else None


app = FastAPI(
    title="Covasant · WealthGate Onboarding & KYC",
    description="PostgreSQL-backed onboarding, KYC and AML screening workspace.",
    version="1.0.0",
)


@app.on_event("startup")
def startup() -> None:
    global _pool
    try:
        _pool = pool.ThreadedConnectionPool(1, 10, app_dsn())
    except psycopg2.Error as exc:
        raise RuntimeError(
            f"cannot connect to {describe_target()} — check .env and that "
            f"PostgreSQL is running.\n{exc}"
        ) from exc
    with db() as cur:
        cur.execute("SELECT count(*) AS n FROM clients")
        n = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM app_user WHERE is_active")
        users = cur.fetchone()["n"]

    # New Account (KYC intake) tables live in their own schema; create them if missing.
    try:
        with db(readonly=False) as cur:
            cur.execute(INTAKE_SQL.read_text(encoding="utf-8"))
    except psycopg2.Error as exc:
        raise RuntimeError(f"could not apply {INTAKE_SQL.name}: {exc}") from exc
    if not AADHAAR_HMAC_KEY:
        print("  WARNING: AADHAAR_HMAC_KEY is not set in .env — using a development "
              "key. Set a long random value before storing real data.")
    # Load the OCR models in the background so the first report is not slow.
    threading.Thread(target=ocr.warm_up, name="ocr-warm-up", daemon=True).start()

    # Housekeeping: drop sessions that have already expired.
    try:
        with db(readonly=False) as cur:
            cur.execute("DELETE FROM app_session WHERE expires_at <= now()")
            if cur.rowcount:
                print(f"  cleared {cur.rowcount} expired session(s)")
    except psycopg2.Error:
        pass

    print(f"  connected to {describe_target()} — {n} clients, {users} user accounts")


@app.on_event("shutdown")
def shutdown() -> None:
    if _pool is not None:
        _pool.closeall()


# ==========================================================================
#  Authentication
# ==========================================================================
PUBLIC_API = {"/api/auth/login", "/api/auth/logout", "/api/auth/me", "/api/health"}


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _public_user(row: dict) -> dict:
    """The user shape the browser is allowed to see — never the hash.
    Datetimes are stringified here because the sign-in routes return a raw
    starlette JSONResponse, which does not know how to encode them."""
    last = row.get("last_login_at")
    return {
        "user_id": row["user_id"], "username": row["username"],
        "full_name": row["full_name"], "email": row["email"],
        "role": row["role"], "rm_id": row["rm_id"], "branch": row["branch"],
        "initials": "".join(w[0] for w in row["full_name"].split()[:2]).upper(),
        "last_login_at": last.strftime("%Y-%m-%dT%H:%M:%S") if last else None,
    }


def lookup_session(token: str | None) -> dict | None:
    """Resolve a cookie token to a live user, or None."""
    if not token:
        return None
    th = auth.token_hash(token)
    with db() as cur:
        cur.execute("""
            SELECT u.*, s.expires_at, s.last_seen
            FROM app_session s JOIN app_user u USING (user_id)
            WHERE s.token_hash = %s AND s.expires_at > now() AND u.is_active
        """, (th,))
        row = one(cur)
    if row is None:
        return None
    # Refresh last_seen at most once every five minutes.
    if row["last_seen"] < datetime.now(timezone.utc) - timedelta(minutes=5):
        try:
            with db(readonly=False) as cur:
                cur.execute("UPDATE app_session SET last_seen = now() WHERE token_hash = %s", (th,))
        except psycopg2.Error:
            pass
    return row


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """Every /api route except the public ones needs a live session."""
    path = request.url.path
    if path.startswith("/api/") and path not in PUBLIC_API:
        token = request.cookies.get(auth.COOKIE_NAME)
        user = await run_in_threadpool(lookup_session, token)
        if user is None:
            return JSONResponse({"detail": "Not signed in"}, status_code=401)
        request.state.user = user
    return await call_next(request)


@app.post("/api/auth/login", summary="Sign in and open a session")
def login(request: Request,
          username: str = Body(..., embed=True),
          password: str = Body(..., embed=True)) -> JSONResponse:
    username = (username or "").strip().lower()
    ip, ua = _client_ip(request), request.headers.get("user-agent")

    with db(readonly=False) as cur:
        cur.execute("SELECT * FROM app_user WHERE lower(username) = %s", (username,))
        user = one(cur)

        def audit(event: str, uid: int | None) -> None:
            cur.execute("""INSERT INTO auth_event (username, user_id, event, ip_address, user_agent)
                           VALUES (%s, %s, %s, %s, %s)""", (username, uid, event, ip, ua))

        # A missing user still runs a hash so timing does not leak existence.
        if user is None:
            auth.verify_password(password, auth.hash_password("decoy"))
            audit("failed_login", None)
            raise HTTPException(401, "Incorrect username or password")

        if not user["is_active"]:
            audit("failed_login", user["user_id"])
            raise HTTPException(403, "This account has been deactivated")

        if user["locked_until"] and user["locked_until"] > datetime.now(timezone.utc):
            mins = int((user["locked_until"] - datetime.now(timezone.utc)).total_seconds() // 60) + 1
            audit("failed_login", user["user_id"])
            raise HTTPException(429, f"Too many attempts — try again in {mins} minute(s)")

        if not auth.verify_password(password, user["password_hash"]):
            failed = user["failed_logins"] + 1
            if failed >= auth.MAX_FAILED_LOGINS:
                cur.execute("""UPDATE app_user SET failed_logins = 0, locked_until = %s
                               WHERE user_id = %s""", (auth.lockout_until(), user["user_id"]))
                audit("locked_out", user["user_id"])
                raise HTTPException(429, f"Too many attempts — locked for "
                                         f"{auth.LOCKOUT_MINUTES} minutes")
            cur.execute("UPDATE app_user SET failed_logins = %s WHERE user_id = %s",
                        (failed, user["user_id"]))
            audit("failed_login", user["user_id"])
            raise HTTPException(401, "Incorrect username or password")

        token = auth.new_session_token()
        cur.execute("""INSERT INTO app_session (token_hash, user_id, expires_at, user_agent, ip_address)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (auth.token_hash(token), user["user_id"], auth.session_expiry(), ua, ip))
        cur.execute("""UPDATE app_user SET failed_logins = 0, locked_until = NULL,
                       last_login_at = now() WHERE user_id = %s""", (user["user_id"],))
        audit("login", user["user_id"])
        cur.execute("SELECT * FROM app_user WHERE user_id = %s", (user["user_id"],))
        fresh = one(cur)

    body = JSONResponse({"user": _public_user(fresh)})
    body.set_cookie(auth.COOKIE_NAME, token, max_age=auth.SESSION_HOURS * 3600,
                    httponly=True, samesite="lax", secure=COOKIE_SECURE, path="/")
    return body


@app.post("/api/auth/logout", summary="Sign out and revoke the session")
def logout(request: Request) -> JSONResponse:
    token = request.cookies.get(auth.COOKIE_NAME)
    if token:
        try:
            with db(readonly=False) as cur:
                cur.execute("""DELETE FROM app_session WHERE token_hash = %s
                               RETURNING user_id""", (auth.token_hash(token),))
                gone = cur.fetchone()
                if gone:
                    cur.execute("""INSERT INTO auth_event (username, user_id, event, ip_address, user_agent)
                                   SELECT username, user_id, 'logout', %s, %s FROM app_user
                                   WHERE user_id = %s""",
                                (_client_ip(request), request.headers.get("user-agent"),
                                 gone["user_id"]))
        except psycopg2.Error:
            pass
    body = JSONResponse({"ok": True})
    body.delete_cookie(auth.COOKIE_NAME, path="/")
    return body


@app.get("/api/auth/me", summary="The currently signed-in user")
def me(request: Request) -> dict:
    user = lookup_session(request.cookies.get(auth.COOKIE_NAME))
    if user is None:
        raise HTTPException(401, "Not signed in")
    return {"user": _public_user(user)}


# ==========================================================================
#  Reference data & metadata
# ==========================================================================
@app.get("/api/reference", summary="Dropdown / lookup values and dataset provenance")
def reference() -> dict:
    with db() as cur:
        cur.execute("SELECT generated_on, disclaimer, source_file, loaded_at FROM dataset_meta")
        meta = one(cur) or {}
        meta.pop("loaded_at", None)

        cur.execute("SELECT name FROM ref_client_type ORDER BY sort_order")
        client_types = [r["name"] for r in cur.fetchall()]

        cur.execute("SELECT name FROM ref_onboarding_stage ORDER BY sort_order")
        stages = [r["name"] for r in cur.fetchall()]

        cur.execute("SELECT code, description FROM ref_kra_status ORDER BY code")
        kra = {r["code"]: r["description"] for r in cur.fetchall()}

        cur.execute("SELECT code, name, min_investment_inr FROM ref_product ORDER BY code")
        products = rows(cur)

        cur.execute("SELECT name FROM ref_risk_category ORDER BY sort_order")
        risk = [r["name"] for r in cur.fetchall()]

        cur.execute("SELECT name FROM ref_screening_list ORDER BY sort_order")
        lists_ = [r["name"] for r in cur.fetchall()]

        cur.execute("SELECT rm_id, name, branch FROM relationship_manager ORDER BY rm_id")
        rms = rows(cur)

    return {
        "generated_on": meta.get("generated_on"),
        "disclaimer": meta.get("disclaimer"),
        "source_file": meta.get("source_file"),
        "client_types": client_types,
        "onboarding_stages": stages,
        "kra_status_codes": kra,
        "products": products,
        "risk_categories": risk,
        "screening_lists": lists_,
        "relationship_managers": rms,
    }


# ==========================================================================
#  Dashboard
# ==========================================================================
@app.get("/api/dashboard", summary="Headline KPIs, pipeline and RM conversion")
def dashboard() -> dict:
    with db() as cur:
        cur.execute("SELECT * FROM v_dashboard_kpis")
        kpis = one(cur)

        cur.execute("SELECT stage, applications FROM v_pipeline_by_stage ORDER BY sort_order")
        pipeline = rows(cur)

        cur.execute("""
            SELECT rm_name, branch, applications, activated,
                   round(activated * 100.0 / nullif(applications, 0))::int AS conversion_pct
            FROM v_rm_performance ORDER BY rm_name
        """)
        rm = rows(cur)

        cur.execute("""
            SELECT to_char(date_trunc('month', created_at), 'Mon YYYY') AS month,
                   date_trunc('month', created_at) AS sort_key,
                   count(*)::int AS applications,
                   round(avg(EXTRACT(EPOCH FROM (last_updated_at - created_at)) / 86400.0)::numeric, 1)
                       AS avg_tat_days
            FROM clients
            GROUP BY date_trunc('month', created_at)
            ORDER BY date_trunc('month', created_at)
        """)
        tat = [{k: v for k, v in r.items() if k != "sort_key"} for r in rows(cur)]

        cur.execute("""
            SELECT min(created_at)::date AS from_date, max(last_updated_at)::date AS to_date
            FROM clients
        """)
        period = one(cur)

    return {"kpis": kpis, "pipeline": pipeline, "by_rm": rm,
            "tat_trend": tat, "period": period}


# ==========================================================================
#  Onboarding queue
# ==========================================================================
@app.get("/api/applications", summary="Onboarding queue, filtered server-side")
def applications(
    stage: str | None = Query(None, description="exact onboarding_stage"),
    client_type: str | None = Query(None, description="exact client_type"),
    aml_risk_rating: str | None = None,
    q: str | None = Query(None, description="matches name, PAN or application id"),
    limit: int = Query(500, ge=1, le=2000),
) -> dict:
    where, params = ["1 = 1"], {}
    if stage:
        where.append("onboarding_stage = %(stage)s")
        params["stage"] = stage
    if client_type:
        where.append("client_type = %(ctype)s")
        params["ctype"] = client_type
    if aml_risk_rating:
        where.append("aml_risk_rating = %(aml)s")
        params["aml"] = aml_risk_rating
    if q:
        where.append("(name ILIKE %(q)s OR pan ILIKE %(q)s OR application_id ILIKE %(q)s)")
        params["q"] = f"%{q}%"
    params["limit"] = limit
    clause = " AND ".join(where)

    with db() as cur:
        cur.execute(f"SELECT count(*)::int AS n FROM clients WHERE {clause}", params)
        total = cur.fetchone()["n"]
        cur.execute(f"""
            SELECT client_id, application_id, name, client_type, pan, kra_status,
                   onboarding_stage, aml_risk_rating, rm_name, branch, last_updated_at
            FROM clients WHERE {clause}
            ORDER BY last_updated_at DESC
            LIMIT %(limit)s
        """, params)
        items = rows(cur)
    return {"total": total, "returned": len(items), "items": items}


# ==========================================================================
#  Client KYC 360 — one bundle per client
# ==========================================================================
@app.get("/api/clients/{client_id}/kyc-summary", summary="Everything about one client")
def kyc_summary(client_id: str) -> dict:
    with db() as cur:
        cur.execute("SELECT * FROM clients WHERE client_id = %s", (client_id,))
        client = one(cur)
        if client is None:
            raise HTTPException(404, f"no client {client_id}")

        cur.execute("SELECT * FROM accounts WHERE client_id = %s ORDER BY account_id", (client_id,))
        accounts = rows(cur)

        cur.execute("""SELECT * FROM kyc_documents WHERE client_id = %s
                       ORDER BY document_id""", (client_id,))
        documents = rows(cur)

        cur.execute("""SELECT * FROM aml_screening WHERE client_id = %s
                       ORDER BY screening_id""", (client_id,))
        screening = rows(cur)

        cur.execute("""SELECT * FROM workflow_events WHERE client_id = %s
                       ORDER BY "timestamp", event_id""", (client_id,))
        timeline = rows(cur)

        cur.execute("""SELECT * FROM bank_accounts WHERE client_id = %s
                       ORDER BY is_primary DESC, bank_link_id""", (client_id,))
        banks = rows(cur)

        cur.execute("SELECT * FROM demat_accounts WHERE client_id = %s", (client_id,))
        demat = one(cur)

        cur.execute("SELECT * FROM nominees WHERE client_id = %s ORDER BY nominee_id", (client_id,))
        nominees = rows(cur)

        cur.execute("SELECT * FROM ubo_details WHERE client_id = %s ORDER BY ubo_id", (client_id,))
        ubos = rows(cur)

        cur.execute("SELECT * FROM fatca_crs WHERE client_id = %s", (client_id,))
        fatca = one(cur)

        cur.execute("SELECT * FROM risk_profiles WHERE client_id = %s", (client_id,))
        risk = one(cur)

        cur.execute("""SELECT * FROM api_call_log WHERE client_id = %s
                       ORDER BY "timestamp" DESC LIMIT 25""", (client_id,))
        api_log = rows(cur)

    return {"client": client, "accounts": accounts, "documents": documents,
            "screening": screening, "timeline": timeline, "bank_accounts": banks,
            "demat": demat, "nominees": nominees, "ubos": ubos,
            "fatca_crs": fatca, "risk_profile": risk, "api_call_log": api_log}


@app.get("/api/kyc/fetch/{pan}", summary="KRA + CKYC lookup by PAN (drives the wizard)")
def kyc_fetch(pan: str) -> dict:
    pan = pan.strip().upper()
    with db() as cur:
        cur.execute("SELECT client_id FROM clients WHERE pan = %s", (pan,))
        hit = one(cur)
    if hit is None:
        return {"found": False, "pan": pan, "kra_status": "Not Found", "ckyc_number": None}
    bundle = kyc_summary(hit["client_id"])
    bundle["found"] = True
    return bundle


@app.get("/api/sample-pans", summary="A few real PANs per client type, for demo input")
def sample_pans(client_type: str | None = None, limit: int = Query(5, ge=1, le=25)) -> list[dict]:
    with db() as cur:
        if client_type:
            cur.execute("""SELECT pan, name, client_type FROM clients
                           WHERE client_type = %s ORDER BY client_id LIMIT %s""",
                        (client_type, limit))
        else:
            cur.execute("SELECT pan, name, client_type FROM clients ORDER BY client_id LIMIT %s",
                        (limit,))
        return rows(cur)


# ==========================================================================
#  Screening alerts
# ==========================================================================
@app.get("/api/screening/alerts", summary="All non-clean screening results")
def screening_alerts(open_only: bool = False) -> dict:
    clause = ""
    if open_only:
        clause = "AND coalesce(disposition, '') <> 'False Positive - Cleared'"
    with db() as cur:
        cur.execute(f"""
            SELECT screening_id, client_id, client_name, list_name, result, match_score,
                   matched_name, disposition, screened_at, reviewer, aml_risk_rating
            FROM v_screening_hits
            WHERE 1 = 1 {clause}
            ORDER BY match_score DESC, screening_id
        """)
        items = rows(cur)
    return {"total": len(items), "items": items}


# ==========================================================================
#  Re-KYC / periodic review
# ==========================================================================
@app.get("/api/reviews/due", summary="Periodic review queue and expiring documents")
def reviews_due(expiring_before: str = "2028-01-01", limit: int = Query(60, ge=1, le=500)) -> dict:
    with db() as cur:
        cur.execute("""
            SELECT client_id, name, aml_risk_rating, due_diligence_level,
                   periodic_kyc_review_due, rm_name
            FROM clients
            WHERE onboarding_stage = 'Account Activated'
            ORDER BY periodic_kyc_review_due
            LIMIT %s
        """, (limit,))
        review_queue = rows(cur)

        cur.execute("""
            SELECT d.document_id, d.client_id, c.name AS client_name,
                   d.document_type, d.expiry_date, d.status
            FROM kyc_documents d JOIN clients c USING (client_id)
            WHERE d.expiry_date IS NOT NULL AND d.expiry_date < %s
            ORDER BY d.expiry_date
        """, (expiring_before,))
        expiring = rows(cur)

    return {"review_queue": review_queue, "expiring_documents": expiring,
            "expiring_before": expiring_before}


# ==========================================================================
#  API catalogue (API Explorer screen)
# ==========================================================================
@app.get("/api/endpoints", summary="Mock downstream API catalogue")
def endpoints() -> list[dict]:
    with db() as cur:
        cur.execute("""
            SELECT api_group AS "group", method, path, summary, description,
                   downstream, success_status, params,
                   sample_request AS request, sample_response AS response
            FROM api_catalogue ORDER BY seq
        """)
        return rows(cur)


@app.get("/api/integration/health", summary="Latency and status mix of downstream calls")
def integration_health() -> dict:
    with db() as cur:
        cur.execute("""
            SELECT downstream,
                   count(*)::int AS calls,
                   round(avg(latency_ms))::int AS avg_latency_ms,
                   max(latency_ms) AS max_latency_ms,
                   count(*) FILTER (WHERE http_status >= 400)::int AS errors
            FROM api_call_log GROUP BY downstream ORDER BY calls DESC
        """)
        by_downstream = rows(cur)
        cur.execute("""
            SELECT http_status, count(*)::int AS calls
            FROM api_call_log GROUP BY http_status ORDER BY http_status
        """)
        by_status = rows(cur)
    return {"by_downstream": by_downstream, "by_status": by_status}


@app.get("/api/health", summary="Liveness + database reachability")
def health() -> dict:
    try:
        with db() as cur:
            cur.execute("SELECT count(*)::int AS n FROM clients")
            n = cur.fetchone()["n"]
        return {"status": "ok", "database": describe_target(), "clients": n}
    except psycopg2.Error as exc:
        raise HTTPException(503, f"database unavailable: {exc}") from exc


# ==========================================================================
#  New Account — KYC intake sessions (schema kyc_intake, see db/kyc_intake.sql)
#
#  POST creates a session, stores the form and the files in one transaction,
#  then runs OCR on the PAN and Aadhaar uploads. What the user typed and what
#  OCR read are stored apart and only compared when a session is read back.
# ==========================================================================
INTAKE_SQL = ROOT / "backend" / "db" / "kyc_intake.sql"
MAX_UPLOAD = MAX_UPLOAD_MB * 1024 * 1024
_HMAC_KEY = (AADHAAR_HMAC_KEY or "wealthgate-development-key").encode()

# form field -> (document type, max files)
DOC_SLOTS = {"pan_card": ("PAN", 1), "aadhaar_card": ("AADHAAR", 2), "signature": ("SIGNATURE", 1)}
REQUEST_TYPES = {"New User", "Modification", "Deletion", "Duplicate Password"}
TRANSACTION_TYPES = {"A", "B", "C", "TFConnect"}


def _aadhaar_hmac(number: str) -> str:
    return hmac.new(_HMAC_KEY, number.encode(), hashlib.sha256).hexdigest()


def _validate_report(form) -> tuple[dict, dict]:
    """Server-side rules for the New Account form — the browser checks the same
    things, but only this copy is trusted. Returns (clean values, errors)."""
    v, err = {}, {}

    def text(key: str, limit: int = 150) -> str | None:
        raw = form.get(key)
        val = re.sub(r"\s+", " ", raw).strip() if isinstance(raw, str) else ""
        if len(val) > limit:
            err[key] = f"Must be at most {limit} characters"
        return val or None

    # ---- mandatory
    name = text("full_name")
    if not name:
        err["full_name"] = "Name is required"
    elif not re.fullmatch(r"[A-Za-z][A-Za-z .'\-]{1,149}", name):
        err["full_name"] = "Use letters, spaces and . ' - only"
    v["full_name"] = name

    dob = text("date_of_birth", 10)
    try:
        d = date.fromisoformat(dob or "")
        if d > date.today():
            err["date_of_birth"] = "Date of birth cannot be in the future"
        elif d.year < 1900:
            err["date_of_birth"] = "Enter a valid date of birth"
        v["date_of_birth"] = d
    except ValueError:
        err["date_of_birth"] = "Date of birth is required" if not dob else "Enter a valid date"

    pan = (text("pan", 12) or "").upper().replace(" ", "")
    if not pan:
        err["pan"] = "PAN is required"
    elif not re.fullmatch(r"[A-Z]{5}[0-9]{4}[A-Z]", pan):
        err["pan"] = "PAN must look like ABCDE1234F"
    v["pan"] = pan

    aadhaar = re.sub(r"[\s\-]", "", text("aadhaar", 14) or "")
    if not aadhaar:
        err["aadhaar"] = "Aadhaar number is required"
    elif not re.fullmatch(r"\d{12}", aadhaar):
        err["aadhaar"] = "Aadhaar must be 12 digits"
    elif not ocr.valid_aadhaar(aadhaar):
        err["aadhaar"] = "Not a valid Aadhaar number — please re-check the digits"
    v["aadhaar"] = aadhaar

    for key, label in (("address_line1", "Address"), ("city", "City"), ("state", "State")):
        v[key] = text(key, 200 if key == "address_line1" else 80)
        if not v[key]:
            err[key] = f"{label} is required"
    v["address_line2"] = text("address_line2", 200)

    pin = text("pincode", 6)
    if not pin:
        err["pincode"] = "PIN code is required"
    elif not re.fullmatch(r"[1-9][0-9]{5}", pin):
        err["pincode"] = "PIN code must be 6 digits"
    v["pincode"] = pin

    # ---- optional (internet-banking request section of the bank form)
    v["customer_id"] = text("customer_id", 30)
    v["existing_user_id"] = text("existing_user_id", 30)
    v["preferred_user_id"] = text("preferred_user_id", 30)

    v["mobile"] = text("mobile", 10)
    if v["mobile"] and not re.fullmatch(r"[6-9][0-9]{9}", v["mobile"]):
        err["mobile"] = "Enter a 10-digit Indian mobile number"
    v["email"] = text("email", 120)
    if v["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v["email"]):
        err["email"] = "Enter a valid email address"

    v["request_type"] = text("request_type", 30)
    if v["request_type"] and v["request_type"] not in REQUEST_TYPES:
        err["request_type"] = "Choose one of the listed request types"
    v["transaction_type"] = text("transaction_type", 10)
    if v["transaction_type"] and v["transaction_type"] not in TRANSACTION_TYPES:
        err["transaction_type"] = "Choose A, B, C or TFConnect"

    for key in ("limit_per_day", "limit_per_transaction"):
        raw = (text(key, 20) or "").replace(",", "")
        v[key] = None
        if raw:
            try:
                v[key] = float(raw)
                if not 0 <= v[key] < 1e13:
                    raise ValueError
            except ValueError:
                err[key] = "Enter an amount in rupees"
    if (v["limit_per_day"] is not None and v["limit_per_transaction"] is not None
            and v["limit_per_transaction"] > v["limit_per_day"]):
        err["limit_per_transaction"] = "Cannot exceed the per-day limit"

    appr = text("approvers_required", 1)
    v["approvers_required"] = int(appr) if appr and appr in "012" else None
    if appr and v["approvers_required"] is None:
        err["approvers_required"] = "Choose 0, 1 or 2"
    return v, err


async def _read_uploads(form) -> tuple[list[dict], dict]:
    """Read every uploaded file, checking count, size and real content type."""
    docs, err = [], {}
    for key, (doc_type, most) in DOC_SLOTS.items():
        files = [f for f in form.getlist(key) if isinstance(f, UploadFile) and f.filename]
        if len(files) > most:
            err[key] = f"Upload at most {most} file{'s' if most > 1 else ''}"
            continue
        for seq, f in enumerate(files, 1):
            data = await f.read(MAX_UPLOAD + 1)
            mime = ocr.sniff_mime(data)
            if not data:
                err[key] = f"{f.filename} is empty"
            elif len(data) > MAX_UPLOAD:
                err[key] = f"{f.filename} is larger than {MAX_UPLOAD_MB} MB"
            elif mime is None or (doc_type == "SIGNATURE" and mime == "application/pdf"):
                err[key] = (f"{f.filename}: upload a JPG, PNG or WebP image"
                            + ("" if doc_type == "SIGNATURE" else ", or a PDF"))
            else:
                docs.append({"doc_type": doc_type, "seq": seq, "file_name": f.filename[-200:],
                             "mime_type": mime, "data": data,
                             "sha256": hashlib.sha256(data).hexdigest()})
    return docs, err


def _event(cur, sid: str, event: str, actor: str, detail: dict | None = None) -> None:
    cur.execute("""INSERT INTO kyc_intake.kyc_session_event (session_id, event, actor, detail)
                   VALUES (%s, %s, %s, %s)""",
                (sid, event, actor, Json(detail) if detail else None))


def _insert_session(data: dict, docs: list[dict], user: dict, ip: str | None) -> str:
    """Session, form and files in one transaction — all of it lands or none."""
    data = dict(data)
    number = data.pop("aadhaar")
    data["aadhaar_masked"] = ocr.mask_aadhaar(number)
    data["aadhaar_hmac"] = _aadhaar_hmac(number)

    with db_tx() as cur:
        for _ in range(5):          # 16.7M ids per day; a clash is near-impossible
            sid = f"KYC-{datetime.now():%Y%m%d}-{secrets.token_hex(3).upper()}"
            cur.execute("""
                INSERT INTO kyc_intake.kyc_session
                    (session_id, created_by_user_id, created_by_username, created_by_name, client_ip)
                VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
            """, (sid, user["user_id"], user["username"], user["full_name"], ip))
            if cur.rowcount:
                break
        else:
            raise HTTPException(500, "could not allocate a KYC session id")

        cols = list(data)           # keys come from _validate_report, never from the client
        cur.execute(f"""INSERT INTO kyc_intake.kyc_form_data (session_id, {', '.join(cols)})
                        VALUES (%s, {', '.join(['%s'] * len(cols))})""", [sid, *data.values()])
        for d in docs:
            cur.execute("""
                INSERT INTO kyc_intake.kyc_document
                    (session_id, doc_type, seq, file_name, mime_type, size_bytes, sha256, content)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (sid, d["doc_type"], d["seq"], d["file_name"], d["mime_type"],
                  len(d["data"]), d["sha256"], psycopg2.Binary(d["data"])))
        _event(cur, sid, "session_created", user["username"],
               {"documents": [f"{d['doc_type']} #{d['seq']}" for d in docs]})
    return sid


def _run_ocr(sid: str, actor: str) -> None:
    """OCR the session's PAN and Aadhaar files and store a new attempt for each."""
    with db() as cur:
        cur.execute("""SELECT doc_type, mime_type, content FROM kyc_intake.kyc_document
                       WHERE session_id = %s AND doc_type IN ('PAN', 'AADHAAR')
                       ORDER BY doc_type, seq""", (sid,))
        docs = rows(cur)

    with db(readonly=False) as cur:
        cur.execute("""UPDATE kyc_intake.kyc_session SET status = %s, updated_at = now()
                       WHERE session_id = %s""",
                    ("OCR Running" if docs else "No ID Documents", sid))
    if not docs:
        return

    results = {}
    for doc_type in ("PAN", "AADHAAR"):
        files = [(bytes(d["content"]), d["mime_type"]) for d in docs if d["doc_type"] == doc_type]
        if files:
            results[doc_type] = ocr.run(doc_type, files)

    with db_tx() as cur:
        for doc_type, r in results.items():
            f = r["fields"]
            number = f.pop("aadhaar_number", None)       # hashed here, never stored
            cur.execute("""
                INSERT INTO kyc_intake.kyc_ocr_result
                    (session_id, doc_type, attempt, status, engine,
                     extracted_name, extracted_dob, extracted_yob, extracted_pan,
                     extracted_aadhaar_masked, extracted_aadhaar_hmac, extracted_address,
                     field_confidence, raw_text, lines, mean_confidence, pages, error, duration_ms)
                SELECT %s, %s, coalesce(max(attempt), 0) + 1, %s, %s,
                       %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                FROM kyc_intake.kyc_ocr_result WHERE session_id = %s AND doc_type = %s
                RETURNING attempt
            """, (sid, doc_type, r["status"], r["engine"],
                  f.get("name"), f.get("date_of_birth"), f.get("year_of_birth"), f.get("pan"),
                  f.get("aadhaar_masked"), _aadhaar_hmac(number) if number else None,
                  f.get("address"), Json(r["confidence"]), r["raw_text"],
                  Json(r["lines"]), r["mean_confidence"], r["pages"],
                  r["error"], r["duration_ms"], sid, doc_type))
            attempt = cur.fetchone()["attempt"]
            _event(cur, sid, "ocr_failed" if r["status"] in ("Failed", "No Text", "Wrong Document")
                   else "ocr_completed",
                   actor, {"doc_type": doc_type, "attempt": attempt, "status": r["status"],
                           "fields": sorted(k for k in f if k != "aadhaar_masked"),
                           "duration_ms": r["duration_ms"], "error": r["error"]})
        failed = any(r["status"] in ("Failed", "No Text", "Wrong Document") for r in results.values())
        cur.execute("""UPDATE kyc_intake.kyc_session
                       SET status = %s, ocr_completed_at = now(), updated_at = now()
                       WHERE session_id = %s""", ("OCR Failed" if failed else "OCR Complete", sid))


# ---- comparison: form vs. OCR, computed on read, never stored -------------
def _norm_name(s: str) -> str:
    return " ".join(sorted(re.sub(r"[^A-Z ]", " ", s.upper()).split()))


def _cmp_name(form_val: str, ocr_val: str | None) -> str:
    if not ocr_val:
        return "Not read"
    a, b = _norm_name(form_val), _norm_name(ocr_val)
    if a == b:
        return "Match"
    wa, wb = set(a.split()), set(b.split())
    if wa <= wb or wb <= wa or difflib.SequenceMatcher(None, a, b).ratio() >= 0.85:
        return "Partial"
    return "Mismatch"


ADDR_NOISE = {"road", "rd", "street", "st", "no", "flat", "near", "opp", "the", "and",
              "of", "house", "floor", "india"}


def _cmp_address(form: dict, ocr_val: str | None) -> str:
    if not ocr_val:
        return "Not read"
    full = " ".join(filter(None, (form["address_line1"], form["address_line2"],
                                  form["city"], form["state"], form["pincode"])))
    words = lambda s: {w for w in re.findall(r"[a-z0-9]+", s.lower())
                       if len(w) > 1 and w not in ADDR_NOISE}
    mine, card = words(full), words(ocr_val)
    share = len(mine & card) / max(1, len(mine))
    pin_ok = form["pincode"] in re.sub(r"\s", "", ocr_val)
    if pin_ok and share >= 0.6:
        return "Match"
    return "Partial" if share >= 0.35 or pin_ok else "Mismatch"


def _compare(form: dict, latest: dict) -> list[dict]:
    pan, aad = latest.get("PAN"), latest.get("AADHAAR")

    def source(rec, fn):
        if rec is None:
            return "No document"
        if rec["status"] in ("Failed", "No Text"):
            return "OCR failed"
        if rec["status"] == "Wrong Document":
            return "Wrong document"
        return fn(rec)

    def dob(rec):
        if rec["extracted_dob"]:
            return "Match" if rec["extracted_dob"] == form["date_of_birth"] else "Mismatch"
        if rec.get("extracted_yob"):
            return "Partial" if rec["extracted_yob"] == form["date_of_birth"].year else "Mismatch"
        return "Not read"

    def aadhaar(rec):
        if rec["extracted_aadhaar_hmac"]:
            return "Match" if hmac.compare_digest(rec["extracted_aadhaar_hmac"],
                                                  form["aadhaar_hmac"]) else "Mismatch"
        if rec["extracted_aadhaar_masked"]:      # masked card: only the last four to go on
            return ("Partial" if rec["extracted_aadhaar_masked"] == form["aadhaar_masked"]
                    else "Mismatch")
        return "Not read"

    def pan_no(rec):
        if not rec["extracted_pan"]:
            return "Not read"
        return "Match" if rec["extracted_pan"].strip() == form["pan"].strip() else "Mismatch"

    return [
        {"field": "Name", "pan": source(pan, lambda r: _cmp_name(form["full_name"], r["extracted_name"])),
         "aadhaar": source(aad, lambda r: _cmp_name(form["full_name"], r["extracted_name"]))},
        {"field": "Date of birth", "pan": source(pan, dob), "aadhaar": source(aad, dob)},
        {"field": "PAN number", "pan": source(pan, pan_no), "aadhaar": "n/a"},
        {"field": "Aadhaar number", "pan": "n/a", "aadhaar": source(aad, aadhaar)},
        {"field": "Address",
         "pan": source(pan, lambda r: _cmp_address(form, r["extracted_address"])
                       if r["extracted_address"] else "Not on card"),
         "aadhaar": source(aad, lambda r: _cmp_address(form, r["extracted_address"]))},
    ]


# ---- verification outcome: one word per report, for the queue and dashboard
ATTENTION = ("Wrong Document", "OCR Failed", "Mismatch", "Needs Review")
OUTCOMES = ("Verified", "Needs Review", "Mismatch", "Wrong Document", "OCR Failed",
            "Incomplete", "No Documents", "Pending")


def _outcome(status: str, comparison: list[dict], doc_types: set) -> str:
    """Worst finding wins: an unreadable card or a contradiction outranks a
    missing document, which outranks a clean result."""
    if status in ("Submitted", "OCR Running"):
        return "Pending"
    has_pan, has_aadhaar = "PAN" in doc_types, "AADHAAR" in doc_types
    if not (has_pan or has_aadhaar):
        return "No Documents"
    verdicts = [v for c in comparison for v in (c["pan"], c["aadhaar"])]
    if "Wrong document" in verdicts:
        return "Wrong Document"
    if "OCR failed" in verdicts:
        return "OCR Failed"
    if "Mismatch" in verdicts:
        return "Mismatch"
    if "Partial" in verdicts or "Not read" in verdicts:
        return "Needs Review"
    if not (has_pan and has_aadhaar):
        return "Incomplete"
    return "Verified"


def _flags(comparison: list[dict]) -> list[str]:
    """Fields with any finding short of a clean match."""
    bad = {"Mismatch", "Partial", "Not read", "OCR failed", "Wrong document"}
    return [c["field"] for c in comparison if c["pan"] in bad or c["aadhaar"] in bad]


def _load_reports(q: str | None = None) -> list[dict]:
    """Every report with its outcome. The comparison runs in Python (fuzzy
    name and address matching), so this reads forms and latest OCR in bulk —
    three queries however many reports there are."""
    where, params = "", {}
    if q:
        where = "WHERE f.full_name ILIKE %(q)s OR f.pan ILIKE %(q)s OR s.session_id ILIKE %(q)s"
        params["q"] = f"%{q.strip()}%"
    with db() as cur:
        cur.execute(f"""
            SELECT f.*, s.status, s.created_at, s.created_by_username, s.created_by_name
            FROM kyc_intake.kyc_session s JOIN kyc_intake.kyc_form_data f USING (session_id)
            {where} ORDER BY s.created_at DESC
        """, params)
        forms = rows(cur)
        if not forms:
            return []
        ids = [f["session_id"] for f in forms]
        cur.execute("""SELECT * FROM kyc_intake.v_latest_ocr WHERE session_id = ANY(%s)""", (ids,))
        latest: dict[str, dict] = {}
        for r in rows(cur):
            latest.setdefault(r["session_id"], {})[r["doc_type"]] = r
        cur.execute("""SELECT session_id, array_agg(DISTINCT doc_type) AS types
                       FROM kyc_intake.kyc_document WHERE session_id = ANY(%s)
                       GROUP BY session_id""", (ids,))
        types = {r["session_id"]: set(r["types"]) for r in rows(cur)}

    out = []
    for f in forms:
        sid = f["session_id"]
        comparison = _compare(f, latest.get(sid, {}))
        docs = types.get(sid, set())
        ocr = latest.get(sid, {})
        out.append({
            "session_id": sid, "status": f["status"],
            "created_at": f["created_at"], "created_by_username": f["created_by_username"],
            "created_by_name": f["created_by_name"],
            "full_name": f["full_name"], "pan": f["pan"], "aadhaar_masked": f["aadhaar_masked"],
            "city": f["city"], "state": f["state"],
            "documents": sorted(docs),
            "outcome": _outcome(f["status"], comparison, docs),
            "flags": _flags(comparison),
            "comparison": comparison,
            "ocr": {t: {"status": r["status"], "mean_confidence": r["mean_confidence"],
                        "duration_ms": r["duration_ms"]} for t, r in ocr.items()},
        })
    return out


def _session_detail(sid: str) -> dict:
    with db() as cur:
        cur.execute("SELECT * FROM kyc_intake.kyc_session WHERE session_id = %s", (sid,))
        session = one(cur)
        if session is None:
            raise HTTPException(404, f"no KYC session {sid}")
        cur.execute("SELECT * FROM kyc_intake.kyc_form_data WHERE session_id = %s", (sid,))
        form = one(cur)
        cur.execute("""SELECT document_id, doc_type, seq, file_name, mime_type, size_bytes,
                              sha256, uploaded_at
                       FROM kyc_intake.kyc_document WHERE session_id = %s
                       ORDER BY array_position(ARRAY['PAN','AADHAAR','SIGNATURE'], doc_type), seq""",
                    (sid,))
        documents = rows(cur)
        cur.execute("SELECT * FROM kyc_intake.v_latest_ocr WHERE session_id = %s", (sid,))
        latest = {r["doc_type"]: r for r in rows(cur)}
        cur.execute("""SELECT event, actor, detail, at FROM kyc_intake.kyc_session_event
                       WHERE session_id = %s ORDER BY at, event_id""", (sid,))
        events = rows(cur)

    comparison = _compare(form, latest)
    session["outcome"] = _outcome(session["status"], comparison, {d["doc_type"] for d in documents})
    session["flags"] = _flags(comparison)
    # The hashes exist only for comparison; they never leave the server.
    form.pop("aadhaar_hmac", None)
    for r in latest.values():
        r.pop("extracted_aadhaar_hmac", None)
        r.pop("lines", None)            # boxes are evidence, too heavy for the page
    return {"session": session, "form": form, "documents": documents,
            "ocr": latest, "comparison": comparison, "events": events}


@app.post("/api/kyc-sessions", summary="New Account — submit the form and documents, then run OCR")
async def create_kyc_session(request: Request):
    """multipart/form-data: the form fields plus `pan_card`, `aadhaar_card`
    (front and back, up to two files) and `signature`."""
    form = await request.form(max_files=len(DOC_SLOTS) + 2, max_fields=60)
    data, errors = _validate_report(form)
    docs, file_errors = await _read_uploads(form)
    errors.update(file_errors)
    if errors:
        return JSONResponse({"detail": "Please correct the highlighted fields.",
                             "errors": errors}, status_code=422)

    user = request.state.user
    sid = await run_in_threadpool(_insert_session, data, docs, user, _client_ip(request))
    await run_in_threadpool(_run_ocr, sid, user["username"])
    return await run_in_threadpool(_session_detail, sid)


@app.get("/api/kyc-sessions", summary="Onboarding queue — submitted reports with their verification outcome")
def list_kyc_sessions(q: str | None = Query(None, description="matches name, PAN or session id"),
                      outcome: str | None = Query(None, description="one outcome, or 'attention' for "
                                                  "Mismatch + Needs Review + OCR Failed"),
                      limit: int = Query(100, ge=1, le=1000)) -> dict:
    reports = _load_reports(q)
    counts = {o: 0 for o in OUTCOMES}
    for r in reports:
        counts[r["outcome"]] += 1
    if outcome == "attention":
        reports = [r for r in reports if r["outcome"] in ATTENTION]
    elif outcome:
        reports = [r for r in reports if r["outcome"] == outcome]
    for r in reports:
        r.pop("comparison")
    return {"total": len(reports), "counts": counts,
            "attention": sum(counts[o] for o in ATTENTION),
            "returned": min(len(reports), limit), "items": reports[:limit]}


@app.get("/api/kyc-sessions/dashboard", summary="Onboarding dashboard — figures from submitted reports only")
def kyc_dashboard() -> dict:
    reports = _load_reports()
    today = date.today()
    day = lambda r: r["created_at"].astimezone().date()

    outcomes = {o: 0 for o in OUTCOMES}
    for r in reports:
        outcomes[r["outcome"]] += 1
    processed = sum(n for o, n in outcomes.items() if o not in ("Pending", "No Documents"))

    # Which checks fail most — per field, counted once per report.
    fields = {}
    for r in reports:
        for c in r["comparison"]:
            f = fields.setdefault(c["field"], {"field": c["field"], "checked": 0,
                                               "match": 0, "review": 0, "mismatch": 0})
            got = [v for v in (c["pan"], c["aadhaar"])
                   if v in ("Match", "Partial", "Not read", "Mismatch", "OCR failed", "Wrong document")]
            if not got:
                continue
            f["checked"] += 1
            if "Mismatch" in got:
                f["mismatch"] += 1
            elif any(v in ("Partial", "Not read", "OCR failed", "Wrong document") for v in got):
                f["review"] += 1
            else:
                f["match"] += 1

    trend = []
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        trend.append({"date": d, "reports": sum(1 for r in reports if day(r) == d)})

    people = {}
    for r in reports:
        p = people.setdefault(r["created_by_username"], {"name": r["created_by_name"], "reports": 0,
                                                         "verified": 0, "attention": 0})
        p["reports"] += 1
        p["verified"] += r["outcome"] == "Verified"
        p["attention"] += r["outcome"] in ATTENTION

    with db() as cur:
        cur.execute("""
            SELECT doc_type, count(*)::int AS cards,
                   round(avg(mean_confidence), 3) AS avg_confidence,
                   round(avg(duration_ms))::int AS avg_ms,
                   count(*) FILTER (WHERE status IN ('Failed', 'No Text'))::int AS unreadable,
                   count(*) FILTER (WHERE status = 'Wrong Document')::int AS wrong_document
            FROM kyc_intake.v_latest_ocr GROUP BY doc_type ORDER BY doc_type
        """)
        ocr = rows(cur)

    attention = [{k: r[k] for k in ("session_id", "full_name", "pan", "outcome", "flags",
                                    "created_at", "created_by_name")}
                 for r in reports if r["outcome"] in ATTENTION][:8]
    return {
        "totals": {
            "reports": len(reports),
            "today": sum(1 for r in reports if day(r) == today),
            "last_7_days": sum(1 for r in reports if (today - day(r)).days < 7),
            "verified": outcomes["Verified"],
            "processed": processed,
            "attention": sum(outcomes[o] for o in ATTENTION),
            "incomplete": outcomes["Incomplete"] + outcomes["No Documents"],
        },
        "outcomes": [{"outcome": o, "reports": n} for o, n in outcomes.items()],
        "fields": list(fields.values()),
        "trend": trend,
        "by_user": sorted(people.values(), key=lambda p: -p["reports"]),
        "ocr": ocr,
        "attention": attention,
    }


@app.get("/api/kyc-sessions/{session_id}", summary="One submitted report: form, documents, OCR, comparison")
def get_kyc_session(session_id: str) -> dict:
    return _session_detail(session_id)


@app.post("/api/kyc-sessions/{session_id}/ocr", summary="Run OCR again (adds a new attempt)")
def rerun_kyc_ocr(session_id: str, request: Request) -> dict:
    with db() as cur:
        cur.execute("""SELECT status, updated_at > now() - interval '5 minutes' AS recent
                       FROM kyc_intake.kyc_session WHERE session_id = %s""", (session_id,))
        s = one(cur)
    if s is None:
        raise HTTPException(404, f"no KYC session {session_id}")
    if s["status"] == "OCR Running" and s["recent"]:
        raise HTTPException(409, "OCR is already running for this session")
    actor = request.state.user["username"]
    with db(readonly=False) as cur:
        _event(cur, session_id, "ocr_rerun_requested", actor)
    _run_ocr(session_id, actor)
    return _session_detail(session_id)


@app.get("/api/kyc-sessions/{session_id}/documents/{document_id}",
         summary="The uploaded file itself", response_class=Response)
def get_kyc_document(session_id: str, document_id: int, download: bool = False) -> Response:
    with db() as cur:
        cur.execute("""SELECT file_name, mime_type, content FROM kyc_intake.kyc_document
                       WHERE session_id = %s AND document_id = %s""", (session_id, document_id))
        doc = one(cur)
    if doc is None:
        raise HTTPException(404, "no such document")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", doc["file_name"]) or "document"
    headers = {
        "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{safe}"',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
    }
    if doc["mime_type"].startswith("image/"):
        headers["Content-Security-Policy"] = "default-src 'none'"
    return Response(bytes(doc["content"]), media_type=doc["mime_type"], headers=headers)


# ==========================================================================
#  Static front end
# ==========================================================================
app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")

# The front end has no build step and no fingerprinted filenames, so an edited
# file must never be served from a stale browser cache. Revalidate every time.
NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


@app.get("/", include_in_schema=False)
async def index(request: Request):
    """The console itself — anyone without a live session goes to sign-in."""
    user = await run_in_threadpool(lookup_session, request.cookies.get(auth.COOKIE_NAME))
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return FileResponse(WEB / "index.html", headers=NO_CACHE)


@app.get("/login", include_in_schema=False)
async def login_page(request: Request):
    """Sign-in page. Already signed in? Go straight through."""
    user = await run_in_threadpool(lookup_session, request.cookies.get(auth.COOKIE_NAME))
    if user is not None:
        return RedirectResponse("/", status_code=303)
    return FileResponse(WEB / "login.html", headers=NO_CACHE)


@app.get("/login.js", include_in_schema=False)
def loginjs() -> FileResponse:
    return FileResponse(WEB / "login.js", media_type="application/javascript",
                        headers=NO_CACHE)


@app.get("/app.js", include_in_schema=False)
def appjs() -> FileResponse:
    return FileResponse(WEB / "app.js", media_type="application/javascript",
                        headers=NO_CACHE)


@app.get("/styles.css", include_in_schema=False)
def styles() -> FileResponse:
    return FileResponse(WEB / "styles.css", media_type="text/css",
                        headers=NO_CACHE)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.app:app", host=APP_HOST, port=APP_PORT, reload=False)
