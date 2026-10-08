# Covasant · WealthGate — Onboarding & KYC

A PostgreSQL-backed web application for wealth-management account opening
(PMS / AIF / Advisory), rebuilt from the `wealth_kyc_wireframe.html` mock and
themed with the Covasant design system.

Every screen reads from the database. Nothing is hard-coded in the front end:
the 240 clients, 295 product subscriptions, 2,068 KYC documents, 1,440 screening
results and the rest are loaded from `wealth_kyc_synthetic_data.xlsx` into
PostgreSQL, and the UI pre-fills itself from live SQL queries.

> All data is **synthetic**. PAN, CKYC, bank and demat numbers are randomly
> generated in valid formats; Aadhaar is masked. No real person or entity.

---

## Quick start

```powershell
# 1. install dependencies (once)
pip install -r backend/requirements.txt

# 2. create the database and load the workbook (once)
python backend/etl/load_excel_to_pg.py

# 3. run the app
python backend/app.py
```

Then open **http://127.0.0.1:8010** and sign in.

The interactive API docs are at **http://127.0.0.1:8010/docs**.

### Sign-in accounts

The loader creates nine console accounts from the relationship-manager roster.
Every one starts with the password **`Covasant@2026`**.

| Username | Name | Role | Branch |
|---|---|---|---|
| `kavita.rao` | Kavita Rao | Relationship Manager | Mumbai |
| `arvind.menon` | Arvind Menon | Relationship Manager | Bengaluru |
| `sonal.mehta` | Sonal Mehta | Relationship Manager | New Delhi |
| `rakesh.iyer` | Rakesh Iyer | Relationship Manager | Chennai |
| `neeraj.kapoor` | Neeraj Kapoor | Relationship Manager | Gurugram |
| `pallavi.shah` | Pallavi Shah | Relationship Manager | Ahmedabad |
| `vivek.trivedi` | Vivek Trivedi | Compliance Officer | Mumbai |
| `asha.nair` | Asha Nair | Compliance Officer | Mumbai |
| `admin` | System Administrator | Administrator | — |

Set a different initial password with `SEED_PASSWORD` before loading:

```powershell
$env:SEED_PASSWORD = 'something-else'; python backend/etl/load_excel_to_pg.py
```

**How the authentication works**

- Passwords are stored as **scrypt** derived keys (`n=16384, r=8, p=1`) with a
  per-user random salt — never in plain text, and no extra dependency.
- Sessions are **server-side**. The browser holds an opaque random token in an
  `HttpOnly`, `SameSite=Lax` cookie; the database stores only its SHA-256. So
  signing out genuinely revokes access, and the session table cannot be
  replayed if it leaks.
- Sessions last 8 hours. Expired ones are swept at startup.
- Five failed attempts lock an account for 15 minutes.
- Every sign-in, sign-out, failure and lockout is written to `auth_event`.
- Unknown usernames still run a hash, so response timing does not reveal
  which accounts exist.
- Every `/api` route except `/api/health` and the sign-in routes returns
  **401** without a live session, and the browser is redirected to `/login`.

Serving this over HTTPS? Set `COOKIE_SECURE=true` in `.env`.

### On Windows you can just run

```powershell
.\start.ps1          # loads the DB if needed, then starts the server
.\start.ps1 -Reload  # with auto-reload for development
```

---

## Configuration

All settings live in `.env` (not committed):

| Variable | Default | Meaning |
|---|---|---|
| `PGHOST` / `PGPORT` | `localhost` / `5432` | PostgreSQL server |
| `PGUSER` / `PGPASSWORD` | `admin` | credentials |
| `PGDATABASE` | `wealth_kyc` | **its own database** — your existing `prms` database is never touched |
| `PGMAINTENANCE` | `postgres` | used only to issue `CREATE DATABASE` |
| `APP_PORT` | `8010` | chosen so it does not collide with the PRMS app on 8000 |
| `COOKIE_SECURE` | `false` | set to `true` when serving over HTTPS |
| `SEED_PASSWORD` | `Covasant@2026` | initial password for every seeded account |
| `AADHAAR_HMAC_KEY` | *(dev key)* | secret for hashing Aadhaar numbers — set a long random value and keep it stable |
| `MAX_UPLOAD_MB` | `10` | per-file limit for New Account uploads |

---

## How it is put together

