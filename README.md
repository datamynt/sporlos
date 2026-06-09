# Sporløs — personvernvennlig webanalyse (EØS)

> Cookieløs, samtykke-fri webanalyse bygget i Norge. Åpen kjerne (AGPL),
> hosted SaaS, og self-host for byråer. BSV-anchring gir **kryptografisk
> verifiserbare tall** — noe ingen Matomo-instans kan matche.

**Arbeidsnavn:** Sporløs. Alternativer å vurdere: _Synlig_, _Innsikt_, _Måling_, _Telle_, _Anonym_.

---

## Hvorfor

- Datatilsynet (+ flere EU-DPA-er) har i praksis kjent Google Analytics ulovlig (Schrems II).
- Norske bedrifter vil ha analyse, men GA er juridisk grums og Plausible/Matomo er EØS-generisk uten norsk faktura/support.
- Det finnes **ingen norsk-bygget privacy-analytics SaaS** i dag — bare webbyråer som hoster Matomo/Plausible manuelt per kunde. Det er luken.

## Posisjonering: open-core, to målgrupper

| Spor | Hvem | Modell |
|---|---|---|
| **Hosted** | SMB direkte (som Plausible Cloud) | abonnement, norsk faktura/MVA, selvbetjent |
| **Self-host** | webbyråer | AGPL gratis, de drifter selv, white-label per kunde |
| **Premium** | begge | BSV-anchring = "beviselig uforfalskede tall" (resellbart for byrå) |

Byrå-vinkelen er hovedmålet: få webhusene til å bytte fra Plausible til vår variant.
AGPL-åpenhet er **tillits-vektoren** som gjør at byråene tør adoptere.

## Datasuverenitet — ærlig

Juridisk krav = **EØS** (ikke "Norge spesifikt"). Schrems-trygghet handler om _eierskap_,
ikke geografi: GCP `europe-north1` er fortsatt Google = US CLOUD Act. Med self-host løses
dette elegant — byrået velger hosting, vi leverer koden. For hosted: bruk EØS-eid hosting
hvis "Schrems-trygt" skal stå i marketing.

> **BSV/anchring legger ALDRI rådata på kjeden** — kun en hash av dags-/måneds-aggregater.
> Kjeden er global og offentlig; rådata blir i EØS-DB. Anchren beviser bare at tallene ikke er etterjustert.

---

## MVP-scope

1. **Tracker-snippet** (`tracker/sporlos.js`) — <1 KB, ingen cookies, sender pageview-beacon.
2. **Ingestion** (`POST /api/event`) — parser event, beregner cookieløs visitor-hash, lagrer.
3. **Cookieløs unik-teller** (`app/privacy.py`) — daglig-roterende salt → ikke re-identifiserbar → ingen samtykke-banner. **Den ene biten som MÅ være riktig.**
4. **Dashboard** — sanntid (SSE via Redis) + dags-aggregater. Gjenbruker peck-ui `stat`/`card`.
5. **Multi-tenancy fra dag én** — tenant (byrå) → sites → events. Isolasjon per site.
6. **Anchor-jobb** (senere i MVP) — nattlig rollup → hash → 1Sat-anchor (kopier merdata-mønster).

### Gjenbruk fra peck-flåten
- Stack: FastHTML + Starlette + Cloud SQL (psycopg2) + Redis SSE — hus-standard.
- UI: peck-ui `stat` (KPI-boks m/ delta), `card`, `breadcrumbs`.
- Anchor: merdata `buildSpendableBeef` 1Sat-mønster (bevist on-chain 2026-06-02), nytt OP_RETURN-prefix `PECKSTAT`.
- E-post-rapporter: peck-mail (utgående relay).
- `anchor-client` / `web-base` fra standardiserings-katalogen.

Det _eneste_ nye vi skriver: ingestion-endpoint, salt-rotasjons-telleren, rollup-jobben. Resten er montering.

---

## Regulatorisk sjekkliste
- [ ] Ingen rå-IP lagres noensinne (kun daglig-saltet hash).
- [ ] Salt roteres daglig + forkastes (gårsdagens hash ikke reversibel/lenkbar).
- [ ] Visitor-hash inkluderer `site_id` → samme besøkende på to byrå-kunder gir ulik hash (cross-site non-linkability).
- [ ] **Databehandleravtale (DPA)** på norsk — byråene vil kreve den. Leveranse på lik linje med kode.
- [ ] DPIA-vennlig dokumentasjon (hvorfor data ikke er personopplysning).
- [ ] Ikke oversell "Schrems-trygt" med mindre hosting er EØS-eid.

## Lisens
AGPL-3.0 for kjernen (open-core). Premium-anchring/hosted kan være separat.
Følger flåte-policy: lisensier etter kjede-binding — anchring-modulen mot BSV = vurder Open BSV License v5.
