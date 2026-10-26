# Sporløs på Shopify

Cookieløs, samtykke-fri webanalyse for Shopify-butikker — uten cookie-banner, uten å
lekke besøkende til tredjepart. Måler også checkout-stegene, som vanlige tema-snippets
ikke får tilgang til.

## Fase 1 — Egendefinert pixel (live i dag, ingen app-review)

Dette er Shopify-ekvivalenten til WordPress-pluginen vår: ett lim-inn, virker umiddelbart.

### Slik installerer du

1. Logg inn på Shopify-admin → **Innstillinger → Kundehendelser**.
2. Klikk **«Legg til egendefinert pixel»**, gi den navnet `Sporløs`.
3. Åpne [`sporlos-pixel.js`](./sporlos-pixel.js), kopier **hele** innholdet og lim det inn.
4. Bytt linjen `var SITE_ID = "DITT_SITE_ID_HER";` med din egen site-ID.
   Den finner du i [Sporløs-dashbordet](https://sporlos.no/app) under **«Vis sporings-kode»**.
5. Klikk **Lagre**, deretter **Koble til**.

Ferdig. Trafikk dukker opp i dashbordet i løpet av sekunder.

### Hva som måles

| Shopify-hendelse | Sporløs-navn | |
|---|---|---|
| `page_viewed` | `pageview` | sidevisninger (også SPA-navigasjon) |
| `product_viewed` | `product_view` | produktvisning |
| `product_added_to_cart` | `add_to_cart` | lagt i handlekurv |
| `checkout_started` | `checkout_start` | påbegynt checkout |
| `checkout_completed` | `purchase` | fullført kjøp (sett som konverteringsmål i Sporløs) |
| `search_submitted` | `search` | butikksøk |

Ingen ordreverdi eller kundedata sendes — kun hendelsesnavnet. Sporløs er aggregert
analyse, ikke en revenue-tracker.

### Personvern

- Ingen cookies, ingen `localStorage`, ingen fingerprinting.
- Kun sti, referrer-kilde og hvitlistede `utm_*` sendes — aldri hele query-strengen.
- Serveren lagrer aldri rå-IP; besøkende telles via en daglig-roterende enveis-hash.
- Derfor: ingen cookie-banner nødvendig for Sporløs.
- **Global Privacy Control og Do Not Track respekteres:** en besøkende som har slått på
  GPC eller DNT telles aldri — pixelen sender ingenting (samme regel som
  `tracker/sporlos.js`). Automatiserte nettlesere (`navigator.webdriver`) flagges `wd:1`
  og telles som robot, ikke som person.

#### Hva sandboxen lar oss se av GPC/DNT

Shopifys `event.context.navigator` har bare `userAgent`, `language`, `languages` og
`cookieEnabled` — **ikke** `globalPrivacyControl`, `doNotTrack` eller `webdriver`. Pixelen
dekker derfor signalet slik:

| Kilde | Dekket |
|---|---|
| Global `navigator` i den egendefinerte pixelens lax sandbox (`globalPrivacyControl`, `doNotTrack`, `msDoNotTrack`, `window.doNotTrack`) | Ja, best effort. Ikke dokumentert av Shopify; flagg som ikke finnes leses som «ikke satt». |
| `event.context.navigator` (samme flagg) | Sjekkes, men Shopify eksponerer dem ikke i dag. |
| Shopify Customer Privacy API (`init.customerPrivacy` + `visitorConsentCollected`): `saleOfDataAllowed === false` | Ja. Shopify honorerer GPC ved å sette dette i regioner med opt-out, så det er det pålitelige GPC-signalet. DNT har ingen tilsvarende Shopify-flagg. |
| Analyse-samtykke (`analyticsProcessingAllowed`) | Brukes **ikke**: Sporløs er samtykke-fri, og kjøpmannen styrer ellers når pixelen lastes via pixelens tillatelse i Shopify-admin. |

Merk: app-pixelen (Fase 2) kjører i en Web Worker (strict sandbox) der DNT ikke finnes på
`WorkerNavigator`; der er `saleOfDataAllowed` det som dekker GPC.

### ⚠️ Merk om Shopifys «Optimized»-modus

Dette gjelder kun for *app-pixler* (Fase 2), **ikke** den egendefinerte pixelen over.
Egendefinerte pixler behandles som butikkens egen kode og strupes aldri. Du er trygg.

## Fase 2 — Listet app i Shopify App Store (planlagt)

Discovery-laget: en ekte app slik at butikker finner Sporløs når de søker
«GDPR analytics» / «privacy analytics» i App Store. Status og plan ligger i
[`app/README.md`](./app/README.md).
