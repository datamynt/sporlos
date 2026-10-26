// Runs integrations/shopify/sporlos-pixel.js in a fake Shopify Web Pixels sandbox and
// prints what it sent.  Driven by tests/test_shopify_pixel.py:
//   node shopify_pixel_harness.js <file> <scenario>
const fs = require("fs");
const vm = require("vm");

const [file, scenario] = process.argv.slice(2);
const sent = [];
const subs = {};
const consentSubs = [];

const sandbox = {
  analytics: { subscribe: (name, fn) => { (subs[name] = subs[name] || []).push(fn); } },
  fetch: (url, opts) => { sent.push(JSON.parse(opts.body)); },
  JSON, Object, Math, isFinite, Number, String, decodeURIComponent,
};
const init = { context: {}, data: {} };
const ctxNavigator = { userAgent: "Mozilla/5.0", language: "nb", cookieEnabled: true };
const nav = {};

if (scenario === "gpc" || scenario === "gpc-purchase") nav.globalPrivacyControl = true;
if (scenario === "dnt") nav.doNotTrack = "1";
if (scenario === "dnt-yes") nav.doNotTrack = "yes";
if (scenario === "dnt-ms") nav.msDoNotTrack = "1";
if (scenario === "dnt-off") nav.doNotTrack = "0";
if (scenario === "dnt-window") sandbox.doNotTrack = "1";
if (scenario === "ctx-gpc") ctxNavigator.globalPrivacyControl = true;
if (scenario === "ctx-dnt") ctxNavigator.doNotTrack = "1";
if (scenario === "webdriver") nav.webdriver = true;
if (scenario === "sale-off") init.customerPrivacy = { saleOfDataAllowed: false, analyticsProcessingAllowed: true };
if (scenario === "sale-on") init.customerPrivacy = { saleOfDataAllowed: true, analyticsProcessingAllowed: true };
if (scenario === "consent-event") {
  sandbox.customerPrivacy = { subscribe: (n, fn) => consentSubs.push(fn) };
}
if (scenario !== "no-navigator") sandbox.navigator = nav;
sandbox.init = init;
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(
  fs.readFileSync(file, "utf8").replace("DITT_SITE_ID_HER", "SITE"),
  sandbox,
);

const context = {
  navigator: ctxNavigator,
  document: {
    location: { pathname: "/products/x", search: "?utm_source=nyhetsbrev&email=ola@example.no" },
    referrer: "https://www.google.com/",
  },
};
const emit = (name, data) => (subs[name] || []).forEach((fn) => fn({ context, data }));

emit("page_viewed");
if (scenario === "consent-event") {
  consentSubs.forEach((fn) => fn({ customerPrivacy: { saleOfDataAllowed: false } }));
  emit("product_viewed");
}
if (scenario === "purchase" || scenario === "gpc-purchase") {
  emit("checkout_completed", { checkout: { totalPrice: { amount: 10 }, currencyCode: "NOK" } });
}
console.log(JSON.stringify(sent));
