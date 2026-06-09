# Databehandleravtale (DPA) — Sporløs

> **UTKAST v0 — mal.** Skal kvalitetssikres av jurist før den signeres/tilbys kunder.
> Bygger på GDPR artikkel 28 og er tilpasset KS' krav til kommuner (data i Norge).
> Klammeparenteser `[...]` fylles ut per kunde.

Denne databehandleravtalen ("Avtalen") regulerer Databehandlers behandling av
personopplysninger på vegne av Behandlingsansvarlig i forbindelse med bruk av
webanalysetjenesten Sporløs.

## 1. Parter

- **Behandlingsansvarlig:** [Kundens navn], org.nr [•] ("Kunden")
- **Databehandler:** Datamynt AS, org.nr 936 017 207, Maridalsveien 163, 0461 Oslo ("Datamynt")

## 2. Bakgrunn og formål

Datamynt leverer Sporløs, en personvernvennlig webanalysetjeneste. Tjenesten er
designet for å **ikke samle inn personopplysninger**: den bruker ingen
informasjonskapsler, lagrer ikke IP-adresser, og bruker ingen vedvarende
identifikatorer. Avtalen gir likevel Kunden fulle garantier etter GDPR art. 28 for
den begrensede behandlingen som skjer (se Bilag A), slik at Kunden trygt kan
dokumentere etterlevelse.

## 3. Databehandlers plikter (GDPR art. 28 nr. 3)

Datamynt skal:

a) **Kun behandle** personopplysninger etter dokumenterte instrukser fra Kunden,
   herunder det som følger av Avtalen og tjenestens konfigurasjon. Datamynt varsler
   Kunden dersom en instruks anses å være i strid med personvernregelverket.

b) **Sikre konfidensialitet** — kun personell med tjenstlig behov gis tilgang, og
   disse er underlagt taushetsplikt.

c) **Iverksette sikkerhetstiltak** etter GDPR art. 32 (se Bilag B).

d) **Ikke engasjere underleverandører** uten Kundens forhåndsgodkjenning. Godkjente
   underleverandører fremgår av Bilag C. Datamynt varsler Kunden ved planlagte
   endringer, slik at Kunden kan motsette seg dem.

e) **Bistå Kunden** med egnede tekniske og organisatoriske tiltak for å besvare
   henvendelser om de registrertes rettigheter (innsyn, retting, sletting mv.).

f) **Bistå Kunden** med å oppfylle pliktene etter art. 32–36 (sikkerhet,
   avviksvarsling, personvernkonsekvensvurdering og forhåndsdrøfting), hensyntatt
   behandlingens art og opplysningene Datamynt har tilgjengelig.

g) Ved opphør, etter Kundens valg, **slette eller tilbakelevere** alle
   personopplysninger, og slette eksisterende kopier, med mindre lagring er pålagt.

h) **Gjøre tilgjengelig** all informasjon som er nødvendig for å vise at pliktene
   etter art. 28 etterleves, og muliggjøre og bidra til revisjoner (se pkt. 8).

## 4. Avvikshåndtering (brudd på personopplysningssikkerheten)

Datamynt varsler Kunden **uten ugrunnet opphold** etter å ha blitt kjent med et
brudd, med tilstrekkelig informasjon til at Kunden kan oppfylle sin varslingsplikt
til Datatilsynet (art. 33) og eventuelt de registrerte (art. 34).

## 5. Overføring til tredjeland

Personopplysninger behandles og lagres **i Norge** (se Bilag C). Det skjer **ingen
overføring av personopplysninger ut av EØS**. Eventuell fremtidig endring krever
Kundens forhåndsgodkjenning og et gyldig overføringsgrunnlag etter GDPR kap. V.

## 6. De registrertes rettigheter

Henvendelser fra registrerte som Datamynt mottar direkte, videreformidles til Kunden
uten ugrunnet opphold. Datamynt svarer ikke registrerte på egen hånd uten instruks.

## 7. Sletting og tilbakelevering

Ved Avtalens opphør slettes aggregerte data knyttet til Kundens nettsted innen
[30] dager, om ikke Kunden ber om tilbakelevering først.

## 8. Revisjon og dokumentasjon

Datamynt gir Kunden informasjon nødvendig for å demonstrere etterlevelse, og
muliggjør revisjon (egen eller ved uavhengig revisor) med rimelig varsel, inntil én
gang per år eller ved mistanke om brudd. Sporløs er åpen kildekode, slik at
behandlingens art kan etterprøves direkte.

## 9. Varighet

Avtalen gjelder så lenge Datamynt behandler personopplysninger på vegne av Kunden,
og uansett så lenge tjenesteavtalen løper.

## 10. Lovvalg og verneting

Norsk rett. Verneting er [Oslo tingrett].

---

## Bilag A — Behandlingens art, formål og omfang

| | |
|---|---|
| **Formål** | Aggregert webanalyse (besøksstatistikk) for Kundens nettsted(er) |
| **Behandlingens art** | Innsamling av anonyme hendelsesdata; flyktig bruk av IP + User-Agent til a) en daglig-roterende enveis-hash for å telle unike, og b) land/fylke-oppslag. IP og User-Agent **lagres aldri** |
| **Kategorier registrerte** | Besøkende på Kundens nettsted |
| **Kategorier opplysninger** | Sidesti, henvisningskilde (host), enhet/nettleser/OS (grovt), land/fylke, daglig-roterende hash. **Ingen direkte identifikatorer, ingen cookies, ingen by-/posisjonsnivå** |
| **Varighet** | For tjenesteavtalens løpetid |

## Bilag B — Sikkerhetstiltak (GDPR art. 32)

- Ingen lagring av IP-adresser; daglig-roterende salt forkastes hvert døgn
- Ingen informasjonskapsler eller vedvarende identifikatorer; ingen fingerprinting
- Geo begrenses bevisst til land/fylke (ikke by) for å unngå re-identifisering
- All trafikk over TLS (HTTPS)
- Tilgangsstyring; database ikke eksponert mot internett
- Drift på norsk-eid/EU-eid infrastruktur i Norge
- Åpen kildekode muliggjør uavhengig verifikasjon

## Bilag C — Godkjente underleverandører

| Underleverandør | Tjeneste | Lokasjon | Eierskap |
|---|---|---|---|
| UpCloud Ltd | Hosting (server/database) | Stavanger, Norge | Finsk (EU) — utenfor US CLOUD Act |

Datamynt varsler Kunden ved endringer i denne listen.

---

_Datamynt AS · org.nr 936 017 207 · utkast, ikke juridisk rådgivning._
