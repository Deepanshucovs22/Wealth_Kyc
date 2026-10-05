"""
Covasant · WealthGate KYC — Excel ➜ PostgreSQL loader.

Creates the `wealth_kyc` database, applies db/schema.sql, then bulk-loads every
sheet of wealth_kyc_synthetic_data.xlsx plus the reference data and API
catalogue extracted from the source wireframe.

Usage:
    python backend/etl/load_excel_to_pg.py                 # create + load
    python backend/etl/load_excel_to_pg.py --verify-only   # just report row counts
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import openpyxl
import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values

BACKEND = Path(__file__).resolve().parents[1]   # .../backend
PROJECT = Path(__file__).resolve().parents[2]   # project root
sys.path.insert(0, str(PROJECT))
from backend.auth import hash_password  # noqa: E402
from backend.config import DB_NAME, admin_dsn, app_dsn, describe_target  # noqa: E402

DEFAULT_SEED_PASSWORD = "Covasant@2026"

XLSX = BACKEND / "data" / "wealth_kyc_synthetic_data.xlsx"
SCHEMA_SQL = BACKEND / "db" / "schema.sql"
REF_JSON = BACKEND / "data" / "reference_data.json"
EPS_JSON = BACKEND / "data" / "api_endpoints.json"

# sheet name -> (table, ordered column list). Column names match the sheet
# headers one-for-one except where noted.
SHEETS: list[tuple[str, str, list[str]]] = [
    ("clients", "clients", [
        "client_id", "application_id", "client_type", "name", "first_name", "last_name",
        "gender", "date_of_birth_or_incorporation", "pan", "pan_aadhaar_linked",
        "aadhaar_masked", "ckyc_number", "kra_name", "kra_status", "residential_status",
        "nationality", "tax_residency_country", "email", "mobile", "occupation",
        "annual_income_band", "net_worth_inr", "source_of_wealth", "pep_flag",
        "address_line1", "address_line2", "city", "state", "pincode", "country",
        "correspondence_same_as_permanent", "rm_id", "rm_name", "branch", "channel",
        "onboarding_stage", "created_at", "last_updated_at", "risk_category",
        "aml_risk_rating", "due_diligence_level", "periodic_kyc_review_due", "ipv_mode",
        "ipv_status", "liveness_score", "esign_mode", "esign_status", "nomination_opt_out",
    ]),
    ("accounts", "accounts", [
        "account_id", "client_id", "product_code", "product_name", "strategy",
        "holding_pattern", "commitment_or_corpus_inr", "funding_mode", "fee_structure",
        "custodian", "account_status", "activation_date", "ucc_code",
    ]),
    ("kyc_documents", "kyc_documents", [
        "document_id", "client_id", "document_type", "source", "ocr_confidence",
        "status", "rejection_reason", "expiry_date", "uploaded_at",
    ]),
    ("fatca_crs", "fatca_crs", [
        "client_id", "us_person", "tax_residencies", "tin_or_pan", "place_of_birth",
        "giin", "fatca_entity_classification", "self_cert_date", "declaration_status",
    ]),
    ("bank_accounts", "bank_accounts", [
        "bank_link_id", "client_id", "bank_name", "ifsc", "account_number_masked",
        "account_type", "is_primary", "penny_drop_status", "name_match_score", "verified_at",
    ]),
    ("demat_accounts", "demat_accounts", [
        "client_id", "depository", "dp_name", "dp_id", "client_id_at_dp", "poa_ddpi", "status",
    ]),
    ("nominees", "nominees", [
        "nominee_id", "client_id", "nominee_name", "relationship", "share_pct",
        "is_minor", "id_type",
    ]),
    ("ubo_details", "ubo_details", [
        "ubo_id", "client_id", "ubo_name", "pan", "holding_pct", "role", "pep_flag", "nationality",
    ]),
    ("risk_profiles", "risk_profiles", [
        "client_id", "questionnaire_version", "completed_on", "investment_horizon",
        "investment_experience", "loss_tolerance", "liquidity_need", "score",
        "risk_category", "suitability_ok",
    ]),
    ("aml_screening", "aml_screening", [
        "screening_id", "client_id", "list_name", "result", "match_score",
        "matched_name", "disposition", "screened_at", "reviewer",
    ]),
    ("workflow_events", "workflow_events", [
        "event_id", "client_id", "stage", "action", "actor", "timestamp", "remarks",
    ]),
    ("api_call_log", "api_call_log", [
        "request_id", "client_id", "endpoint", "downstream", "http_status",
        "latency_ms", "timestamp",
    ]),
]


def clean(v):
    """Blank cells and whitespace-only strings become SQL NULL."""
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def read_sheet(wb, sheet: str, columns: list[str]) -> list[tuple]:
    ws = wb[sheet]
    it = ws.iter_rows(values_only=True)
    header = [str(h).strip() if h is not None else "" for h in next(it)]
    missing = [c for c in columns if c not in header]
    if missing:
        raise SystemExit(f"[{sheet}] sheet is missing expected columns: {missing}")
    idx = [header.index(c) for c in columns]
    rows = []
    for raw in it:
        if raw is None or all(c is None for c in raw):
            continue  # skip fully blank trailing rows
        rows.append(tuple(clean(raw[i]) if i < len(raw) else None for i in idx))
    return rows


def insert(cur, table: str, columns: list[str], rows: list[tuple]) -> int:
    if not rows:
        return 0
    stmt = sql.SQL("INSERT INTO kyc.{} ({}) VALUES %s").format(
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(c) for c in columns),
    )
    execute_values(cur, stmt, rows, page_size=1000)
    return len(rows)


def ensure_database() -> None:
    """CREATE DATABASE wealth_kyc if it is not already there."""
    conn = psycopg2.connect(admin_dsn())
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
            if cur.fetchone():
                print(f"  database {DB_NAME!r} already exists — reusing it")
            else:
                cur.execute(
                    sql.SQL("CREATE DATABASE {} ENCODING 'UTF8'").format(sql.Identifier(DB_NAME))
                )
                print(f"  created database {DB_NAME!r}")
    finally:
        conn.close()


def load_reference(cur) -> None:
    ref = json.loads(REF_JSON.read_text(encoding="utf-8"))

    insert(cur, "ref_client_type", ["name", "sort_order"],
           [(n, i) for i, n in enumerate(ref["client_types"])])
    insert(cur, "ref_onboarding_stage", ["name", "sort_order"],
           [(n, i) for i, n in enumerate(ref["onboarding_stages"])])
    insert(cur, "ref_kra_status", ["code", "description"],
           list(ref["kra_status_codes"].items()))
    insert(cur, "ref_product", ["code", "name", "min_investment_inr"],
           [(p["code"], p["name"], p["min_investment_inr"]) for p in ref["products"]])
    insert(cur, "ref_risk_category", ["name", "sort_order"],
           [(n, i) for i, n in enumerate(ref["risk_categories"])])
    insert(cur, "ref_screening_list", ["name", "sort_order"],
           [(n, i) for i, n in enumerate(ref["screening_lists"])])
    insert(cur, "relationship_manager", ["rm_id", "name", "branch"],
           [(r["rm_id"], r["name"], r["branch"]) for r in ref["relationship_managers"]])
    print(f"  reference data: {len(ref['client_types'])} client types, "
          f"{len(ref['products'])} products, {len(ref['relationship_managers'])} RMs, "
          f"{len(ref['screening_lists'])} screening lists")


def load_users(cur) -> int:
    """Create one console account per relationship manager, plus compliance
    and administrator logins. Every account gets the same initial password,
    which is meant to be changed — override it with SEED_PASSWORD."""
    password = os.environ.get("SEED_PASSWORD", DEFAULT_SEED_PASSWORD)

    cur.execute("SELECT rm_id, name, branch FROM relationship_manager ORDER BY rm_id")
    rms = cur.fetchall()

    people: list[tuple[str, str, str, str | None, str | None]] = []
    for rm_id, name, branch in rms:
        username = name.lower().replace(" ", ".").replace("'", "")
        people.append((username, name, "Relationship Manager", rm_id, branch))

    people += [
        ("vivek.trivedi", "Vivek Trivedi", "Compliance Officer", None, "Mumbai"),
        ("asha.nair", "Asha Nair", "Compliance Officer", None, "Mumbai"),
        ("admin", "System Administrator", "Administrator", None, None),
    ]

    rows = [
        (username, full_name, f"{username}@covasant.com",
         hash_password(password), role, rm_id, branch)
        for username, full_name, role, rm_id, branch in people
    ]
    n = insert(cur, "app_user",
               ["username", "full_name", "email", "password_hash",
                "role", "rm_id", "branch"], rows)
    print(f"  app_user: {n} accounts seeded "
          f"({len(rms)} RM · 2 compliance · 1 administrator)")
    print(f"    initial password for every account: {password}")
    return n


def load_api_catalogue(cur) -> int:
    eps = json.loads(EPS_JSON.read_text(encoding="utf-8"))
    rows = [
        (
            i + 1, e["group"], e["method"], e["path"], e["summary"], e["description"],
            e["downstream"], e["success_status"], json.dumps(e.get("params") or []),
            json.dumps(e["request"]) if e.get("request") is not None else None,
            json.dumps(e["response"]) if e.get("response") is not None else None,
        )
        for i, e in enumerate(eps)
    ]
    return insert(cur, "api_catalogue", [
        "seq", "api_group", "method", "path", "summary", "description",
        "downstream", "success_status", "params", "sample_request", "sample_response",
    ], rows)


def check_integrity(cur) -> None:
    """Fail loudly if the loaded data violates an expectation the UI relies on."""
    problems: list[str] = []

    cur.execute("""
        SELECT c.client_id, count(n.nominee_id), coalesce(sum(n.share_pct), 0)
        FROM clients c JOIN nominees n USING (client_id)
        GROUP BY c.client_id HAVING coalesce(sum(n.share_pct), 0) <> 100
    """)
    bad = cur.fetchall()
    if bad:
        problems.append(f"{len(bad)} client(s) have nominee shares not summing to 100: "
                        f"{[b[0] for b in bad][:5]}")

    cur.execute("SELECT count(*) FROM clients c LEFT JOIN risk_profiles r USING (client_id) "
                "WHERE r.client_id IS NULL")
    n = cur.fetchone()[0]
    if n:
        problems.append(f"{n} client(s) have no risk profile")

    cur.execute("SELECT count(*) FROM clients c LEFT JOIN fatca_crs f USING (client_id) "
                "WHERE f.client_id IS NULL")
    n = cur.fetchone()[0]
    if n:
        problems.append(f"{n} client(s) have no FATCA/CRS record")

    if problems:
        print("\n  data quality notes:")
        for p in problems:
            print(f"    ! {p}")
    else:
        print("  integrity checks: all passed")


def report(cur) -> None:
    cur.execute("""
        SELECT relname, n_live_tup FROM pg_stat_user_tables
        WHERE schemaname = 'kyc' ORDER BY relname
    """)
    print("\n  Row counts")
    print("  " + "-" * 44)
    for name, _ in cur.fetchall():
        cur.execute(sql.SQL("SELECT count(*) FROM kyc.{}").format(sql.Identifier(name)))
        print(f"    {name:<28} {cur.fetchone()[0]:>8,}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verify-only", action="store_true",
                    help="skip loading; just print row counts for the existing database")
    args = ap.parse_args()

    print(f"\nCovasant · WealthGate KYC loader")
    print(f"  target: {describe_target()}")

    if args.verify_only:
        with psycopg2.connect(app_dsn()) as conn, conn.cursor() as cur:
            cur.execute("SET search_path TO kyc, public")
            report(cur)
            check_integrity(cur)
        return

    if not XLSX.exists():
        raise SystemExit(f"workbook not found: {XLSX}")

    print("\n[1/4] ensuring database exists")
    ensure_database()

    print("[2/4] applying schema (db/schema.sql)")
    conn = psycopg2.connect(app_dsn())
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
            cur.execute("SET search_path TO kyc, public")

            print("[3/4] loading reference data + API catalogue")
            load_reference(cur)
            n = load_api_catalogue(cur)
            print(f"  api_catalogue: {n} endpoints")
            load_users(cur)

            print(f"[4/4] loading workbook sheets from {XLSX.name}")
            wb = openpyxl.load_workbook(XLSX, read_only=True, data_only=True)
            total = 0
            for sheet, table, cols in SHEETS:
                rows = read_sheet(wb, sheet, cols)
                total += insert(cur, table, cols, rows)
                print(f"    {sheet:<20} -> kyc.{table:<20} {len(rows):>6,} rows")
            wb.close()

            meta = json.loads(REF_JSON.read_text(encoding="utf-8"))  # noqa: F841 (shape check)
            cur.execute(
                "INSERT INTO dataset_meta (generated_on, disclaimer, source_file) "
                "VALUES (%s, %s, %s)",
                ("2026-10-01",
                 "Synthetic data for demo/testing only. All identities are fictitious.",
                 XLSX.name),
            )

            cur.execute("ANALYZE")
            check_integrity(cur)
            report(cur)
        conn.commit()
        print(f"\n  committed — {total:,} data rows loaded into {DB_NAME}.kyc\n")
    except Exception:
        conn.rollback()
        print("\n  ROLLED BACK — nothing was written.\n")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
