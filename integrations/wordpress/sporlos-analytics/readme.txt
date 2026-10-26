=== Sporløs Analytics ===
Contributors: datamynt
Tags: analytics, privacy, cookieless, statistics, gdpr
Requires at least: 6.3
Tested up to: 7.0
Stable tag: 1.1.0
Requires PHP: 7.4
License: GPLv2 or later
License URI: https://www.gnu.org/licenses/gpl-2.0.html

Cookieless, consent-free web analytics — no cookie banner needed. Privacy-first, hosted in Norway, open source tracker.

== Description ==

Sporløs measures your website without cookies and without storing IP addresses,
and it keeps as little data as possible. Because nothing is ever stored or read on
the visitor's device, no consent is required under the EU ePrivacy rules and the
Norwegian Electronic Communications Act — which means **you can remove your
cookie banner**.

* No cookies, nothing written to the browser
* No IP addresses stored
* Individual page views are deleted after 90 days; only daily totals are kept
* Data hosted in Norway on European-owned infrastructure
* Lightweight script (~1.5 kB) that never slows your site down
* Page views, sources, UTM campaigns, goals, funnels and more
* The tracking script is open source (MIT) so anyone can verify the claims:
  https://github.com/datamynt/sporlos-tracker

This plugin adds the Sporløs tracking script to all public pages of your site.
You need an account at [sporlos.no](https://sporlos.no) (30-day free trial) or a
self-hosted Sporløs server. The service interface is currently in Norwegian.

= WooCommerce =

If WooCommerce is active, the plugin also reports completed purchases (revenue,
orders, best-selling products and revenue per source and payment method in your
Sporløs dashboard). One purchase is sent per paid order, from the order
confirmation page, and never twice for the same order. Turn it off under
Settings → Sporløs → "Spor WooCommerce-kjøp".

== Installation ==

1. Install and activate the plugin.
2. Go to Settings → Sporløs.
3. Paste the site ID from your Sporløs dashboard ("Show tracking code").

== Frequently Asked Questions ==

= Do I need a cookie banner? =

No. Sporløs never stores or reads anything on the visitor's device, so consent
is not required.

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

= What data is sent for a purchase? =

Only the order total, the currency, the product names with quantity and unit
price, and the payment gateway id as a short code (for example `bacs` or
`vipps`). Never the order number, e-mail address, name, address, phone number or
any other customer data, and Sporløs has no fields for them. Variation attributes
(size, colour, personalisation) are left out; only the catalogue product name is
sent. The address of the order confirmation page is recorded like any other
page, without its query string, and Sporløs replaces the order number in it
with `:id` before anything is stored.

= Which orders are counted? =

Orders with status Processing or Completed when the customer reaches the order
confirmation page. Failed, pending, cancelled and on-hold orders (for example
bank transfer, until you mark them paid) are not counted, and neither are orders
with a total of zero. Each order is marked once it has been reported, so
reloading the confirmation page, or opening it again later, does not count it
twice. The numbers come from the customer's browser and are indicative; use
WooCommerce for your accounts. Refunds are not subtracted.

= Is revenue reported with or without VAT? =

With VAT by default (the total the customer paid, including shipping and fees).
Under Settings → Sporløs you can choose "Uten mva" to report the order total
minus tax instead. Product unit prices follow the same choice.

= Does it work with HPOS and the block checkout? =

Yes. The plugin only uses the WooCommerce order API and declares compatibility
with High-Performance Order Storage.

== Changelog ==

= 1.1.0 =
* New: WooCommerce purchase tracking. One `purchase` event per paid order (Processing or Completed) from the order confirmation page: total, currency, product names, quantity, unit price and payment method code. Never order numbers or customer data. Each order is reported once.
* New settings: "Spor WooCommerce-kjøp" (on by default, does nothing without WooCommerce) and revenue with or without VAT.
* HPOS compatibility declared. Logged-in users stay excluded unless enabled.

= 1.0.1 =
* Wording: the plugin description and readme now match what Sporløs stores (no cookies, no IP addresses, page views deleted after 90 days). No code changes.

= 0.1.0 =
* First release: script enqueue with defer strategy, settings page, logged-in users excluded by default.
