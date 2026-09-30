"""Databehandleravtalen (DPA) — the one source for /databehandleravtale.

Accepted electronically when an account is created (GDPR art. 28 nr. 9 allows
electronic form); store.create_account records VERSION and the time on the
tenant. A new text gets a new VERSION: existing customers keep the version they
accepted until they accept the new one, so never edit the text of a published
version in place.

Facts in the appendices must match how the service actually runs (privacy.py,
the backup chain on b550, the subprocessor list on /personvern).
"""

VERSION = "1"
VALID_FROM = "2026-09-30"

HTML = """<h1>Databehandleravtale</h1>
<p class=muted>Versjon 1 · gjelder fra 30.09.2026 · <a href="#" onclick="window.print();return false">Skriv ut eller lagre som PDF</a></p>

<p>Denne databehandleravtalen («Avtalen») regulerer Datamynts behandling av personopplysninger
på vegne av Kunden når Kunden bruker webanalysetjenesten Sporløs.</p>

<h2>1. Parter</h2>
<ul>
<li><b>Behandlingsansvarlig:</b> virksomheten som har opprettet konto i Sporløs, slik den er registrert i kontoen («Kunden»).</li>
<li><b>Databehandler:</b> Datamynt AS, org.nr 936 017 207, Maridalsveien 163, 0461 Oslo («Datamynt»).</li>
</ul>

<h2>2. Inngåelse</h2>
<p>Avtalen inngås elektronisk når Kunden oppretter konto og godtar vilkårene, jf. GDPR art. 28 nr. 9.
Datamynt lagrer tidspunktet og hvilken versjon som ble godtatt. Kunden kan be om et signert
eksemplar på <a href="mailto:post@sporlos.no">post@sporlos.no</a>.</p>
<p>Har partene signert en egen databehandleravtale (for eksempel KS' standardavtale), gjelder den
foran denne.</p>

<h2>3. Bakgrunn og formål</h2>
<p>Sporløs er laget for å <b>ikke samle inn personopplysninger</b>: tjenesten bruker ingen
informasjonskapsler, lagrer ikke IP-adresser og bruker ingen vedvarende identifikatorer. Avtalen
gir likevel Kunden fulle garantier etter GDPR art. 28 for den begrensede behandlingen som skjer
(se Bilag A), slik at Kunden kan dokumentere etterlevelse.</p>

<h2>4. Datamynts plikter (GDPR art. 28 nr. 3)</h2>
<p>Datamynt skal:</p>
<ol type=a>
<li><b>Kun behandle</b> personopplysninger etter dokumenterte instrukser fra Kunden, herunder det som
følger av Avtalen og tjenestens konfigurasjon. Datamynt varsler Kunden dersom en instruks anses å
være i strid med personvernregelverket.</li>
<li><b>Sikre konfidensialitet:</b> kun personell med tjenstlig behov får tilgang, og de er underlagt
taushetsplikt.</li>
<li><b>Iverksette sikkerhetstiltak</b> etter GDPR art. 32 (se Bilag B).</li>
<li><b>Bare bruke underleverandørene i Bilag C.</b> Planlagte endringer varsles Kunden på e-post
minst 30 dager i forveien. Kunden kan motsette seg endringen og si opp tjenesten med virkning
før endringen trer i kraft.</li>
<li><b>Bistå Kunden</b> med egnede tekniske og organisatoriske tiltak for å besvare henvendelser om
de registrertes rettigheter (innsyn, retting, sletting mv.).</li>
<li><b>Bistå Kunden</b> med å oppfylle pliktene etter art. 32–36 (sikkerhet, avviksvarsling,
personvernkonsekvensvurdering og forhåndsdrøfting), ut fra behandlingens art og opplysningene
Datamynt har tilgjengelig.</li>
<li>Ved opphør <b>slette</b> personopplysningene, med mindre lagring er pålagt (se punkt 8).</li>
<li><b>Gjøre tilgjengelig</b> informasjonen som trengs for å vise at pliktene etter art. 28
etterleves, og muliggjøre revisjoner (se punkt 9).</li>
</ol>

<h2>5. Avvik (brudd på personopplysningssikkerheten)</h2>
<p>Datamynt varsler Kunden <b>uten ugrunnet opphold</b> etter å ha blitt kjent med et brudd, med
tilstrekkelig informasjon til at Kunden kan oppfylle sin varslingsplikt til Datatilsynet
(art. 33) og eventuelt de registrerte (art. 34).</p>

<h2>6. Overføring til tredjeland</h2>
<p>Personopplysninger behandles og lagres <b>i Norge</b> (se Bilag C). Det skjer <b>ingen overføring
ut av EØS</b>. En eventuell endring krever varsel etter punkt 4 d og et gyldig overføringsgrunnlag
etter GDPR kapittel V.</p>

<h2>7. De registrertes rettigheter</h2>
<p>Henvendelser fra registrerte som Datamynt mottar direkte, videreformidles til Kunden uten
ugrunnet opphold. Datamynt svarer ikke registrerte på egen hånd uten instruks.</p>

<h2>8. Sletting</h2>
<p>Kunden kan be om sletting av ett nettsted eller hele kontoen, i tjenesten eller på
<a href="mailto:post@sporlos.no">post@sporlos.no</a>. Dataene slettes da fra tjenesten
umiddelbart. Kopier i nattlige sikkerhetskopier slettes automatisk, senest 35 dager etter.</p>
<p>Avsluttes kundeforholdet uten at Kunden har slettet selv, slettes dataene innen 90 dager etter
at avtaleforholdet er avsluttet, slik salgsbetingelsene sier. Kunden kan laste ned sine data som
CSV før det.</p>

<h2>9. Revisjon og dokumentasjon</h2>
<p>Datamynt gir Kunden informasjonen som trengs for å dokumentere etterlevelse, og muliggjør
revisjon (egen eller ved uavhengig revisor) med rimelig varsel, inntil én gang per år eller ved
mistanke om brudd. Sporløs er åpen kildekode, så behandlingens art kan etterprøves direkte.</p>

<h2>10. Varighet</h2>
<p>Avtalen gjelder så lenge Datamynt behandler personopplysninger på vegne av Kunden, og uansett
så lenge tjenesteavtalen løper.</p>

<h2>11. Endringer</h2>
<p>Datamynt kan publisere nye versjoner av Avtalen. Endringer som svekker Kundens vern, varsles
minst 30 dager i forveien, og Kunden kan si opp før de trer i kraft. Tidligere versjoner gis ut på
forespørsel.</p>

<h2>12. Lovvalg og verneting</h2>
<p>Norsk rett. Verneting er Oslo tingrett.</p>

<h2>Bilag A — Behandlingens art, formål og omfang</h2>
<table>
<tr><td><b>Formål</b></td><td>Aggregert webanalyse (besøksstatistikk) for Kundens nettsted(er)</td></tr>
<tr><td><b>Behandlingens art</b></td><td>Innsamling av anonyme hendelsesdata. IP-adresse og nettleserinformasjon (User-Agent)
brukes flyktig til a) en daglig-roterende enveis-hash for å telle unike besøkende og b) oppslag av land og fylke.
IP-adresse og User-Agent <b>lagres aldri</b>.</td></tr>
<tr><td><b>Registrerte</b></td><td>Besøkende på Kundens nettsted</td></tr>
<tr><td><b>Opplysninger</b></td><td>Sidesti, henvisningskilde (vertsnavn), grov enhets-, nettleser- og OS-type, land og fylke,
daglig-roterende hash. <b>Ingen direkte identifikatorer, ingen informasjonskapsler, ingen posisjon under fylkesnivå.</b></td></tr>
<tr><td><b>Varighet</b></td><td>Tjenesteavtalens løpetid. Hashen lages med et tilfeldig salt som byttes og forkastes
hvert døgn, så besøk kan ikke kobles på tvers av dager.</td></tr>
</table>

<h2>Bilag B — Sikkerhetstiltak (GDPR art. 32)</h2>
<ul>
<li>Ingen lagring av IP-adresser. Dagens salt er tilfeldig og forkastes etter døgnet.</li>
<li>Ingen informasjonskapsler, vedvarende identifikatorer eller fingerprinting hos de besøkende.</li>
<li>Geografi bevisst begrenset til land og fylke, ikke by, for å hindre re-identifisering.</li>
<li>All trafikk over TLS (HTTPS). Databasen er ikke eksponert mot internett.</li>
<li>Innlogging med passord (lagret som hash) eller Google/Microsoft. Innlogginger kan trekkes tilbake,
mislykkede forsøk strupes, og ingen økt varer mer enn 30 dager.</li>
<li>Drift på Datamynts egen server i Oslo, uten ekstern hostingleverandør.</li>
<li>Nattlige sikkerhetskopier på Datamynts egne maskiner i Oslo, på to adresser, og gjenopprettet på
prøve hver natt.</li>
<li>Åpen kildekode, slik at alt over kan etterprøves.</li>
</ul>

<h2>Bilag C — Underleverandører</h2>
<p>Ingen. Drift og sikkerhetskopier skjer på Datamynts egne maskiner i Oslo.</p>
"""
