# Sporløs Shopify App — Fase 2 (planlagt, ikke startet)

Discovery-laget: en listet app i Shopify App Store. Fase 1 (egendefinert pixel, se
mappen over) gir oss butikker uten review; denne fasen handler om å bli **funnet**.

Presedens: Fathom («FA – Privacy First Analytics»), Plausible, Piwik PRO og Matomo har
alle privacy-analytics-apper i App Store. Kategorien slipper inn — wedgen vår må være den
norske/Datatilsynet-vinkelen, ikke «vi er privacy-vennlige» (det sier alle fire).

## Arkitektur

En ekte OAuth-app med en **Web Pixel-app-extension** (ikke en egendefinert pixel).
Samme event-mapping som Fase 1 — gjenbruk logikken i `../sporlos-pixel.js`.

- Extension-template: `web_pixel`
- Scopes: `write_pixels`, `read_customer_events`
- Aktivering: `webPixelCreate`-mutasjon (GraphQL Admin API), site-ID lagres som
  pixel-`settings` (ikke hardkodet, slik som i Fase 1)
- Billing: Shopify Billing API (recurring) — speil pris-planene fra sporlos.no
- Sandbox: `strict` mode

## Byggeløp (når vi tar fatt)

```bash
npm init @shopify/app@latest            # eller: shopify app init
shopify app generate extension --template web_pixel
# konfigurer shopify.extension.toml: privacy-settings (analytics/marketing/preferences/sale_of_data)
# index.js: gjenbruk send()/utm()/EVENTS fra ../sporlos-pixel.js, les SITE_ID fra settings
shopify app dev                          # test mot dev-store
shopify app deploy
```

## ⚠️ Throttling-gotcha (ny 13. jan 2026)

Shopify satte *app-pixler* til **«Optimized»** som default. I den modusen strupes pixler
som ikke korrelerer med salg/trafikk — og en analytics-app som sender til et eksternt
endepunkt (oss) er nettopp noe Shopify ikke ser attribusjon fra. Dataflyt kan pauses.

**Onboarding må be kjøpmannen sette pixelen til «Always on»** under
Innstillinger → Kundehendelser → App-pixler. Ellers ser appen ødelagt ut (klassisk
«ser ut som en bug, er en innstilling»). Egendefinerte pixler (Fase 1) rammes ikke.

## Krav før vi starter

- Shopify Partner-konto + dev-store
- App-review-krav gjennomgått (listing, personvern-erklæring, billing-flyt)
- Beslutning: egen app-listing-tekst på engelsk (App Store) vs. norsk wedge i beskrivelsen
