-- Product Internship Outreach Agent - SQLite schema (version 1)
-- All timestamps are UTC ISO-8601 strings: YYYY-MM-DDTHH:MM:SS+00:00

CREATE TABLE IF NOT EXISTS companies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    domain          TEXT    NOT NULL UNIQUE,              -- normalised, e.g. "example.com"
    website         TEXT,
    description     TEXT,
    market          TEXT    NOT NULL CHECK (market IN ('india', 'international')),
    sector          TEXT    NOT NULL DEFAULT 'Other'
                    CHECK (sector IN ('AI', 'SaaS', 'Fintech', 'EdTech', 'Consumer Tech', 'Other')),
    country         TEXT,
    city            TEXT,
    remote_policy   TEXT    NOT NULL DEFAULT 'unknown'
                    CHECK (remote_policy IN ('remote', 'remote_india_ok', 'hybrid', 'onsite', 'unknown')),
    funding_stage   TEXT,
    employee_count  TEXT,
    hiring_signal   TEXT,                                  -- e.g. careers page mentions PM interns
    research_summary TEXT,                                 -- Gemini-generated
    fit_score       INTEGER CHECK (fit_score BETWEEN 0 AND 100),
    status          TEXT    NOT NULL DEFAULT 'discovered'
                    CHECK (status IN ('discovered', 'researched', 'contacts_found', 'contacted', 'disqualified')),
    source          TEXT    NOT NULL DEFAULT 'manual',     -- manual | discovery | seed
    source_url      TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_companies_status ON companies (status);
CREATE INDEX IF NOT EXISTS idx_companies_market ON companies (market);

-- v5: rebuilt from the v1 table (see database/migrations.py); ids are preserved.
CREATE TABLE IF NOT EXISTS contacts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    full_name           TEXT    NOT NULL,
    first_name          TEXT,
    name_norm           TEXT    NOT NULL,                  -- dedup key, e.g. "nithin kamath"
    role_title          TEXT,                              -- designation as published, e.g. "Co-founder & CEO"
    role_category       TEXT    NOT NULL DEFAULT 'other'
                        CHECK (role_category IN ('founder', 'cofounder', 'ceo', 'executive', 'head_of_product',
                                                 'product_lead', 'product_manager', 'hiring_manager',
                                                 'talent_acquisition', 'recruiter', 'other')),
    email               TEXT,
    email_type          TEXT    NOT NULL DEFAULT 'none'    -- how trustworthy the email is
                        CHECK (email_type IN ('public_verified', 'hunter', 'unverified', 'none')),
    email_confidence    INTEGER CHECK (email_confidence BETWEEN 0 AND 100),   -- Hunter score
    email_status        TEXT    NOT NULL DEFAULT 'unverified'                 -- Hunter verification result
                        CHECK (email_status IN ('unverified', 'valid', 'accept_all', 'invalid', 'unknown')),
    linkedin_url        TEXT,
    source              TEXT    NOT NULL DEFAULT 'manual', -- source type: website | listing | hunter | linkedin |
                                                           -- google | csv | manual | seed
    source_url          TEXT,                              -- page supporting the name and role
    evidence            TEXT,                              -- quote supporting the name and role
    opportunity_id      INTEGER REFERENCES opportunities (id) ON DELETE SET NULL,  -- associated listing
    verification_status TEXT    NOT NULL DEFAULT 'unverified'
                        CHECK (verification_status IN ('unverified', 'evidence_found', 'manually_verified')),
    score_role          INTEGER,                           -- 0-40 role relevance
    score_authority     INTEGER,                           -- 0-25 decision-making authority
    score_association   INTEGER,                           -- 0-20 company association confidence
    score_contact       INTEGER,                           -- 0-15 contact information quality
    score_total         INTEGER CHECK (score_total BETWEEN 0 AND 100),
    confidence_score    INTEGER CHECK (confidence_score BETWEEN 0 AND 100),
    outreach_priority   INTEGER,                           -- 1 = contact first, within the company
    notes               TEXT,
    do_not_contact      INTEGER NOT NULL DEFAULT 0 CHECK (do_not_contact IN (0, 1)),
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    discovered_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    last_verified_at    TEXT,
    updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    UNIQUE (company_id, email),
    UNIQUE (company_id, name_norm)
);

CREATE INDEX IF NOT EXISTS idx_contacts_company ON contacts (company_id);
CREATE INDEX IF NOT EXISTS idx_contacts_score   ON contacts (score_total);

