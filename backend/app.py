"""
Covasant · WealthGate — Onboarding & KYC web application.

Every screen in the UI is driven by live queries against the `wealth_kyc`
PostgreSQL database; nothing is hard-coded in the front end.

Run:  python backend/app.py         (or: uvicorn backend.app:app --reload)
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

try:  # works both as `python backend/app.py` and `uvicorn backend.app:app`
    from backend import auth
    from backend.config import (APP_HOST, APP_PORT, COOKIE_SECURE, app_dsn,
                                describe_target)
except ModuleNotFoundError:  # pragma: no cover
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from backend import auth
    from backend.config import (APP_HOST, APP_PORT, COOKIE_SECURE, app_dsn,
                                describe_target)

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
