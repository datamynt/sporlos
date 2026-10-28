# Deploy — Sporløs på norsk-eid VPS

Mål: «go live» = noen få kommandoer. Stack: Postgres + app + Caddy (auto-HTTPS) i Docker.
Anbefalt vert: norsk-eid VPS (f.eks. PRO ISP fra ~99 kr/mnd) for suverenitet.

## Forutsetninger på VPS-en
- Docker + Docker Compose installert
- Et domene/subdomene (f.eks. `analytics.dittdomene.no`) med A-record som peker på VPS-ens IP
- Porter 80 + 443 åpne (Caddy trenger dem for Let's Encrypt)

## Steg
```bash
# 1. Hent koden til VPS-en (git clone / scp)
cd sporlos

# 2. Konfig
cp .env.example .env
#   sett SPORLOS_DOMAIN (= domenet over),
#   SPORLOS_SALT_SECRET (openssl rand -hex 32),
#   POSTGRES_PASSWORD (sterkt passord)

# 3a. Geo-database (land + fylke). DB-IP Lite City, gratis/CC-BY, ingen signup.
#     Uten denne degraderer geo pent (Land/Fylke = ukjent).
mkdir -p geoip
curl -fsSL "https://download.db-ip.com/free/dbip-city-lite-$(date +%Y-%m).mmdb.gz" \
  | gunzip > geoip/geoip.mmdb

# 3a2. ASN-database (datasenter-filter — crawlere med «vanlig» UA telles ikke).
#      Samme kilde/lisens. Uten denne degraderer filteret pent (alt telles, som før).
curl -fsSL "https://download.db-ip.com/free/dbip-asn-lite-$(date +%Y-%m).mmdb.gz" \
  | gunzip > geoip/asn.mmdb

# 3b. Bygg + start (Caddy henter TLS-sert automatisk for SPORLOS_DOMAIN)
docker compose -f docker-compose.prod.yml up -d --build

# 4. Initialiser databasen (kjører db/schema.sql)
docker compose -f docker-compose.prod.yml exec app python -m app.manage init

# 5. Lag en site (gir public_id + ferdig snippet)
docker compose -f docker-compose.prod.yml exec app \
  python -m app.manage create-site "Datamynt" merdata.no
```

## Ta i bruk på et nettsted
Lim snippet fra `create-site` inn på siden, men pek `data-api` på ditt domene:
```html
<script defer
  data-site="DIN_PUBLIC_ID"
  data-api="https://analytics.dittdomene.no/api/event"
  src="https://analytics.dittdomene.no/sporlos.js"></script>
```
Dashboard: `https://analytics.dittdomene.no/?site=DIN_PUBLIC_ID`

## Drift
- Logger: `docker compose -f docker-compose.prod.yml logs -f app`
- Oppdater: `git pull && docker compose -f docker-compose.prod.yml up -d --build`
- Backup: `docker compose -f docker-compose.prod.yml exec db pg_dump -U sporlos sporlos > backup.sql`

## Backup directory for one-off scripts
`purge_bot_signups.py --apply` writes a 0600 backup to `./backups` (bind-mounted at `/app/backups`).
Create the directory as the deploy user (uid 1000) before the first `up`. Otherwise Docker creates it
as root, the script cannot write the backup and stops before deleting anything:
```bash
mkdir -p backups
```

## Private source addresses
Ingest drops events whose client IP is private, loopback, link-local or otherwise non-global
(RFC1918, `::1`, `fe80::/10`, `fc00::/7`, IPv4-mapped forms). Behind a reverse proxy on the same
network as the operator's office, hairpin NAT makes the operator's own browsers arrive from a
private address; they would otherwise show up as visitors with country "ukjent". The drop is
logged as `ingest_drop reason=private` and is not added to the robot count. For an intranet
install where every visitor has a private address, set `SPORLOS_COUNT_PRIVATE_IPS=1` in `.env`
to count them as before.

## Retention job (terms §10, DPA §8)
`python -m app.manage purge-dormant` deletes accounts whose customer relationship has ended:
never-verified sign-ups with no data after 14 days, expired trials and cancelled plans 84 days
after the end (inside the 90 days the terms promise), plus expired reset links and invites.
It is a **dry run unless `SPORLOS_RETENTION_ENFORCE=1`** is set for that run, prints one JSON
line with counts and cutoffs only (no ids, names or addresses) and writes the same to the
`retention_runs` table. Accounts with a Vipps agreement, an annual invoice, or a paid, owner
or agency plan are never touched. Schedule it daily next to `retention`:
```bash
docker compose -f docker-compose.prod.yml exec -T app python -m app.manage purge-dormant
# enforce, once the dry-run counts look right:
docker compose -f docker-compose.prod.yml exec -T -e SPORLOS_RETENTION_ENFORCE=1 app python -m app.manage purge-dormant
```

### Warning mail before deletion
The same command warns first. Expired trials and cancelled plans get **one e-mail to the account
owner 14 days before they become deletable** (day 70 after the end; `store.DELETION_WARN_DAYS`),
with a signed download link (valid until the day after the deletion date) and a pointer to
the plan page. It is sent by `purge-dormant` itself, before the purge, so no extra cron entry is
needed: with `SPORLOS_RETENTION_ENFORCE=1` it sends, in a dry run it only counts
(`deletion_warnings.due` in the JSON line). The marker is `tenants.deletion_warned_at`
(self-healing column, set only when SMTP accepted the mail, cleared when a plan is bought), so
the mail goes out once per ending and a failed send is retried the next day (exit code 1 when
any send failed). The purge **waits for the warning**: an ended account is deleted no sooner than
7 days after its mail (`DELETION_MIN_NOTICE_DAYS`), and an account nobody could warn (SMTP down
for weeks) is deleted anyway from day 89 (`RETENTION_HARD_DAYS`) because the terms promise 90 days.
`held_for_notice` in the JSON line counts accounts waiting for their mail. SMTP must be configured
(`SMTP_HOST`, `MAIL_FROM`), otherwise nothing is warned and deletion falls back to day 89.

### Full export ("Last ned alle data")
`/app/export/alt?site=<id>` (one site) and `/app/export/konto` (the account) stream a ZIP of CSV
files built on the fly (`app/export.py`; at most two exports at a time, then 429). They need a
login, which also works for a cancelled account in its grace period. The warning mail links to
`/eksport?tid=…&exp=…&t=…`, a signed link (HMAC with the session secret, so changing
`SPORLOS_SESSION_SECRET` invalidates links already mailed) that shows a button; the download is a
POST. The API-key API does not offer the export: it is documented as sums only, and the export
contains the stored event rows.

## sporlos.no måler seg selv (goals + funnels)
Selve hendelsene (`signup_submit`, `signup_complete`, `checkout_submit`, `checkout_card|vipps|faktura`,
`purchase` med omsetning) sendes fra koden. Målene og traktene på sporlos.no-siten (`SPORLOS_DEMO_SITE`,
standard `6LIACtOSP-S7`) opprettes én gang etter deploy; kommandoen er idempotent (hopper over navn
som finnes), så den kan kjøres på nytt uten skade:
```bash
docker compose -f docker-compose.b550.yml exec -T app python -m app.manage seed-own-goals
```

## Søkedata (valgfritt — «Søk og AI»-modulen)
1. Lag en Google service account, legg JSON-nøkkelen i `./secrets/gsc-sa.json`
   (gitignored; mountes read-only av compose) og gi kontoens e-postadresse
   lesetilgang på hver property i Search Console. Bing: API-nøkkel fra
   Webmaster Tools → Settings → API access. Begge settes i `.env`
   (`GSC_SERVICE_ACCOUNT`, `BING_WEBMASTER_API_KEY`).
2. Første synk (backfill, GSC husker ~16 mnd):
   `docker compose -f docker-compose.b550.yml exec -T app python -m app.manage seo-sync 480`
3. Daglig cron (GSC publiserer nye tall utpå morgenen):
   `40 6 * * * cd ~/sporlos && docker compose -f docker-compose.b550.yml exec -T app python -m app.manage seo-sync >> /var/log/sporlos-seo.log 2>&1`
   ⚠️ b550-crontab er delt med andre tjenester — rediger via `crontab -l`-dump, aldri overskriv.
4. Hosted-kunder («Koble til Search Console»-knappen): krever GOOGLE_CLIENT_ID/SECRET
   (samme OAuth-app som «Logg inn med Google») + scope
   `webmasters.readonly` godkjent på consent-skjermen i Google Cloud Console.
   Refresh-tokens lagres Fernet-kryptert (nøkkel avledet av SPORLOS_SALT_SECRET).
5. IndexNow (valgfritt): sett `INDEXNOW_KEY` (openssl rand -hex 16) og legg
   `python -m app.manage indexnow-ping` i daglig cron — pinger Bing m.fl. om nye
   sitemap-URL-er. Nøkkelfila serveres automatisk på `/<nøkkel>.txt`.

## Suverenitet — sjekkliste før kommune-salg
- [ ] VPS hos norsk-eid leverandør (ikke AWS/GCP/Azure) → se hosting-leads
- [ ] Backup lagret i Norge/EØS, ikke US-sky
- [ ] SPORLOS_SALT_SECRET er ekte (ikke dev-default)
- [ ] Databehandleravtale klar (KS-mal: data i Norge — VPS-valget oppfyller dette)

## Per-day breakdowns (long history)
`daily_breakdowns` keeps, per site and day, the breakdowns the dashboard shows (pages, entry/exit
pages, sources, campaigns, countries, regions, devices, browsers, OS, custom events, AI referrals,
e-commerce, robots) as plain counts with no visitor hash (`app/history.py`). The nightly
`manage rollup` writes yesterday's rows in the same transaction as the daily totals; `manage
retention` never deletes them (only site or account deletion does) and first fills in any day that
is about to lose its raw events without them. The table is created by `manage init`, which the
container runs on start. Once after the first deploy, fill in the days still in `events`:
```bash
docker compose -f docker-compose.b550.yml exec -T app python -m app.manage backfill-breakdowns          # dry run: counts
docker compose -f docker-compose.b550.yml exec -T app python -m app.manage backfill-breakdowns --apply  # idempotent
```
