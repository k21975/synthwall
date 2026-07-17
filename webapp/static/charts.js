/* charts.js — tiny dependency-free neon canvas charts (line + bar).
   Synthwave styling: glowing strokes over a faint perspective-free grid. */

(function (global) {
  "use strict";

  const FONT = "'Orbitron', sans-serif";
  const AXIS = "rgba(140,110,255,0.55)";
  const GRIDC = "rgba(140,110,255,0.14)";

  function dpiSetup(canvas) {
    const dpr = global.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(1, Math.floor(rect.width));
    const h = Math.max(1, Math.floor(rect.height));
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w, h };
  }

  function niceMax(v) {
    if (v <= 0) return 1;
    const pow = Math.pow(10, Math.floor(Math.log10(v)));
    const n = v / pow;
    const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
    return step * pow;
  }

  function fmt(v) {
    const a = Math.abs(v);
    if (a >= 1000) return (v / 1000).toFixed(1) + "k";
    if (a >= 100) return v.toFixed(0);
    if (a >= 10) return v.toFixed(1);
    return v.toFixed(a < 1 ? 2 : 1);
  }

  /* Midnight (start of day) containing timestamp t, in the display timezone.
     tz undefined = browser local. The toLocaleString round-trip reinterprets
     the zoned wall-clock as system time; off-by-1h only across a DST jump
     inside the range, which shifts tick positions invisibly for date labels. */
  function zonedMidnight(t, tz) {
    if (!tz) {
      const m = new Date(t); m.setHours(0, 0, 0, 0);
      return m.getTime();
    }
    const d = new Date(t);
    const wall = new Date(d.toLocaleString("en-US", { timeZone: tz }));
    const offset = d.getTime() - wall.getTime();
    wall.setHours(0, 0, 0, 0);
    return wall.getTime() + offset;
  }

  /* Round wall-clock tick marks for a time axis, spaced to fit the plot width.
     Returns [{frac: 0..1 position, label}]; tz picks the display timezone. */
  function timeTicks(t0, t1, plotW, multiDay, tz) {
    const span = t1 - t0;
    if (!(span > 0)) return [];
    const MIN = 6e4, HOUR = 36e5, DAY = 864e5;
    const steps = multiDay
      ? [DAY, 2 * DAY, 7 * DAY, 14 * DAY, 30 * DAY, 60 * DAY, 90 * DAY]
      : [15 * MIN, 30 * MIN, HOUR, 2 * HOUR, 3 * HOUR, 4 * HOUR, 6 * HOUR, 12 * HOUR, DAY];
    const maxTicks = Math.max(3, Math.floor(plotW / (multiDay ? 48 : 56)));
    const step = steps.find(s => span / s <= maxTicks - 1) || steps[steps.length - 1];
    const base = zonedMidnight(t0, tz);
    const ticks = [];
    for (let t = base + Math.ceil((t0 - base) / step) * step; t <= t1; t += step) {
      const d = new Date(t);
      const label = multiDay
        ? d.toLocaleDateString([], { month: "numeric", day: "numeric", timeZone: tz })
        : step < HOUR
          ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", timeZone: tz })
          : d.toLocaleTimeString([], { hour: "numeric", timeZone: tz });
      ticks.push({ frac: (t - t0) / span, label });
    }
    return ticks;
  }

  /* opts: { labels:[], series:[{name,color,data:[],fill?:bool}],
            zero?:bool (center on 0),
            timeAxis?:{multiDay:bool, tz?:string} (labels are ms timestamps;
              ticks at round wall-clock times in tz — default browser local —
              count adapted to width),
            xticks?:[idx...], xlabels?:[] (explicit fallback) } */
  function line(canvas, opts) {
    const { ctx, w, h } = dpiSetup(canvas);
    ctx.clearRect(0, 0, w, h);
    const padL = 44, padR = 12, padT = 12, padB = 24;
    const plotW = w - padL - padR, plotH = h - padT - padB;
    const series = opts.series.filter(s => s.data.some(v => v != null));

    let max = 0, min = 0;
    for (const s of series) for (const v of s.data) {
      if (v == null) continue;
      if (v > max) max = v;
      if (v < min) min = v;
    }
    const center = opts.zero || min < 0;
    if (center) {
      const m = niceMax(Math.max(max, -min, 0.1));
      max = m; min = -m;
    } else {
      max = niceMax(max || 1); min = 0;
    }
    const span = max - min || 1;
    const yOf = v => padT + plotH - ((v - min) / span) * plotH;
    const n = opts.labels.length;
    const xOf = i => padL + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);

    // grid + y labels
    ctx.font = "10px " + FONT;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    const lines = 4;
    for (let i = 0; i <= lines; i++) {
      const val = min + (span * i) / lines;
      const y = yOf(val);
      ctx.strokeStyle = (center && Math.abs(val) < 1e-6) ? AXIS : GRIDC;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
      ctx.fillStyle = "rgba(160,150,210,0.8)";
      ctx.fillText(fmt(val), padL - 6, y);
    }

    // x ticks
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillStyle = "rgba(160,150,210,0.8)";
    if (opts.timeAxis && n > 1) {
      for (const tk of timeTicks(opts.labels[0], opts.labels[n - 1], plotW,
                                 opts.timeAxis.multiDay, opts.timeAxis.tz)) {
        ctx.fillText(tk.label, padL + tk.frac * plotW, h - padB + 6);
      }
    } else if (opts.xticks && opts.xlabels) {
      opts.xticks.forEach((idx, k) => {
        ctx.fillText(opts.xlabels[k], xOf(idx), h - padB + 6);
      });
    }

    // series
    for (const s of series) {
      // fill
      if (s.fill) {
        ctx.beginPath();
        let started = false;
        for (let i = 0; i < n; i++) {
          const v = s.data[i];
          if (v == null) continue;
          const x = xOf(i), y = yOf(v);
          if (!started) { ctx.moveTo(x, yOf(min)); ctx.lineTo(x, y); started = true; }
          else ctx.lineTo(x, y);
        }
        if (started) {
          ctx.lineTo(xOf(lastIdx(s.data)), yOf(min));
          ctx.closePath();
          const g = ctx.createLinearGradient(0, padT, 0, padT + plotH);
          g.addColorStop(0, hexA(s.color, 0.35));
          g.addColorStop(1, hexA(s.color, 0.02));
          ctx.fillStyle = g;
          ctx.fill();
        }
      }
      // glow stroke
      ctx.strokeStyle = s.color;
      ctx.lineWidth = 2;
      ctx.shadowColor = s.color;
      ctx.shadowBlur = 8;
      ctx.lineJoin = "round";
      ctx.beginPath();
      let started = false;
      for (let i = 0; i < n; i++) {
        const v = s.data[i];
        if (v == null) { started = false; continue; }
        const x = xOf(i), y = yOf(v);
        if (!started) { ctx.moveTo(x, y); started = true; }
        else ctx.lineTo(x, y);
      }
      ctx.stroke();
      ctx.shadowBlur = 0;
    }
  }

  function lastIdx(arr) {
    for (let i = arr.length - 1; i >= 0; i--) if (arr[i] != null) return i;
    return 0;
  }

  /* opts: { labels:[], series:[{name,color,data:[]}], stacked?:[groupName...] }
     Groups bars per label; series sharing a stack name stack together. */
  function bars(canvas, opts) {
    const { ctx, w, h } = dpiSetup(canvas);
    ctx.clearRect(0, 0, w, h);
    const padL = 44, padR = 12, padT = 12, padB = 26;
    const plotW = w - padL - padR, plotH = h - padT - padB;
    const n = opts.labels.length;
    const series = opts.series;

    // resolve stacks: map stackKey -> [series]
    const stacks = {};
    const order = [];
    series.forEach(s => {
      const key = s.stack || s.name;
      if (!stacks[key]) { stacks[key] = []; order.push(key); }
      stacks[key].push(s);
    });

    // max = tallest stack across labels
    let max = 0;
    for (let i = 0; i < n; i++) {
      for (const key of order) {
        let sum = 0;
        for (const s of stacks[key]) sum += Math.max(0, s.data[i] || 0);
        if (sum > max) max = sum;
      }
    }
    max = niceMax(max || 1);
    const yOf = v => padT + plotH - (v / max) * plotH;

    // y grid
    ctx.font = "10px " + FONT;
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    for (let i = 0; i <= 4; i++) {
      const val = (max * i) / 4, y = yOf(val);
      ctx.strokeStyle = GRIDC; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
      ctx.fillStyle = "rgba(160,150,210,0.8)";
      ctx.fillText(fmt(val), padL - 6, y);
    }

    const groupW = plotW / Math.max(1, n);
    const nStacks = order.length;
    const barGap = 6;
    const barW = Math.max(4, (groupW * 0.7 - barGap * (nStacks - 1)) / nStacks);
    const groupPad = (groupW - (barW * nStacks + barGap * (nStacks - 1))) / 2;

    ctx.textAlign = "center"; ctx.textBaseline = "top";
    for (let i = 0; i < n; i++) {
      const gx = padL + i * groupW + groupPad;
      order.forEach((key, si) => {
        const x = gx + si * (barW + barGap);
        let cum = 0; // cumulative value already stacked
        for (const s of stacks[key]) {
          const v = Math.max(0, s.data[i] || 0);
          if (v <= 0) continue;
          const yBottom = yOf(cum);
          const yTop = yOf(cum + v);
          ctx.fillStyle = s.color;
          ctx.shadowColor = s.color; ctx.shadowBlur = 6;
          roundRect(ctx, x, yTop, barW, yBottom - yTop, 3);
          ctx.fill();
          ctx.shadowBlur = 0;
          cum += v;
        }
      });
      // x label
      ctx.fillStyle = "rgba(180,170,225,0.9)";
      ctx.fillText(opts.labels[i], padL + i * groupW + groupW / 2, h - padB + 7);
    }
  }

  function roundRect(ctx, x, y, w, h, r) {
    r = Math.min(r, w / 2, Math.abs(h) / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function hexA(hex, a) {
    const c = hex.replace("#", "");
    const n = c.length === 3 ? c.split("").map(x => x + x).join("") : c;
    const r = parseInt(n.slice(0, 2), 16);
    const g = parseInt(n.slice(2, 4), 16);
    const b = parseInt(n.slice(4, 6), 16);
    return `rgba(${r},${g},${b},${a})`;
  }

  global.NeonChart = { line, bars };
})(window);
