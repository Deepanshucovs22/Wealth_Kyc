-- =====================================================================
--  Covasant · WealthGate — "New Account" KYC intake sessions
--
--  Schema `kyc_intake`, deliberately separate from `kyc`: the Excel loader
--  drops and rebuilds `kyc` on every run, and that must never delete the
--  applications people have actually submitted.
--
--  Idempotent — the application applies this file at every start.
--
--  One KYC session links everything captured for one applicant:
--
--      kyc_session ──┬── kyc_form_data      what the user typed   (1 : 1)
--                    ├── kyc_document       uploaded files        (1 : n)
--                    ├── kyc_ocr_result     what OCR read         (1 : n)
--                    └── kyc_session_event  audit trail           (1 : n)
--
--  User-entered data and OCR-extracted data live in different tables and
--  are only ever compared at read time, so neither can overwrite the other.
--
--  Aadhaar: the full 12-digit number is never stored in clear (Aadhaar Act
--  s.29 / UIDAI storage rules). Tables keep the masked form plus an
--  HMAC-SHA256 under AADHAAR_HMAC_KEY, enough to compare form vs. card.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS kyc_intake;

-- ---------------------------------------------------------------------
-- The session — one per report submitted from the New Account screen
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kyc_intake.kyc_session (
    session_id          text PRIMARY KEY
                        CHECK (session_id ~ '^KYC-[0-9]{8}-[0-9A-F]{6}$'),
    status              text NOT NULL DEFAULT 'Submitted'
                        CHECK (status IN ('Submitted', 'OCR Running', 'OCR Complete',
                                          'OCR Failed', 'No ID Documents')),
    -- No FK to kyc.app_user: that schema is rebuilt by the loader and its
    -- serial ids are reissued. Who submitted is kept as a snapshot instead.
    created_by_user_id  integer NOT NULL,
    created_by_username text    NOT NULL,
    created_by_name     text    NOT NULL,
    client_ip           text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    ocr_completed_at    timestamptz
);
CREATE INDEX IF NOT EXISTS idx_intake_session_created
    ON kyc_intake.kyc_session (created_at DESC);

-- ---------------------------------------------------------------------
-- What the user entered on the form (never touched by OCR)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kyc_intake.kyc_form_data (
    session_id            text PRIMARY KEY
                          REFERENCES kyc_intake.kyc_session (session_id) ON DELETE CASCADE,

    -- mandatory
    full_name             text     NOT NULL CHECK (length(btrim(full_name)) BETWEEN 2 AND 150),
    date_of_birth         date     NOT NULL CHECK (date_of_birth >= DATE '1900-01-01'),
    pan                   char(10) NOT NULL CHECK (pan ~ '^[A-Z]{5}[0-9]{4}[A-Z]$'),
    aadhaar_masked        text     NOT NULL CHECK (aadhaar_masked ~ '^XXXX XXXX [0-9]{4}$'),
    aadhaar_hmac          char(64) NOT NULL,
    address_line1         text     NOT NULL CHECK (length(btrim(address_line1)) >= 3),
    address_line2         text,
    city                  text     NOT NULL,
    state                 text     NOT NULL,
    pincode               char(6)  NOT NULL CHECK (pincode ~ '^[1-9][0-9]{5}$'),

    -- optional — carried over from the bank's internet-banking request form
    customer_id           text,
    mobile                text CHECK (mobile ~ '^[6-9][0-9]{9}$'),
    email                 text CHECK (email ~* '^[^@\s]+@[^@\s]+\.[^@\s]+$'),
    request_type          text CHECK (request_type IN ('New User', 'Modification',
                                                       'Deletion', 'Duplicate Password')),
    existing_user_id      text,
    preferred_user_id     text,
    limit_per_day         numeric(15,2) CHECK (limit_per_day >= 0),
    limit_per_transaction numeric(15,2) CHECK (limit_per_transaction >= 0),
    approvers_required    smallint CHECK (approvers_required BETWEEN 0 AND 2),
    transaction_type      text CHECK (transaction_type IN ('A', 'B', 'C', 'TFConnect')),

    submitted_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT form_txn_within_day CHECK (
        limit_per_day IS NULL OR limit_per_transaction IS NULL
        OR limit_per_transaction <= limit_per_day)
);
CREATE INDEX IF NOT EXISTS idx_intake_form_pan ON kyc_intake.kyc_form_data (pan);

