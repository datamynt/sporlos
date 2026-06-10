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

  function send(name) {
    try {
      var body = JSON.stringify({
        s: site,
        n: name,
        p: location.pathname,        // ingen query-string => ingen utilsiktet PII
        r: document.referrer || null,
        us: utm.source || null,
        um: utm.medium || null,
        uc: utm.campaign || null,
      });
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

  // Egendefinerte hendelser: sporlos("signup") fra kundens egen kode.
  // Også auto: klikk på elementer med data-sporlos-event="navn".
  window.sporlos = function (name) { if (name) send(String(name)); };
  document.addEventListener("click", function (e) {
    var el = e.target.closest ? e.target.closest("[data-sporlos-event]") : null;
    if (el) send(el.getAttribute("data-sporlos-event"));
  });
})();