```
covasant-wealth-kyc/
├─ backend/                             Python · FastAPI · PostgreSQL
│   ├─ app.py                           REST API + static hosting (all endpoints)
│   ├─ auth.py                          scrypt hashing, session tokens
│   ├─ ocr.py                           PAN / Aadhaar OCR (RapidOCR) + field parsers
│   ├─ config.py                        reads ../.env
│   ├─ requirements.txt
│   ├─ db/
│   │   ├─ schema.sql                   24 tables, 4 views, FKs + indexes
│   │   └─ kyc_intake.sql               New Account tables (applied at app start)
│   ├─ etl/
│   │   └─ load_excel_to_pg.py          Excel ➜ PostgreSQL loader
│   └─ data/
│       ├─ wealth_kyc_synthetic_data.xlsx   source workbook (13 sheets)
│       ├─ wealth_kyc_openapi.yaml          full API spec
│       ├─ reference_data.json              lookups extracted from the wireframe
│       └─ api_endpoints.json               35-endpoint catalogue
│
├─ frontend/                            static · no build step
│   ├─ index.html                       console shell
│   ├─ app.js                           all seven screens
│   ├─ login.html                       sign-in page
│   ├─ login.js                         sign-in behaviour
│   ├─ styles.css                       Covasant theme (light + dark)
│   └─ assets/covasant-logo.svg
│
├─ .env                                 database credentials + port
├─ start.ps1                            launcher
└─ README.md
```

The front end is plain HTML/CSS/JS with no build step — `backend/app.py` serves
`frontend/` directly, so editing a file and refreshing the browser is enough.

### Database

Schema `kyc` inside database `wealth_kyc`:

| Table | Rows | |
|---|---:|---|
| `clients` | 240 | master profile, KRA/CKYC, AML rating, IPV & eSign status |
| `accounts` | 295 | PMS / AIF / Advisory subscriptions |
| `kyc_documents` | 2,068 | checklist with source, OCR confidence, status |
| `aml_screening` | 1,440 | UN, OFAC, MHA, SEBI, PEP, adverse media |
| `workflow_events` | 1,489 | stage-transition audit trail |
| `api_call_log` | 806 | downstream latency / status log |
| `bank_accounts` | 291 | penny-drop verification, name-match score |
| `fatca_crs`, `demat_accounts`, `risk_profiles` | 240 each | one per client |
| `nominees` | 220 | shares sum to 100 % per client |
| `ubo_details` | 139 | for HUF / company / LLP / trust |
| `api_catalogue` | 35 | drives the API Explorer screen |
| `app_user` | 9 | console logins — scrypt hashes, roles, lockout state |
| `app_session` | — | live sessions, keyed by SHA-256 of the cookie token |
| `auth_event` | — | sign-in / sign-out / failure audit trail |
| 7 × `ref_*` / `relationship_manager` | — | lookups |

Views `v_dashboard_kpis`, `v_pipeline_by_stage`, `v_rm_performance` and
`v_screening_hits` back the aggregate endpoints, so the dashboard is one query
rather than a client-side scan.

Referential integrity is enforced — `client_type`, `onboarding_stage`,
`kra_status`, `risk_category`, `product_code`, `list_name` and `rm_id` are all
foreign keys, and every child row cascades from `clients`.

### API

| Endpoint | Purpose |
|---|---|
| `GET /api/reference` | dropdown values, products, RMs, dataset provenance |
| `GET /api/dashboard` | KPIs, pipeline, RM conversion, turnaround trend |
| `GET /api/applications` | onboarding queue — filtered **server-side** by stage, type, free text |
| `GET /api/clients/{id}/kyc-summary` | the whole 360 bundle in one round trip |
| `GET /api/kyc/fetch/{pan}` | KRA + CKYC lookup, drives the wizard's step 2 |
| `GET /api/sample-pans` | real PANs per client type, for demo input |
| `GET /api/screening/alerts` | non-clean screening results (`?open_only=true`) |
| `GET /api/reviews/due` | periodic review queue + expiring documents |
| `GET /api/endpoints` | the 35-endpoint catalogue |
| `GET /api/integration/health` | latency and status mix per downstream |
| `GET /api/health` | liveness + database reachability |
| `POST /api/kyc-sessions` | **New Account** — multipart form + `pan_card`, `aadhaar_card` (≤ 2), `signature`; creates the session and runs OCR |
| `GET /api/kyc-sessions` | onboarding queue: reports with outcome and flagged fields (`?q=`, `?outcome=` or `attention`) |
| `GET /api/kyc-sessions/dashboard` | dashboard figures, from submitted reports only |
| `GET /api/kyc-sessions/{id}` | form, documents, latest OCR per card, form-vs-OCR comparison, activity |
| `POST /api/kyc-sessions/{id}/ocr` | run OCR again — adds an attempt, keeps the old ones |
| `GET /api/kyc-sessions/{id}/documents/{doc}` | the uploaded file (`?download=true` to save) |
| `POST /api/auth/login` | sign in, opens a session and sets the cookie |
| `POST /api/auth/logout` | sign out, deletes the session server-side |
| `GET /api/auth/me` | the currently signed-in user |

