# Deploy — Sporløs på norsk-eid VPS

Mål: «go live» = noen få kommandoer. Stack: Postgres + app + Caddy (auto-HTTPS) i Docker.
Anbefalt vert: norsk-eid VPS (f.eks. PRO ISP fra ~99 kr/mnd) for suverenitet. Se GTM_RESEARCH_LEADS.

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

## Suverenitet — sjekkliste før kommune-salg
- [ ] VPS hos norsk-eid leverandør (ikke AWS/GCP/Azure) → se hosting-leads
- [ ] Backup lagret i Norge/EØS, ikke US-sky
- [ ] SPORLOS_SALT_SECRET er ekte (ikke dev-default)
- [ ] Databehandleravtale klar (KS-mal: data i Norge — VPS-valget oppfyller dette)
