"""Blogg-innhold som data — samme mønster som _GUIDES i main.py.

Nytt innlegg = ny oppføring ØVERST i POSTS (rekkefølgen styrer /blogg og RSS,
nyeste først). Body er ren HTML — hold deg til <p>, <h2>, <ul>/<li>,
<blockquote>, <a>, <em>/<b>. Slug: [a-z0-9-]. Sitater fra kilder gjengis på
originalspråket (også nynorsk); brødtekst er bokmål. Etter deploy: re-kjør
`manage assist-ingest https://sporlos.no` så assistenten kan innholdet.
"""

POSTS = {
    "kringkastet-340-ganger-om-dagen": {
        "tittel": "Kringkastet 340 ganger om dagen — hva NRK-saken om cookies betyr for nettstedet ditt",
        "dato": "2026-07-18",
        "beskrivelse": "NRK viser hvordan personopplysninger kringkastes 340 ganger om "
        "dagen gjennom annonsebørser. Her er hva saken betyr for deg som eier et "
        "nettsted — og hvorfor cookie-banneret er symptomet, ikke løsningen.",
        "ingress": "NRK forklarte i dag hva som skjer når du trykker «godkjenn alle "
        "cookies»: opplysningene dine sendes ut i sanntidsauksjoner — i snitt 340 "
        "ganger om dagen. Saken er skrevet til deg som besøker nettsider. Vi vil "
        "gjerne snakke til deg som eier ett.",
        "body": """
<p>NRK publiserte i dag <a href="https://www.nrk.no/artikkel/dette-skjer-med-informasjonen-du-legg-igjen-pa-nettet-967863"
rel="noopener">en sak om hva som skjer med informasjonen du legger igjen på nettet</a>
— nærmere bestemt hva som skjer i det øyeblikket du trykker «godkjenn alle cookies».
Den er verdt fem minutter, også — kanskje særlig — hvis du eier et nettsted selv.</p>

<h2>340 ganger om dagen</h2>
<p>Kjernen i saken er noe de fleste aldri ser: I det en nettside med annonser laster,
auksjoneres oppmerksomheten din bort i sanntid. Interessene dine, omtrent hvor du er,
hva slags enhet du bruker og hva du har klikket på, kringkastes til budgivere som
avgjør hvilken annonse du får se. Ifølge en rapport fra 2022, gjengitt i saken, får
en gjennomsnittlig nordmann personopplysningene sine kringkastet på denne måten
340 ganger om dagen.</p>
<p>Tobias Judin i Datatilsynet oppsummerer byttehandelen slik:</p>
<blockquote>«Du mistar kontroll for alltid for at dei kan tena nokre kroner på deg.»</blockquote>

<h2>Saken er skrevet til den som besøker nettsider</h2>
<p>Rådene i saken går til privatpersoner, og de er gode — videreformidlet herved:
ikke trykk «godkjenn alle» på autopilot, velg heller «innstillinger» eller «les mer»
og avvis, og vurder en utvidelse som blokkerer sporing.</p>
<p>Men det finnes en gruppe til som bør lese den: alle som <em>eier</em> et nettsted.
Innsamlingen skjer ikke et abstrakt sted «på nettet» — den skjer på helt vanlige
norske nettsider, gjennom annonseteknologi og analyseverktøy som ble skrudd på en
gang fordi alle andre hadde det. Judin påpeker i saken at mye av det nettsteder
ønsker seg, er uskyldig: å forstå hvordan folk bruker siden, og hvor mange som har
vært innom. Behovet er helt legitimt. Men standardverktøyet de fleste når etter,
kobler nettstedet ditt rett inn i den samme maskinen NRK beskriver.</p>

<h2>Cookie-banneret er symptomet, ikke løsningen</h2>
<p>Grunnen til at norske nettsider er dekket av samtykkebannere, er ekomloven
§ 3-15: skal du lagre eller lese noe på utstyret til den besøkende, må du spørre
først. Banneret er altså ikke et lovkrav i seg selv — det er konsekvensen av et
valg. Sporer du, må du spørre. Sporer du ikke, er det ingenting å spørre om.</p>
<p>For de fleste vanlige nettsteder — bedrifter, byråkunder, nettbutikker,
foreninger — er det verdt å snu på spørsmålet: hva får dere egentlig igjen for
sporingen banneret må be om lov til? Svaret er ofte en besøksrapport dere kunne
fått uten.</p>

<h2>Det går an å måle uten å spore</h2>
<p>Her er vi part i saken, og det skal vi være ærlige om: Sporløs lever av akkurat
dette. Vår tilnærming er å telle i stedet for å følge — hvor mange som besøker
siden, hvilke sider som leses, hvor folk kommer fra. Uten cookies, uten
fingerprinting, og uten at rå IP-adresse noen gang lagres: den besøkende er en
daglig-saltet enveis-hash som ikke kan føres tilbake til en person, og som
nullstilles hver natt. Dataene bor i Norge, og koden er
<a href="https://github.com/datamynt/sporlos" rel="noopener">åpen (AGPL)</a>,
så påstandene kan etterprøves.</p>
<p>Konsekvensen er at analysen ikke trenger samtykke — og dermed ikke banner. Ikke
fordi vi fant et smutthull, men fordi det ikke samles inn noe § 3-15 krever
samtykke til.</p>

<h2>Hva er egentlig oppsiden?</h2>
<p>Saken slutter med at Judin får spørsmål om det er verdt det å godta alt. Svaret
hans ender i at «det er uklart kva oppsida er for oss». For den som besøker
nettsider er oppsiden av sporingen, i beste fall, litt mer treffsikker reklame.</p>
<p>For deg som eier et nettsted er oppsiden av å slutte konkret: mindre juss å
holde styr på, et banner mindre mellom innholdet og leseren, en raskere side — og
tall du kan stå for når noen spør hva dere gjør med informasjonen folk legger
igjen.</p>
<p class=muted>Se hvordan det ser ut i praksis på <a href="/demo">live-demoen</a>,
eller les <a href="/sporsmal">spørsmål og svar</a> — inkludert det ærlige svaret på
om Google Analytics er lovlig i Norge.</p>
""",
    },
}
