// Runs a tracker build inside a minimal fake browser and prints what it sent.
// Driven by tests/test_tracker.py:  node tracker_harness.js <file> <scenario>
const fs = require("fs");
const vm = require("vm");

const [file, scenario] = process.argv.slice(2);
const sent = [];
const listeners = { window: {}, document: {} };

function on(bucket) {
  return (type, fn) => { (bucket[type] = bucket[type] || []).push(fn); };
}
function fire(bucket, type, ev) {
  (bucket[type] || []).forEach((fn) => fn(ev || {}));
}

const location = { pathname: "/", search: "", origin: "https://shop.example" };
const document = {
  currentScript: {
    getAttribute: (k) => ({ "data-site": "SITE", "data-api": "https://sporlos.test/api/event" }[k] || null),
  },
  referrer: "https://www.google.com/search?q=secret+query",
  prerendering: false,
  addEventListener: on(listeners.document),
};
const navigator = {
  webdriver: false,
  sendBeacon: (url, body) => { sent.push(JSON.parse(body)); return true; },
};
const history = {
  pushState(state, title, url) {
    const u = new URL(url, location.origin + location.pathname);
    location.pathname = u.pathname;
    location.search = u.search;
  },
};

if (scenario === "webdriver") navigator.webdriver = true;
if (scenario === "prerender") document.prerendering = true;
if (scenario === "utm") location.search = "?utm_source=nyhetsbrev&utm_medium=epost&email=ola@example.no";

const sandbox = {
  document, navigator, history, location, URLSearchParams, URL,
  addEventListener: on(listeners.window),
  isFinite, Math, JSON, String, Array,
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(file, "utf8"), sandbox);

const go = (url) => sandbox.history.pushState({}, "", url);

switch (scenario) {
  case "spa":
    go("/produkter");
    go("/produkter?sort=pris");      // same page, only the query changed
    go("/produkter?sort=pris#topp");
    go("/kasse");
    break;
  case "popstate":
    go("/a");
    location.pathname = "/";
    fire(listeners.window, "popstate");
    break;
  case "bfcache":
    fire(listeners.window, "pageshow", { persisted: false });
    fire(listeners.window, "pageshow", { persisted: true });
    break;
  case "prerender":
    sent.push({ marker: "before-activation" });
    document.prerendering = false;
    fire(listeners.document, "prerenderingchange");
    break;
  case "custom":
    sandbox.sporlos("purchase", {
      revenue: 1198, currency: "nok", payment: " Vipps ",
      items: [{ name: "eSIM Europa 10 GB", qty: 2, price: 599 }],
    });
    sandbox.sporlos("signup");
    break;
}

process.stdout.write(JSON.stringify(sent));
