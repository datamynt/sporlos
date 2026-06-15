/* Sporløs — Web Pixel-app-extension (Fase 2).
 *
 * Samme event-mapping og /api/event-kontrakt som Fase 1 (../../sporlos-pixel.js),
 * men site-ID leses fra app-settings (kjøpmann fyller inn ved installasjon) i stedet
 * for å hardkodes. Kjører i Shopifys strict sandbox.
 *
 * ⚠️ App-pixler er default «Optimized» (13. jan 2026) → kan strupes uten attribusjon.
 * Onboarding må be kjøpmann sette pixelen til «Always on». Se ../../README.md → Fase 2.
 */
import { register } from "@shopify/web-pixels-extension";

// Shopify-hendelse → Sporløs-hendelsesnavn (identisk med Fase 1).
const EVENTS = {
  product_viewed: "product_view",
  product_added_to_cart: "add_to_cart",
  checkout_started: "checkout_start",
  checkout_completed: "purchase",
  search_submitted: "search",
};

// Kun hvitlistede utm_*-nøkler — aldri hele query-strengen.
function utm(search) {
  const out = {};
  if (!search) return out;
  const s = search.charAt(0) === "?" ? search.slice(1) : search;
  for (const pair of s.split("&")) {
    const i = pair.indexOf("=");
    const k = i < 0 ? pair : pair.slice(0, i);
    let v = "";
    try { v = i < 0 ? "" : decodeURIComponent(pair.slice(i + 1).replace(/\+/g, " ")); } catch (e) {}
    if (k === "utm_source") out.source = v.slice(0, 120);
    else if (k === "utm_medium") out.medium = v.slice(0, 120);
    else if (k === "utm_campaign") out.campaign = v.slice(0, 120);
  }
  return out;
}

register(({ analytics, settings }) => {
  const siteId = (settings && settings.site_id) || "";
  const endpoint = (settings && settings.endpoint) || "https://sporlos.no/api/event";
  if (!siteId) return; // ikke konfigurert ennå

  function send(name, ctx) {
    try {
      const doc = (ctx && ctx.document) || {};
      const loc = doc.location || {};
      const u = utm(loc.search);
      const body = JSON.stringify({
        s: siteId,
        n: name,
        p: loc.pathname || "/",
        r: doc.referrer || null,
        us: u.source || null,
        um: u.medium || null,
        uc: u.campaign || null,
      });
      // Pixelen kjører i en Web Worker på Shopifys domene → POST til sporlos.no er
      // cross-origin. To krav: (1) ingen Content-Type-header => «simple request» =>
      // ingen CORS-preflight; (2) serveren MÅ svare med Access-Control-Allow-Origin.
      // sporlos.no/api/event setter ACAO: * (CORSMiddleware) — verifisert i prod.
      fetch(endpoint, { method: "POST", body, keepalive: true });
    } catch (e) { /* analytics skal aldri knekke butikken */ }
  }

  analytics.subscribe("page_viewed", (event) => send("pageview", event.context));
  for (const [shopifyName, sporlosName] of Object.entries(EVENTS)) {
    analytics.subscribe(shopifyName, (event) => send(sporlosName, event.context));
  }
});