### Screens

0. **Sign in** — `/login`, with the Covasant brand panel and the form on the right
1. **Dashboard** — built only from reports submitted through New Account: reports submitted, verified,
   needs attention, missing documents; verification outcome; which checks fail most;
   reports per day; who submitted what; OCR reading quality
2. **Onboarding Queue** — every submitted report with its verification outcome
   (Verified, Needs Review, Mismatch, Wrong Document, OCR Failed, Incomplete),
   the fields at issue, filter chips and search; the sidebar badge counts reports
   needing attention
3. **New Account** — KYC intake form modelled on the bank's internet-banking
   request form (customer, identity, address, request type, transaction limits)
   with PAN, Aadhaar and signature uploads. Submitting creates a session id such
   as `KYC-20261006-7F3A9C`, runs OCR and opens the report, which compares
   what was typed with what was read off each card

The menu has only these three screens and stays fixed while the page scrolls.
The sample-data endpoints (`/api/applications`, `/api/clients/…`, `/api/screening/…`,
`/api/reviews/…`) are still served by the API but no longer appear in the UI.

The signed-in user appears top right — name, role, branch and last sign-in,
with **Sign out** in the dropdown.

## New Account — KYC intake and OCR

### Tables (schema `kyc_intake`)

| Table | Holds |
|---|---|
| `kyc_session` | one row per report: session id, status, who submitted it, when |
| `kyc_form_data` | **what the user typed** — 1 : 1 with the session |
| `kyc_document` | the uploaded files (bytes, type, size, SHA-256) — PAN, Aadhaar front / back, signature |
| `kyc_ocr_result` | **what OCR read** — one row per card per attempt: extracted fields, per-field confidence, raw text, engine, timing, errors |
| `kyc_session_event` | audit trail: submitted, OCR completed / failed, re-run |
| `v_latest_ocr` | view: the newest attempt per session and card |

Typed and read values never share a table, and neither is overwritten by the
other. The comparison on the report page is computed when it is opened.

The schema is separate from `kyc` on purpose: the Excel loader drops and
rebuilds `kyc`, and that must never delete submitted reports. The app creates
`kyc_intake` itself at start-up, so an existing database needs no reload.

### Aadhaar handling

The full 12-digit number is never stored. The form keeps `XXXX XXXX 1234` plus an
HMAC-SHA256 under `AADHAAR_HMAC_KEY`. That is enough to tell whether the number on the
card matches the number on the form. OCR text is masked the same way before it
is saved. The checksum (Verhoeff) is checked in the browser and on the server.
The uploaded card images themselves still show the number. Mask them, or keep
them in an Aadhaar Data Vault, before using this with real customers.

### OCR

`backend/ocr.py` uses [RapidOCR](https://github.com/RapidAI/RapidOCR), which runs
the PaddleOCR PP-OCRv4 models on onnxruntime. The models ship inside the pip
wheel, so it works offline and needs no Tesseract install. PDFs are rasterised
with PyMuPDF.

- **PAN:** number (format, holder-type letter and common O/0, I/1 and S/5 fixes), name
  (labelled and older unlabelled layouts), date of birth, and address if one
  is printed
- **Aadhaar:** number (must pass the checksum), name (the English line above
  the date of birth), date of birth or year of birth, and the address block up to
  the PIN code; front and back can be uploaded as two files or one PDF
- Expect about 5–10 s per card on a laptop CPU. A password-protected e-Aadhaar PDF
  is rejected with a message asking for an unlocked copy.

## Theme

Taken from the Covasant design system:

| Token | Light | Dark |
|---|---|---|
| brand / sidebar | `#122572` | `#0B0E14` |
| accent (`--volt`) | `#2048D5` | `#C9F24B` |
| foreground | `#19214F` | `#F3F4F1` |
| surface | `#FFFFFF` / `#F7F8FC` | `#0D0F13` / `#010611` |
| ok / hold / deny | `#0F7A55` / `#9A6410` / `#B8372B` | `#5FC08A` / `#E3B341` / `#E0736A` |

Use the ◐ button in the header to switch; the choice is remembered per browser.

## Reloading the data

`python backend/etl/load_excel_to_pg.py` drops and rebuilds the `kyc` schema, so it is
safe to re-run after editing the workbook. The whole load runs in one
transaction — if anything fails, nothing is written.

To check what is in the database without reloading:

```powershell
python backend/etl/load_excel_to_pg.py --verify-only
```
