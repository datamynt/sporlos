# Webanalyse uten cookie-banner — og uten å bryte norsk lov

> **Sporløs er bygget for å falle utenfor samtykkekravet i ekomloven § 3-15:
> vi lagrer ingenting på besøkerens enhet, og samler ingen personopplysninger.**
> Derfor trenger nettstedet ditt verken cookie-banner eller samtykke for å bruke oss.

_Utkast v0 — produktmateriale, ikke juridisk rådgivning. Skal kvalitetssikres av jurist før publisering._

---

## Hvorfor de fleste analyseverktøy krever banner

Fra **1. januar 2025** gjelder nye **ekomloven § 3-15** — Norges gjennomføring av ePrivacy-reglene.
Den sier, kort fortalt:

> Det er ikke tillatt å **lagre** eller å skaffe seg **tilgang til opplysninger i sluttbrukerens
> kommunikasjonsutstyr** uten at brukeren har samtykket.
> — Datatilsynet, om informasjonskapsler og sporingsteknologier

To ting er verdt å merke seg:

1. **§ 3-15 er strengere enn GDPR.** Der GDPR åpner for flere behandlingsgrunnlag, krever § 3-15
   i praksis *samtykke* for lagring/tilgang på enheten.
2. **Hindringen handler om enheten — ikke om analyse.** Det er selve det å skrive til eller lese fra
   besøkerens nettleser (cookies, localStorage, fingerprinting) som utløser samtykkekravet.

Google Analytics og de fleste andre verktøy setter cookies / lagrer identifikatorer på enheten →
de utløser § 3-15 → de krever cookie-banner og aktivt samtykke.

> ⚠️ **«Strengt nødvendig»-unntaket hjelper ikke.** § 3-15 har et unntak for lagring som er strengt
> nødvendig for å levere tjenesten brukeren ba om. Nkom er tydelig på at dette **ikke** dekker
> statistikk/publikumsmåling. Et analyseverktøy kan altså ikke lene seg på dette unntaket — det må
> rett og slett la være å lagre på enheten.

---

## Hvordan Sporløs er bygget for å unngå hele kravet

Sporløs utløser ikke § 3-15, fordi vi rett og slett **ikke rører enheten**:

| Sporløs gjør **ikke** | Konsekvens |
|---|---|
| Setter ingen cookies | Ingenting lagres i nettleseren |
| Bruker ingen localStorage / device-identifikatorer | Ingen «tilgang til opplysninger på utstyret» |
| Lagrer aldri IP-adresse | IP brukes kun flyktig til en enveis-hash og forkastes |
| Bruker ingen fingerprinting | Besøkende kan ikke gjenkjennes på tvers av dager |

I stedet teller vi unike besøkende med en **daglig-roterende, enveis-hash** som nullstilles hvert
døgn. Den kan verken reverseres til en person eller lenkes på tvers av dager eller nettsteder.
Resultatet er aggregert statistikk uten personopplysninger.

**Følgene for deg som kunde:**
- ✅ **Ingen cookie-banner** nødvendig for analysen
- ✅ **Ingen samtykke** å innhente
- ✅ Data du faktisk får se, helt fra første sidevisning (ingen «consent-tap»)

---

## Trenger du en personvernkonsekvensvurdering (DPIA)?

En DPIA er bare **påkrevd når en behandling sannsynligvis medfører høy risiko** for de registrerte
(GDPR art. 35). Cookieløs analyse uten personopplysninger og uten profilering treffer få eller ingen
av de høy-risiko-kriteriene. **For en standard Sporløs-installasjon er DPIA derfor ikke påkrevd.**

(Datatilsynet anbefaler likevel en frivillig vurdering ved tvil — og forutsetningen er at no-PII-løftet
er ekte. Fingerprinting eller vedvarende identifikatorer ville endret bildet.)

---

## Hvor lagres dataene?

> _Bekreftet: den hostede tjenesten driftes på europeisk-eid infrastruktur
> med servere i Stavanger, Norge._

Sporløs lagrer aggregert statistikk på **norsk/EØS-eid infrastruktur** — ikke på amerikansk-eid sky.
Dette er bevisst: norsk lov og praksis (Datatilsynet, EDPB, KS' standardavtale for kommuner) slår fast
at en EØS-*region* hos en US-eid skyleverandør **ikke** er tilstrekkelig for ekte datasuverenitet,
fordi amerikansk lovgivning (FISA 702 / CLOUD Act) kan kreve innsyn uavhengig av hvor dataene fysisk ligger.

For kommuner og offentlig sektor er dette spesielt viktig: KS' standard databehandleravtale har som
**hovedregel** at personopplysninger ikke føres ut av Norge og at servere står i Norge. Sporløs er bygget
for å kunne oppfylle dette ut av boksen.

---

## Ærlig om jussen

Vi vil heller være presise enn skråsikre:

- Den **forsvarlige kjernen** i vårt standpunkt er at vi ikke lagrer eller aksesserer noe på enheten,
  og ikke behandler personopplysninger. Det er det som holder oss utenfor § 3-15.
- Spørsmålet om *enhver* form for cookieløs analyse alltid er samtykkefri, er **omdiskutert** internasjonalt
  (særlig rundt hashing av IP). Vi påstår derfor ikke at «cookieløst = alltid lovlig» generelt — vi sier at
  **Sporløs sin konkrete arkitektur** er designet for å unngå utløsende-faktorene i § 3-15.
- Dette dokumentet er informasjon, ikke juridisk rådgivning. Endelig vurdering for ditt nettsted bør
  gjøres av din egen personvernansvarlige / jurist.

---

## Kilder
- Datatilsynet — *Bruk av informasjonskapsler og andre sporingsteknologier* (ekomloven § 3-15)
- Nkom — *Informasjonskapsler (cookies)* (§ 3-15, i kraft 1. jan 2025; «strengt nødvendig»-unntakets grense)
- Datatilsynet — *Når må man gjennomføre en vurdering av personvernkonsekvenser (DPIA)?* (GDPR art. 35)
