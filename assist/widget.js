/* Sporløs-assistenten — flytende chat som kjenner nettstedet.
   Standalone by design (ingen avhengighet til sidens CSS): dette er frøet til
   embed-produktet. Ingen cookies/lagring — samtalen lever kun i fanens minne. */
(function () {
  "use strict";
  if (window.__sporlosAssist) return;
  window.__sporlosAssist = 1;

  var API = (document.currentScript && document.currentScript.getAttribute("data-api")) || "/api/assist";
  var ACCENT = "#2f6fed", INK = "#17263e", LINE = "#e8e6e0", BG = "#faf9f6";
  var history = [];

  var css = [
    ".spl-btn{position:fixed;right:18px;bottom:18px;width:54px;height:54px;border-radius:50%;",
    "background:" + INK + ";border:0;box-shadow:0 8px 24px rgba(23,38,62,.30);",
    "cursor:pointer;z-index:99990;display:flex;align-items:center;justify-content:center;",
    "transition:transform .15s}.spl-btn:hover{transform:translateY(-2px)}",
    ".spl-panel{position:fixed;right:18px;bottom:84px;width:min(350px,calc(100vw - 36px));",
    "height:min(480px,calc(100vh - 120px));background:#fff;border:1px solid " + LINE + ";",
    "border-radius:16px;box-shadow:0 18px 50px rgba(23,38,62,.20);z-index:99991;",
    "display:none;flex-direction:column;overflow:hidden;",
    "font:15px/1.5 'Schibsted Grotesk',system-ui,sans-serif;color:" + INK + "}",
    ".spl-panel.open{display:flex}",
    ".spl-head{padding:.8rem 1rem;border-bottom:1px solid " + LINE + ";background:" + BG + "}",
    ".spl-head b{font-size:.95rem}.spl-head small{display:block;color:#5f6b7d;font-size:.72rem}",
    ".spl-msgs{flex:1;overflow-y:auto;padding:.8rem;display:flex;flex-direction:column;gap:.5rem}",
    ".spl-m{max-width:85%;padding:.5rem .75rem;border-radius:12px;white-space:pre-wrap;",
    "word-wrap:break-word;font-size:.9rem}",
    ".spl-m.u{align-self:flex-end;background:" + ACCENT + ";color:#fff;border-bottom-right-radius:4px}",
    ".spl-m.a{align-self:flex-start;background:" + BG + ";border:1px solid " + LINE + ";",
    "border-bottom-left-radius:4px}",
    ".spl-in{display:flex;gap:.5rem;padding:.7rem;border-top:1px solid " + LINE + "}",
    ".spl-in input{flex:1;padding:.55rem .7rem;border:1px solid " + LINE + ";border-radius:9px;",
    "font:inherit;font-size:.9rem;outline:none}.spl-in input:focus{border-color:" + ACCENT + "}",
    ".spl-in button{background:" + ACCENT + ";color:#fff;border:0;border-radius:9px;",
    "padding:.55rem .9rem;font:inherit;font-size:.9rem;font-weight:600;cursor:pointer}",
    ".spl-in button:disabled{opacity:.5;cursor:default}"
  ].join("");
  var style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  /* «Blekk»-merket fra app-ikonet: aksent-disk med blekk-strek på blekkmørk knapp */
  var MARK = '<svg width="34" height="34" viewBox="0 0 64 64" aria-hidden="true">' +
    '<circle cx="32" cy="32" r="26" fill="' + ACCENT + '"/>' +
    '<line x1="16" y1="52" x2="48" y2="12" stroke="' + INK + '" stroke-width="8" stroke-linecap="round"/></svg>';

  var btn = document.createElement("button");
  btn.className = "spl-btn";
  btn.setAttribute("aria-label", "Åpne chat");
  btn.innerHTML = MARK;

  var panel = document.createElement("div");
  panel.className = "spl-panel";
  panel.innerHTML =
    '<div class="spl-head"><b>Sporløs-assistenten</b>' +
    "<small>KI-assistent — kan ta feil. Ikke del personopplysninger.</small></div>" +
    '<div class="spl-msgs"></div>' +
    '<form class="spl-in"><input type="text" maxlength="1000" placeholder="Spør om Sporløs…" autocomplete="off">' +
    "<button type=\"submit\">Send</button></form>";

  document.body.appendChild(btn);
  document.body.appendChild(panel);

  var msgs = panel.querySelector(".spl-msgs");
  var form = panel.querySelector("form");
  var input = panel.querySelector("input");
  var send = panel.querySelector("form button");

  /* Kjente stier i assistent-svar gjøres klikkbare. Bygger DOM-noder (aldri
     innerHTML fra modelltekst) — ny fane så samtalen overlever klikket. */
  var PATHS = /(\/(?:integrasjoner(?:\/[a-z]+)?|blogg(?:\/[a-z0-9-]+)?|priser|demo|sporsmal|signup|vilkar|personvern|google-analytics-alternativ|utviklere|shopify|registrer|logg-inn|login|app))(?![\w-])/g;

  function render(el, text) {
    el.textContent = "";
    var last = 0, m;
    PATHS.lastIndex = 0;
    while ((m = PATHS.exec(text))) {
      if (m.index > last) el.appendChild(document.createTextNode(text.slice(last, m.index)));
      var a = document.createElement("a");
      a.href = m[1];
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = m[1];
      a.style.cssText = "color:" + ACCENT + ";font-weight:600";
      el.appendChild(a);
      last = m.index + m[1].length;
    }
    el.appendChild(document.createTextNode(text.slice(last)));
  }

  function add(role, text) {
    var d = document.createElement("div");
    d.className = "spl-m " + (role === "user" ? "u" : "a");
    if (role === "user") d.textContent = text;
    else render(d, text);
    msgs.appendChild(d);
    msgs.scrollTop = msgs.scrollHeight;
    return d;
  }

  btn.onclick = function () {
    panel.classList.toggle("open");
    if (panel.classList.contains("open")) {
      if (!msgs.childElementCount) {
        add("assistant", "Hei! Spør meg om Sporløs — priser, personvern, eller hvordan du kommer i gang.");
      }
      input.focus();
    }
  };

  form.onsubmit = function (e) {
    e.preventDefault();
    var q = input.value.trim();
    if (!q || send.disabled) return;
    input.value = "";
    add("user", q);
    history.push({ role: "user", content: q });
    var wait = add("assistant", "…");
    send.disabled = true;
    fetch(API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ q: q, history: history.slice(0, -1) })
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var svar = d.a || "Beklager, noe gikk galt.";
        render(wait, svar);
        history.push({ role: "assistant", content: svar });
      })
      .catch(function () {
        wait.textContent = "Beklager, fikk ikke kontakt. Prøv igjen — eller skriv til post@sporlos.no.";
      })
      .finally(function () {
        send.disabled = false;
        input.focus();
      });
  };
})();
