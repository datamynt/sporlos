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

## Hva som er klart nå (full CLI-app, ikke bare extension)

App-wrapperen er scaffoldet og verifisert mot gjeldende Shopify-docs (adversariell review
2026-06-15): `shopify.app.toml` (client_id 00e912…, scopes write_pixels+read_customer_events,
GDPR-webhooks → sporlos.no, ingen [auth] siden managed install), `package.json`, og extensionen
med påkrevd `api_version` + `uid`. GDPR-compliance-webhooken er bygget i Sporløs-backenden
(`/webhooks/shopify/compliance`, HMAC-verifisert, gated på SHOPIFY_API_SECRET).

## Hva som gjenstår — krever deg (Thomas)

1. **Dev-store** under Partner-kontoen (partners.shopify.com → Stores → add development store).
2. Kjør CLI-en herfra:
   ```bash
   cd integrations/shopify/app
   npm install
   shopify app config link    # browser-login → koble til Sporløs-appen (client_id matcher alt)
   shopify app dev            # test: installer på dev-store, fyll site_id, se events i sporlos.no/app
   shopify app deploy         # pusher pixel-extensionen
   ```
   CLI-en åpner nettleser-login → må kjøres av deg. Klager `deploy` på toml-format (skjemaet
   drifter mellom CLI-versjoner), kjør `shopify app generate extension --template web_pixel`
   én gang og flett inn vår `src/index.js` + settings.
3. Når review nærmer seg: legg `SHOPIFY_API_SECRET` (client secret) i server-`.env`, så svarer
   compliance-webhooken 200 i stedet for 503.
4. **App-listing**: ikon (Blekk-merket), skjermbilder, beskrivelse. Engelsk topp-tekst (App
   Store-krav), norsk Datatilsynet-vinkel i body.

## ⚖️ Consent-nyansen (beslutning før review)

`[extensions.customer_privacy] analytics = true` betyr at pixelen kun fyrer når kjøpmannens
kunde har gitt analyse-samtykke (Shopifys Customer Privacy API). Det er litt på tvers av Sporløs'
egen tese — at vi IKKE utløser samtykkekrav siden vi ikke lagrer på enheten. Men innenfor Shopifys
rammeverk er det ærligst å deklarere analyse-intensjon. Konsekvens: i butikker med samtykkebanner
under-teller pixelen besøk uten samtykke. Avvei før innsending: ærlig deklarasjon (analytics=true,
som nå) vs. å argumentere for unntak. Anbefaling: behold analytics=true til Shopify-review evt.
sier noe annet — det er den trygge, ærlige posisjonen.

## ⚠️ Throttling-gotcha (ny 13. jan 2026)

App-pixler er nå default **«Optimized»** → strupes hvis Shopify ikke ser salgs-/trafikk-
attribusjon, og en analytics-app som sender til eksternt endepunkt er nettopp det. **Onboarding
må be kjøpmann sette pixelen til «Always on»** (Innstillinger → Kundehendelser → App-pixler).
Ellers ser appen ødelagt ut. (Fase 1, egendefinert pixel, rammes ikke.)

## Beslutning før innsending

Egen app-listing-tekst på engelsk (App Store-krav) vs. norsk wedge i selve beskrivelsen —
anbefaling: engelsk topp-tekst, norsk Datatilsynet-vinkel i body + skjermbilder.
