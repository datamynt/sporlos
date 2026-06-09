-- Sporløs datamodell (PostgreSQL / Cloud SQL)
-- Multi-tenant fra dag én: tenant (byrå) -> sites -> events.

CREATE TABLE IF NOT EXISTS tenants (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    -- byrå eller direktekunde; white-label-felter kan legges til her
    plan          TEXT NOT NULL DEFAULT 'trial',
    trial_ends_at TIMESTAMPTZ,
    trial_reminded_at TIMESTAMPTZ,
    email_optout INTEGER NOT NULL DEFAULT 0,
    stripe_customer_id     TEXT,
    stripe_subscription_id TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL REFERENCES tenants(id),
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sites (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   BIGINT NOT NULL REFERENCES tenants(id),
    domain      TEXT NOT NULL,
    public_id   TEXT NOT NULL UNIQUE,      -- brukes i tracker-snippet
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, domain)
);

-- Rå events. MERK: ingen IP, ingen cookie, ingen PII.
-- visitor_hash = daglig-saltet enveis-hash (se app/privacy.py).
CREATE TABLE IF NOT EXISTS events (
    id            BIGSERIAL PRIMARY KEY,
    site_id       BIGINT NOT NULL REFERENCES sites(id),
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    name          TEXT NOT NULL DEFAULT 'pageview',
    path          TEXT NOT NULL,
    referrer_src  TEXT,                    -- normalisert kilde (ikke full URL m/ query)
    country       TEXT,                    -- land fra IP-geo, IP forkastes
    region        TEXT,                    -- fylke/subdivision (IKKE by — personvern-cap)
    device        TEXT,                    -- desktop/mobile/tablet
    browser       TEXT,
    os            TEXT,
    visitor_hash  TEXT NOT NULL,           -- IKKE re-identifiserbar på tvers av dager
    session_id    TEXT
);
-- Dekker både tidsvindu-filteret og DISTINCT visitor_hash per site/dag.
CREATE INDEX IF NOT EXISTS events_site_ts_visitor ON events (site_id, ts, visitor_hash);

-- Dags-aggregater. Disse hashes og anchres on-chain (aldri rådata).
CREATE TABLE IF NOT EXISTS daily_rollups (
    site_id     BIGINT NOT NULL REFERENCES sites(id),
    day         DATE NOT NULL,
    pageviews   BIGINT NOT NULL DEFAULT 0,
    visitors    BIGINT NOT NULL DEFAULT 0,   -- distinct visitor_hash
    sessions    BIGINT NOT NULL DEFAULT 0,
    bounce_rate REAL,
    rollup_hash TEXT,                        -- sha256 av kanonisk JSON av raden
    merkle_root TEXT,                        -- roten denne raden ble batchet under
    merkle_proof TEXT,                       -- inklusjons-sti (JSON) til roten
    txid        TEXT,                        -- BSV-anchor av roten, NULL til forankret
    anchored_at TIMESTAMPTZ,
    PRIMARY KEY (site_id, day)
);

-- Funnels: ordnede steg (sti/hendelse) per site, lagret som JSON.
CREATE TABLE IF NOT EXISTS funnels (
    id          BIGSERIAL PRIMARY KEY,
    site_id     BIGINT NOT NULL REFERENCES sites(id),
    name        TEXT NOT NULL,
    steps       TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Passord-reset-tokens (engangs, utløper).
CREATE TABLE IF NOT EXISTS reset_tokens (
    token       TEXT PRIMARY KEY,
    email       TEXT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL
);

-- Mål/konverteringer per site (egendefinert hendelse eller sti).
CREATE TABLE IF NOT EXISTS goals (
    id          BIGSERIAL PRIMARY KEY,
    site_id     BIGINT NOT NULL REFERENCES sites(id),
    name        TEXT NOT NULL,
    match_type  TEXT NOT NULL,   -- 'event' | 'path'
    match_value TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- BSV-anchre av rollups. Beviser at tallene ikke er etterjustert.
CREATE TABLE IF NOT EXISTS anchors (
    id          BIGSERIAL PRIMARY KEY,
    site_id     BIGINT NOT NULL REFERENCES sites(id),
    period      DATERANGE NOT NULL,
    rollup_hash TEXT NOT NULL,
    txid        TEXT,                        -- 1Sat / OP_RETURN PECKSTAT
    anchored_at TIMESTAMPTZ
);
