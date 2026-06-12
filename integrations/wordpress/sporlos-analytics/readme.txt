=== Sporløs Analytics ===
Contributors: datamynt
Tags: analytics, privacy, cookieless, statistics, gdpr
Requires at least: 6.3
Tested up to: 7.0
Stable tag: 1.0.0
Requires PHP: 7.4
License: GPLv2 or later
License URI: https://www.gnu.org/licenses/gpl-2.0.html

Cookieless, consent-free web analytics — no cookie banner needed. Privacy-first, hosted in Norway, open source tracker.

== Description ==

Sporløs measures your website without cookies, without storing IP addresses and
without collecting any personal data. Because nothing is ever stored or read on
the visitor's device, no consent is required under the EU ePrivacy rules and the
Norwegian Electronic Communications Act — which means **you can remove your
cookie banner**.

* No cookies, nothing written to the browser
* No IP addresses stored
* Data hosted in Norway on European-owned infrastructure
* Lightweight script (~1.5 kB) that never slows your site down
* Page views, sources, UTM campaigns, goals, funnels and more
* The tracking script is open source (MIT) so anyone can verify the claims:
  https://github.com/datamynt/sporlos-tracker

This plugin adds the Sporløs tracking script to all public pages of your site.
You need an account at [sporlos.no](https://sporlos.no) (30-day free trial) or a
self-hosted Sporløs server. The service interface is currently in Norwegian.

== Installation ==

1. Install and activate the plugin.
2. Go to Settings → Sporløs.
3. Paste the site ID from your Sporløs dashboard ("Show tracking code").

== Frequently Asked Questions ==

= Do I need a cookie banner? =

No. Sporløs never stores anything on the visitor's device and collects no
personal data, so consent is not required.

= Are logged-in users tracked? =

By default no, so your own editors do not pollute the numbers. You can enable
it under Settings → Sporløs.

= Can I self-host? =

Yes. Sporløs' tracker is open source and the service supports self-hosting.
Point the "Sporløs server" setting to your own installation.

= What data is sent? =

Site ID, page path, referrer and whitelisted utm_source/utm_medium/utm_campaign
parameters. Never the full URL, never cookies, never fingerprints. The script
source is public: https://github.com/datamynt/sporlos-tracker

== Changelog ==

= 0.1.0 =
* First release: script enqueue with defer strategy, settings page, logged-in users excluded by default.
