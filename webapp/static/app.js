/* app.js — data fetching + DOM wiring for the Powerwall synthwave dashboard. */
(function () {
  "use strict";

  const VERSION = "3.4";

  // Most-recent first. Shown in the version-history modal.
  // Tenth-point releases only; notes consolidated.
  const CHANGELOG = [
    ["3.4", "A separate home dashboard page can now show live numbers from here. Set DASHBOARD_ORIGINS in compose.env to that page's address to allow it; it can only read. With that set, a HOME button appears beside the settings gear and takes you back to it."],
    ["3.3", "The HISTORY bill box now also shows your running account BALANCE — the cumulative credit (or amount due). Credits carry forward and amounts due auto-pay once the banked credit runs out, matching how the utility rolls the balance. Pick any billing period to see the balance as of that bill; the current cycle folds in the estimate."],
    ["3.2", "HISTORY now shows your actual utility bill for a billed cycle (from the amounts on file) and falls back to the estimate — marked with a ~ — only for the in-progress cycle. The most recent billed cycle was added to the period picker. RECORDS gains an ALL USED metric (whole-home consumption, cars included) alongside HOUSE USED (house only)."],
    ["3.1", "RECORDS now lists the top 5 and bottom 5 (not just one each) and follows the combined / panel-a / panel-b selector at the top. HISTORY shows an estimated bill when you pick a billing period — fixed monthly charge plus net grid priced at your import or export/credit rate — with the fixed charge editable in settings."],
    ["3.0", "New RECORDS section (best & worst): pick a metric — solar made, house used, or a specific car charged — and a period — day, week, month, or year — to see the most and least ever, with the date it happened. Only complete, already-ended periods count, so a partial current day/month can't set a false record."],
    ["2.9", "HISTORY's BILLING PERIOD choice now opens a second picker listing your real utility billing cycles (newest first) plus the current estimated period — pick any one to view its totals and charts, not just the current cycle. Billing/custom and other multi-day totals are now sourced from the complete hourly rollup, so they match the meter (earlier they undercounted anything before mid-June). The settings 'billing start day' is now just a fallback used until real bills are on file."],
    ["2.8", "With ON SOLAR on, each charging donut now breaks into four wedges — peak grid, peak solar, off-peak grid, off-peak solar — with a labelled breakdown under it, so you can see how much of each period's charging came from the grid vs your own solar. Grid keeps the vivid peak/off-peak colors; solar is the lighter tint."],
    ["2.7", "CHARGING pies now split each car's peak/off-peak totals right beside the donut, and attribute charging to grid vs your own solar/battery per panel. An ON SOLAR toggle in the section header: on (default) counts all charging by time of day — what the utility sees, since it reads the car — while off shows only what was pulled from the grid while charging, so solar charging during peak no longer looks like grid peak use."],
    ["2.6", "The billing-period dropdown now follows the real utility statement cycles (their start day drifts month to month) instead of a fixed day-of-month, with the in-progress cycle estimated from the typical cycle length. The BILLING PERIOD range lines up with the current real cycle too."],
    ["2.5", "New CHARGING section: a pie per car splitting its charging into peak vs off-peak kWh, with a billing-period dropdown to page back through cycles. Peak is 1PM–9PM every day, editable in settings along with optional weekend/holiday carve-outs."],
    ["2.4", "Every tile expands a breakdown panel right below it (one at a time): per-panel splits on COMBINED, home split into panels + renamable chargers. MONTHLY gets its own section — 6 months at a time with ◂ ▸ paging. Phone layout polish: side-by-side tiles, one-line headers."],
    ["2.3", "Collapsible LIVE / HISTORY / MONTHLY sections in boxed layout with live tiles up top; clock + refresh next to LIVE. Monthly chart rebuilt on true calendar months (it had frozen). Version history lives in settings; version shown on the gear."],
    ["2.2", "Settings page (gear): site name, panel labels, rates, capacities, timezone — applied live, 1-3 gateways. Ranges: billing period, lifetime, custom dates; house or local time display. Phone app updates itself; chart time labels land on round times."],
    ["2.1", "Solar-vs-usage balance card + cumulative chart; today/month/year ranges; auto-refresh control; custom site name; Feb–May history backfilled from the Tesla app; HOME USED = car + house."],
  ];

  const C = {
    solar: "#ffd319", home: "#00f0ff", grid: "#ff2e97",
    battery: "#b14aed", soc: "#2bff88", green: "#2bff88",
  };

  let range = "today";
  let system = "combined";
  let customStart = null, customEnd = null;   // ms epoch, for range === "custom"

  // Loaded from /api/settings before first render; drives branding, the
  // system toggle, and per-gateway cards. DEFAULTS backs the reset button.
  let SETTINGS = null;
  let DEFAULTS = null;

  // Friendly labels for the balance header; "to date" = since start of period.
  const RANGE_LABELS = {
    today: "TODAY", "24h": "LAST 24H", "7d": "LAST 7 DAYS",
    month: "MONTH TO DATE", "30d": "LAST 30 DAYS", year: "YEAR TO DATE",
    billing: "BILLING PERIOD", lifetime: "LIFETIME",
  };
  // Ranges spanning multiple days use date (not time) axis labels.
  const MULTI_DAY = new Set(["7d", "month", "30d", "year", "billing", "lifetime"]);

  function isMultiDay() {
    if (range === "custom") {
      return (customEnd - customStart) > 48 * 3600 * 1000;
    }
    return MULTI_DAY.has(range);
  }

  // Query-string for the current range (custom carries explicit bounds).
  function rangeQS() {
    if (range === "custom") return `range=custom&start=${customStart}&end=${customEnd}`;
    // billing drives off the period chosen in the history picker (known bill or
    // the estimated current one); before periods load, fall back to the API's
    // "current cycle to date".
    if (range === "billing") {
      const p = histBillPeriod();
      if (p) return `range=custom&start=${p.start}&end=${p.end}`;
      return "range=billing";
    }
    return `range=${range}`;
  }

  // Timezone the UI should render times in: undefined = viewer's browser
  // (local), or the configured house timezone when the setting says so.
  function dispTZ() {
    return (SETTINGS && SETTINGS.time_display === "house") ? SETTINGS.timezone : undefined;
  }

  // Curated house-timezone choices for the settings dropdown; the saved value
  // is injected on top if it isn't in the list (so nothing ever breaks).
  const TZ_CHOICES = [
    "America/New_York", "America/Chicago", "America/Denver", "America/Phoenix",
    "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu",
    "America/Toronto", "America/Vancouver", "America/Mexico_City",
    "Europe/London", "Europe/Dublin", "Europe/Paris", "Europe/Berlin",
    "Europe/Madrid", "Europe/Rome", "Europe/Amsterdam", "Europe/Stockholm",
    "Europe/Warsaw", "Europe/Athens", "Australia/Sydney", "Australia/Melbourne",
    "Australia/Brisbane", "Australia/Perth", "Pacific/Auckland", "Asia/Tokyo",
    "Asia/Seoul", "Asia/Shanghai", "Asia/Hong_Kong", "Asia/Singapore",
    "Asia/Kolkata", "Asia/Dubai", "Africa/Johannesburg", "America/Sao_Paulo",
    "America/Santiago", "America/Bogota", "UTC",
  ];

  const $ = sel => document.querySelector(sel);
  const $$ = sel => Array.from(document.querySelectorAll(sel));

  async function getJSON(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(url + " -> " + r.status);
    return r.json();
  }

  function kw(w) { return (w / 1000); }
  function fmtKw(w) {
    const v = kw(w);
    return (Math.abs(v) < 10 ? v.toFixed(2) : v.toFixed(1));
  }

  function setConn(ok) {
    const dot = $("#conn-dot");
    dot.classList.toggle("ok", ok);
    dot.classList.toggle("bad", !ok);
    dot.title = ok ? "live" : "offline";
  }

  function tick() {
    const d = new Date();
    $("#clock").textContent = d.toLocaleTimeString([], {
      hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: dispTZ(),
    });
  }

  // ---------- LIVE ----------
  async function refreshLive() {
    try {
      const data = await getJSON("/api/live");
      setConn(true);
      lastLive = data;
      const s = data.systems[system] || data.systems.combined;
      if (!s) return;

      setTile("solar", s.solar, "kW", s.solar > 50 ? "generating" : "idle");
      setTile("home", s.home, "kW", "consuming");

      // grid: + import, - export
      const gImport = s.grid >= 0;
      setTile("grid", Math.abs(s.grid), "kW", Math.abs(s.grid) < 30 ? "idle" : (gImport ? "importing" : "exporting"));
      flowClass(".tile-grid", Math.abs(s.grid) < 30 ? null : (gImport ? "out" : "in"));

      // battery: + discharging, - charging
      const bDis = s.battery >= 0;
      setTile("battery", Math.abs(s.battery), "kW", Math.abs(s.battery) < 30 ? "idle" : (bDis ? "discharging" : "charging"));
      flowClass(".tile-battery", Math.abs(s.battery) < 30 ? null : (bDis ? "out" : "in"));

      const soc = Math.round(s.soc);
      $("#soc-fill").style.width = soc + "%";
      $("#soc-text").textContent = soc + "%";

      setTile("self", s.self_sufficiency, "%", "solar + battery", true);

      // per-gateway cards
      for (const gw of SETTINGS.gateways) renderPanel(gw.tag, data.systems[gw.tag]);
      syncSplits();

      $("#last-update").textContent = new Date().toLocaleTimeString([], { timeZone: dispTZ() });
    } catch (e) {
      console.error(e);
      setConn(false);
    }
  }

  function setTile(key, valW, unit, sub, isPct) {
    const tile = $(`.tile[data-k="${key}"]`);
    if (!tile) return;
    const numEl = tile.querySelector(".num");
    const newVal = isPct ? Math.round(valW) + "" : fmtKw(valW);
    if (numEl.textContent !== newVal) {
      numEl.textContent = newVal;
      tile.classList.remove("flash"); void tile.offsetWidth; tile.classList.add("flash");
    }
    tile.querySelector(".unit").textContent = unit;
    if (sub) tile.querySelector(".tile-sub").textContent = sub;
  }

  // ---------- expandable tile splits (accordion: one open at a time) ----------
  // Every LIVE / HISTORY tile can expand a connected panel. Per-gateway
  // breakdowns only make sense on COMBINED; the house/charger splits scope
  // themselves to the selected system instead.
  let lastLive = null, lastTotals = null;
  let activeSplit = localStorage.getItem("activeSplit") || "";

  function splitGateways() {
    return system === "combined"
      ? SETTINGS.gateways
      : SETTINGS.gateways.filter(g => g.tag === system);
  }

  const pct = v => Math.round(v) + "";

  // one tile per gateway showing a live metric
  function gwLive(metric, unit, accent, fmt) {
    return SETTINGS.gateways.map(gw => {
      const s = (lastLive && lastLive.systems[gw.tag]) || {};
      let v = s[metric] || 0, sub = "";
      if (metric === "grid") {
        sub = Math.abs(v) < 30 ? "idle" : (v >= 0 ? "importing" : "exporting");
        v = Math.abs(v);
      }
      if (metric === "battery") {
        sub = Math.abs(v) < 30 ? "idle" : (v >= 0 ? "discharging" : "charging");
        v = Math.abs(v);
      }
      return { key: gw.tag, label: gw.label, unit, accent,
               val: fmt ? fmt(v) : fmtKw(v), sub };
    });
  }

  // one tile per gateway showing a period total
  function gwPeriod(metric, unit, accent) {
    const by = (lastTotals && lastTotals.by_system) || {};
    return SETTINGS.gateways.map(gw => ({
      key: gw.tag, label: gw.label, unit, accent,
      val: fmt1((by[gw.tag] || {})[metric]),
    }));
  }

  function chargerLabel(gw) {
    return gw.charger_label || "CHARGER " + (SETTINGS.gateways.indexOf(gw) + 1);
  }

  // Split home into per-panel house-only loads + one tile per charger:
  // PANEL A, PANEL B, CHARGER 1, CHARGER 2 (scoped to the selected system).
  function liveHouseItems() {
    if (!lastLive) return [];
    const gws = splitGateways();
    const part = gw => {
      const p = lastLive.systems[gw.tag] || {};
      const home = p.home || 0;
      // live EV estimate can overcount — never let a charger exceed its panel
      const ev = Math.min(Math.max(p.ev_load || 0, 0), home);
      return { home, ev };
    };
    return [
      ...gws.map(gw => ({ key: "house_" + gw.tag, label: gw.label,
                          unit: "kW", accent: C.home,
                          val: fmtKw(Math.max(part(gw).home - part(gw).ev, 0)),
                          sub: "house only" })),
      ...gws.map(gw => {
        const { ev } = part(gw);
        return { key: "chg_" + gw.tag, label: chargerLabel(gw),
                 unit: "kW", accent: C.battery,
                 val: fmtKw(ev), sub: ev > 100 ? "charging" : "idle" };
      }),
    ];
  }

  function periodHouseItems() {
    if (!lastTotals) return [];
    const by = lastTotals.by_system || {};
    const chargers = lastTotals.totals.chargers || {};
    const gws = splitGateways();
    const part = gw => {
      const home = (by[gw.tag] || {}).home || 0;
      const ev = Math.min(chargers[gw.tag] || 0, home);
      return { home, ev };
    };
    return [
      ...gws.map(gw => ({ key: "house_" + gw.tag, label: gw.label,
                          unit: "kWh", accent: C.home,
                          val: fmt1(Math.max(part(gw).home - part(gw).ev, 0)),
                          sub: "house only" })),
      ...gws.map(gw => ({ key: "chg_" + gw.tag, label: chargerLabel(gw),
                          unit: "kWh", accent: C.battery, val: fmt1(part(gw).ev) })),
    ];
  }

  // comb: split only offered on COMBINED. panels: shows the gateway cards.
  const SPLITS = {
    "l-solar": { tile: '#live-tiles .tile[data-k="solar"]', area: "live", comb: true,
                 accent: C.solar, items: () => gwLive("solar", "kW", C.solar) },
    "l-home":  { tile: '#live-tiles .tile[data-k="home"]', area: "live", comb: false,
                 accent: C.home, items: liveHouseItems },
    "l-grid":  { tile: '#live-tiles .tile[data-k="grid"]', area: "live", comb: true,
                 accent: C.grid, items: () => gwLive("grid", "kW", C.grid) },
    "l-batt":  { tile: '#live-tiles .tile[data-k="battery"]', area: "live", comb: true,
                 accent: C.battery, panels: true },
    "l-self":  { tile: '#live-tiles .tile[data-k="self"]', area: "live", comb: true,
                 accent: C.green, items: () => gwLive("self_sufficiency", "%", C.green, pct) },
    "b-solar": { tile: "#bal-solar-box", area: "bal", comb: true,
                 accent: C.solar, items: () => gwPeriod("solar", "kWh", C.solar) },
    "b-home":  { tile: "#bal-home-box", area: "bal", comb: false,
                 accent: C.home, items: periodHouseItems },
    "b-net":   { tile: "#bal-net-box", area: "bal", comb: true,
                 accent: C.green, items: () => gwPeriod("net", "kWh", C.green) },
    "s-solar": { tile: '.stat[data-k="solar"]', area: "stats", comb: true,
                 accent: C.solar, items: () => gwPeriod("solar", "kWh", C.solar) },
    "s-home":  { tile: '.stat[data-k="home"]', area: "stats", comb: true,
                 accent: C.home, items: () => gwPeriod("home", "kWh", C.home) },
    "s-fgrid": { tile: '.stat[data-k="from_grid"]', area: "stats", comb: true,
                 accent: C.grid, items: () => gwPeriod("from_grid", "kWh", C.grid) },
    "s-tgrid": { tile: '.stat[data-k="to_grid"]', area: "stats", comb: true,
                 accent: C.green, items: () => gwPeriod("to_grid", "kWh", C.green) },
    "s-fpw":   { tile: '.stat[data-k="from_pw"]', area: "stats", comb: true,
                 accent: C.battery, items: () => gwPeriod("from_pw", "kWh", C.battery) },
    "s-self":  { tile: '.stat[data-k="self_sufficiency"]', area: "stats", comb: true,
                 accent: C.green, items: () => gwPeriod("self_sufficiency", "%", C.green) },
    "s-save":  { tile: '.stat[data-k="savings"]', area: "stats", comb: true,
                 accent: C.green, items: () => gwPeriod("savings", "$", C.green) },
  };
  const AREAS = {
    live: ["#live-wrap", "#live-split"],
    bal: ["#bal-wrap", "#bal-split"],
    stats: ["#stats-wrap", "#stats-split"],
  };

  function splitAvailable(def) {
    return def.comb ? (SETTINGS.gateways.length > 1 && system === "combined") : true;
  }

  function renderSplitItems(el, items) {
    const want = items.map(i => i.key).join("|");
    if (el.dataset.keys !== want) {
      el.dataset.keys = want;
      el.innerHTML = items.map(i =>
        `<div class="tile" data-k="${i.key}">` +
        '<div class="tile-label"></div>' +
        '<div class="tile-value"><span class="num">--</span><span class="unit"></span></div>' +
        '<div class="tile-sub"></div></div>'
      ).join("");
    }
    for (const i of items) {
      const tile = el.querySelector(`[data-k="${i.key}"]`);
      if (!tile) continue;
      tile.style.setProperty("--accent", i.accent);
      tile.querySelector(".tile-label").textContent = i.label;
      tile.querySelector(".num").textContent = i.val;
      tile.querySelector(".unit").textContent = i.unit;
      tile.querySelector(".tile-sub").textContent = i.sub || "";
    }
  }

  // Re-home the wrap inside the tile grid, on its own full-width row directly
  // below the clicked tile's row (phones stack tiles 1-2 per row, so panels
  // parked at the end of the section went unnoticed).
  function placeWrap(wrap, tile) {
    const rowEls = Array.from(tile.parentElement.children).filter(el =>
      el !== wrap && Math.abs(el.offsetTop - tile.offsetTop) < 2);
    const anchor = rowEls[rowEls.length - 1] || tile;
    if (anchor.nextElementSibling !== wrap) anchor.after(wrap);
  }

  function syncSplits() {
    if (!SETTINGS) return;
    for (const [id, def] of Object.entries(SPLITS)) {
      const tile = $(def.tile);
      if (!tile) continue;
      const avail = splitAvailable(def);
      if (!avail && activeSplit === id) activeSplit = "";
      tile.classList.toggle("tile-click", avail);
      let caret = tile.querySelector(".tile-caret");
      if (!caret) {
        caret = document.createElement("span");
        caret.className = "tile-caret";
        tile.prepend(caret);
      }
      caret.hidden = !avail;
      caret.textContent = activeSplit === id ? "▾" : "▸";
    }
    for (const [area, [wrapSel, contSel]] of Object.entries(AREAS)) {
      const wrap = $(wrapSel), cont = $(contSel);
      const def = SPLITS[activeSplit];
      const on = !!def && def.area === area;
      wrap.hidden = !on;
      if (area === "live") $("#panels").hidden = !(on && def && def.panels);
      if (!on) continue;
      placeWrap(wrap, $(def.tile));
      cont.hidden = !!def.panels;
      wrap.style.setProperty("--sw", def.accent);
      if (def.items) renderSplitItems(cont, def.items());
      const p = $(def.tile).getBoundingClientRect(), w = wrap.getBoundingClientRect();
      wrap.style.setProperty("--notch-x", Math.max(p.left + p.width / 2 - w.left - 8, 12) + "px");
    }
  }

  function bindSplits() {
    for (const [id, def] of Object.entries(SPLITS)) {
      const tile = $(def.tile);
      if (!tile) continue;
      tile.setAttribute("role", "button");
      tile.tabIndex = 0;
      const toggle = () => {
        if (!splitAvailable(def)) return;
        activeSplit = activeSplit === id ? "" : id;
        localStorage.setItem("activeSplit", activeSplit);
        syncSplits();
        if (activeSplit === id) {
          $(AREAS[def.area][0]).scrollIntoView({ behavior: "smooth", block: "nearest" });
        }
      };
      tile.addEventListener("click", toggle);
      tile.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
      });
    }
  }

  function flowClass(sel, dir) {
    const el = $(sel);
    el.classList.toggle("flow-in", dir === "in");
    el.classList.toggle("flow-out", dir === "out");
  }

  function renderPanel(tag, s) {
    const card = document.getElementById(`panel-${tag}`);
    if (!card) return;
    if (!s) { card.style.opacity = 0.4; return; }
    card.style.opacity = 1;
    card.querySelector(".panel-soc").textContent = Math.round(s.soc) + "%";
    const cells = [
      ["SOLAR", fmtKw(s.solar), "kW", C.solar],
      ["HOME", fmtKw(s.home), "kW", C.home],
      ["GRID", fmtKw(Math.abs(s.grid)), "kW", C.grid],
      ["BATT", fmtKw(Math.abs(s.battery)), "kW", C.battery],
    ];
    card.querySelector(".panel-grid").innerHTML = cells.map(([l, v, u, c]) =>
      `<div class="pm" style="--c:${c}"><div class="l">${l}</div><div class="v">${v} <i>${u}</i></div></div>`
    ).join("");
  }

  // ---------- HISTORY ----------
  async function refreshHistory() {
    try {
      const data = await getJSON(`/api/history?${rangeQS()}&system=${system}`);
      const s = data.series;
      const timeAxis = { multiDay: isMultiDay(), tz: dispTZ() };

      NeonChart.line($("#chart-power"), {
        labels: s.t,
        zero: true,
        timeAxis,
        series: [
          { name: "Solar", color: C.solar, data: s.solar },
          { name: "Home", color: C.home, data: s.home },
          { name: "Grid", color: C.grid, data: s.grid },
          { name: "Battery", color: C.battery, data: s.battery },
        ],
      });
      legend("#power-legend", [
        ["Solar", C.solar], ["Home", C.home], ["Grid +in/-out", C.grid], ["Batt +dis/-chg", C.battery],
      ]);

      NeonChart.line($("#chart-soc"), {
        labels: s.t,
        timeAxis,
        series: [{ name: "SOC", color: C.soc, data: s.soc, fill: true }],
      });

      renderCumulative(s, timeAxis);
    } catch (e) { console.error(e); }
  }

  // Running solar-vs-usage energy (kWh) over the period; crossover = ahead/behind.
  function cumulative(power, ts) {
    const out = []; let acc = 0;
    for (let i = 0; i < power.length; i++) {
      if (i > 0 && power[i] != null) {
        const dtH = (ts[i] - ts[i - 1]) / 3.6e6;   // ms -> hours
        acc += (power[i] * dtH) / 1000;             // W·h -> kWh
      }
      out.push(acc);
    }
    return out;
  }

  function renderCumulative(s, timeAxis) {
    const solar = cumulative(s.solar, s.t);
    const home = cumulative(s.home, s.t);
    NeonChart.line($("#chart-cumulative"), {
      labels: s.t,
      timeAxis,
      series: [
        { name: "Solar", color: C.solar, data: solar, fill: true },
        { name: "Home", color: C.home, data: home },
      ],
    });
    legend("#cumulative-legend", [["Solar made", C.solar], ["Home used", C.home]]);
  }

  function legend(sel, items) {
    $(sel).innerHTML = items.map(([n, c]) => `<span style="--c:${c}">${n}</span>`).join("");
  }

  // ---------- TOTALS ----------
  async function refreshTotals() {
    try {
      const data = await getJSON(`/api/totals?${rangeQS()}&system=${system}`);
      lastTotals = data;
      const t = data.totals;
      setStat("solar", t.solar);
      setStat("home", t.home);
      setStat("from_grid", t.from_grid);
      setStat("to_grid", t.to_grid);
      setStat("from_pw", t.from_pw);
      setStat("self_sufficiency", t.self_sufficiency);
      setStat("savings", t.savings);
      renderBalance(t);
      syncSplits();
    } catch (e) { console.error(e); }
  }

  function fmt1(v) {
    return (v == null) ? "--" : Number(v).toLocaleString(undefined, { maximumFractionDigits: 1 });
  }

  // "$94.18 credit" / "$126.07 due"; approx → whole dollars with a ~ prefix.
  function fmtMoney(v, approx) {
    const amt = (approx ? "~$" : "$") +
      (approx ? Math.round(Math.abs(v)) : Math.abs(v).toFixed(2));
    return v < 0 ? amt + " credit" : amt + " due";
  }

  // Headline: solar made vs total home used (incl. car); net = ahead/behind.
  function rangeLabel() {
    if (range === "custom" && customStart && customEnd) {
      const f = (ms) => new Date(ms).toLocaleDateString([], {
        month: "numeric", day: "numeric", year: "2-digit", timeZone: dispTZ(),
      });
      return `${f(customStart)} – ${f(customEnd)}`;
    }
    if (range === "billing") {
      const p = histBillPeriod();
      if (p) return p.label;
    }
    return RANGE_LABELS[range] || range.toUpperCase();
  }

  function renderBalance(t) {
    $("#bal-range").textContent = rangeLabel();
    $("#bal-solar").textContent = fmt1(t.solar);
    $("#bal-home").textContent = fmt1(t.home);

    // Bill for a selected billing period (whole house only): the real per-cycle
    // charge if billed, else the estimate; plus the running account balance.
    const be = $("#bill-est");
    if (be) {
      const p = range === "billing" ? histBillPeriod() : null;
      const actual = !!(p && p.bill != null);
      const charge = actual ? Number(p.bill)
        : (t.bill_estimate != null ? Number(t.bill_estimate) : null);
      const show = range === "billing" && system === "combined" && p && charge != null;
      be.hidden = !show;
      if (show) {
        // running total (Total Due): billed cycle → known; in-progress → carry + estimate
        const balance = actual
          ? (p.balance != null ? Number(p.balance) : null)
          : (p.prior_balance != null ? Number(p.prior_balance) + charge : null);
        const setRow = (labelSel, valSel, label, v, approx) => {
          const l = $(labelSel), el = $(valSel);
          if (l) l.textContent = label;
          if (el) {
            el.textContent = v == null ? "--" : fmtMoney(v, approx);
            el.classList.toggle("credit", v != null && v < 0);
          }
        };
        setRow("#bill-charge-label", "#bill-est-val",
               actual ? "BILL" : "EST. BILL", charge, !actual);
        const balRow = $("#bill-bal-val").closest(".bill-row");
        if (balance == null) {
          if (balRow) balRow.hidden = true;
        } else {
          if (balRow) balRow.hidden = false;
          setRow("#bill-bal-label", "#bill-bal-val",
                 actual ? "BALANCE" : "EST. BALANCE", balance, !actual);
        }
      }
    }

    const net = Number(t.net || 0);
    const ahead = net >= 0;
    $("#bal-net").textContent = (ahead ? "+" : "") + fmt1(net);
    const flag = $("#bal-flag");
    flag.textContent = ahead ? "AHEAD ▲" : "BEHIND ▼";
    const box = $("#bal-net-box");
    box.classList.toggle("ahead", ahead);
    box.classList.toggle("behind", !ahead);
  }

  function setStat(key, val) {
    const el = $(`.stat[data-k="${key}"] .stat-val`);
    if (!el) return;
    const unit = el.querySelector("i");
    const u = unit ? unit.outerHTML : "";
    el.innerHTML = (val == null ? "--" : val.toLocaleString(undefined, { maximumFractionDigits: 1 })) + " " + u;
  }

  // ---------- MONTHLY ----------
  // Own section, independent of the HISTORY range: shows a MONTH_WINDOW-wide
  // slice of the month list; ◂ ▸ page back/forward as history accumulates.
  const MONTH_WINDOW = 6;
  let monthlyRows = [];
  let monthOffset = 0;   // months back from the latest window (0 = most recent)

  async function refreshMonthly() {
    try {
      const data = await getJSON(`/api/monthly?system=${system}`);
      monthlyRows = dedupeMonths(data.months);
      renderMonthly();
    } catch (e) { console.error(e); }
  }

  function renderMonthly() {
    const n = monthlyRows.length;
    if (!n) return;
    const maxOff = Math.max(n - MONTH_WINDOW, 0);
    monthOffset = Math.min(monthOffset, maxOff);
    const end = n - monthOffset;
    const rows = monthlyRows.slice(Math.max(end - MONTH_WINDOW, 0), end);

    const prev = $("#month-prev"), next = $("#month-next"), lbl = $("#month-label");
    if (prev) prev.disabled = monthOffset >= maxOff;
    if (next) next.disabled = monthOffset === 0;
    if (lbl && rows.length) {
      const mon = r => new Date(r.ts)
        .toLocaleDateString([], { month: "short", timeZone: dispTZ() }).toUpperCase();
      lbl.textContent = `${mon(rows[0])} – ${mon(rows[rows.length - 1])}`;
    }

    const labels = rows.map(r => new Date(r.ts).toLocaleDateString([], { month: "short", timeZone: dispTZ() }));
    NeonChart.bars($("#chart-monthly"), {
      labels,
      series: [
        { name: "Solar", color: C.solar, data: rows.map(r => r.solar || 0) },
        { name: "House", color: C.home, stack: "load", data: rows.map(r => r.house_kwh ?? (r.home || 0)) },
        { name: "EV", color: C.battery, stack: "load", data: rows.map(r => r.ev_kwh || 0) },
        { name: "Grid In", color: C.grid, data: rows.map(r => r.from_grid || 0) },
        { name: "Grid Out", color: C.soc, data: rows.map(r => r.to_grid || 0) },
      ],
    });
    legend("#monthly-legend", [
      ["Solar", C.solar], ["House", C.home], ["EV", C.battery], ["Grid In", C.grid], ["Grid Out", C.soc],
    ]);
  }

  function bindMonthlyPager() {
    const prev = $("#month-prev"), next = $("#month-next");
    if (!prev || !next) return;
    prev.addEventListener("click", () => {
      monthOffset = Math.min(monthOffset + MONTH_WINDOW,
        Math.max(monthlyRows.length - MONTH_WINDOW, 0));
      renderMonthly();
    });
    next.addEventListener("click", () => {
      monthOffset = Math.max(monthOffset - MONTH_WINDOW, 0);
      renderMonthly();
    });
  }

  // monthly RP sometimes has overlapping points per month — keep the latest per month bucket
  function dedupeMonths(rows) {
    const byMonth = new Map();
    for (const r of rows) {
      const d = new Date(r.ts);
      const key = d.getUTCFullYear() + "-" + d.getUTCMonth();
      const prev = byMonth.get(key);
      // prefer the row with the most fields populated
      if (!prev || Object.keys(r).length >= Object.keys(prev).length) byMonth.set(key, r);
    }
    return Array.from(byMonth.values()).sort((a, b) => a.ts - b.ts);
  }

  // ---------- RECORDS · BEST & WORST ----------
  // Most / least of a metric (solar, house, or a car) over day/week/month/year.
  let recMetric = localStorage.getItem("recMetric") || "solar";
  let recPeriod = localStorage.getItem("recPeriod") || "day";

  // Metric dropdown is data-driven: solar, house, then one option per charger.
  function fillRecMetric() {
    const sel = $("#rec-metric");
    if (!sel || !SETTINGS) return;
    const opts = [["solar", "SOLAR MADE"], ["house", "HOUSE USED"],
                  ["home", "ALL USED"]];
    for (const gw of SETTINGS.gateways) {
      opts.push([gw.tag, ((gw.charger_label || gw.tag) + " CHARGED").toUpperCase()]);
    }
    if (!opts.some(([v]) => v === recMetric)) recMetric = "solar";
    sel.innerHTML = opts.map(([v, l]) =>
      `<option value="${v}">${l}</option>`).join("");
    sel.value = recMetric;
  }

  function metricPhrase(metric) {
    if (metric === "solar") return "SOLAR MADE";
    if (metric === "house") return "HOUSE USED";
    if (metric === "home") return "ALL USED";
    const gw = (SETTINGS && SETTINGS.gateways || []).find(g => g.tag === metric);
    return (((gw && gw.charger_label) || metric) + " CHARGED").toUpperCase();
  }

  async function refreshRecords() {
    if (!$("#rec-body")) return;
    try {
      renderRecords(await getJSON(
        `/api/records?metric=${recMetric}&period=${recPeriod}&system=${system}`));
    } catch (e) { console.error(e); }
  }

  function renderRecords(d) {
    const perWord = { day: "DAY", week: "WEEK", month: "MONTH", year: "YEAR" }[d.period];
    const title = $("#rec-title");
    if (title) title.textContent = `${metricPhrase(d.metric)} · PER ${perWord}`;
    const li = r =>
      `<li><b>${fmt1(r.value)}</b><i>kWh</i><span>${r.label}</span></li>`;
    const top = $("#rec-top"), bottom = $("#rec-bottom");
    if (top) top.innerHTML = (d.top && d.top.length)
      ? d.top.map(li).join("") : '<li class="rec-empty">—</li>';
    if (bottom) {
      // with fewer than ~10 periods the top list already covers them all
      bottom.innerHTML = (d.bottom && d.bottom.length)
        ? d.bottom.map(li).join("")
        : (d.count ? `<li class="rec-empty">all ${d.count} shown at left</li>`
                   : '<li class="rec-empty">—</li>');
    }
    const note = $("#rec-note");
    if (note) {
      note.textContent = d.count
        ? `Ranked over ${d.count} complete ${perWord.toLowerCase()}${d.count === 1 ? "" : "s"}. Partial and in-progress periods are excluded.`
        : `Not enough complete ${perWord.toLowerCase()}s of data yet.`;
    }
  }

  function bindRecords() {
    const m = $("#rec-metric"), p = $("#rec-period");
    if (p) p.value = recPeriod;
    if (m) m.addEventListener("change", () => {
      recMetric = m.value; localStorage.setItem("recMetric", recMetric);
      refreshRecords();
    });
    if (p) p.addEventListener("change", () => {
      recPeriod = p.value; localStorage.setItem("recPeriod", recPeriod);
      refreshRecords();
    });
  }

  // ---------- CHARGING · PEAK vs OFF-PEAK ----------
  // One pie per car showing how much of its charging landed in the utility's
  // peak window, for a billing period picked from the dropdown. The window
  // itself lives in settings (1PM-9PM daily by default); hours come back
  // labelled by the server in the house timezone.
  let billPeriods = [];
  let billKey = localStorage.getItem("billKey") || "";
  let histBillKey = localStorage.getItem("histBillKey") || "";
  let lastTou = null;

  // Billing period selected in the HISTORY range picker (defaults to current).
  function histBillPeriod() {
    if (!billPeriods.length) return null;
    return billPeriods.find(p => p.key === histBillKey) || billPeriods[0];
  }

  function fillHistBillSelect() {
    const sel = $("#hist-bill-select");
    if (!sel) return;
    sel.innerHTML = billPeriods
      .map(p => `<option value="${p.key}">${p.label}</option>`).join("");
    const p = histBillPeriod();
    if (p) { histBillKey = p.key; sel.value = p.key; }
  }
  // ON SOLAR toggle: on (default) counts solar/battery charging in the pies by
  // time of day; off shows only what was pulled from the grid while charging.
  let touSolar = localStorage.getItem("touSolar") !== "0";

  async function refreshBillingPeriods() {
    try {
      const data = await getJSON("/api/billing_periods");
      billPeriods = data.periods || [];
      const sel = $("#bill-select");
      sel.innerHTML = billPeriods
        .map(p => `<option value="${p.key}">${p.label}</option>`).join("");
      if (!billPeriods.some(p => p.key === billKey)) {
        billKey = billPeriods.length ? billPeriods[0].key : "";
      }
      sel.value = billKey;
      fillHistBillSelect();   // keep the HISTORY picker in sync
    } catch (e) { console.error(e); }
  }

  function billPeriod() {
    return billPeriods.find(p => p.key === billKey);
  }

  async function refreshTou() {
    const p = billPeriod();
    if (!p) { renderTou(null); return; }
    try {
      lastTou = await getJSON(
        `/api/tou?range=custom&start=${p.start}&end=${p.end}&system=${system}`);
      renderTou(lastTou);
    } catch (e) { console.error(e); }
  }

  // 9 -> "9AM", 18 -> "6PM", 24 -> "MIDNIGHT"
  function hourLabel(h) {
    if (h === 0 || h === 24) return "MIDNIGHT";
    if (h === 12) return "NOON";
    return (h % 12 || 12) + (h < 12 ? "AM" : "PM");
  }

  // "EVERY DAY" unless the tariff carves weekends and/or holidays out
  function daysLabel(cfg) {
    if (cfg.weekend_offpeak && cfg.holiday_offpeak) return "WEEKDAYS EXCEPT HOLIDAYS";
    if (cfg.weekend_offpeak) return "WEEKDAYS";
    if (cfg.holiday_offpeak) return "EVERY DAY EXCEPT HOLIDAYS";
    return "EVERY DAY";
  }

  function renderTou(data) {
    const wrap = $("#tou-pies");
    if (!wrap) return;
    const gws = SETTINGS ? splitGateways() : [];
    const chargers = (data && data.chargers) || {};

    // rebuild the card set only when the charger list itself changes
    const want = gws.map(g => g.tag).join("|");
    if (wrap.dataset.keys !== want) {
      wrap.dataset.keys = want;
      wrap.innerHTML = "";
      for (const gw of gws) {
        const card = document.createElement("div");
        card.className = "card chart-card pie-card";
        card.id = "tou-" + gw.tag;
        card.innerHTML =
          '<div class="card-head"><h2></h2></div>' +
          '<div class="pie-totals">' +
          '<span class="pt peak"><i>PEAK</i><b></b></span>' +
          '<span class="pt off"><i>OFF-PEAK</i><b></b></span>' +
          "</div>" +
          "<canvas></canvas>" +
          '<div class="pie-legend"></div>';
        wrap.appendChild(card);
      }
    }

    // lighter tints for the solar (on-site) share of each period; grid keeps
    // the vivid peak=magenta / off-peak=green so grid-only mode reads the same
    const SOLAR_PEAK = "#ff9dcb", SOLAR_OFF = "#a9ffcf";

    for (const gw of gws) {
      const card = document.getElementById("tou-" + gw.tag);
      if (!card) continue;
      card.querySelector("h2").textContent = chargerLabel(gw);
      const c = chargers[gw.tag] ||
        { peak: 0, off_peak: 0, total: 0, peak_grid: 0, off_peak_grid: 0, grid_total: 0 };
      const peakSolar = Math.max(0, c.peak - c.peak_grid);
      const offSolar = Math.max(0, c.off_peak - c.off_peak_grid);
      // ON SOLAR: 4 slices (grid + solar per period). Off: grid-sourced only.
      const totalV = touSolar ? c.total : c.grid_total;
      const slices = touSolar ? [
        { name: "PEAK · GRID", color: C.grid, value: c.peak_grid },
        { name: "PEAK · SOLAR", color: SOLAR_PEAK, value: peakSolar },
        { name: "OFF-PEAK · GRID", color: C.green, value: c.off_peak_grid },
        { name: "OFF-PEAK · SOLAR", color: SOLAR_OFF, value: offSolar },
      ] : [
        { name: "PEAK", color: C.grid, value: c.peak_grid },
        { name: "OFF-PEAK", color: C.green, value: c.off_peak_grid },
      ];

      NeonChart.pie(card.querySelector("canvas"), {
        slices,
        center: { val: fmt1(totalV), unit: "kWh",
                  sub: touSolar ? "CHARGED" : "FROM GRID" },
        empty: touSolar ? "NO CHARGING" : "NO GRID CHARGING",
      });

      // headline peak/off-peak totals just outside the pie (sums of the slices)
      const pct = v => totalV > 0 ? ` · ${Math.round((v / totalV) * 100)}%` : "";
      const peakV = touSolar ? c.peak : c.peak_grid;
      const offV = touSolar ? c.off_peak : c.off_peak_grid;
      card.querySelector(".pt.peak b").textContent = `${fmt1(peakV)} kWh${pct(peakV)}`;
      card.querySelector(".pt.off b").textContent = `${fmt1(offV)} kWh${pct(offV)}`;

      // per-slice breakdown so the (up to 4) wedges read without hovering
      card.querySelector(".pie-legend").innerHTML = slices.map(s =>
        `<span style="--c:${s.color}"><em>${s.name}</em>` +
        `<b>${fmt1(s.value)} kWh${pct(s.value)}</b></span>`).join("");
    }

    const note = $("#tou-note");
    const cfg = data && data.tou;
    if (!note) return;
    if (!cfg) { note.textContent = "—"; return; }
    const peak = cfg.peak || [13, 21];
    const p = billPeriod();
    const source = touSolar
      ? "All charging is counted by time of day — what the utility sees (it reads "
        + "charging from the car) — with each period split into grid vs your own "
        + "solar/battery (per panel). "
      : "Showing only energy pulled from the grid while charging, per panel; "
        + "solar/battery charging is excluded. ";
    note.innerHTML =
      `PEAK · <b>${hourLabel(peak[0])}–${hourLabel(peak[1])}</b> ` +
      `${daysLabel(cfg)} — everything else off-peak. ` +
      `Hours in ${data.timezone}` + (p ? ` · ${p.label}` : "") + ". " +
      source + "Charging is estimated from house load above the baseline.";
  }

  function bindBillingPicker() {
    const sel = $("#bill-select");
    if (!sel) return;
    sel.addEventListener("change", () => {
      billKey = sel.value;
      localStorage.setItem("billKey", billKey);
      refreshTou();
    });
  }

  function bindSolarToggle() {
    const cb = $("#tou-solar");
    if (!cb) return;
    cb.checked = touSolar;
    cb.addEventListener("change", () => {
      touSolar = cb.checked;
      localStorage.setItem("touSolar", touSolar ? "1" : "0");
      renderTou(lastTou);   // both modes already in the payload — no refetch
    });
  }

  // ---------- settings-driven UI ----------
  // Branding, the system toggle, and per-gateway cards all come from
  // /api/settings; re-run after every settings save.
  function applySettings() {
    document.title = SETTINGS.site_name;
    $("#site-title").textContent = SETTINGS.site_name;
    $("#site-kicker").textContent = SETTINGS.tagline;

    const multi = SETTINGS.gateways.length > 1;

    $("#sys-controls").hidden = !multi;   // no empty sticky bar for 1-gateway
    const seg = $("#sys-seg");
    seg.hidden = !multi;
    seg.innerHTML = "";
    if (multi) {
      if (!["combined", ...SETTINGS.gateways.map(g => g.tag)].includes(system)) system = "combined";
      const mk = (sys, label) => {
        const b = document.createElement("button");
        b.dataset.sys = sys;
        b.textContent = label;
        if (sys === system) b.classList.add("active");
        seg.appendChild(b);
      };
      mk("combined", "COMBINED");
      for (const gw of SETTINGS.gateways) mk(gw.tag, gw.label);
    } else {
      system = "combined";
    }

    const panels = $("#panels");
    const want = new Set(SETTINGS.gateways.map(g => `panel-${g.tag}`));
    for (const el of Array.from(panels.children)) {
      if (!want.has(el.id)) el.remove();
    }
    for (const gw of SETTINGS.gateways) {
      let card = document.getElementById(`panel-${gw.tag}`);
      if (!card) {
        card = document.createElement("div");
        card.className = "card panel-card";
        card.id = `panel-${gw.tag}`;
        card.innerHTML =
          '<div class="card-head"><h2></h2><span class="panel-soc">--%</span></div>' +
          '<div class="panel-grid"></div>';
        panels.appendChild(card);
      }
      card.querySelector("h2").textContent = gw.label;
    }
    fillRecMetric();   // metric dropdown includes one option per charger
    syncSplits();   // split availability depends on gateway count
  }

  // ---------- controls ----------
  function bindSegments() {
    const sel = $("#range-select");
    const custom = $("#custom-range");
    const startIn = $("#custom-start"), endIn = $("#custom-end");

    const histBill = $("#hist-bill-range");

    sel.addEventListener("change", () => {
      if (sel.value === "custom") {
        // default the pickers to the last full month if empty
        if (!startIn.value || !endIn.value) {
          const now = new Date();
          const a = new Date(now.getFullYear(), now.getMonth() - 1, now.getDate());
          startIn.value = a.toISOString().slice(0, 10);
          endIn.value = now.toISOString().slice(0, 10);
        }
        if (histBill) histBill.hidden = true;
        custom.hidden = false;
        return;   // wait for GO — don't refresh with stale bounds
      }
      custom.hidden = true;
      range = sel.value;
      localStorage.setItem("range", range);
      if (sel.value === "billing") {
        // reveal the period picker; load periods first if we don't have them
        if (histBill) histBill.hidden = false;
        const go = () => { fillHistBillSelect(); refreshHistory(); refreshTotals(); };
        if (billPeriods.length) go(); else refreshBillingPeriods().then(go);
        return;
      }
      if (histBill) histBill.hidden = true;
      refreshHistory(); refreshTotals();
    });

    const hbSel = $("#hist-bill-select");
    if (hbSel) hbSel.addEventListener("change", () => {
      histBillKey = hbSel.value;
      localStorage.setItem("histBillKey", histBillKey);
      refreshHistory(); refreshTotals();
    });

    $("#custom-go").addEventListener("click", () => {
      const s = Date.parse(startIn.value);
      // include the whole end day (midnight-to-midnight exclusive)
      const e = Date.parse(endIn.value) + 24 * 3600 * 1000 - 1;
      if (isNaN(s) || isNaN(e) || s >= e) return;
      range = "custom"; customStart = s; customEnd = e;
      refreshHistory(); refreshTotals();
    });

    // restore last range (except custom, which needs dates)
    const saved = localStorage.getItem("range");
    if (saved && saved !== "custom" && sel.querySelector(`option[value="${saved}"]`)) {
      sel.value = saved;
      range = saved;
      // reveal the billing picker; refreshBillingPeriods (init) fills it
      if (saved === "billing" && histBill) histBill.hidden = false;
    }

    // system buttons are re-rendered on settings changes — delegate
    $("#sys-seg").addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      $$("#sys-seg button").forEach(x => x.classList.remove("active"));
      b.classList.add("active");
      system = b.dataset.sys;
      syncSplits();   // combined-only splits close/hide off COMBINED
      refreshLive(); refreshHistory(); refreshTotals(); refreshMonthly();
      refreshTou();   // pies scope to the selected panel's charger
      refreshRecords();   // records follow the combined/panel selection too
    });
  }

  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      refreshHistory(); refreshMonthly();
      renderTou(lastTou);   // canvases redraw at the new size
      syncSplits();   // realign the connector notch
    }, 200);
  });

  // ---------- auto-refresh (master rate for the whole dashboard) ----------
  let masterTimer = null, monthlyTimer = null;

  // Live + totals + history all refresh at the chosen interval; the slow-moving
  // monthly chart refreshes at most every 10 min. ms = 0 (Off) freezes everything.
  function applyRefreshRate(ms) {
    if (masterTimer) { clearInterval(masterTimer); masterTimer = null; }
    if (monthlyTimer) { clearInterval(monthlyTimer); monthlyTimer = null; }
    if (ms > 0) {
      masterTimer = setInterval(() => {
        refreshLive(); refreshTotals(); refreshHistory(); refreshTou();
      }, ms);
      monthlyTimer = setInterval(refreshMonthly, Math.max(ms, 600000));
    }
  }

  function bindRefreshRate() {
    const sel = $("#refresh-select");
    if (!sel) return;
    const saved = localStorage.getItem("refreshMs");
    if (saved != null && sel.querySelector(`option[value="${saved}"]`)) sel.value = saved;
    applyRefreshRate(Number(sel.value));
    sel.addEventListener("change", () => {
      localStorage.setItem("refreshMs", sel.value);
      applyRefreshRate(Number(sel.value));
      refreshLive(); refreshTotals(); refreshHistory();   // immediate update on change
    });
  }

  // ---------- self-update ----------
  // The iOS home-screen web app never re-fetches index.html on its own, so a
  // long-lived instance would show old code forever. Compare the app.js
  // cache-buster in a freshly fetched index.html against our own script tag
  // and reload when they differ. Checked periodically and whenever the app
  // returns to the foreground.
  const ownSrc = document.querySelector('script[src*="app.js"]')?.getAttribute("src") || "";
  async function checkForUpdate() {
    try {
      const r = await fetch("/", { cache: "no-store" });
      if (!r.ok) return;
      const m = (await r.text()).match(/app\.js\?v=\d+/);
      if (m && ownSrc && m[0] !== ownSrc) location.reload();
    } catch { /* offline — try again next time */ }
  }
  function bindSelfUpdate() {
    setInterval(checkForUpdate, 15 * 60 * 1000);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") checkForUpdate();
    });
  }

  // ---------- settings modal ----------
  function bindSettings() {
    const btn = $("#settings-btn"), modal = $("#settings-modal"), close = $("#settings-close");
    const form = $("#settings-form"), err = $("#sf-error");
    if (!btn || !modal) return;

    function fill(s) {
      $("#sf-site-name").value = s.site_name;
      $("#sf-tagline").value = s.tagline;
      const tzSel = $("#sf-timezone");
      const zones = TZ_CHOICES.includes(s.timezone)
        ? TZ_CHOICES : [s.timezone, ...TZ_CHOICES];
      tzSel.innerHTML = "";
      for (const z of zones) {
        const o = document.createElement("option");
        o.value = z;
        o.textContent = z.replace(/_/g, " ");
        tzSel.appendChild(o);
      }
      tzSel.value = s.timezone;
      $("#sf-time-display").value = s.time_display || "local";
      $("#sf-billing-day").value = s.billing_day || 1;
      $("#sf-rate-import").value = s.rate_import;
      $("#sf-rate-export").value = s.rate_export;
      $("#sf-fixed-charge").value = s.fixed_charge;
      const tou = s.tou || {};
      const peak = tou.peak || [13, 21];
      $("#sf-peak-start").value = peak[0];
      $("#sf-peak-end").value = peak[1];
      $("#sf-weekend-offpeak").checked = !!tou.weekend_offpeak;
      $("#sf-holiday-offpeak").checked = !!tou.holiday_offpeak;
      const wrap = $("#sf-gateways");
      wrap.innerHTML = "";
      const phd = document.createElement("div");
      phd.className = "sf-heading";
      phd.textContent = "PANELS";
      wrap.appendChild(phd);
      for (const gw of s.gateways) {
        const row = document.createElement("div");
        row.className = "sf-row";
        row.innerHTML =
          '<label class="sf-field"><span class="sf-label"></span>' +
          '<input type="text" class="sf-gw-label" maxlength="24" required /></label>' +
          '<label class="sf-field"><span class="sf-label">CAPACITY kWh</span>' +
          '<input type="number" class="sf-gw-cap" min="0.1" max="1000" step="0.1" required /></label>';
        row.querySelector(".sf-label").textContent = gw.tag.replace("_", " ").toUpperCase() + " LABEL";
        const lbl = row.querySelector(".sf-gw-label");
        lbl.value = gw.label;
        lbl.dataset.tag = gw.tag;
        row.querySelector(".sf-gw-cap").value = gw.capacity_kwh;
        wrap.appendChild(row);
      }

      // chargers get their own section, apart from the panel name/capacity
      const hd = document.createElement("div");
      hd.className = "sf-heading";
      hd.textContent = "CHARGERS";
      wrap.appendChild(hd);
      const crow = document.createElement("div");
      crow.className = "sf-row";
      crow.style.gridTemplateColumns = `repeat(${s.gateways.length}, 1fr)`;
      s.gateways.forEach((gw, i) => {
        const f = document.createElement("label");
        f.className = "sf-field";
        f.innerHTML =
          '<span class="sf-label"></span>' +
          '<input type="text" class="sf-gw-charger" maxlength="24" required />';
        f.querySelector(".sf-label").textContent = "CHARGER " + (i + 1) + " NAME";
        const inp = f.querySelector(".sf-gw-charger");
        inp.value = gw.charger_label || "CHARGER " + (i + 1);
        inp.dataset.tag = gw.tag;
        crow.appendChild(f);
      });
      wrap.appendChild(crow);
    }

    const open = () => { fill(SETTINGS); err.hidden = true; modal.hidden = false; };
    const hide = () => modal.hidden = true;
    btn.addEventListener("click", open);
    close.addEventListener("click", hide);
    modal.addEventListener("click", (e) => { if (e.target === modal) hide(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") hide(); });
    $("#sf-reset").addEventListener("click", () => fill(DEFAULTS));

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const save = $("#sf-save");
      const patch = {
        site_name: $("#sf-site-name").value.trim(),
        tagline: $("#sf-tagline").value.trim(),
        timezone: $("#sf-timezone").value,
        time_display: $("#sf-time-display").value,
        billing_day: Number($("#sf-billing-day").value),
        rate_import: Number($("#sf-rate-import").value),
        rate_export: Number($("#sf-rate-export").value),
        fixed_charge: Number($("#sf-fixed-charge").value),
        tou: {
          peak: [Number($("#sf-peak-start").value), Number($("#sf-peak-end").value)],
          weekend_offpeak: $("#sf-weekend-offpeak").checked,
          holiday_offpeak: $("#sf-holiday-offpeak").checked,
        },
        gateways: $$("#sf-gateways .sf-gw-label").map(inp => {
          const chg = $$("#sf-gateways .sf-gw-charger")
            .find(c => c.dataset.tag === inp.dataset.tag);
          return {
            tag: inp.dataset.tag,
            label: inp.value.trim(),
            charger_label: chg ? chg.value.trim() : "",
            capacity_kwh: Number(inp.closest(".sf-row").querySelector(".sf-gw-cap").value),
          };
        }),
      };
      save.disabled = true; save.textContent = "SAVING…"; err.hidden = true;
      try {
        const r = await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(patch),
        });
        const body = await r.json();
        if (!r.ok) throw new Error(body.detail || "save failed");
        SETTINGS = body.settings;
        applySettings();
        refreshLive(); refreshHistory(); refreshTotals(); refreshMonthly();
        refreshRecords();
        // billing day and peak windows both feed the charging pies
        refreshBillingPeriods().then(refreshTou);
        hide();
      } catch (ex) {
        err.textContent = ex.message;
        err.hidden = false;
      } finally {
        save.disabled = false; save.textContent = "SAVE";
      }
    });
  }

  // ---------- collapsible sections ----------
  // LIVE defaults closed (period info is what matters day-to-day); THIS PERIOD
  // defaults open. Charts render at zero size while hidden, so re-fetch/redraw
  // on expand. State sticks per device via localStorage.
  function bindCollapse(btnSel, bodySel, storageKey, defOpen, onOpen) {
    const btn = $(btnSel), body = $(bodySel);
    if (!btn || !body) return;
    const saved = localStorage.getItem(storageKey);
    let open = saved == null ? defOpen : saved === "1";
    const render = () => {
      body.hidden = !open;
      btn.setAttribute("aria-expanded", String(open));
      btn.querySelector(".sec-arrow").textContent = open ? "▾" : "▸";
    };
    render();
    btn.addEventListener("click", () => {
      open = !open;
      localStorage.setItem(storageKey, open ? "1" : "0");
      render();
      if (open && onOpen) onOpen();
    });
  }

  function bindSections() {
    bindCollapse("#live-toggle", "#live-body", "liveOpen", false, refreshLive);
    bindCollapse("#period-toggle", "#period-body", "periodOpen", true, () => {
      refreshHistory(); refreshTotals();
    });
    bindCollapse("#monthly-toggle", "#monthly-body", "monthlyOpen", true, renderMonthly);
    bindCollapse("#rec-toggle", "#rec-body", "recOpen", true, refreshRecords);
    bindCollapse("#tou-toggle", "#tou-body", "touOpen", true, refreshTou);
  }

  // ---------- version history modal ----------
  function bindVersion() {
    const btn = $("#version"), modal = $("#ver-modal"), close = $("#ver-close");
    if (btn) btn.textContent = "VERSION HISTORY · v" + VERSION;
    const gearVer = $("#settings-ver");
    if (gearVer) gearVer.textContent = "v" + VERSION;
    if (!btn || !modal) return;
    $("#ver-list").innerHTML = CHANGELOG.map(([v, note]) =>
      `<div class="ver-row"><span class="ver-tag">v${v}</span><span class="ver-note">${note}</span></div>`
    ).join("");
    const open = () => modal.hidden = false;
    const hide = () => modal.hidden = true;
    btn.addEventListener("click", open);
    close.addEventListener("click", hide);
    modal.addEventListener("click", (e) => { if (e.target === modal) hide(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") hide(); });
  }

  // ---------- boot ----------
  async function init() {
    bindVersion();
    bindSegments();
    bindSections();
    bindMonthlyPager();
    bindBillingPicker();
    bindSolarToggle();
    bindRecords();
    bindSettings();
    tick(); setInterval(tick, 1000);
    try {
      const data = await getJSON("/api/settings");
      SETTINGS = data.settings;
      DEFAULTS = data.defaults;
      if (data.home_url) {
        $("#home-btn").href = data.home_url;
        $("#home-btn").hidden = false;
      }
    } catch (e) {
      console.error("settings load failed; dashboard runs with defaults", e);
      SETTINGS = { site_name: "POWERWALL", tagline: "POWERWALL", gateways: [] };
      DEFAULTS = SETTINGS;
    }
    applySettings();
    bindSplits();
    refreshLive(); refreshHistory(); refreshTotals(); refreshMonthly();
    refreshRecords();
    await refreshBillingPeriods(); refreshTou();
    // if HISTORY restored to a specific billing period, re-query now that the
    // period bounds are known (the first pass fell back to the current cycle)
    if (range === "billing") { refreshHistory(); refreshTotals(); }
    bindRefreshRate();   // starts the master auto-refresh at the saved/selected rate
    bindSelfUpdate();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
