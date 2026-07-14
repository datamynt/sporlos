/* Sporløs — Shopify Custom Pixel (Fase 1)
 * Cookieløs, samtykke-fri webanalyse. Ingen cookies, ingen localStorage, ingen PII.
 *
 * Speiler /api/event-kontrakten fra tracker/sporlos.js, men leser hendelser fra
 * Shopifys Customer Events API. Pixelen kjører i Shopifys sandbox — derfor ingen
 * direkte tilgang til document/window/history; alt leses fra event.context.
 *
 * INSTALLERING (tar to minutter):
 *   1. Shopify-admin → Innstillinger → Kundehendelser → «Legg til egendefinert pixel».
 *   2. Gi den navnet «Sporløs» og lim inn HELE denne koden.
 *   3. Bytt SITE_ID under med din egen (Sporløs-dashbord → «Vis sporings-kode»).
 *   4. Klikk Lagre → Koble til.
 *
 * Fordel over et tema-snippet: dette måler også checkout-stegene — Shopify-checkout
 * ligger på et låst domene der vanlige snippets ikke får kjøre.
 *
 * Åpen kildekode (AGPL-3.0): https://github.com/datamynt/sporlos
 */

var SITE_ID = "DITT_SITE_ID_HER";               // ← BYTT MEG (f.eks. 6LIACtOSP-S7)
var ENDPOINT = "https://sporlos.no/api/event";  // self-hostere: bytt til eget domene

// Shopify-hendelse → Sporløs-hendelsesnavn. «pageview» og «checkout_completed»
// (som sender ordresum + produktlinjer) håndteres separat under.
var EVENTS = {
  product_viewed: "product_view",
  product_added_to_cart: "add_to_cart",
  checkout_started: "checkout_start",
  search_submitted: "search"
};

// Kun hvitlistede utm_*-nøkler — aldri hele query-strengen (samme regel som serveren).
// Manuell parsing: URLSearchParams er ikke garantert i Shopify-sandboxen.
function utm(search) {
  var out = {};
  if (!search) return out;
  var s = search.charAt(0) === "?" ? search.slice(1) : search;
  s.split("&").forEach(function (pair) {
    var i = pair.indexOf("=");
    var k = i < 0 ? pair : pair.slice(0, i);
    var v = "";
    try { v = i < 0 ? "" : decodeURIComponent(pair.slice(i + 1).replace(/\+/g, " ")); } catch (e) {}
    if (k === "utm_source") out.source = v.slice(0, 120);
    else if (k === "utm_medium") out.medium = v.slice(0, 120);
    else if (k === "utm_campaign") out.campaign = v.slice(0, 120);
  });
  return out;
}

function send(name, ctx, extra) {
  if (!SITE_ID || SITE_ID === "DITT_SITE_ID_HER") return;  // ikke konfigurert ennå
  try {
    var doc = (ctx && ctx.document) || {};
    var loc = doc.location || {};
    var u = utm(loc.search);
    var b = {
      s: SITE_ID,
      n: name,
      p: loc.pathname || "/",   // ingen query-string => ingen utilsiktet PII
      r: doc.referrer || null,
      us: u.source || null,
      um: u.medium || null,
      uc: u.campaign || null
    };
    if (extra) for (var k in extra) b[k] = extra[k];
    var body = JSON.stringify(b);
    // Ingen Content-Type-header => «simple request» => ingen CORS-preflight.
    // Serveren leser body som JSON uansett.
    fetch(ENDPOINT, { method: "POST", body: body, keepalive: true });
  } catch (e) { /* analytics skal aldri knekke butikken */ }
}

analytics.subscribe("page_viewed", function (event) {
  send("pageview", event.context);
});

Object.keys(EVENTS).forEach(function (shopifyName) {
  analytics.subscribe(shopifyName, function (event) {
    send(EVENTS[shopifyName], event.context);
  });
});

// Fullført kjøp: ordresum + produktlinjer (beløp i øre). Kun beløp og produktnavn
// sendes — aldri ordre-ID eller kundedata, så kjøp kan ikke kobles til person.
analytics.subscribe("checkout_completed", function (event) {
  var extra = null;
  try {
    var co = (event.data && event.data.checkout) || {};
    var money = function (m) {
      var a = m && Number(m.amount);
      return isFinite(a) && a >= 0 ? Math.round(a * 100) : null;
    };
    var o = {};
    var rv = money(co.totalPrice);
    if (rv !== null) o.rv = rv;
    if (co.currencyCode) o.cur = String(co.currencyCode).slice(0, 3);
    var it = [];
    (co.lineItems || []).slice(0, 25).forEach(function (li) {
      if (!li) return;
      var title = li.title || (li.variant && li.variant.product && li.variant.product.title);
      if (!title) return;
      var line = { n: String(title).slice(0, 160) };
      var q = Number(li.quantity);
      if (isFinite(q) && q >= 1) line.q = Math.min(999, Math.round(q));
      var p = money(li.variant && li.variant.price);
      if (p === null && li.finalLinePrice && q >= 1) {
        p = money({ amount: Number(li.finalLinePrice.amount) / q });
      }
      if (p !== null) line.p = p;
      it.push(line);
    });
    if (it.length) o.it = it;
    if (o.rv != null || o.it) extra = o;
  } catch (e) { extra = null; /* kjøpet telles uansett, bare uten beløp */ }
  send("purchase", event.context, extra);
});
