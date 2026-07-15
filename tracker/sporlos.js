/* Sporløs tracker — cookieløs, ingen localStorage, ingen fingerprint.
 * Bygg inn:  <script defer data-site="PUBLIC_ID" src="https://.../sporlos.js"></script>
 * Sender kun: site-id, sti, referrer-kilde + hvitlistede utm_*-kampanjeparametre.
 * Ingen PII, ingen samtykke nødvendig.
 * Åpen kildekode (MIT): https://github.com/datamynt/sporlos-tracker */
(function () {
  var s = document.currentScript;
  var site = s && s.getAttribute("data-site");
  var endpoint = (s && s.getAttribute("data-api")) || "/api/event";
  if (!site) return;

  // Kampanjeparametre: KUN hvitlistede utm_*-nøkler — aldri hele query-strengen.
  var utm = {};
  try {
    var q = new URLSearchParams(location.search);
    ["source", "medium", "campaign"].forEach(function (k) {
      var v = q.get("utm_" + k);
      if (v) utm[k] = v.slice(0, 120);
    });
  } catch (e) { /* eldre nettlesere: dropp kampanjedata, mål resten */ }

  function send(name, extra) {
    try {
      var b = {
        s: site,
        n: name,
        p: location.pathname,        // ingen query-string => ingen utilsiktet PII
        r: document.referrer || null,
        us: utm.source || null,
        um: utm.medium || null,
        uc: utm.campaign || null,
      };
      if (extra) for (var k in extra) b[k] = extra[k];
      var body = JSON.stringify(b);
      // sendBeacon overlever sidebytte og blokkerer ikke navigasjon
      if (navigator.sendBeacon) {
        navigator.sendBeacon(endpoint, body);
      } else {
        fetch(endpoint, { method: "POST", body: body, keepalive: true });
      }
    } catch (e) { /* analytics skal aldri knekke siden */ }
  }

  send("pageview");

  // SPA-støtte: re-send ved history-navigasjon
  var push = history.pushState;
  history.pushState = function () {
    push.apply(this, arguments);
    send("pageview");
  };
  addEventListener("popstate", function () { send("pageview"); });

  // E-handel: beløp i KRONER inn, øre (heltall) over ledningen. Kun beløp og
  // produktnavn — det finnes ikke felt for ordre-ID eller kundedata, og du skal
  // heller ikke sende dem (unngå ordrenummer/personaliserte navn i name-feltet).
  //   sporlos("purchase", {revenue: 1198, currency: "NOK",
  //     items: [{name: "eSIM Europa 10 GB", qty: 1, price: 599}]});
  function money(v) {
    return typeof v === "number" && isFinite(v) && v >= 0 ? Math.round(v * 100) : null;
  }
  function ecom(d) {
    if (!d || typeof d !== "object") return null;
    var o = {}, rv = money(d.revenue);
    if (rv !== null) o.rv = rv;
    if (typeof d.currency === "string") o.cur = d.currency.toUpperCase().slice(0, 3);
    if (Array.isArray(d.items)) {
      var it = [];
      d.items.slice(0, 25).forEach(function (x) {
        if (!x || typeof x.name !== "string") return;
        var line = { n: x.name.slice(0, 160) };
        if (typeof x.qty === "number" && x.qty >= 1) line.q = Math.min(999, Math.round(x.qty));
        var p = money(x.price);
        if (p !== null) line.p = p;
        it.push(line);
      });
      if (it.length) o.it = it;
    }
    return o.rv != null || o.it ? o : null;
  }

  // Egendefinerte hendelser: sporlos("signup") fra kundens egen kode.
  // Også auto: klikk på elementer med data-sporlos-event="navn".
  window.sporlos = function (name, data) {
    if (!name) return;
    var extra = null;
    try { extra = ecom(data); } catch (e) { /* ugyldig data ⇒ send hendelsen uten */ }
    send(String(name), extra);
  };
  document.addEventListener("click", function (e) {
    var el = e.target.closest ? e.target.closest("[data-sporlos-event]") : null;
    if (el) send(el.getAttribute("data-sporlos-event"));
  });
})();