-- ---------------------------------------------------------------------
-- Uploaded files. Bytes live in the database so a session is one
-- consistent unit (one transaction, one backup, one cascade delete).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kyc_intake.kyc_document (
    document_id  bigserial PRIMARY KEY,
    session_id   text NOT NULL
                 REFERENCES kyc_intake.kyc_session (session_id) ON DELETE CASCADE,
    doc_type     text NOT NULL CHECK (doc_type IN ('PAN', 'AADHAAR', 'SIGNATURE')),
    seq          smallint NOT NULL DEFAULT 1 CHECK (seq BETWEEN 1 AND 2),  -- Aadhaar front / back
    file_name    text NOT NULL,
    mime_type    text NOT NULL CHECK (mime_type IN ('image/jpeg', 'image/png',
                                                    'image/webp', 'application/pdf')),
    size_bytes   integer  NOT NULL CHECK (size_bytes > 0),
    sha256       char(64) NOT NULL,
    content      bytea    NOT NULL,
    uploaded_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, doc_type, seq)
);
-- Images and PDFs are already compressed; skip TOAST's pointless re-compression.
ALTER TABLE kyc_intake.kyc_document ALTER COLUMN content SET STORAGE EXTERNAL;

-- ---------------------------------------------------------------------
-- What OCR read — one row per document type per attempt. Re-running OCR
-- adds an attempt rather than overwriting history.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kyc_intake.kyc_ocr_result (
    ocr_id           bigserial PRIMARY KEY,
    session_id       text NOT NULL
                     REFERENCES kyc_intake.kyc_session (session_id) ON DELETE CASCADE,
    doc_type         text NOT NULL CHECK (doc_type IN ('PAN', 'AADHAAR')),
    attempt          smallint NOT NULL DEFAULT 1,
    status           text NOT NULL,      -- see kyc_ocr_result_status_check below
    engine           text NOT NULL,

    -- extracted fields (NULL = not found on the card)
    extracted_name     text,
    extracted_dob      date,
    extracted_yob      smallint,          -- older Aadhaar cards print only the year
    extracted_pan      char(10),
    extracted_aadhaar_masked text,
    extracted_aadhaar_hmac   char(64),
    extracted_address  text,
    field_confidence   jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- evidence (Aadhaar numbers are masked before storage)
    raw_text         text,
    lines            jsonb NOT NULL DEFAULT '[]'::jsonb,
    mean_confidence  numeric(4,3),
    pages            smallint,
    error            text,
    duration_ms      integer,
    processed_at     timestamptz NOT NULL DEFAULT now(),

    UNIQUE (session_id, doc_type, attempt)
);
-- Re-stated on every start so a database created by an earlier version picks
-- up new values. 'Wrong Document': the upload is not the card it claims to be.
ALTER TABLE kyc_intake.kyc_ocr_result DROP CONSTRAINT IF EXISTS kyc_ocr_result_status_check;
ALTER TABLE kyc_intake.kyc_ocr_result ADD CONSTRAINT kyc_ocr_result_status_check
    CHECK (status IN ('Success', 'Partial', 'No Text', 'Failed', 'Wrong Document'));

-- ---------------------------------------------------------------------
-- Audit trail
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kyc_intake.kyc_session_event (
    event_id   bigserial PRIMARY KEY,
    session_id text NOT NULL
               REFERENCES kyc_intake.kyc_session (session_id) ON DELETE CASCADE,
    event      text NOT NULL,
    actor      text NOT NULL,
    detail     jsonb,
    at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_intake_event_session
    ON kyc_intake.kyc_session_event (session_id, at);

-- ---------------------------------------------------------------------
-- Latest OCR attempt per session and document type
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW kyc_intake.v_latest_ocr AS
SELECT DISTINCT ON (session_id, doc_type) *
FROM kyc_intake.kyc_ocr_result
ORDER BY session_id, doc_type, attempt DESC;
