/* app.js — data fetching + DOM wiring for the Powerwall synthwave dashboard. */
(function () {
  "use strict";

  const VERSION = "2.39";

  // Most-recent first. Shown in the version-history modal.
  const CHANGELOG = [
    ["2.39", "Monthly pager stays on one line on phones (the ▸ was wrapping underneath)."],
    ["2.38", "Monthly pager shows just the month names (FEB – JUL)."],
    ["2.37", "Settings: charger names moved to their own CHARGERS section, apart from panel name/capacity."],
    ["2.36", "Home splits show PANEL A / PANEL B / CHARGER 1 / CHARGER 2; chargers renamable in settings; monthly label no longer reads like a date."],
    ["2.35", "Tile split panels open directly below the tile you tapped (they were parked at the bottom of the section — easy to miss on phones)."],
    ["2.34", "Phone: section controls sit beside the LIVE/HISTORY buttons instead of below; settings version link is a proper button."],
    ["2.33", "MONTHLY ENERGY moved to its own section: 6 months at a time, ◂ ▸ to page back through older months."],
    ["2.32", "Every LIVE/HISTORY tile expands a per-panel breakdown (one open at a time; most need COMBINED); version shown on the gear; THIS PERIOD renamed HISTORY."],
    ["2.31", "Split rows are compact and visually attached to their tile; live split opens from the HOME tile; BATTERY tile shows/hides the panel cards."],
    ["2.30", "HOME split into HOME / CHARGER 1 / CHARGER 2: live row under the panel cards, and a period row that opens by pressing HOME USED."],
    ["2.29", "Section headers: LIVE / THIS PERIOD buttons stay left; clock, refresh, and range picker center in the row."],
    ["2.28", "Boxed LIVE / THIS PERIOD sections, left-aligned toggles, status dot by the clock, bigger gear in the header, un-clipped title descenders."],
    ["2.27", "MONTHLY ENERGY was frozen at May — now computed live per calendar month (the old rollups used 30-day buckets with no live combined feed)."],
    ["2.26", "Clock + refresh rate moved next to the LIVE button; system bar sits on the page background; bigger gear on desktop."],
    ["2.25", "Live tiles (and panel cards) moved to a collapsible LIVE section up top, hidden by default; THIS PERIOD collapses too."],
    ["2.24", "Merged the version button into settings — it now lives at the bottom of the settings panel."],
    ["2.23", "Range dropdown with BILLING PERIOD, LIFETIME, and custom dates; times shown in house or local timezone (settings)."],
    ["2.22", "Settings page (gear icon): site name, panel labels, rates, capacities, timezone — applied live. Supports 1-3 gateways."],
    ["2.21", "Chart time labels land on round times (2h/4h/…) and fit the screen width."],
    ["2.20", "The phone web app now notices new versions and reloads itself (was caching forever)."],
    ["2.19", "Spaced out the big digits — narrow 1s were merging into neighbors (17 read like Π)."],
    ["2.18", "Removed the unreliable house/car split — HOME USED is the total (car + house)."],
    ["2.17", "Backfilled Feb–May history from the Tesla app; version history; sticky range/system bar."],
    ["2.16", "Refresh dropdown now controls the whole dashboard (Off freezes it)."],
    ["2.15", "Today / Month / Year (to-date) ranges; centered top bar."],
    ["2.14", "Custom site name; tightened the mobile header."],
    ["2.13", "Added the version label and auto-refresh control."],
    ["2.12", "Solar-vs-usage balance card + cumulative chart; fixed self-powered %."],
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
    return range === "custom"
      ? `range=custom&start=${customStart}&end=${customEnd}`
      : `range=${range}`;
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

  // Headline: solar made vs total home used (incl. car); net = ahead/behind.
  function rangeLabel() {
    if (range === "custom" && customStart && customEnd) {
      const f = (ms) => new Date(ms).toLocaleDateString([], {
        month: "numeric", day: "numeric", year: "2-digit", timeZone: dispTZ(),
      });
      return `${f(customStart)} – ${f(customEnd)}`;
    }
    return RANGE_LABELS[range] || range.toUpperCase();
  }

  function renderBalance(t) {
    $("#bal-range").textContent = rangeLabel();
    $("#bal-solar").textContent = fmt1(t.solar);
    $("#bal-home").textContent = fmt1(t.home);

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
    syncSplits();   // split availability depends on gateway count
  }

  // ---------- controls ----------
  function bindSegments() {
    const sel = $("#range-select");
    const custom = $("#custom-range");
    const startIn = $("#custom-start"), endIn = $("#custom-end");

    sel.addEventListener("change", () => {
      if (sel.value === "custom") {
        // default the pickers to the last full month if empty
        if (!startIn.value || !endIn.value) {
          const now = new Date();
          const a = new Date(now.getFullYear(), now.getMonth() - 1, now.getDate());
          startIn.value = a.toISOString().slice(0, 10);
          endIn.value = now.toISOString().slice(0, 10);
        }
        custom.hidden = false;
        return;   // wait for GO — don't refresh with stale bounds
      }
      custom.hidden = true;
      range = sel.value;
      localStorage.setItem("range", range);
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
    });
  }

  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      refreshHistory(); refreshMonthly();
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
      masterTimer = setInterval(() => { refreshLive(); refreshTotals(); refreshHistory(); }, ms);
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
      const wrap = $("#sf-gateways");
      wrap.innerHTML = "";
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
    bindSettings();
    tick(); setInterval(tick, 1000);
    try {
      const data = await getJSON("/api/settings");
      SETTINGS = data.settings;
      DEFAULTS = data.defaults;
    } catch (e) {
      console.error("settings load failed; dashboard runs with defaults", e);
      SETTINGS = { site_name: "POWERWALL", tagline: "POWERWALL", gateways: [] };
      DEFAULTS = SETTINGS;
    }
    applySettings();
    bindSplits();
    refreshLive(); refreshHistory(); refreshTotals(); refreshMonthly();
    bindRefreshRate();   // starts the master auto-refresh at the saved/selected rate
    bindSelfUpdate();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
