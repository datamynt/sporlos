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

### ⚠️ Merk om Shopifys «Optimized»-modus

Dette gjelder kun for *app-pixler* (Fase 2), **ikke** den egendefinerte pixelen over.
Egendefinerte pixler behandles som butikkens egen kode og strupes aldri. Du er trygg.

## Fase 2 — Listet app i Shopify App Store (planlagt)

Discovery-laget: en ekte app slik at butikker finner Sporløs når de søker
«GDPR analytics» / «privacy analytics» i App Store. Status og plan ligger i
[`app/README.md`](./app/README.md).
