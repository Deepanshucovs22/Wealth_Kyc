# Database schema — `wealth_kyc`

Generated from the live database (`postgresql://postgres@localhost:5432/wealth_kyc`, PostgreSQL 18.6). Regenerate after schema changes rather than editing by hand.

Source DDL: [backend/db/schema.sql](../backend/db/schema.sql) (schema `kyc`) and [backend/db/kyc_intake.sql](../backend/db/kyc_intake.sql) (schema `kyc_intake`).

## Overview

### Schema `kyc`

Existing onboarding app — loaded from the Excel workbook. Dropped and rebuilt by `backend/etl/load_excel_to_pg.py`.

| Object | Kind | Rows | Columns |
|---|---|---:|---:|
| [`v_dashboard_kpis`](#kycvdashboardkpis) | view | — | 6 |
| [`v_pipeline_by_stage`](#kycvpipelinebystage) | view | — | 3 |
| [`v_rm_performance`](#kycvrmperformance) | view | — | 5 |
| [`v_screening_hits`](#kycvscreeninghits) | view | — | 11 |
| [`accounts`](#kycaccounts) | table | 295 | 13 |
| [`aml_screening`](#kycamlscreening) | table | 1,440 | 9 |
| [`api_call_log`](#kycapicalllog) | table | 806 | 7 |
| [`api_catalogue`](#kycapicatalogue) | table | 35 | 11 |
| [`app_session`](#kycappsession) | table | 8 | 7 |
| [`app_user`](#kycappuser) | table | 9 | 13 |
| [`auth_event`](#kycauthevent) | table | 11 | 7 |
| [`bank_accounts`](#kycbankaccounts) | table | 291 | 10 |
| [`clients`](#kycclients) | table | 240 | 48 |
| [`dataset_meta`](#kycdatasetmeta) | table | 1 | 5 |
| [`demat_accounts`](#kycdemataccounts) | table | 240 | 7 |
| [`fatca_crs`](#kycfatcacrs) | table | 240 | 9 |
| [`kyc_documents`](#kyckycdocuments) | table | 2,068 | 9 |
| [`nominees`](#kycnominees) | table | 220 | 7 |
| [`ref_client_type`](#kycrefclienttype) | table | 5 | 2 |
| [`ref_kra_status`](#kycrefkrastatus) | table | 5 | 2 |
| [`ref_onboarding_stage`](#kycrefonboardingstage) | table | 10 | 2 |
| [`ref_product`](#kycrefproduct) | table | 5 | 3 |
| [`ref_risk_category`](#kycrefriskcategory) | table | 4 | 2 |
| [`ref_screening_list`](#kycrefscreeninglist) | table | 6 | 2 |
| [`relationship_manager`](#kycrelationshipmanager) | table | 6 | 3 |
| [`risk_profiles`](#kycriskprofiles) | table | 240 | 10 |
| [`ubo_details`](#kycubodetails) | table | 139 | 8 |
| [`workflow_events`](#kycworkflowevents) | table | 1,489 | 7 |

### Schema `kyc_intake`

New Report — KYC intake sessions. Created by the app at start-up from `backend/db/kyc_intake.sql`; the loader never touches it.

| Object | Kind | Rows | Columns |
|---|---|---:|---:|
| [`v_latest_ocr`](#kyc_intakevlatestocr) | view | — | 21 |
| [`kyc_document`](#kyc_intakekycdocument) | table | 6 | 10 |
| [`kyc_form_data`](#kyc_intakekycformdata) | table | 2 | 22 |
| [`kyc_ocr_result`](#kyc_intakekycocrresult) | table | 4 | 21 |
| [`kyc_session`](#kyc_intakekycsession) | table | 2 | 9 |
| [`kyc_session_event`](#kyc_intakekycsessionevent) | table | 6 | 6 |

<a id="kycvdashboardkpis"></a>

## `kyc.v_dashboard_kpis`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `applications` | integer | null |  |  |
| `activated` | integer | null |  |  |
| `in_compliance_or_esign` | integer | null |  |  |
| `high_risk_clients` | integer | null |  |  |
| `open_screening_hits` | integer | null |  |  |
| `aum_onboarded_inr` | bigint | null |  |  |

**Definition**

```sql
SELECT (( SELECT count(*) AS count
           FROM kyc.clients))::integer AS applications,
    (( SELECT count(*) AS count
           FROM kyc.clients
          WHERE clients.onboarding_stage = 'Account Activated'::text))::integer AS activated,
    (( SELECT count(*) AS count
           FROM kyc.clients
          WHERE clients.onboarding_stage = ANY (ARRAY['Compliance Review'::text, 'eSign Pending'::text])))::integer AS in_compliance_or_esign,
    (( SELECT count(*) AS count
           FROM kyc.clients
          WHERE clients.aml_risk_rating = 'High'::text))::integer AS high_risk_clients,
    (( SELECT count(*) AS count
           FROM kyc.aml_screening
          WHERE aml_screening.result <> 'No Match'::text AND COALESCE(aml_screening.disposition, ''::text) <> 'False Positive - Cleared'::text))::integer AS open_screening_hits,
    ( SELECT COALESCE(sum(accounts.commitment_or_corpus_inr), 0::numeric)::bigint AS "coalesce"
           FROM kyc.accounts
          WHERE accounts.account_status = 'Active'::text) AS aum_onboarded_inr;
```

<a id="kycvpipelinebystage"></a>

## `kyc.v_pipeline_by_stage`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `stage` | text | null |  |  |
| `sort_order` | smallint | null |  |  |
| `applications` | integer | null |  |  |

**Definition**

```sql
SELECT s.name AS stage,
    s.sort_order,
    count(c.client_id)::integer AS applications
   FROM kyc.ref_onboarding_stage s
     LEFT JOIN kyc.clients c ON c.onboarding_stage = s.name
  GROUP BY s.name, s.sort_order;
```

<a id="kycvrmperformance"></a>

## `kyc.v_rm_performance`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `rm_id` | text | null |  |  |
| `rm_name` | text | null |  |  |
| `branch` | text | null |  |  |
| `applications` | integer | null |  |  |
| `activated` | integer | null |  |  |

**Definition**

```sql
SELECT rm_id,
    rm_name,
    branch,
    count(*)::integer AS applications,
    count(*) FILTER (WHERE onboarding_stage = 'Account Activated'::text)::integer AS activated
   FROM kyc.clients c
  GROUP BY rm_id, rm_name, branch;
```

<a id="kycvscreeninghits"></a>

## `kyc.v_screening_hits`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `screening_id` | text | null |  |  |
| `client_id` | text | null |  |  |
| `list_name` | text | null |  |  |
| `result` | text | null |  |  |
| `match_score` | smallint | null |  |  |
| `matched_name` | text | null |  |  |
| `disposition` | text | null |  |  |
| `screened_at` | timestamp without time zone | null |  |  |
| `reviewer` | text | null |  |  |
| `client_name` | text | null |  |  |
| `aml_risk_rating` | text | null |  |  |

**Definition**

```sql
SELECT s.screening_id,
    s.client_id,
    s.list_name,
    s.result,
    s.match_score,
    s.matched_name,
    s.disposition,
    s.screened_at,
    s.reviewer,
    c.name AS client_name,
    c.aml_risk_rating
   FROM kyc.aml_screening s
     JOIN kyc.clients c USING (client_id)
  WHERE s.result <> 'No Match'::text;
```

<a id="kycaccounts"></a>

## `kyc.accounts`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `account_id` | text | NOT NULL |  | **PK** |
| `client_id` | text | NOT NULL |  | FK → kyc.clients.client_id · cascade |
| `product_code` | text | NOT NULL |  | FK → kyc.ref_product.code |
| `product_name` | text | NOT NULL |  |  |
| `strategy` | text | NOT NULL |  |  |
| `holding_pattern` | text | NOT NULL |  |  |
| `commitment_or_corpus_inr` | bigint | NOT NULL |  |  |
| `funding_mode` | text | NOT NULL |  |  |
| `fee_structure` | text | NOT NULL |  |  |
| `custodian` | text | NOT NULL |  |  |
| `account_status` | text | NOT NULL |  |  |
| `activation_date` | date | null |  |  |
| `ucc_code` | text | null |  |  |

**Indexes**

- `idx_accounts_client`: `btree (client_id)`
- `idx_accounts_status`: `btree (account_status)`

<a id="kycamlscreening"></a>

## `kyc.aml_screening`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `screening_id` | text | NOT NULL |  | **PK** |
| `client_id` | text | NOT NULL |  | FK → kyc.clients.client_id · cascade |
| `list_name` | text | NOT NULL |  | FK → kyc.ref_screening_list.name |
| `result` | text | NOT NULL |  |  |
| `match_score` | smallint | NOT NULL |  |  |
| `matched_name` | text | null |  |  |
| `disposition` | text | null |  |  |
| `screened_at` | timestamp without time zone | NOT NULL |  |  |
| `reviewer` | text | NOT NULL |  |  |

**Indexes**

- `idx_screen_client`: `btree (client_id)`
- `idx_screen_hits`: `btree (result) WHERE (result <> 'No Match'::text)`

<a id="kycapicalllog"></a>

## `kyc.api_call_log`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `request_id` | text | NOT NULL |  | **PK** |
| `client_id` | text | NOT NULL |  | FK → kyc.clients.client_id · cascade |
| `endpoint` | text | NOT NULL |  |  |
| `downstream` | text | NOT NULL |  |  |
| `http_status` | smallint | NOT NULL |  |  |
| `latency_ms` | integer | NOT NULL |  |  |
| `timestamp` | timestamp without time zone | NOT NULL |  |  |

**Indexes**

- `idx_apilog_client`: `btree (client_id)`

<a id="kycapicatalogue"></a>

## `kyc.api_catalogue`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `seq` | smallint | NOT NULL |  | **PK** |
| `api_group` | text | NOT NULL |  |  |
| `method` | text | NOT NULL |  |  |
| `path` | text | NOT NULL |  |  |
| `summary` | text | NOT NULL |  |  |
| `description` | text | NOT NULL |  |  |
| `downstream` | text | NOT NULL |  |  |
| `success_status` | smallint | NOT NULL |  |  |
| `params` | jsonb | NOT NULL | `'[]'::jsonb` |  |
| `sample_request` | jsonb | null |  |  |
| `sample_response` | jsonb | null |  |  |

<a id="kycappsession"></a>

## `kyc.app_session`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `token_hash` | text | NOT NULL |  | **PK** |
| `user_id` | integer | NOT NULL |  | FK → kyc.app_user.user_id · cascade |
| `created_at` | timestamp with time zone | NOT NULL | `now()` |  |
| `expires_at` | timestamp with time zone | NOT NULL |  |  |
| `last_seen` | timestamp with time zone | NOT NULL | `now()` |  |
| `user_agent` | text | null |  |  |
| `ip_address` | text | null |  |  |

**Indexes**

- `idx_session_expires`: `btree (expires_at)`
- `idx_session_user`: `btree (user_id)`

<a id="kycappuser"></a>

## `kyc.app_user`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `user_id` | integer | NOT NULL | `nextval('kyc.app_user_user_id_seq'::regclass)` | **PK** |
| `username` | text | NOT NULL |  | unique |
| `full_name` | text | NOT NULL |  |  |
| `email` | text | NOT NULL |  | unique |
| `password_hash` | text | NOT NULL |  |  |
| `role` | text | NOT NULL |  |  |
| `rm_id` | text | null |  | FK → kyc.relationship_manager.rm_id |
| `branch` | text | null |  |  |
| `is_active` | boolean | NOT NULL | `true` |  |
| `failed_logins` | smallint | NOT NULL | `0` |  |
| `locked_until` | timestamp with time zone | null |  |  |
| `last_login_at` | timestamp with time zone | null |  |  |
| `created_at` | timestamp with time zone | NOT NULL | `now()` |  |

**Constraints**

- `app_user_role_check`: `CHECK ((role = ANY (ARRAY['Relationship Manager'::text, 'Compliance Officer'::text, 'Administrator'::text])))`

**Indexes**

- `idx_user_username`: `btree (lower(username))`

<a id="kycauthevent"></a>

## `kyc.auth_event`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `event_id` | bigint | NOT NULL | `nextval('kyc.auth_event_event_id_seq'::regclass)` | **PK** |
| `username` | text | NOT NULL |  |  |
| `user_id` | integer | null |  | FK → kyc.app_user.user_id · set null |
| `event` | text | NOT NULL |  |  |
| `ip_address` | text | null |  |  |
| `user_agent` | text | null |  |  |
| `at` | timestamp with time zone | NOT NULL | `now()` |  |

**Constraints**

- `auth_event_event_check`: `CHECK ((event = ANY (ARRAY['login'::text, 'logout'::text, 'failed_login'::text, 'locked_out'::text])))`

**Indexes**

- `idx_auth_event_at`: `btree (at DESC)`

<a id="kycbankaccounts"></a>

## `kyc.bank_accounts`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `bank_link_id` | text | NOT NULL |  | **PK** |
| `client_id` | text | NOT NULL |  | FK → kyc.clients.client_id · cascade |
| `bank_name` | text | NOT NULL |  |  |
| `ifsc` | text | NOT NULL |  |  |
| `account_number_masked` | text | NOT NULL |  |  |
| `account_type` | text | NOT NULL |  |  |
| `is_primary` | boolean | NOT NULL |  |  |
| `penny_drop_status` | text | NOT NULL |  |  |
| `name_match_score` | smallint | NOT NULL |  |  |
| `verified_at` | timestamp without time zone | null |  |  |

**Constraints**

- `bank_accounts_name_match_score_check`: `CHECK (((name_match_score >= 0) AND (name_match_score <= 100)))`

**Indexes**

- `idx_bank_client`: `btree (client_id)`

<a id="kycclients"></a>

## `kyc.clients`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `client_id` | text | NOT NULL |  | **PK** |
| `application_id` | text | NOT NULL |  | unique |
| `client_type` | text | NOT NULL |  | FK → kyc.ref_client_type.name |
| `name` | text | NOT NULL |  |  |
| `first_name` | text | null |  |  |
| `last_name` | text | null |  |  |
| `gender` | character(1) | null |  |  |
| `date_of_birth_or_incorporation` | date | NOT NULL |  |  |
| `pan` | character(10) | NOT NULL |  | unique |
| `pan_aadhaar_linked` | boolean | null |  |  |
| `aadhaar_masked` | text | null |  |  |
| `ckyc_number` | text | null |  |  |
| `kra_name` | text | NOT NULL |  |  |
| `kra_status` | text | NOT NULL |  | FK → kyc.ref_kra_status.code |
| `residential_status` | text | NOT NULL |  |  |
| `nationality` | text | NOT NULL |  |  |
| `tax_residency_country` | text | NOT NULL |  |  |
| `email` | text | NOT NULL |  |  |
| `mobile` | text | NOT NULL |  |  |
| `occupation` | text | null |  |  |
| `annual_income_band` | text | NOT NULL |  |  |
| `net_worth_inr` | bigint | NOT NULL |  |  |
| `source_of_wealth` | text | NOT NULL |  |  |
| `pep_flag` | boolean | NOT NULL |  |  |
| `address_line1` | text | NOT NULL |  |  |
| `address_line2` | text | null |  |  |
| `city` | text | NOT NULL |  |  |
| `state` | text | null |  |  |
| `pincode` | text | NOT NULL |  |  |
| `country` | text | NOT NULL |  |  |
| `correspondence_same_as_permanent` | boolean | NOT NULL |  |  |
| `rm_id` | text | NOT NULL |  | FK → kyc.relationship_manager.rm_id |
| `rm_name` | text | NOT NULL |  |  |
| `branch` | text | NOT NULL |  |  |
| `channel` | text | NOT NULL |  |  |
| `onboarding_stage` | text | NOT NULL |  | FK → kyc.ref_onboarding_stage.name |
| `created_at` | timestamp without time zone | NOT NULL |  |  |
| `last_updated_at` | timestamp without time zone | NOT NULL |  |  |
| `risk_category` | text | NOT NULL |  | FK → kyc.ref_risk_category.name |
| `aml_risk_rating` | text | NOT NULL |  |  |
| `due_diligence_level` | text | NOT NULL |  |  |
| `periodic_kyc_review_due` | date | NOT NULL |  |  |
| `ipv_mode` | text | NOT NULL |  |  |
| `ipv_status` | text | NOT NULL |  |  |
| `liveness_score` | numeric(4,2) | null |  |  |
| `esign_mode` | text | NOT NULL |  |  |
| `esign_status` | text | NOT NULL |  |  |
| `nomination_opt_out` | boolean | null |  |  |

**Constraints**

- `clients_aml_risk_rating_check`: `CHECK ((aml_risk_rating = ANY (ARRAY['Low'::text, 'Medium'::text, 'High'::text])))`
- `clients_due_diligence_level_check`: `CHECK ((due_diligence_level = ANY (ARRAY['CDD'::text, 'EDD'::text])))`

**Indexes**

- `idx_clients_aml`: `btree (aml_risk_rating)`
- `idx_clients_pan`: `btree (pan)`
- `idx_clients_review`: `btree (periodic_kyc_review_due)`
- `idx_clients_rm`: `btree (rm_id)`
- `idx_clients_search`: `gin (to_tsvector('simple'::regconfig, ((((name || ' '::text) || (pan)::text) || ' '::text) || application_id)))`
- `idx_clients_stage`: `btree (onboarding_stage)`
- `idx_clients_type`: `btree (client_type)`

<a id="kycdatasetmeta"></a>

## `kyc.dataset_meta`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `id` | smallint | NOT NULL | `1` | **PK** |
| `generated_on` | date | NOT NULL |  |  |
| `disclaimer` | text | NOT NULL |  |  |
| `source_file` | text | NOT NULL |  |  |
| `loaded_at` | timestamp with time zone | NOT NULL | `now()` |  |

**Constraints**

- `dataset_meta_id_check`: `CHECK ((id = 1))`

<a id="kycdemataccounts"></a>

## `kyc.demat_accounts`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `client_id` | text | NOT NULL |  | FK → kyc.clients.client_id · cascade, **PK** |
| `depository` | text | NOT NULL |  |  |
| `dp_name` | text | NOT NULL |  |  |
| `dp_id` | text | null |  |  |
| `client_id_at_dp` | text | NOT NULL |  |  |
| `poa_ddpi` | text | NOT NULL |  |  |
| `status` | text | NOT NULL |  |  |

<a id="kycfatcacrs"></a>

## `kyc.fatca_crs`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `client_id` | text | NOT NULL |  | FK → kyc.clients.client_id · cascade, **PK** |
| `us_person` | boolean | NOT NULL |  |  |
| `tax_residencies` | text | NOT NULL |  |  |
| `tin_or_pan` | text | NOT NULL |  |  |
| `place_of_birth` | text | NOT NULL |  |  |
| `giin` | text | null |  |  |
| `fatca_entity_classification` | text | null |  |  |
| `self_cert_date` | date | NOT NULL |  |  |
| `declaration_status` | text | NOT NULL |  |  |

<a id="kyckycdocuments"></a>

## `kyc.kyc_documents`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `document_id` | text | NOT NULL |  | **PK** |
| `client_id` | text | NOT NULL |  | FK → kyc.clients.client_id · cascade |
| `document_type` | text | NOT NULL |  |  |
| `source` | text | null |  |  |
| `ocr_confidence` | numeric(4,3) | null |  |  |
| `status` | text | NOT NULL |  |  |
| `rejection_reason` | text | null |  |  |
| `expiry_date` | date | null |  |  |
| `uploaded_at` | timestamp without time zone | null |  |  |

**Indexes**

- `idx_docs_client`: `btree (client_id)`
- `idx_docs_expiry`: `btree (expiry_date) WHERE (expiry_date IS NOT NULL)`

<a id="kycnominees"></a>

## `kyc.nominees`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `nominee_id` | text | NOT NULL |  | **PK** |
| `client_id` | text | NOT NULL |  | FK → kyc.clients.client_id · cascade |
| `nominee_name` | text | NOT NULL |  |  |
| `relationship` | text | NOT NULL |  |  |
| `share_pct` | smallint | NOT NULL |  |  |
| `is_minor` | boolean | NOT NULL |  |  |
| `id_type` | text | NOT NULL |  |  |

**Constraints**

- `nominees_share_pct_check`: `CHECK (((share_pct >= 0) AND (share_pct <= 100)))`

**Indexes**

- `idx_nominees_client`: `btree (client_id)`

<a id="kycrefclienttype"></a>

## `kyc.ref_client_type`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `name` | text | NOT NULL |  | **PK** |
| `sort_order` | smallint | NOT NULL |  |  |

<a id="kycrefkrastatus"></a>

## `kyc.ref_kra_status`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `code` | text | NOT NULL |  | **PK** |
| `description` | text | NOT NULL |  |  |

<a id="kycrefonboardingstage"></a>

## `kyc.ref_onboarding_stage`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `name` | text | NOT NULL |  | **PK** |
| `sort_order` | smallint | NOT NULL |  |  |

<a id="kycrefproduct"></a>

## `kyc.ref_product`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `code` | text | NOT NULL |  | **PK** |
| `name` | text | NOT NULL |  |  |
| `min_investment_inr` | bigint | NOT NULL |  |  |

<a id="kycrefriskcategory"></a>

## `kyc.ref_risk_category`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `name` | text | NOT NULL |  | **PK** |
| `sort_order` | smallint | NOT NULL |  |  |

<a id="kycrefscreeninglist"></a>

## `kyc.ref_screening_list`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `name` | text | NOT NULL |  | **PK** |
| `sort_order` | smallint | NOT NULL |  |  |

<a id="kycrelationshipmanager"></a>

## `kyc.relationship_manager`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `rm_id` | text | NOT NULL |  | **PK** |
| `name` | text | NOT NULL |  |  |
| `branch` | text | NOT NULL |  |  |

<a id="kycriskprofiles"></a>

## `kyc.risk_profiles`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `client_id` | text | NOT NULL |  | FK → kyc.clients.client_id · cascade, **PK** |
| `questionnaire_version` | text | NOT NULL |  |  |
| `completed_on` | date | NOT NULL |  |  |
| `investment_horizon` | text | NOT NULL |  |  |
| `investment_experience` | text | NOT NULL |  |  |
| `loss_tolerance` | text | NOT NULL |  |  |
| `liquidity_need` | text | NOT NULL |  |  |
| `score` | smallint | NOT NULL |  |  |
| `risk_category` | text | NOT NULL |  | FK → kyc.ref_risk_category.name |
| `suitability_ok` | boolean | NOT NULL |  |  |

<a id="kycubodetails"></a>

## `kyc.ubo_details`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `ubo_id` | text | NOT NULL |  | **PK** |
| `client_id` | text | NOT NULL |  | FK → kyc.clients.client_id · cascade |
| `ubo_name` | text | NOT NULL |  |  |
| `pan` | character(10) | NOT NULL |  |  |
| `holding_pct` | smallint | NOT NULL |  |  |
| `role` | text | NOT NULL |  |  |
| `pep_flag` | boolean | NOT NULL |  |  |
| `nationality` | text | NOT NULL |  |  |

**Constraints**

- `ubo_details_holding_pct_check`: `CHECK (((holding_pct >= 0) AND (holding_pct <= 100)))`

**Indexes**

- `idx_ubo_client`: `btree (client_id)`

<a id="kycworkflowevents"></a>

## `kyc.workflow_events`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `event_id` | text | NOT NULL |  | **PK** |
| `client_id` | text | NOT NULL |  | FK → kyc.clients.client_id · cascade |
| `stage` | text | NOT NULL |  |  |
| `action` | text | NOT NULL |  |  |
| `actor` | text | NOT NULL |  |  |
| `timestamp` | timestamp without time zone | NOT NULL |  |  |
| `remarks` | text | null |  |  |

**Indexes**

- `idx_events_client`: `btree (client_id, "timestamp")`

<a id="kyc_intakevlatestocr"></a>

## `kyc_intake.v_latest_ocr`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `ocr_id` | bigint | null |  |  |
| `session_id` | text | null |  |  |
| `doc_type` | text | null |  |  |
| `attempt` | smallint | null |  |  |
| `status` | text | null |  |  |
| `engine` | text | null |  |  |
| `extracted_name` | text | null |  |  |
| `extracted_dob` | date | null |  |  |
| `extracted_yob` | smallint | null |  |  |
| `extracted_pan` | character(10) | null |  |  |
| `extracted_aadhaar_masked` | text | null |  |  |
| `extracted_aadhaar_hmac` | character(64) | null |  |  |
| `extracted_address` | text | null |  |  |
| `field_confidence` | jsonb | null |  |  |
| `raw_text` | text | null |  |  |
| `lines` | jsonb | null |  |  |
| `mean_confidence` | numeric(4,3) | null |  |  |
| `pages` | smallint | null |  |  |
| `error` | text | null |  |  |
| `duration_ms` | integer | null |  |  |
| `processed_at` | timestamp with time zone | null |  |  |

**Definition**

```sql
SELECT DISTINCT ON (session_id, doc_type) ocr_id,
    session_id,
    doc_type,
    attempt,
    status,
    engine,
    extracted_name,
    extracted_dob,
    extracted_yob,
    extracted_pan,
    extracted_aadhaar_masked,
    extracted_aadhaar_hmac,
    extracted_address,
    field_confidence,
    raw_text,
    lines,
    mean_confidence,
    pages,
    error,
    duration_ms,
    processed_at
   FROM kyc_intake.kyc_ocr_result
  ORDER BY session_id, doc_type, attempt DESC;
```

<a id="kyc_intakekycdocument"></a>

## `kyc_intake.kyc_document`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `document_id` | bigint | NOT NULL | `nextval('kyc_intake.kyc_document_document_id_seq'::regclass)` | **PK** |
| `session_id` | text | NOT NULL |  | FK → kyc_intake.kyc_session.session_id · cascade |
| `doc_type` | text | NOT NULL |  |  |
| `seq` | smallint | NOT NULL | `1` |  |
| `file_name` | text | NOT NULL |  |  |
| `mime_type` | text | NOT NULL |  |  |
| `size_bytes` | integer | NOT NULL |  |  |
| `sha256` | character(64) | NOT NULL |  |  |
| `content` | bytea | NOT NULL |  |  |
| `uploaded_at` | timestamp with time zone | NOT NULL | `now()` |  |

**Constraints**

- `kyc_document_session_id_doc_type_seq_key`: `UNIQUE (session_id, doc_type, seq)`
- `kyc_document_doc_type_check`: `CHECK ((doc_type = ANY (ARRAY['PAN'::text, 'AADHAAR'::text, 'SIGNATURE'::text])))`
- `kyc_document_mime_type_check`: `CHECK ((mime_type = ANY (ARRAY['image/jpeg'::text, 'image/png'::text, 'image/webp'::text, 'application/pdf'::text])))`
- `kyc_document_seq_check`: `CHECK (((seq >= 1) AND (seq <= 2)))`
- `kyc_document_size_bytes_check`: `CHECK ((size_bytes > 0))`

<a id="kyc_intakekycformdata"></a>

## `kyc_intake.kyc_form_data`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `session_id` | text | NOT NULL |  | FK → kyc_intake.kyc_session.session_id · cascade, **PK** |
| `full_name` | text | NOT NULL |  |  |
| `date_of_birth` | date | NOT NULL |  |  |
| `pan` | character(10) | NOT NULL |  |  |
| `aadhaar_masked` | text | NOT NULL |  |  |
| `aadhaar_hmac` | character(64) | NOT NULL |  |  |
| `address_line1` | text | NOT NULL |  |  |
| `address_line2` | text | null |  |  |
| `city` | text | NOT NULL |  |  |
| `state` | text | NOT NULL |  |  |
| `pincode` | character(6) | NOT NULL |  |  |
| `customer_id` | text | null |  |  |
| `mobile` | text | null |  |  |
| `email` | text | null |  |  |
| `request_type` | text | null |  |  |
| `existing_user_id` | text | null |  |  |
| `preferred_user_id` | text | null |  |  |
| `limit_per_day` | numeric(15,2) | null |  |  |
| `limit_per_transaction` | numeric(15,2) | null |  |  |
| `approvers_required` | smallint | null |  |  |
| `transaction_type` | text | null |  |  |
| `submitted_at` | timestamp with time zone | NOT NULL | `now()` |  |

**Constraints**

- `form_txn_within_day`: `CHECK (((limit_per_day IS NULL) OR (limit_per_transaction IS NULL) OR (limit_per_transaction <= limit_per_day)))`
- `kyc_form_data_aadhaar_masked_check`: `CHECK ((aadhaar_masked ~ '^XXXX XXXX [0-9]{4}$'::text))`
- `kyc_form_data_address_line1_check`: `CHECK ((length(btrim(address_line1)) >= 3))`
- `kyc_form_data_approvers_required_check`: `CHECK (((approvers_required >= 0) AND (approvers_required <= 2)))`
- `kyc_form_data_date_of_birth_check`: `CHECK ((date_of_birth >= '1900-01-01'::date))`
- `kyc_form_data_email_check`: `CHECK ((email ~* '^[^@\s]+@[^@\s]+\.[^@\s]+$'::text))`
- `kyc_form_data_full_name_check`: `CHECK (((length(btrim(full_name)) >= 2) AND (length(btrim(full_name)) <= 150)))`
- `kyc_form_data_limit_per_day_check`: `CHECK ((limit_per_day >= (0)::numeric))`
- `kyc_form_data_limit_per_transaction_check`: `CHECK ((limit_per_transaction >= (0)::numeric))`
- `kyc_form_data_mobile_check`: `CHECK ((mobile ~ '^[6-9][0-9]{9}$'::text))`
- `kyc_form_data_pan_check`: `CHECK ((pan ~ '^[A-Z]{5}[0-9]{4}[A-Z]$'::text))`
- `kyc_form_data_pincode_check`: `CHECK ((pincode ~ '^[1-9][0-9]{5}$'::text))`
- `kyc_form_data_request_type_check`: `CHECK ((request_type = ANY (ARRAY['New User'::text, 'Modification'::text, 'Deletion'::text, 'Duplicate Password'::text])))`
- `kyc_form_data_transaction_type_check`: `CHECK ((transaction_type = ANY (ARRAY['A'::text, 'B'::text, 'C'::text, 'TFConnect'::text])))`

**Indexes**

- `idx_intake_form_pan`: `btree (pan)`

<a id="kyc_intakekycocrresult"></a>

## `kyc_intake.kyc_ocr_result`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `ocr_id` | bigint | NOT NULL | `nextval('kyc_intake.kyc_ocr_result_ocr_id_seq'::regclass)` | **PK** |
| `session_id` | text | NOT NULL |  | FK → kyc_intake.kyc_session.session_id · cascade |
| `doc_type` | text | NOT NULL |  |  |
| `attempt` | smallint | NOT NULL | `1` |  |
| `status` | text | NOT NULL |  |  |
| `engine` | text | NOT NULL |  |  |
| `extracted_name` | text | null |  |  |
| `extracted_dob` | date | null |  |  |
| `extracted_yob` | smallint | null |  |  |
| `extracted_pan` | character(10) | null |  |  |
| `extracted_aadhaar_masked` | text | null |  |  |
| `extracted_aadhaar_hmac` | character(64) | null |  |  |
| `extracted_address` | text | null |  |  |
| `field_confidence` | jsonb | NOT NULL | `'{}'::jsonb` |  |
| `raw_text` | text | null |  |  |
| `lines` | jsonb | NOT NULL | `'[]'::jsonb` |  |
| `mean_confidence` | numeric(4,3) | null |  |  |
| `pages` | smallint | null |  |  |
| `error` | text | null |  |  |
| `duration_ms` | integer | null |  |  |
| `processed_at` | timestamp with time zone | NOT NULL | `now()` |  |

**Constraints**

- `kyc_ocr_result_session_id_doc_type_attempt_key`: `UNIQUE (session_id, doc_type, attempt)`
- `kyc_ocr_result_doc_type_check`: `CHECK ((doc_type = ANY (ARRAY['PAN'::text, 'AADHAAR'::text])))`
- `kyc_ocr_result_status_check`: `CHECK ((status = ANY (ARRAY['Success'::text, 'Partial'::text, 'No Text'::text, 'Failed'::text])))`

<a id="kyc_intakekycsession"></a>

## `kyc_intake.kyc_session`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `session_id` | text | NOT NULL |  | **PK** |
| `status` | text | NOT NULL | `'Submitted'::text` |  |
| `created_by_user_id` | integer | NOT NULL |  |  |
| `created_by_username` | text | NOT NULL |  |  |
| `created_by_name` | text | NOT NULL |  |  |
| `client_ip` | text | null |  |  |
| `created_at` | timestamp with time zone | NOT NULL | `now()` |  |
| `updated_at` | timestamp with time zone | NOT NULL | `now()` |  |
| `ocr_completed_at` | timestamp with time zone | null |  |  |

**Constraints**

- `kyc_session_session_id_check`: `CHECK ((session_id ~ '^KYC-[0-9]{8}-[0-9A-F]{6}$'::text))`
- `kyc_session_status_check`: `CHECK ((status = ANY (ARRAY['Submitted'::text, 'OCR Running'::text, 'OCR Complete'::text, 'OCR Failed'::text, 'No ID Documents'::text])))`

**Indexes**

- `idx_intake_session_created`: `btree (created_at DESC)`

<a id="kyc_intakekycsessionevent"></a>

## `kyc_intake.kyc_session_event`

| Column | Type | Null | Default | Key |
|---|---|---|---|---|
| `event_id` | bigint | NOT NULL | `nextval('kyc_intake.kyc_session_event_event_id_seq'::regclass)` | **PK** |
| `session_id` | text | NOT NULL |  | FK → kyc_intake.kyc_session.session_id · cascade |
| `event` | text | NOT NULL |  |  |
| `actor` | text | NOT NULL |  |  |
| `detail` | jsonb | null |  |  |
| `at` | timestamp with time zone | NOT NULL | `now()` |  |

**Indexes**

- `idx_intake_event_session`: `btree (session_id, at)`

