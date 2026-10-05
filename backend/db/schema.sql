-- =====================================================================
--  Covasant · WealthGate Onboarding & KYC  —  database schema
--  Target: PostgreSQL 14+   Database: wealth_kyc   Schema: kyc
--  Source of record: wealth_kyc_synthetic_data.xlsx (synthetic data)
-- =====================================================================

DROP SCHEMA IF EXISTS kyc CASCADE;
CREATE SCHEMA kyc;

SET search_path TO kyc, public;

-- ---------------------------------------------------------------------
-- Dataset provenance
-- ---------------------------------------------------------------------
CREATE TABLE dataset_meta (
    id           smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    generated_on date        NOT NULL,
    disclaimer   text        NOT NULL,
    source_file  text        NOT NULL,
    loaded_at    timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Reference data
-- ---------------------------------------------------------------------
CREATE TABLE ref_client_type (
    name       text PRIMARY KEY,
    sort_order smallint NOT NULL
);

CREATE TABLE ref_onboarding_stage (
    name       text PRIMARY KEY,
    sort_order smallint NOT NULL
);

CREATE TABLE ref_kra_status (
    code        text PRIMARY KEY,
    description text NOT NULL
);

CREATE TABLE ref_product (
    code               text PRIMARY KEY,
    name               text   NOT NULL,
    min_investment_inr bigint NOT NULL
);

CREATE TABLE ref_risk_category (
    name       text PRIMARY KEY,
    sort_order smallint NOT NULL
);

CREATE TABLE ref_screening_list (
    name       text PRIMARY KEY,
    sort_order smallint NOT NULL
);

CREATE TABLE relationship_manager (
    rm_id  text PRIMARY KEY,
    name   text NOT NULL,
    branch text NOT NULL
);

-- ---------------------------------------------------------------------
-- Core: clients / prospects
-- ---------------------------------------------------------------------
CREATE TABLE clients (
    client_id                        text PRIMARY KEY,
    application_id                   text NOT NULL UNIQUE,
    client_type                      text NOT NULL REFERENCES ref_client_type(name),
    name                             text NOT NULL,
    first_name                       text,
    last_name                        text,
    gender                           char(1),
    date_of_birth_or_incorporation   date NOT NULL,
    pan                              char(10) NOT NULL UNIQUE,
    pan_aadhaar_linked               boolean,
    aadhaar_masked                   text,
    ckyc_number                      text,
    kra_name                         text NOT NULL,
    kra_status                       text NOT NULL REFERENCES ref_kra_status(code),
    residential_status               text NOT NULL,
    nationality                      text NOT NULL,
    tax_residency_country            text NOT NULL,
    email                            text NOT NULL,
    mobile                           text NOT NULL,
    occupation                       text,
    annual_income_band               text NOT NULL,
    net_worth_inr                    bigint NOT NULL,
    source_of_wealth                 text NOT NULL,
    pep_flag                         boolean NOT NULL,
    address_line1                    text NOT NULL,
    address_line2                    text,
    city                             text NOT NULL,
    state                            text,
    pincode                          text NOT NULL,
    country                          text NOT NULL,
    correspondence_same_as_permanent boolean NOT NULL,
    rm_id                            text NOT NULL REFERENCES relationship_manager(rm_id),
    rm_name                          text NOT NULL,
    branch                           text NOT NULL,
    channel                          text NOT NULL,
    onboarding_stage                 text NOT NULL REFERENCES ref_onboarding_stage(name),
    created_at                       timestamp NOT NULL,
    last_updated_at                  timestamp NOT NULL,
    risk_category                    text NOT NULL REFERENCES ref_risk_category(name),
    aml_risk_rating                  text NOT NULL CHECK (aml_risk_rating IN ('Low','Medium','High')),
    due_diligence_level              text NOT NULL CHECK (due_diligence_level IN ('CDD','EDD')),
    periodic_kyc_review_due          date NOT NULL,
    ipv_mode                         text NOT NULL,
    ipv_status                       text NOT NULL,
    liveness_score                   numeric(4,2),
    esign_mode                       text NOT NULL,
    esign_status                     text NOT NULL,
    nomination_opt_out               boolean
);

CREATE INDEX idx_clients_stage   ON clients (onboarding_stage);
CREATE INDEX idx_clients_type    ON clients (client_type);
CREATE INDEX idx_clients_rm      ON clients (rm_id);
CREATE INDEX idx_clients_aml     ON clients (aml_risk_rating);
CREATE INDEX idx_clients_review  ON clients (periodic_kyc_review_due);
CREATE INDEX idx_clients_pan     ON clients (pan);
CREATE INDEX idx_clients_search  ON clients USING gin (
    to_tsvector('simple', name || ' ' || pan || ' ' || application_id)
);

-- ---------------------------------------------------------------------
-- Product subscriptions
-- ---------------------------------------------------------------------
CREATE TABLE accounts (
    account_id               text PRIMARY KEY,
    client_id                text   NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
    product_code             text   NOT NULL REFERENCES ref_product(code),
    product_name             text   NOT NULL,
    strategy                 text   NOT NULL,
    holding_pattern          text   NOT NULL,
    commitment_or_corpus_inr bigint NOT NULL,
    funding_mode             text   NOT NULL,
    fee_structure            text   NOT NULL,
    custodian                text   NOT NULL,
    account_status           text   NOT NULL,
    activation_date          date,
    ucc_code                 text
);
CREATE INDEX idx_accounts_client ON accounts (client_id);
CREATE INDEX idx_accounts_status ON accounts (account_status);

-- ---------------------------------------------------------------------
-- KYC document checklist
-- ---------------------------------------------------------------------
CREATE TABLE kyc_documents (
    document_id      text PRIMARY KEY,
    client_id        text NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
    document_type    text NOT NULL,
    source           text,
    ocr_confidence   numeric(4,3),
    status           text NOT NULL,
    rejection_reason text,
    expiry_date      date,
    uploaded_at      timestamp
);
CREATE INDEX idx_docs_client ON kyc_documents (client_id);
CREATE INDEX idx_docs_expiry ON kyc_documents (expiry_date) WHERE expiry_date IS NOT NULL;

-- ---------------------------------------------------------------------
-- FATCA / CRS self-certification
-- ---------------------------------------------------------------------
CREATE TABLE fatca_crs (
    client_id                   text PRIMARY KEY REFERENCES clients(client_id) ON DELETE CASCADE,
    us_person                   boolean NOT NULL,
    tax_residencies             text    NOT NULL,
    tin_or_pan                  text    NOT NULL,
    place_of_birth              text    NOT NULL,
    giin                        text,
    fatca_entity_classification text,
    self_cert_date              date    NOT NULL,
    declaration_status          text    NOT NULL
);

-- ---------------------------------------------------------------------
-- Linked bank accounts
-- ---------------------------------------------------------------------
CREATE TABLE bank_accounts (
    bank_link_id          text PRIMARY KEY,
    client_id             text    NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
    bank_name             text    NOT NULL,
    ifsc                  text    NOT NULL,
    account_number_masked text    NOT NULL,
    account_type          text    NOT NULL,
    is_primary            boolean NOT NULL,
    penny_drop_status     text    NOT NULL,
    name_match_score      smallint NOT NULL CHECK (name_match_score BETWEEN 0 AND 100),
    verified_at           timestamp
);
CREATE INDEX idx_bank_client ON bank_accounts (client_id);

-- ---------------------------------------------------------------------
-- Demat linkage
-- ---------------------------------------------------------------------
CREATE TABLE demat_accounts (
    client_id       text PRIMARY KEY REFERENCES clients(client_id) ON DELETE CASCADE,
    depository      text NOT NULL,
    dp_name         text NOT NULL,
    dp_id           text,
    client_id_at_dp text NOT NULL,
    poa_ddpi        text NOT NULL,
    status          text NOT NULL
);

-- ---------------------------------------------------------------------
-- Nominees
-- ---------------------------------------------------------------------
CREATE TABLE nominees (
    nominee_id   text PRIMARY KEY,
    client_id    text     NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
    nominee_name text     NOT NULL,
    relationship text     NOT NULL,
    share_pct    smallint NOT NULL CHECK (share_pct BETWEEN 0 AND 100),
    is_minor     boolean  NOT NULL,
    id_type      text     NOT NULL
);
CREATE INDEX idx_nominees_client ON nominees (client_id);

-- ---------------------------------------------------------------------
-- Ultimate beneficial owners (non-individual clients)
-- ---------------------------------------------------------------------
CREATE TABLE ubo_details (
    ubo_id      text PRIMARY KEY,
    client_id   text     NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
    ubo_name    text     NOT NULL,
    pan         char(10) NOT NULL,
    holding_pct smallint NOT NULL CHECK (holding_pct BETWEEN 0 AND 100),
    role        text     NOT NULL,
    pep_flag    boolean  NOT NULL,
    nationality text     NOT NULL
);
CREATE INDEX idx_ubo_client ON ubo_details (client_id);

-- ---------------------------------------------------------------------
-- Risk profiling questionnaire outcome
-- ---------------------------------------------------------------------
CREATE TABLE risk_profiles (
    client_id             text PRIMARY KEY REFERENCES clients(client_id) ON DELETE CASCADE,
    questionnaire_version text     NOT NULL,
    completed_on          date     NOT NULL,
    investment_horizon    text     NOT NULL,
    investment_experience text     NOT NULL,
    loss_tolerance        text     NOT NULL,
    liquidity_need        text     NOT NULL,
    score                 smallint NOT NULL,
    risk_category         text     NOT NULL REFERENCES ref_risk_category(name),
    suitability_ok        boolean  NOT NULL
);

-- ---------------------------------------------------------------------
-- AML / sanctions screening
-- ---------------------------------------------------------------------
CREATE TABLE aml_screening (
    screening_id text PRIMARY KEY,
    client_id    text     NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
    list_name    text     NOT NULL REFERENCES ref_screening_list(name),
    result       text     NOT NULL,
    match_score  smallint NOT NULL,
    matched_name text,
    disposition  text,
    screened_at  timestamp NOT NULL,
    reviewer     text     NOT NULL
);
CREATE INDEX idx_screen_client ON aml_screening (client_id);
CREATE INDEX idx_screen_hits   ON aml_screening (result) WHERE result <> 'No Match';

-- ---------------------------------------------------------------------
-- Workflow audit trail
-- ---------------------------------------------------------------------
CREATE TABLE workflow_events (
    event_id  text PRIMARY KEY,
    client_id text NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
    stage     text NOT NULL,
    action    text NOT NULL,
    actor     text NOT NULL,
    "timestamp" timestamp NOT NULL,
    remarks   text
);
CREATE INDEX idx_events_client ON workflow_events (client_id, "timestamp");

-- ---------------------------------------------------------------------
-- Downstream API invocation log
-- ---------------------------------------------------------------------
CREATE TABLE api_call_log (
    request_id  text PRIMARY KEY,
    client_id   text NOT NULL REFERENCES clients(client_id) ON DELETE CASCADE,
    endpoint    text NOT NULL,
    downstream  text NOT NULL,
    http_status smallint NOT NULL,
    latency_ms  integer  NOT NULL,
    "timestamp" timestamp NOT NULL
);
CREATE INDEX idx_apilog_client ON api_call_log (client_id);

-- ---------------------------------------------------------------------
-- API catalogue (drives the API Explorer screen)
-- ---------------------------------------------------------------------
CREATE TABLE api_catalogue (
    seq             smallint PRIMARY KEY,
    api_group       text NOT NULL,
    method          text NOT NULL,
    path            text NOT NULL,
    summary         text NOT NULL,
    description     text NOT NULL,
    downstream      text NOT NULL,
    success_status  smallint NOT NULL,
    params          jsonb NOT NULL DEFAULT '[]'::jsonb,
    sample_request  jsonb,
    sample_response jsonb
);

-- =====================================================================
--  Views used by the application's aggregate endpoints
-- =====================================================================

-- Pipeline counts by onboarding stage (stages with zero clients included)
CREATE VIEW v_pipeline_by_stage AS
SELECT s.name AS stage,
       s.sort_order,
       count(c.client_id)::int AS applications
FROM ref_onboarding_stage s
LEFT JOIN clients c ON c.onboarding_stage = s.name
GROUP BY s.name, s.sort_order;

-- Relationship-manager conversion funnel
CREATE VIEW v_rm_performance AS
SELECT c.rm_id,
       c.rm_name,
       c.branch,
       count(*)::int AS applications,
       count(*) FILTER (WHERE c.onboarding_stage = 'Account Activated')::int AS activated
FROM clients c
GROUP BY c.rm_id, c.rm_name, c.branch;

-- Every screening row that is not a clean pass
CREATE VIEW v_screening_hits AS
SELECT s.*, c.name AS client_name, c.aml_risk_rating
FROM aml_screening s
JOIN clients c USING (client_id)
WHERE s.result <> 'No Match';

-- Dashboard headline numbers, one row
CREATE VIEW v_dashboard_kpis AS
SELECT
    (SELECT count(*) FROM clients)::int AS applications,
    (SELECT count(*) FROM clients WHERE onboarding_stage = 'Account Activated')::int AS activated,
    (SELECT count(*) FROM clients WHERE onboarding_stage IN ('Compliance Review','eSign Pending'))::int AS in_compliance_or_esign,
    (SELECT count(*) FROM clients WHERE aml_risk_rating = 'High')::int AS high_risk_clients,
    (SELECT count(*) FROM aml_screening
      WHERE result <> 'No Match'
        AND coalesce(disposition,'') <> 'False Positive - Cleared')::int AS open_screening_hits,
    (SELECT coalesce(sum(commitment_or_corpus_inr),0)::bigint FROM accounts
      WHERE account_status = 'Active') AS aum_onboarded_inr;

-- =====================================================================
--  Authentication — application users and server-side sessions
-- =====================================================================

CREATE TABLE app_user (
    user_id       serial PRIMARY KEY,
    username      text NOT NULL UNIQUE,
    full_name     text NOT NULL,
    email         text NOT NULL UNIQUE,
    -- scrypt$n$r$p$<base64 salt>$<base64 derived key>
    password_hash text NOT NULL,
    role          text NOT NULL CHECK (role IN ('Relationship Manager',
                                                'Compliance Officer',
                                                'Administrator')),
    rm_id         text REFERENCES relationship_manager(rm_id),
    branch        text,
    is_active     boolean     NOT NULL DEFAULT true,
    failed_logins smallint    NOT NULL DEFAULT 0,
    locked_until  timestamptz,
    last_login_at timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_user_username ON app_user (lower(username));

-- Sessions are server-side so that a sign-out genuinely revokes access.
-- Only the SHA-256 of the cookie token is stored, never the token itself.
CREATE TABLE app_session (
    token_hash text PRIMARY KEY,
    user_id    integer     NOT NULL REFERENCES app_user(user_id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    last_seen  timestamptz NOT NULL DEFAULT now(),
    user_agent text,
    ip_address text
);

CREATE INDEX idx_session_user    ON app_session (user_id);
CREATE INDEX idx_session_expires ON app_session (expires_at);

-- Audit trail for sign-in activity.
CREATE TABLE auth_event (
    event_id   bigserial PRIMARY KEY,
    username   text NOT NULL,
    user_id    integer REFERENCES app_user(user_id) ON DELETE SET NULL,
    event      text NOT NULL CHECK (event IN ('login', 'logout',
                                              'failed_login', 'locked_out')),
    ip_address text,
    user_agent text,
    at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_auth_event_at ON auth_event (at DESC);