-- Every observation of a contact (discovery history). Never deleted on merge.
CREATE TABLE IF NOT EXISTS contact_evidence (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id  INTEGER NOT NULL REFERENCES contacts (id) ON DELETE CASCADE,
    source      TEXT    NOT NULL,                          -- website | listing | hunter | csv | manual | manual_edit
    source_url  TEXT,
    evidence    TEXT,
    role_title  TEXT,
    email       TEXT,
    observed_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_contact_evidence ON contact_evidence (contact_id);

-- One row per company per discovery attempt; failed runs can be retried.
CREATE TABLE IF NOT EXISTS contact_discovery_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id       INTEGER NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    started_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    finished_at      TEXT,
    status           TEXT    NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running', 'ok', 'partial', 'failed')),
    pages_checked    INTEGER NOT NULL DEFAULT 0,
    people_found     INTEGER NOT NULL DEFAULT 0,
    contacts_added   INTEGER NOT NULL DEFAULT 0,
    contacts_updated INTEGER NOT NULL DEFAULT 0,
    hunter_used      INTEGER NOT NULL DEFAULT 0 CHECK (hunter_used IN (0, 1)),
    error            TEXT
);

CREATE INDEX IF NOT EXISTS idx_discovery_runs_company ON contact_discovery_runs (company_id);

