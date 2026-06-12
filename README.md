# Sporløs

> Webanalyse uten cookies, uten IP-lagring og uten samtykkebanner — bygget i Norge.
> Hosted på [sporlos.no](https://sporlos.no), eller kjør den selv (AGPL).

**[Live-demo med ekte tall →](https://sporlos.no/demo)**

## Hvorfor

Samtykkekravet i ekomloven § 3-15 utløses av det som lagres eller leses på
besøkerens enhet. Sporløs rører aldri enheten: ingen cookies, ingen
localStorage, ingen fingerprinting. IP-adressen brukes flyktig til en
daglig-roterende enveis-hash og forkastes — ingen kan gjenkjennes på tvers
av dager eller nettsteder. Dermed trengs verken banner eller samtykke, og du
måler **alle** besøk, ikke bare de som trykker «godta».

Begrensningen er produktet: du får trafikk, kilder, geografi (bevisst capped
på fylkesnivå), enheter, mål, funnels og kampanjer — du får ikke sporing av
enkeltpersoner.

## Hva er i repoet

| Del | Beskrivelse |
|---|---|
| `app/` | Starlette-app: ingestion, dashbord, multi-tenant konto, betaling, API |
| `tracker/sporlos.js` | Sporingsscriptet (~1,5 kB) — også publisert som [datamynt/sporlos-tracker](https://github.com/datamynt/sporlos-tracker) (MIT) |
| `app/privacy.py` | Kjernen i samtykke-friheten: daglig-saltet enveis-hash, IP forkastes |
| `app/merkle.py` + `app/anchor.py` | «Verifiserbare tall»: dagstall forsegles med hash og forankres i en offentlig logg (BSV) — valgfritt, alt virker uten |
| `integrations/wordpress/` | [WordPress-pluginen](https://wordpress.org/plugins/sporlos-analytics/) |
| `integrations/mcp/` | MCP-server oppå Stats-API-et (for AI-verktøy) |
| `db/schema.sql` | Datamodellen (PostgreSQL; SQLite-speil for lokal kjøring) |

## Kjør lokalt

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python3 -m app.manage init                # SQLite, null oppsett
.venv/bin/python3 -m app.manage create-site "Test" eksempel.no
.venv/bin/uvicorn app.main:app                      # http://localhost:8000
```

## Self-host (produksjon)

Full stack (PostgreSQL + app + Caddy med auto-HTTPS) i `docker-compose.prod.yml`
— se [DEPLOY.md](DEPLOY.md). Kort versjon:

```bash
cp .env.example .env   # fyll inn hemmeligheter
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml exec app python -m app.manage init
```

Betaling (Stripe/Vipps), e-post, KI-assistent og forsegling er alle gated på
miljøvariabler — tomt felt betyr at funksjonen er av, og alt annet virker.

## Personvern-prinsippene (ufravikelige)

1. **Aldri rå-IP eller cookie lagret.** Kun daglig-saltet enveis-hash.
   Dette er hele samtykke-fritaket — brytes det, er produktet ulovlig som GA.
2. **Geografi stopper på fylkesnivå.** By-nivå + lav trafikk = re-identifiserbart.
3. **Aggregater ut, aldri rådata.** API-et og dashbordet serverer kun tellinger.

## Lisens

[AGPL-3.0](LICENSE). Sporingsscriptet er separat publisert under MIT.
«Sporløs» som navn og merke tilhører Datamynt AS — bygg gjerne på koden,
men ikke under vårt navn.

---

## English summary

Sporløs is consent-free, cookieless web analytics built in Norway. No cookies,
no stored IPs, no fingerprinting — visitors are counted via a daily-rotating
one-way hash that is then discarded, so no consent banner is required under
Norwegian/EU ePrivacy rules, and every visit is measured (not just the ones
that click "accept"). Daily aggregates can optionally be sealed with a
cryptographic hash anchored to a public ledger, making the numbers tamper-
evident. The codebase is intentionally in Norwegian — it is built for the
Norwegian market, comments and all. Licensed AGPL-3.0; the tracker script is
MIT at [datamynt/sporlos-tracker](https://github.com/datamynt/sporlos-tracker).
Hosted version at [sporlos.no](https://sporlos.no).
