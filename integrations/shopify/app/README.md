# Sporløs Shopify App — Fase 2 (kjerne scaffoldet)

Discovery-laget: en listet app i Shopify App Store, så butikker finner Sporløs når de
søker «GDPR analytics» / «privacy analytics». Fase 1 (egendefinert pixel) gir oss butikker
uten review; denne fasen handler om å bli **funnet**.

Presedens: Fathom, Plausible, Piwik PRO og Matomo har alle privacy-analytics-apper i
App Store. Kategorien slipper inn — wedgen vår må være den norske/Datatilsynet-vinkelen.

## Modell: gratis app, betaling på sporlos.no

Som WordPress-pluginen (og som Fathoms Shopify-app): appen er **gratis**, kjøpmann trenger
en Sporløs-konto + site-ID. Vi bruker **ikke** Shopify Billing — betaling går via Stripe/
Vipps på sporlos.no som i dag. Det fjerner hele Billing-API-jobben og Shopifys andel.
(Endre dette her hvis vi senere vil ha App Store-fakturering for bedre konvertering.)

## Hva som er scaffoldet (klart, ingen Partner-konto nødvendig)

```
extensions/sporlos-pixel/
  shopify.extension.toml   web_pixel_extension, strict sandbox, settings: site_id + endpoint
  src/index.js             samme event-mapping som Fase 1, site_id fra settings
```

`index.js` er gjenbruk av den beviste Fase 1-logikken (`../../sporlos-pixel.js`), men leser
`settings.site_id` (kjøpmann fyller inn ved installasjon) i stedet for hardkodet konstant.

## Hva som gjenstår — krever deg (Thomas)

1. **Shopify Partner-konto** + en **dev-store** (partners.shopify.com). Kan ikke gjøres av Claude.
2. Kjør CLI-en (genererer app-wrapperen rundt extensionen vår):
   ```bash
   npm init @shopify/app@latest sporlos-shopify-app   # velg «start with extension»
   # kopier extensions/sporlos-pixel/ inn i den genererte appen
   shopify app dev      # test mot dev-store: installer, fyll site_id, sjekk events i sporlos.no/app
   shopify app deploy
   ```
   Merk: CLI-en åpner nettleser-login → må kjøres av deg, ikke headless.
3. **App-listing**: ikon (bruk Blekk-merket), skjermbilder, beskrivelse. Engelsk i App Store,
   men la NO/Datatilsynet-vinkelen bære beskrivelsen.
4. **Personvern-review**: Shopify krever data-erklæring. Vår er enkel (ingen PII, ingen salg
   av data) — settings-feltene i toml-en deklarerer dette.

## ⚠️ Throttling-gotcha (ny 13. jan 2026)

App-pixler er nå default **«Optimized»** → strupes hvis Shopify ikke ser salgs-/trafikk-
attribusjon, og en analytics-app som sender til eksternt endepunkt er nettopp det. **Onboarding
må be kjøpmann sette pixelen til «Always on»** (Innstillinger → Kundehendelser → App-pixler).
Ellers ser appen ødelagt ut. (Fase 1, egendefinert pixel, rammes ikke.)

## Beslutning før innsending

Egen app-listing-tekst på engelsk (App Store-krav) vs. norsk wedge i selve beskrivelsen —
anbefaling: engelsk topp-tekst, norsk Datatilsynet-vinkel i body + skjermbilder.
