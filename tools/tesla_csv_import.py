#!/usr/bin/env python3
"""
tesla_csv_import.py — Import Tesla app CSV exports into the Powerwall-Dashboard schema.

Two CSV formats are auto-detected from column headers:

5-min / Day view (kW):
  Date time, Home (kW), Vehicle (kW), Powerwall (kW), Solar (kW), Grid (kW), Energy Remaining (%)
  Sign: Powerwall + = discharge, Grid + = import.
  Writes: autogen (5-min power), kwh (hourly), daily, monthly, raw.enriched (recent).

Monthly / Day-by-day view (kWh):
  Date time, Home (kWh), Vehicle (kWh), From Powerwall (kWh), Solar Energy (kWh),
             From Grid (kWh), To Grid (kWh)
  Already daily energy totals; no SOC, no To-Powerwall column.
  Writes: daily, monthly only.

Maps PW1 -> system=panel_a, PW2 -> system=panel_b, and computes system=combined.

Usage:
  python3 tesla_csv_import.py --indir ~/Downloads --out ~/Downloads/powerwall_backfill.lp
Files are matched by name: *PW1*.csv -> panel_a, *PW2*.csv -> panel_b.
Re-running is safe (points overwrite by time+tags).
"""
import argparse, csv, glob, os, sys
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# Rates: match your compose.env (env vars override these defaults)
RATE_IMPORT = float(os.environ.get("RATE_IMPORT", 0.25))
RATE_EXPORT = float(os.environ.get("RATE_EXPORT", 0.08))
CAP_A = 13.5
CAP_B = 13.5
INTERVAL_H = 5.0 / 60.0          # 5-minute samples -> hours
RAW_WINDOW_DAYS = 3              # raw.enriched retention; only backfill it for recent data

MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

def _csv_mode(path):
    """Return 'daily_kwh' or '5min' based on header of first line."""
    with open(path, newline="") as f:
        header = f.readline()
    return "daily_kwh" if "From Powerwall" in header else "5min"

def parse_csv(path):
    """Return list of dicts: {dt(aware), home, veh, pw, solar, grid, soc(or None)} in kW."""
    rows = []
    with open(path, newline="") as f:
        r = csv.DictReader(f)
        for d in r:
            t = (d.get("Date time") or "").strip()
            if not t:
                continue
            dt = datetime.fromisoformat(t)            # tz-aware (has -04:00 offset)
            def num(k):
                v = (d.get(k) or "").strip()
                return float(v) if v not in ("", None) else 0.0
            soc_raw = (d.get("Energy Remaining (%)") or "").strip()
            rows.append({
                "dt": dt,
                "home": num("Home (kW)"),
                "veh": num("Vehicle (kW)"),
                "pw": num("Powerwall (kW)"),
                "solar": num("Solar (kW)"),
                "grid": num("Grid (kW)"),
                "soc": float(soc_raw) if soc_raw else None,
            })
    rows.sort(key=lambda x: x["dt"])
    # forward-fill SOC (only present every 15 min)
    last = None
    for x in rows:
        if x["soc"] is None:
            x["soc"] = last
        else:
            last = x["soc"]
    return rows

def parse_daily_kwh_csv(path):
    """Parse monthly export (daily kWh totals). Returns list of dicts in kWh."""
    rows = []
    with open(path, newline="") as f:
        r = csv.DictReader(f)
        for d in r:
            t = (d.get("Date time") or "").strip()
            if not t:
                continue
            dt = datetime.fromisoformat(t)
            def num(k):
                v = (d.get(k) or "").strip()
                return float(v) if v not in ("", None) else 0.0
            rows.append({
                "dt": dt,
                "home":      num("Home (kWh)"),
                "veh":       num("Vehicle (kWh)"),
                "from_pw":   num("From Powerwall (kWh)"),
                "to_pw":     0.0,   # not exported in monthly view
                "solar":     num("Solar Energy (kWh)"),
                "from_grid": num("From Grid (kWh)"),
                "to_grid":   num("To Grid (kWh)"),
            })
    rows.sort(key=lambda x: x["dt"])
    return rows

def split_fields(s):
    """From a kW sample dict -> power fields in WATTS with signed splits."""
    pw = s["pw"]; grid = s["grid"]
    return {
        "home":     (s["home"] + s["veh"]) * 1000.0,  # total load incl EV (matches live gateway "home")
        "solar":    s["solar"] * 1000.0,
        "from_pw":  max(0.0,  pw)   * 1000.0,   # discharge
        "to_pw":    max(0.0, -pw)   * 1000.0,   # charge
        "from_grid":max(0.0,  grid) * 1000.0,   # import
        "to_grid":  max(0.0, -grid) * 1000.0,   # export
        "veh":      s["veh"]   * 1000.0,
        "soc":      s["soc"],
    }

