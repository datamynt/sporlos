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
    overage_notified_month TEXT,          -- 'YYYY-MM' sist varslet om grense-passering
    stripe_customer_id     TEXT,
    stripe_subscription_id TEXT,
    vipps_agreement_id     TEXT,           -- aktiv/påbegynt Vipps-avtale (Recurring v3)
    vipps_pending_plan     TEXT,           -- plan som venter på godkjenning i Vipps-appen
    vipps_charged_through  TEXT,           -- 'YYYY-MM-DD' neste forfall som IKKE er trukket ennå
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL REFERENCES tenants(id),
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    email_verified INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sites (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   BIGINT NOT NULL REFERENCES tenants(id),
    domain      TEXT NOT NULL,
    public_id   TEXT NOT NULL UNIQUE,      -- brukes i tracker-snippet
    public_dash INTEGER NOT NULL DEFAULT 0, -- opt-in: åpent dashboard på /p/<public_id>
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
    utm_source    TEXT,                    -- hvitlistede kampanjeparametre fra URL
    utm_medium    TEXT,                    -- (aldri hele query-strengen — PII-vern)
    utm_campaign  TEXT,
    visitor_hash  TEXT NOT NULL,           -- IKKE re-identifiserbar på tvers av dager
    session_id    TEXT,
    revenue_cents BIGINT,                  -- ordresum i øre på kjøps-hendelser (aldri ordre-ID)
    currency      TEXT                     -- ISO 4217 (NOK når utelatt)
);
-- Dekker både tidsvindu-filteret og DISTINCT visitor_hash per site/dag.
CREATE INDEX IF NOT EXISTS events_site_ts_visitor ON events (site_id, ts, visitor_hash);
-- Kilde-attribusjon for kjøp: første pageview per visitor_hash (revenue_by_source).
CREATE INDEX IF NOT EXISTS events_site_visitor_ts ON events (site_id, visitor_hash, ts);
-- MERK: events_site_ecom (partiell indeks på revenue_cents, for has_ecommerce-proben)
-- opprettes i init_db ETTER kolonne-migreringene — den kan ikke stå her, for på en
-- eksisterende DB kjører denne fila FØR ALTER-en som legger til revenue_cents.

-- E-handel: produktlinjer på kjøps-hendelser. Kun produktnavn/antall/beløp —
-- vi ber aldri om ordre-ID eller kundedata, og lagrer ingen identifikatorer.
CREATE TABLE IF NOT EXISTS event_items (
    id               BIGSERIAL PRIMARY KEY,
    site_id          BIGINT NOT NULL REFERENCES sites(id),
    event_id         BIGINT REFERENCES events(id) ON DELETE CASCADE,
    ts               TIMESTAMPTZ NOT NULL DEFAULT now(),
    name             TEXT NOT NULL,
    qty              INTEGER NOT NULL DEFAULT 1,
    unit_price_cents BIGINT NOT NULL DEFAULT 0,
    currency         TEXT                  -- arver hendelsens valuta
);
CREATE INDEX IF NOT EXISTS event_items_site_ts ON event_items (site_id, ts);
-- FK-cascade fra events-slettinger (retention) trenger denne for å ikke seq-scanne.
CREATE INDEX IF NOT EXISTS event_items_event_id ON event_items (event_id);

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

-- API-nøkler: read-only programmatisk tilgang (AI-verktøy, integrasjoner, rapporter).
-- Selve nøkkelen lagres ALDRI — kun sha256. prefix vises i UI for gjenkjenning.
CREATE TABLE IF NOT EXISTS api_keys (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    BIGINT NOT NULL REFERENCES tenants(id),
    label        TEXT NOT NULL,
    prefix       TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS api_keys_tenant ON api_keys (tenant_id);

-- Nettside-assistent: kunnskap (ingestet fra sitemap) + rate-limit-teller.
-- MERK: selve samtalene lagres ALDRI — kun en anonym dagsteller per visitor-hash.
CREATE TABLE IF NOT EXISTS assist_pages (
    path        TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    content     TEXT NOT NULL,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS assist_usage (
    day     TEXT NOT NULL,
    visitor TEXT NOT NULL,
    n       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, visitor)
);

-- Søkedata fra Google Search Console / Bing Webmaster (app/seo.py).
-- dim: 'total' (key='') | 'query' | 'page'. Én rad per site/dag/kilde/dim/nøkkel.
-- Opprettes også lazily av store.ensure_search_schema() (deploy kjører ikke init).
CREATE TABLE IF NOT EXISTS search_stats (
    site_id     BIGINT NOT NULL REFERENCES sites(id),
    day         DATE NOT NULL,
    source      TEXT NOT NULL,
    dim         TEXT NOT NULL,
    key         TEXT NOT NULL DEFAULT '',
    clicks      BIGINT NOT NULL DEFAULT 0,
    impressions BIGINT NOT NULL DEFAULT 0,
    position    REAL,
    PRIMARY KEY (site_id, day, source, dim, key)
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
