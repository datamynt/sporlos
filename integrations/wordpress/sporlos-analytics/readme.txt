=== Sporløs Analytics ===
Contributors: datamynt
Tags: analytics, statistikk, personvern, gdpr, cookieless
Requires at least: 5.0
Tested up to: 6.8
Stable tag: 0.1.0
Requires PHP: 7.4
License: GPLv2 or later
License URI: https://www.gnu.org/licenses/gpl-2.0.html

Cookieløs, samtykke-fri webanalyse — uten cookie-banner. Norsk, åpen og personvennlig.

== Description ==

Sporløs måler nettstedet ditt uten cookies, uten å lagre IP-adresser og uten å
samle personopplysninger. Dermed utløses verken samtykkekravet i ekomloven § 3-15
eller GDPR — og du kan fjerne cookie-banneret.

* Ingen cookies, ingen lagring i nettleseren
* Ingen IP-adresser lagret
* Data i Norge, på norsk-eid infrastruktur
* Lett script (~1,5 kB) som ikke bremser siden
* Sidevisninger, kilder, UTM-kampanjer, mål, funnels og mer

Pluginen legger inn Sporløs-snippeten på alle offentlige sider. Du trenger en
konto på [sporlos.no](https://sporlos.no) (30 dagers gratis prøve) eller en
self-hostet Sporløs-server.

== Installation ==

1. Installer og aktiver pluginen.
2. Gå til Innstillinger → Sporløs.
3. Lim inn site-ID-en fra Sporløs-dashbordet («Vis sporings-kode»).

== Frequently Asked Questions ==

= Trenger jeg cookie-banner? =

Nei. Sporløs lagrer ingenting på besøkerens enhet og samler ingen
personopplysninger, så samtykke kreves ikke.

= Måles innloggede brukere? =

Som standard nei (så egne redaktører ikke forstyrrer tallene). Kan slås på
under Innstillinger → Sporløs.

= Kan jeg self-hoste? =

Ja, Sporløs er åpen kildekode. Pek «Sporløs-server» på din egen installasjon.

== Changelog ==

= 0.1.0 =
* Første versjon: snippet-injeksjon, innstillingsside, hopp over innloggede brukere.