CREATE TABLE IF NOT EXISTS emails (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id       INTEGER NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    contact_id       INTEGER NOT NULL REFERENCES contacts (id) ON DELETE CASCADE,
    kind             TEXT    NOT NULL DEFAULT 'initial' CHECK (kind IN ('initial', 'follow_up')),
    subject          TEXT    NOT NULL,
    body             TEXT    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'pending_review'
                     CHECK (status IN ('pending_review', 'approved', 'rejected', 'sent', 'failed', 'replied')),
    model            TEXT,                                 -- Gemini model that drafted it
    outreach_type    TEXT,                                 -- v6: vacancy | speculative
    opportunity_id   INTEGER REFERENCES opportunities (id) ON DELETE SET NULL,  -- v6: listing referred to
    facts_used       TEXT,                                 -- v6: JSON [{id, text, quote, url}] behind the draft
    checks           TEXT,                                 -- v6: JSON {errors: [], warnings: []} at draft time
    recipient        TEXT,                                 -- v7: address the email was actually sent to
    send_started_at  TEXT,                                 -- v7: lock taken just before calling Gmail
    parent_email_id  INTEGER REFERENCES emails (id) ON DELETE CASCADE,  -- v8: follow-up of this email
    message_id_header TEXT,                                -- v8: RFC Message-ID sent, for threading
    replied_at       TEXT,                                 -- v8: when a reply was detected/marked
    reviewer_notes   TEXT,
    gmail_message_id TEXT,
    gmail_thread_id  TEXT,
    error            TEXT,
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    approved_at      TEXT,
    sent_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_emails_status  ON emails (status);
CREATE INDEX IF NOT EXISTS idx_emails_sent_at ON emails (sent_at);

CREATE TABLE IF NOT EXISTS activity_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT    NOT NULL,                          -- company | contact | email | agent | system
    entity_id   INTEGER,
    action      TEXT    NOT NULL,
    details     TEXT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_activity_created ON activity_log (created_at);

-- ============================================================ v2: discovery

-- One row per distinct internship opportunity, merged across all sources.
CREATE TABLE IF NOT EXISTS opportunities (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name        TEXT    NOT NULL,
    company_name_norm   TEXT    NOT NULL,                  -- dedup key, e.g. "razorpay"
    company_domain      TEXT,                              -- dedup key, e.g. "razorpay.com"
    company_id          INTEGER REFERENCES companies (id) ON DELETE SET NULL,
    job_title           TEXT    NOT NULL,
    job_title_norm      TEXT    NOT NULL,
    job_url             TEXT,                              -- application URL as provided
    job_url_norm        TEXT    UNIQUE,                    -- dedup key (NULLs allowed)
    description         TEXT,
    location            TEXT,
    country             TEXT,
    market              TEXT    NOT NULL DEFAULT 'unknown'
                        CHECK (market IN ('india', 'international', 'unknown')),
    remote_status       TEXT    NOT NULL DEFAULT 'unknown'
                        CHECK (remote_status IN ('remote', 'hybrid', 'onsite', 'unknown')),
    india_eligible      TEXT    NOT NULL DEFAULT 'unknown'
                        CHECK (india_eligible IN ('yes', 'no', 'unknown')),
    industry            TEXT,
    company_product     TEXT,
    stipend             TEXT,
    compensation        TEXT    NOT NULL DEFAULT 'unknown'
                        CHECK (compensation IN ('paid', 'unpaid', 'unknown')),
    duration            TEXT,
    experience_required TEXT,
    title_category      TEXT    NOT NULL DEFAULT 'unmatched'   -- rule-based guess from the title
                        CHECK (title_category IN ('core_pm', 'related_product', 'adjacent', 'unmatched')),
    role_category       TEXT    NOT NULL DEFAULT 'unclassified' -- Gemini classification
                        CHECK (role_category IN ('core_pm', 'related_product', 'adjacent', 'not_relevant', 'unclassified')),
    relevance_score     INTEGER CHECK (relevance_score BETWEEN 0 AND 100),
    score_breakdown     TEXT,                              -- JSON sub-scores
    product_responsibilities TEXT,                         -- JSON list, from the listing text
    ai_reasoning        TEXT,
    analysis_model      TEXT,
    analyzed_at         TEXT,
    analysis_error      TEXT,
    status              TEXT    NOT NULL DEFAULT 'new'
                        CHECK (status IN ('new', 'qualified', 'rejected')),
    status_reason       TEXT,
    status_locked       INTEGER NOT NULL DEFAULT 0 CHECK (status_locked IN (0, 1)),  -- manual override
    primary_source      TEXT    NOT NULL
                        CHECK (primary_source IN ('google', 'linkedin', 'wellfound', 'career_page', 'csv', 'manual')),
    posted_date         TEXT,
    discovered_at       TEXT    NOT NULL,                  -- local date YYYY-MM-DD
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_opp_status   ON opportunities (status);
CREATE INDEX IF NOT EXISTS idx_opp_company  ON opportunities (company_name_norm);
CREATE INDEX IF NOT EXISTS idx_opp_domain   ON opportunities (company_domain);
CREATE INDEX IF NOT EXISTS idx_opp_source   ON opportunities (primary_source);

-- Every place an opportunity was seen. Never deleted on merge, so original URLs are preserved.
CREATE TABLE IF NOT EXISTS opportunity_sources (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER NOT NULL REFERENCES opportunities (id) ON DELETE CASCADE,
    source         TEXT    NOT NULL,
    url            TEXT    NOT NULL DEFAULT '',
    discovered_at  TEXT    NOT NULL,
    imported_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    UNIQUE (opportunity_id, source, url)
);

-- Company career boards checked through official public ATS APIs.
CREATE TABLE IF NOT EXISTS career_boards (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name    TEXT    NOT NULL,
    ats             TEXT    NOT NULL CHECK (ats IN ('greenhouse', 'lever', 'ashby')),
    slug            TEXT    NOT NULL,
    website         TEXT,
    market_hint     TEXT    NOT NULL DEFAULT 'unknown'
                    CHECK (market_hint IN ('india', 'international', 'unknown')),
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    last_checked_at TEXT,
    last_result     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    UNIQUE (ats, slug)
);

-- ============================================================ v3: company research

-- Public pages fetched from a company's own website (robots.txt respected). Cached for reuse.
CREATE TABLE IF NOT EXISTS company_pages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  INTEGER NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
    url         TEXT    NOT NULL,
    final_url   TEXT,
    page_type   TEXT,                                      -- home | about | product | careers | ...
    http_status INTEGER,
    title       TEXT,
    description TEXT,
    text        TEXT,
    links       TEXT,                                      -- v5: JSON [[href, anchor text]]
    people_text TEXT,                                      -- v5: all lines incl. short ones (names/titles)
    error       TEXT,                                      -- e.g. "blocked by robots.txt"
    fetched_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')),
    UNIQUE (company_id, url)
);

-- Latest Gemini research per company.
-- VERIFIED:      facts (each with a supporting quote, checked sentence-by-sentence), verified_summary.
-- INTERPRETATION (Gemini, not verified): summary, product, target_customers, business_model, industry,
--                candidate_angle, reasoning, score_breakdown. Never put these in an email as fact.
CREATE TABLE IF NOT EXISTS company_research (
    company_id       INTEGER PRIMARY KEY REFERENCES companies (id) ON DELETE CASCADE,
    summary          TEXT,
    product          TEXT,
    target_customers TEXT,
    business_model   TEXT,
    industry         TEXT,
    facts            TEXT,                                 -- JSON {category: [{text, quote, sources:[...]}]}
    verified_summary TEXT,                                 -- v4: built only from verified facts
    unreadable_pages TEXT,                                 -- v4: JSON [{url, page_type, reason}]
    candidate_angle  TEXT,
    fit_score        INTEGER CHECK (fit_score BETWEEN 0 AND 100),
    score_breakdown  TEXT,                                 -- JSON sub-scores
    reasoning        TEXT,
    insufficient     INTEGER NOT NULL DEFAULT 0 CHECK (insufficient IN (0, 1)),
    sources          TEXT,                                 -- JSON [{id, kind, url, title}]
    dropped_facts    INTEGER NOT NULL DEFAULT 0,           -- facts removed for bad/unsupported citations
    model            TEXT,
    researched_at    TEXT,
    error            TEXT
);

-- Tracks third-party API calls per day (Gemini / Hunter / ATS budgets).
CREATE TABLE IF NOT EXISTS api_usage (
    provider TEXT    NOT NULL,
    day      TEXT    NOT NULL,                             -- local date YYYY-MM-DD
    calls    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, day)
);
