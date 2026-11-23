// Manual Trading (S117): the "Your next trade" section keeps itself current. Every 10 seconds
// it reads /manual/now.json (same site, read-only) and updates the price-dependent figures and
// the recommendation. When the setup itself changes, it asks for a reload instead of redrawing.
// The page works the same without this script; nothing here sends or changes anything.
(function () {
  "use strict";
  var root = document.getElementById("live-now");
  if (!root || !window.fetch) {
    return;
  }
  var EVERY_MS = 10000; // the server reuses one reading for 10 s
  var shownSide = root.getAttribute("data-side") || "";

  function set(name, value) {
    var nodes = root.querySelectorAll('[data-live="' + name + '"]');
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].textContent = value === null || value === undefined ? "—" : String(value);
    }
  }

  function levelClass(level) {
    if (level === "TAKE") {
      return "tag tag-ok";
    }
    if (level === "YOUR CALL") {
      return "tag tag-warn";
    }
    return "tag tag-bad";
  }

  function show(d) {
    var side = d.side || "";
    var banner = root.querySelector("[data-live-banner]");
    if (banner && side !== shownSide) {
      banner.hidden = false; // a new setup (or none): its levels are drawn on a reload
    }
    set("advice", d.advice);
    var tag = root.querySelector('[data-live="advice"]');
    if (tag) {
      tag.className = levelClass(d.advice);
    }
    set("advice_why", d.advice_why);
    if (side && side === shownSide) {
      set("entry", d.entry);
      set("stop", d.stop);
      set("target", d.target);
      set("lots", d.lots);
      set("risk", d.risk);
      set("reward", d.reward);
    }
    var at = new Date(d.as_of);
    set("updated", "Updated " + at.toISOString().slice(11, 19) + " UTC" + (d.fresh ? "" : " · price not current"));
  }

  function tick() {
    if (document.hidden) {
      return;
    }
    fetch("/manual/now.json", { credentials: "same-origin", cache: "no-store" })
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (d) {
        if (d) {
          show(d);
        }
      })
      .catch(function () {
        /* a missed update: the next one comes in a few seconds */
      });
  }

  window.setInterval(tick, EVERY_MS);
})();