def lp_escape_tag(v):
    return str(v).replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")

def line(meas, tags, fields, ts_s):
    tagstr = ",".join("%s=%s" % (k, lp_escape_tag(v)) for k, v in tags.items())
    fieldstr = ",".join("%s=%s" % (k, repr(float(v))) for k, v in fields.items() if v is not None)
    ns = int(ts_s) * 1_000_000_000
    return "%s,%s %s %d" % (meas, tagstr, fieldstr, ns)

def mtag(dt):
    return MONTHS[dt.month - 1], str(dt.year)

def daily_kwh_fields(r):
    """Extract the six core energy fields (kWh) from a daily-kWh row."""
    return {k: r[k] for k in ("home","solar","from_pw","to_pw","from_grid","to_grid")}

def combine(a_samples, b_samples):
    """Union by timestamp; missing panel contributes 0. Returns dict ts->combined fields(W)."""
    by_ts = {}
    def add(samples, key):
        for s in samples:
            f = split_fields(s)
            e = by_ts.setdefault(s["dt"], {"a": None, "b": None})
            e[key] = (s, f)
    add(a_samples, "a"); add(b_samples, "b")
    return by_ts

def _run_daily_kwh(pw1, pw2, out):
    """Process monthly-view (daily kWh) CSVs. Writes to daily + monthly RPs only."""
    a_rows = [r for p in pw1 for r in parse_daily_kwh_csv(p)]
    b_rows = [r for p in pw2 for r in parse_daily_kwh_csv(p)]
    print("parsed panel_a rows:", len(a_rows), "from", [os.path.basename(p) for p in pw1])
    print("parsed panel_b rows:", len(b_rows), "from", [os.path.basename(p) for p in pw2])

    monthly_store = defaultdict(lambda: defaultdict(float))

    def emit_daily_row(system, r):
        m, y = mtag(r["dt"])
        ts = r["dt"].timestamp()
        # home in http = total load (house + EV), consistent with 5-min path
        httpf = dict(daily_kwh_fields(r))
        httpf["home"] = r["home"] + r["veh"]
        out["daily"].append(line("http", {"system": system, "month": m, "year": y}, httpf, ts))
        total_load = r["home"] + r["veh"]
        ss = min(100.0, (r["solar"]+r["from_pw"])/total_load*100.0) if total_load>0 else 100.0
        savings = r["to_grid"] * RATE_EXPORT - r["from_grid"] * RATE_IMPORT
        # house_kwh = Home (kWh) from CSV, which is already house-only (EV is separate column)
        enrf = {"ev_kwh": r["veh"], "house_kwh": r["home"], "savings": savings, "self_sufficiency": ss}
        out["daily"].append(line("enriched", {"system": system, "month": m, "year": y}, enrf, ts))
        # accumulate monthly — key on (year, month) ints to avoid DST timestamp splits
        key = (system, r["dt"].year, r["dt"].month)
        monthly_store[key]["home"] += r["home"] + r["veh"]  # total load
        for k in ("solar","from_pw","to_pw","from_grid","to_grid"):
            monthly_store[key][k] += r[k]
        monthly_store[key]["ev_kwh"] += r["veh"]
        monthly_store[key]["house_kwh"] += r["home"]
        monthly_store[key]["savings"] += savings
        monthly_store[key]["_ss_sum"] += ss
        monthly_store[key]["_ss_n"] += 1

    for r in a_rows:
        emit_daily_row("panel_a", r)
    for r in b_rows:
        emit_daily_row("panel_b", r)

    # combined: union by date
    by_date = {}
    for r in a_rows:
        by_date.setdefault(r["dt"], {})["a"] = r
    for r in b_rows:
        by_date.setdefault(r["dt"], {})["b"] = r
    for dt in sorted(by_date):
        ra = by_date[dt].get("a")
        rb = by_date[dt].get("b")
        def g(rx, k): return rx[k] if rx else 0.0
        cr = {"dt": dt, "veh": g(ra,"veh")+g(rb,"veh")}
        # home here is house-only (from CSV); emit_daily_row will add veh for the http measurement
        for k in ("home","solar","from_pw","to_pw","from_grid","to_grid"):
            cr[k] = g(ra, k) + g(rb, k)
        emit_daily_row("combined", cr)

    # emit monthly rollups — use UTC noon on the 1st to avoid DST ambiguity
    for (system, year, month), v in sorted(monthly_store.items()):
        m = MONTHS[month - 1]
        y = str(year)
        # UTC noon on the 1st avoids any local-midnight DST edge cases
        mb_utc = datetime(year, month, 1, 12, 0, 0, tzinfo=timezone.utc)
        ts = mb_utc.timestamp()
        httpf = {k: v[k] for k in ("home","solar","from_pw","to_pw","from_grid","to_grid")}
        out["monthly"].append(line("http", {"system": system, "month": m, "year": y}, httpf, ts))
        ss = (v["_ss_sum"]/v["_ss_n"]) if v.get("_ss_n") else 0.0
        enrf = {"ev_kwh": v["ev_kwh"], "house_kwh": v["house_kwh"], "savings": v["savings"], "self_sufficiency": ss}
        out["monthly"].append(line("enriched", {"system": system, "month": m, "year": y}, enrf, ts))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--indir", default=os.path.expanduser("~/Downloads"))
    ap.add_argument("--out", default=os.path.expanduser("~/Downloads/powerwall_backfill.lp"))
    args = ap.parse_args()

    pw1 = sorted(glob.glob(os.path.join(args.indir, "*PW1*.csv")))
    pw2 = sorted(glob.glob(os.path.join(args.indir, "*PW2*.csv")))
    if not pw1 or not pw2:
        print("ERROR: need *PW1*.csv and *PW2*.csv in", args.indir); sys.exit(1)

    # auto-detect CSV format from first file
    mode = _csv_mode(pw1[0])
    print("detected format:", mode, f"({os.path.basename(pw1[0])})")

    out = {"autogen": [], "raw": [], "kwh": [], "daily": [], "monthly": []}

    if mode == "daily_kwh":
        _run_daily_kwh(pw1, pw2, out)
        # write output (daily + monthly only)
        with open(args.out, "w") as f:
            f.write("# DML\n# CONTEXT-DATABASE: powerwall\n\n")
            for rp in ("daily", "monthly"):
                if not out[rp]:
                    continue
                f.write("# CONTEXT-RETENTION-POLICY: %s\n" % rp)
                f.write("\n".join(out[rp]))
                f.write("\n\n")
        total = sum(len(out[r]) for r in ("daily","monthly"))
        print("wrote", total, "points to", args.out)
        for rp in ("daily","monthly"):
            print("  %-8s %d points" % (rp, len(out[rp])))
        return

    a = [r for p in pw1 for r in parse_csv(p)]
    b = [r for p in pw2 for r in parse_csv(p)]
    print("parsed panel_a rows:", len(a), "from", [os.path.basename(p) for p in pw1])
    print("parsed panel_b rows:", len(b), "from", [os.path.basename(p) for p in pw2])

    now = datetime.now(timezone.utc)
    out = {"autogen": [], "raw": [], "kwh": [], "daily": [], "monthly": []}

    # energy accumulators: acc[rp_bucket][(system, ts_bucket)] = {field: kwh...}
    hourly = defaultdict(lambda: defaultdict(float))
    daily  = defaultdict(lambda: defaultdict(float))
    monthly= defaultdict(lambda: defaultdict(float))
    ss_accum = defaultdict(lambda: defaultdict(float))  # for self_sufficiency numerators/denoms

    def emit_power(system, dt, f):
        m, y = mtag(dt)
        ts = dt.timestamp()
        flds = {k: f[k] for k in ("home","solar","from_pw","to_pw","from_grid","to_grid")}
        if f.get("soc") is not None:
            flds["percentage"] = f["soc"]
        out["autogen"].append(line("http", {"system": system, "month": m, "year": y}, flds, ts))

    def accumulate_energy(system, dt, f):
        """Add this 5-min interval's energy (kWh) to hourly/daily/monthly buckets."""
        e = {k: f[k] / 1000.0 * INTERVAL_H for k in ("home","solar","from_pw","to_pw","from_grid","to_grid")}
        ev_kwh = f["veh"] / 1000.0 * INTERVAL_H
        hb = dt.replace(minute=0, second=0, microsecond=0)
        db = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        mb = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        for bucket, store in ((hb, hourly), (db, daily), (mb, monthly)):
            key = (system, bucket)
            for k, v in e.items():
                store[key][k] += v
            store[key]["ev_kwh"] += ev_kwh
            store[key]["savings"] += e["to_grid"] * RATE_EXPORT - e["from_grid"] * RATE_IMPORT
            # self-sufficiency: mean of per-interval clamped ratio (matches live CQ mean())
            ss_i = min(100.0, max(0.0, (e["solar"]+e["from_pw"])/e["home"]*100.0)) if e["home"] > 0 else 100.0
            store[key]["_ss_sum"] += ss_i
            store[key]["_ss_n"] += 1

    # --- per-panel 5-min power + energy accumulation ---
    for system, samples in (("panel_a", a), ("panel_b", b)):
        for s in samples:
            f = split_fields(s)
            emit_power(system, s["dt"], f)
            accumulate_energy(system, s["dt"], f)

    # --- combined 5-min (power + raw.enriched for recent) + energy ---
    by_ts = combine(a, b)
    for dt in sorted(by_ts):
        ea, eb = by_ts[dt]["a"], by_ts[dt]["b"]
        fa = ea[1] if ea else None
        fb = eb[1] if eb else None
        def g(f, k): return (f[k] if f else 0.0)
        cf = {k: g(fa,k) + g(fb,k) for k in ("home","solar","from_pw","to_pw","from_grid","to_grid","veh")}
        # combined SOC by capacity weighting (kWh)
        soc_a = fa["soc"] if (fa and fa["soc"] is not None) else None
        soc_b = fb["soc"] if (fb and fb["soc"] is not None) else None
        soc_kwh = 0.0; have_soc = False
        if soc_a is not None: soc_kwh += soc_a/100.0*CAP_A; have_soc = True
        if soc_b is not None: soc_kwh += soc_b/100.0*CAP_B; have_soc = True
        comb = dict(cf)
        comb["soc"] = (soc_kwh/(CAP_A+CAP_B)*100.0) if have_soc else None
        emit_power("combined", dt, comb)
        accumulate_energy("combined", dt, comb)
        # raw.enriched (combined) only for recent data (retention = 3d)
        if (now - dt.astimezone(timezone.utc)).days < RAW_WINDOW_DAYS:
            m, y = mtag(dt)
            ss = min(100.0, max(0.0, (cf["solar"]+cf["from_pw"])/cf["home"]*100.0)) if cf["home"]>0 else 100.0
            flds = {
                "home": cf["home"], "solar": cf["solar"], "from_pw": cf["from_pw"],
                "to_pw": cf["to_pw"], "from_grid": cf["from_grid"], "to_grid": cf["to_grid"],
                "ev_load": cf["veh"], "self_sufficiency": ss,
                "soc_kwh": soc_kwh, "soc": comb["soc"] if comb["soc"] is not None else 0.0,
            }
            out["raw"].append(line("enriched", {"system":"combined","month":m,"year":y}, flds, dt.timestamp()))

    # --- emit rollups ---
    def emit_rollups(store, rp):
        for (system, bucket), v in sorted(store.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            m, y = mtag(bucket)
            ts = bucket.timestamp()
            httpf = {k: v[k] for k in ("home","solar","from_pw","to_pw","from_grid","to_grid")}
            out[rp].append(line("http", {"system":system,"month":m,"year":y}, httpf, ts))
            ss = (v["_ss_sum"]/v["_ss_n"]) if v.get("_ss_n") else 0.0
            enrf = {"ev_kwh": v["ev_kwh"], "savings": v["savings"], "self_sufficiency": ss}
            out[rp].append(line("enriched", {"system":system,"month":m,"year":y}, enrf, ts))

    emit_rollups(hourly, "kwh")
    emit_rollups(daily, "daily")
    emit_rollups(monthly, "monthly")

    # --- write import file ---
    with open(args.out, "w") as f:
        f.write("# DML\n# CONTEXT-DATABASE: powerwall\n\n")
        for rp in ("autogen", "raw", "kwh", "daily", "monthly"):
            if not out[rp]:
                continue
            f.write("# CONTEXT-RETENTION-POLICY: %s\n" % rp)
            f.write("\n".join(out[rp]))
            f.write("\n\n")
    total = sum(len(v) for v in out.values())
    print("wrote", total, "points to", args.out)
    for rp in ("autogen","raw","kwh","daily","monthly"):
        print("  %-8s %d points" % (rp, len(out[rp])))

if __name__ == "__main__":
    main()
