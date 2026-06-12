# Bidra til Sporløs

Takk for at du vil bidra! Noen rammer:

## Språket er norsk — med vilje

Kodebasen er gjennomført norsk: kommentarer, docstrings, commit-meldinger.
Det er ikke en tilfeldighet, det er identitet — Sporløs er bygget for det
norske markedet, helt ned i kommentarene. Bidrag på norsk foretrekkes;
engelsk aksepteres (vi oversetter ev. i review). English PRs are welcome —
we may translate comments during review.

## Det ufravikelige

Les personvern-prinsippene i [README](README.md#personvern-prinsippene-ufravikelige)
før du foreslår noe. PR-er som lagrer rå-IP, setter cookies/localStorage fra
trackeren, går finere enn fylkesnivå på geo, eller eksponerer rådata, blir
avvist uansett hvor nyttige de er. Begrensningen er produktet.

## Praktisk

- **Flyt:** fork → branch → PR mot `master`. Små, fokuserte PR-er.
- **Test:** `python -m py_compile` skal være rent, og endringer i tracker/widget
  skal gjennom `node --check`. Endrer du `tracker/sporlos.js`, må samme endring
  til [datamynt/sporlos-tracker](https://github.com/datamynt/sporlos-tracker).
- **Avhengigheter:** nye deps må begrunnes; ingen runtime-kall til tredjeparts
  CDN-er eller eksterne tjenester fra offentlige sider (personvernlinjen).
- **Lisens:** bidrag lisensieres under AGPL-3.0 som resten av prosjektet.
