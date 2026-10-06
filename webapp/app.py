#!/usr/bin/env python3
"""
app.py — Custom Powerwall web dashboard (synthwave UI).

A thin FastAPI service that joins the powerwall-dashboard compose network,
queries InfluxDB (which is not exposed to the LAN), and serves:

  - a JSON API under /api/*  (live state, history, period totals, monthly)
  - the static single-page synthwave dashboard under /

All InfluxDB access stays server-side; the browser only ever talks to this app.
"""

import json
import logging
import os
import sys
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import requests.exceptions
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from influxdb import InfluxDBClient
from influxdb.exceptions import InfluxDBClientError, InfluxDBServerError

import config
import settings as settings_mod
import tou

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("webapp")

app = FastAPI(title="Powerwall Dashboard", docs_url=None, redoc_url=None)

# Read-only cross-origin access for a separate dashboard page. Off unless
# DASHBOARD_ORIGINS is set. GET only, so POST /api/settings stays same-origin;
# credentials allowed so a cookie-based proxy login (Cloudflare Access) works.
if config.DASHBOARD_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.DASHBOARD_ORIGINS,
        allow_methods=["GET"],
        allow_credentials=True,
    )
    log.info("cross-origin reads allowed from: %s", ", ".join(config.DASHBOARD_ORIGINS))

STATIC_DIR = Path(__file__).parent / "static"

# Display + billing settings: env defaults overlaid with the persisted
# settings file. System tags, timezone, and rates come from here so edits
# on the settings page apply live.
SETTINGS = settings_mod.Settings()

# Pre-live daily history (Feb–May 2026) from the Tesla app, appended in the web
# app only (never written to InfluxDB / shown in Grafana). { system: [{ts,...}] }.
try:
    HISTORICAL = json.loads((Path(__file__).parent / "historical.json").read_text())
    _hmax = max((r["ts"] for r in HISTORICAL.get("combined", [])), default=0)
    log.info("loaded historical backfill: %d combined days", len(HISTORICAL.get("combined", [])))
except Exception as exc:  # noqa: BLE001 — historical backfill is optional
    HISTORICAL, _hmax = {}, 0
    log.warning("no historical backfill loaded: %s", exc)


def _historical_rows(system: str, start_ms: int, end_ms: Optional[int] = None):
    """Daily historical rows for a system within [start_ms, end_ms] (pre-live only)."""
    return [r for r in HISTORICAL.get(system, [])
            if r["ts"] >= start_ms and (end_ms is None or r["ts"] <= end_ms)]


def _load_billing_cycles():
    """Real electric-company billing cycles from billing_cycles.json (or [])."""
    try:
        raw = json.loads((Path(__file__).parent / "billing_cycles.json").read_text())
        cycles = []
        for c in raw.get("cycles", []):
            s = date.fromisoformat(c["start"])
            e = date.fromisoformat(c["end"])
            if e < s:
                log.warning("billing cycle end %s before start %s — skipped", e, s)
                continue
            bill = c.get("bill")
            cycles.append({"start": s, "end": e,
                           "bill": float(bill) if bill is not None else None})
        cycles.sort(key=lambda c: c["start"])
        log.info("loaded %d billing cycles from billing_cycles.json", len(cycles))
        return cycles
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001 — bills are optional; fall back to billing_day
        log.warning("could not load billing_cycles.json: %s", exc)
        return []


# Known billing cycles read off the real bills. The picker + the "billing"
# range use these when present; billing_day is only the fresh-install fallback.
BILLING_CYCLES = _load_billing_cycles()


def _running_balances():
    """Account balance (Total Due) through each billed cycle, and the final carry.

    Credits carry forward; when a cycle's charge exceeds the banked credit the
    overage is auto-paid, so the carried balance never goes positive. Returns
    ({cycle_start_date: total_due_after_this_cycle}, carry_after_last_billed).
    Cycles with no known bill are skipped (assumed auto-paid, no carry change).
    """
    carried = 0.0
    by_start = {}
    for c in BILLING_CYCLES:  # chronological (sorted on load)
        bill = c.get("bill")
        if bill is None:
            continue
        total_due = carried + bill
        by_start[c["start"]] = total_due
        carried = min(0.0, total_due)  # positive dues auto-pay to 0
    return by_start, carried

UTC = ZoneInfo("UTC")

# range token -> (kind, key, GROUP BY interval).
#   rolling:  trailing window ending now.
#   todate:   from local start-of-period to now (settings-page timezone).
#   billing:  from the start of the current real billing cycle (bills, else
#             the billing_day fallback) to now.
#   lifetime: everything, downsampled to daily points.
#   custom:   explicit start/end (ms epoch) via query params.
RANGES = {
    "today": ("todate", "day", "10m"),
    "24h": ("rolling", "24h", "10m"),
    "7d": ("rolling", "7d", "1h"),
    "month": ("todate", "month", "3h"),
    "30d": ("rolling", "30d", "3h"),
    "year": ("todate", "year", "1d"),
    "billing": ("billing", None, "3h"),
    "lifetime": ("lifetime", None, "1d"),
    "custom": ("custom", None, None),
}


def _interval_for_span(span_ms: int) -> str:
    days = span_ms / 86_400_000
    if days <= 2:
        return "10m"
    if days <= 14:
        return "1h"
    if days <= 92:
        return "3h"
    return "1d"


def _interval_hours(interval: str) -> float:
    """'1h' -> 1, '3h' -> 3, '1d' -> 24, '10m' -> 0.1666…"""
    n, unit = float(interval[:-1]), interval[-1]
    return n * 24 if unit == "d" else n / 60.0 if unit == "m" else n


def _prefer_rollup(range: str, start_ms: int, end: Optional[int]) -> bool:
    """Whether to source energy from the complete kwh.http hourly rollup.

    autogen.enriched (1-min, INF) only exists from ~2026-06-13 (cq_enriched_autogen
    was added then), so it undercounts any range reaching earlier. The kwh.http
    rollup is complete and tracks the revenue meter, so use it for multi-day
    ranges; keep the finer autogen.enriched integral for intraday (today/24h),
    where it is both fresh and always covered.
    """
    if range in ("today", "24h"):
        return False
    if range == "custom":
        return (end or 0) - (start_ms or 0) > 2 * 86_400_000
    return True  # 7d, month, 30d, year, billing, lifetime


def _add_months(dt: datetime, k: int) -> datetime:
    """Shift a datetime by k calendar months, keeping the day-of-month.

    Only used on billing-cycle starts, where the day is capped at 28 — so the
    target month always has that day.
    """
    m = dt.month - 1 + k
    return dt.replace(year=dt.year + m // 12, month=m % 12 + 1)


def _billing_start(now: datetime, day: int) -> datetime:
    """Start of the billing cycle `now` falls in (local midnight on `day`)."""
    s = now.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
    return s if s <= now else _add_months(s, -1)


def _median_cycle_days(cycles: list) -> int:
    """Median billing-cycle length in days (defaults to 30 with no data)."""
    lengths = sorted((c["end"] - c["start"]).days + 1 for c in cycles)
    if not lengths:
        return 30
    mid = len(lengths) // 2
    if len(lengths) % 2:
        return lengths[mid]
    return round((lengths[mid - 1] + lengths[mid]) / 2)


def _billing_cycle_periods(now: datetime, tz: ZoneInfo) -> list:
    """Billing cycles as (start_dt, end_dt_exclusive, estimated), oldest first.

    Known cycles come straight from the bills; once they run out, cycles are
    rolled forward by the median known length so the in-progress period (and any
    unbilled ones after the last statement) still appear, flagged estimated.
    """
    def midnight(d: date) -> datetime:
        return datetime(d.year, d.month, d.day, tzinfo=tz)

    periods = [(midnight(c["start"]), midnight(c["end"]) + timedelta(days=1),
                False, c.get("bill")) for c in BILLING_CYCLES]
    if periods:
        span = timedelta(days=_median_cycle_days(BILLING_CYCLES))
        cursor = periods[-1][1]
        while cursor <= now:  # roll forward; unbilled → no actual bill
            periods.append((cursor, cursor + span, True, None))
            cursor += span
    return periods


def _current_billing_start(now: datetime) -> datetime:
    """Start of the billing cycle `now` falls in — real bills first, else billing_day."""
    for s, e, *_ in _billing_cycle_periods(now, SETTINGS.tz()):
        if s <= now < e:
            return s
    return _billing_start(now, SETTINGS.billing_day())


def _range_bounds(range: str, start: Optional[int] = None, end: Optional[int] = None):
    """Return (influxql_time_predicate, group_by_interval, start_ms) for a range."""
    if range not in RANGES:
        raise HTTPException(status_code=400, detail=f"unknown range '{range}'")
    kind, key, interval = RANGES[range]
    # "To date" boundaries start at local midnight in the settings-page
    # timezone, so a TZ edit applies without a restart.
    now = datetime.now(SETTINGS.tz())

    if kind == "custom":
        if start is None or end is None:
            raise HTTPException(status_code=400,
                                detail="custom range needs start and end (ms epoch)")
        if not 0 <= start < end:
            raise HTTPException(status_code=400, detail="custom range: start must be before end")
        return (f"time >= {start}ms AND time <= {end}ms",
                _interval_for_span(end - start), start)

    if kind == "lifetime":
        return "time > '2000-01-01T00:00:00Z'", interval, 0

    if kind == "rolling":
        n, unit = int(key[:-1]), key[-1]
        s = now - (timedelta(hours=n) if unit == "h" else timedelta(days=n))
        return f"time > now() - {key}", interval, int(s.timestamp() * 1000)

    if kind == "billing":
        s = _current_billing_start(now)
    elif key == "day":
        s = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif key == "month":
        s = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:  # year
        s = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    start_utc = s.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"time >= '{start_utc}'", interval, int(s.timestamp() * 1000)

# Power fields stored (W) in the enriched measurement.
POWER_FIELDS = ("home", "solar", "from_pw", "to_pw", "from_grid", "to_grid")


def _client() -> InfluxDBClient:
    return InfluxDBClient(
        host=config.INFLUXDB_HOST,
        port=config.INFLUXDB_PORT,
        database=config.INFLUXDB_DATABASE,
        timeout=10,
    )


def _query(q: str):
    client = _client()
    try:
        return client.query(q, epoch="ms")
    except (InfluxDBClientError, InfluxDBServerError, OSError,
            requests.exceptions.RequestException) as exc:
        log.warning("Query failed: %s — %s", q, exc)
        raise HTTPException(status_code=502, detail=f"InfluxDB query error: {exc}")
    finally:
        client.close()


def _validate_system(system: str) -> str:
    if system not in SETTINGS.systems():
        raise HTTPException(status_code=400, detail=f"unknown system '{system}'")
    return system


@app.get("/api/health")
def health():
    try:
        client = _client()
        client.ping()
        client.close()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"ok": False, "error": str(exc)})


@app.get("/api/live")
def live():
    """Latest enriched row per system from raw.enriched (freshest resolution)."""
    fields = ", ".join(f"last({f}) AS {f}" for f in
                       (*POWER_FIELDS, "soc", "soc_kwh", "self_sufficiency",
                        "grid_dependency", "ev_load", "total_capacity_kwh"))
    q = (
        f"SELECT {fields} FROM \"raw\".\"enriched\" "
        f"WHERE time > now() - 10m GROUP BY system"
    )
    result = _query(q)

    out = {}
    known = SETTINGS.systems()
    merged: dict = {}
    for (_, tags), points in result.items():
        system = tags.get("system")
        if system not in known:
            continue
        # merge partial GROUP BY series entries — first non-null value wins
        slot = merged.setdefault(system, {})
        for row in points:
            for k, v in row.items():
                if v is not None and k not in slot:
                    slot[k] = v
    for system, row in merged.items():
        out[system] = _shape_live(row)

    return {"systems": out}


def _shape_live(row: dict) -> dict:
    def g(k):
        return float(row.get(k) or 0.0)

    from_pw, to_pw = g("from_pw"), g("to_pw")
    from_grid, to_grid = g("from_grid"), g("to_grid")
    home = g("home")
    # Self-powered = share of home load NOT drawn from the grid. Computed here
    # (not taken from the enricher's stored field, which uses (solar+from_pw)/home
    # and pins at 100%) so the tile reflects real grid dependence.
    self_powered = max(0.0, min(100.0, (home - from_grid) / home * 100)) if home > 0 else 100.0
    return {
        "home": home,
        "solar": g("solar"),
        "from_pw": from_pw,
        "to_pw": to_pw,
        "from_grid": from_grid,
        "to_grid": to_grid,
        # net: + = battery discharging / grid importing
        "battery": from_pw - to_pw,
        "grid": from_grid - to_grid,
        "soc": g("soc"),
        "soc_kwh": g("soc_kwh"),
        "self_sufficiency": round(self_powered, 1),
        "ev_load": g("ev_load"),
        "ts": row.get("time"),
    }


def _period_energy(predicate: str, use_rollup: bool) -> dict:
    """Per-system POWER_FIELDS energy (kWh) over the period, as {system: {field}}.

    rollup: sum the complete kwh.http hourly rollup; kwh.http has no 'combined'
    row, so combined is synthesized as the panel sum.
    else:   integral(W,1s)/3.6e6 on autogen.enriched (finer; intraday only).
    """
    known = SETTINGS.systems()
    if use_rollup:
        live_start = _hmax + 86_400_000 if _hmax else 0
        sel = ", ".join(f"sum({f}) AS {f}" for f in POWER_FIELDS)
        q = (f"SELECT {sel} FROM \"kwh\".\"http\" "
             f"WHERE {predicate} AND time >= {live_start}ms GROUP BY system")
    else:
        sel = ", ".join(f"integral({f}, 1s) / 3600000 AS {f}" for f in POWER_FIELDS)
        q = (f"SELECT {sel} FROM \"autogen\".\"enriched\" "
             f"WHERE {predicate} GROUP BY system")

    acc: dict = {}
    for (_, tg), pts in _query(q).items():
        st = tg.get("system")
        if st not in known:
            continue
        # A GROUP BY <tag> response can scatter one series' fields across
        # several partial entries (others null) — merge, first non-null wins.
        slot = acc.setdefault(st, {})
        for row in pts:
            for k in POWER_FIELDS:
                v = row.get(k)
                if v is not None and k not in slot:
                    slot[k] = float(v)
    if use_rollup:
        combined = {k: 0.0 for k in POWER_FIELDS}
        for st, slot in acc.items():
            if st == "combined":
                continue
            for k in POWER_FIELDS:
                combined[k] += slot.get(k, 0.0)
        acc["combined"] = combined
    return acc


def _history_points(predicate: str, interval: str, system: str,
                    use_rollup: bool) -> list:
    """Down-sampled power rows (W) for one system: dicts of POWER_FIELDS + soc.

    autogen path returns mean power directly. The rollup path reads hourly
    energy from kwh.http and converts to mean power (kWh / bucket-hours * 1000);
    combined = sum of panels, and soc has no energy analogue so it is None.
    """
    known = SETTINGS.systems()
    if not use_rollup:
        sel = ", ".join(f"mean({f}) AS {f}" for f in (*POWER_FIELDS, "soc"))
        q = (f"SELECT {sel} FROM \"autogen\".\"enriched\" "
             f"WHERE system = '{system}' AND {predicate} "
             f"GROUP BY time({interval}) fill(none)")
        return list(_query(q).get_points())

    live_start = _hmax + 86_400_000 if _hmax else 0
    conv = 1000.0 / _interval_hours(interval)  # kWh/bucket -> mean W
    sel = ", ".join(f"sum({f}) AS {f}" for f in POWER_FIELDS)
    q = (f"SELECT {sel} FROM \"kwh\".\"http\" "
         f"WHERE {predicate} AND time >= {live_start}ms "
         f"GROUP BY time({interval}), system fill(none)")
    merged: dict = {}
    for (_, tg), pts in _query(q).items():
        st = tg.get("system")
        if st not in known or st == "combined":
            continue
        if system != "combined" and st != system:
            continue
        for row in pts:
            ts = row.get("time")
            slot = merged.setdefault(ts, {})
            for k in POWER_FIELDS:
                v = row.get(k)
                if v is not None:
                    slot[k] = slot.get(k, 0.0) + float(v)
    out = []
    for ts in sorted(merged):
        s = merged[ts]
        row = {"time": ts, "soc": None}
        for k in POWER_FIELDS:
            row[k] = s[k] * conv if k in s else None
        out.append(row)
    return out


@app.get("/api/history")
def history(
    range: str = Query("24h"),
    system: str = Query("combined"),
    start: Optional[int] = Query(None),
    end: Optional[int] = Query(None),
):
    """Down-sampled power + SOC series. Sourced from autogen.enriched intraday,
    or the complete kwh.http rollup for multi-day ranges (see _prefer_rollup)."""
    _validate_system(system)
    predicate, interval, start_ms = _range_bounds(range, start, end)

    points = _history_points(predicate, interval, system,
                             _prefer_rollup(range, start_ms, end))

    series = {
        "t": [],
        "home": [], "solar": [], "battery": [], "grid": [], "soc": [],
    }
    # Prepend pre-live daily history as day-average power (kWh/day -> W) so the
    # cumulative chart integrates back to the right totals over long ranges.
    for r in _historical_rows(system, start_ms, end):
        w = lambda k: round(r[k] * 1000.0 / 24.0, 1)  # noqa: E731
        series["t"].append(r["ts"])
        # backfill "home" is house-only — add the car back for the true total
        series["home"].append(round((r["home"] + r.get("ev", 0.0)) * 1000.0 / 24.0, 1))
        series["solar"].append(w("solar"))
        series["battery"].append(w("from_pw"))
        series["grid"].append(round((r["from_grid"] - r["to_grid"]) * 1000.0 / 24.0, 1))
        series["soc"].append(None)
    for p in points:
        def g(k):
            v = p.get(k)
            return None if v is None else round(float(v), 1)

        series["t"].append(p.get("time"))
        series["home"].append(g("home"))
        series["solar"].append(g("solar"))
        fp, tp = p.get("from_pw") or 0.0, p.get("to_pw") or 0.0
        fg, tg = p.get("from_grid") or 0.0, p.get("to_grid") or 0.0
        series["battery"].append(round(fp - tp, 1))
        series["grid"].append(round(fg - tg, 1))
        series["soc"].append(g("soc"))

    return {"range": range, "system": system, "series": series}


@app.get("/api/totals")
def totals(
    range: str = Query("24h"),
    system: str = Query("combined"),
    start: Optional[int] = Query(None),
    end: Optional[int] = Query(None),
):
    """Energy totals (kWh) over the period. Multi-day ranges sum the complete
    kwh.http rollup (meter-accurate); intraday integrates autogen.enriched."""
    _validate_system(system)
    predicate, _, start_ms = _range_bounds(range, start, end)

    # Per-system energy from the source that is complete for this range, plus
    # the per-gateway breakdown that feeds the tile split panels.
    known = SETTINGS.systems()
    acc = _period_energy(predicate, _prefer_rollup(range, start_ms, end))

    def derived(st: str) -> dict:
        base = acc.get(st) or {}
        vals = {k: base.get(k, 0.0) for k in POWER_FIELDS}
        # Append pre-live daily history that falls within the range (web app
        # only). Backfill "home" is house-only — add the car back.
        for r in _historical_rows(st, start_ms, end):
            vals["home"] += r["home"] + r.get("ev", 0.0); vals["solar"] += r["solar"]
            vals["from_grid"] += r["from_grid"]; vals["to_grid"] += r["to_grid"]
            vals["from_pw"] += r["from_pw"]
        home, solar = round(vals["home"], 2), round(vals["solar"], 2)
        from_grid, to_grid = round(vals["from_grid"], 2), round(vals["to_grid"], 2)
        self_suff = round((home - from_grid) / home * 100, 1) if home > 0 else 0.0
        # Estimated bill under net metering: the meter nets import against export,
        # then the net is billed at the import rate (net consumer) or credited at
        # the export/credit rate (net producer); plus the fixed monthly charge.
        net_grid = from_grid - to_grid
        rate = SETTINGS.rate_import() if net_grid >= 0 else SETTINGS.rate_export()
        bill = SETTINGS.fixed_charge() + net_grid * rate
        return {
            "home": home,
            "solar": solar,
            "from_grid": from_grid,
            "to_grid": to_grid,
            "from_pw": round(vals["from_pw"], 2),
            "to_pw": round(vals["to_pw"], 2),
            # Ahead/behind: solar made minus total consumption (incl. car).
            "net": round(solar - home, 1),
            "self_sufficiency": min(100.0, max(0.0, self_suff)),
            "savings": round(to_grid * SETTINGS.rate_export()
                             - from_grid * SETTINGS.rate_import(), 2),
            "bill_estimate": round(bill, 2),
        }

    t = derived(system)

    # Per-charger (EV) energy for the house/charger split row: one charger per
    # gateway. Scoped to the requested system (combined -> every gateway).
    tags = [t for t in SETTINGS.systems() if t != "combined"]
    if system != "combined":
        tags = [system]
    chargers = {t: 0.0 for t in tags}
    live_start = _hmax + 86_400_000 if _hmax else 0  # backfill covers earlier days
    evq = (
        f"SELECT sum(ev_kwh) AS ev_kwh FROM \"kwh\".\"enriched\" "
        f"WHERE {predicate} AND time >= {live_start}ms GROUP BY system"
    )
    seen = set()
    for (_, tg), pts in _query(evq).items():
        st = tg.get("system")
        if st not in chargers or st in seen:
            continue
        for row in pts:
            v = row.get("ev_kwh")
            if v is not None:
                chargers[st] += float(v)
                seen.add(st)
                break
    for tag in tags:
        chargers[tag] += sum(r.get("ev", 0.0) for r in _historical_rows(tag, start_ms, end))
        chargers[tag] = round(max(chargers[tag], 0.0), 2)
    t["chargers"] = chargers

    return {
        "range": range,
        "system": system,
        "totals": t,
        "by_system": {s: derived(s) for s in known if s != "combined"},
    }


@app.get("/api/monthly")
def monthly(system: str = Query("combined")):
    """Calendar-month energy (kWh) for the monthly bar chart.

    Summed from the hourly kwh.http / kwh.enriched rollups grouped into true
    calendar months in the house timezone, plus the pre-live backfill. The
    monthly.* CQ rollups are deliberately NOT used: they bucket by 30-day
    epochs (not calendar months), only materialize when a bucket closes, and
    have no live 'combined' source — which froze the chart at the imported
    history.
    """
    _validate_system(system)
    tzname = SETTINGS.tz().key

    # kwh.http only has per-gateway rows; combined = sum across gateways
    # (matches how the enricher derives combined from its inputs).
    where = "system <> 'combined'" if system == "combined" else f"system = '{system}'"
    # Days the backfill covers come from it alone — live starts the day after.
    live_start = _hmax + 86_400_000 if _hmax else 0

    def daily(rp_meas: str, sel: str, pred: str):
        q = (f"SELECT {sel} FROM {rp_meas} WHERE {pred} "
             f"AND time >= {live_start}ms GROUP BY time(1d) fill(none)")
        try:
            return list(_query(q + f" TZ('{tzname}')").get_points())
        except HTTPException:  # no tzdata for this zone — UTC days are close enough
            return list(_query(q).get_points())

    months: dict = {}

    def add(ts_ms: int, vals: dict):
        # +12h: day-bucket starts (local or UTC midnight) land mid-day, so the
        # local date — and thus the month — is right in either grouping mode.
        d = datetime.fromtimestamp((ts_ms + 43_200_000) / 1000, SETTINGS.tz())
        s = months.setdefault((d.year, d.month), {})
        for k, v in vals.items():
            if v is not None:
                s[k] = s.get(k, 0.0) + float(v)

    for r in _historical_rows(system, 0):
        # backfill "home" is house-only; the car ("ev") is tracked separately
        add(r["ts"], {k: r.get(k) for k in
                      ("solar", "from_grid", "to_grid", "from_pw")}
            | {"home": r["home"] + r.get("ev", 0.0), "ev_kwh": r.get("ev")})

    sel = ", ".join(f"sum({f}) AS {f}" for f in POWER_FIELDS)
    for p in daily('"kwh"."http"', sel, where):
        add(p["time"], {k: p.get(k) for k in POWER_FIELDS})

    for p in daily('"kwh"."enriched"', "sum(ev_kwh) AS ev_kwh", f"system = '{system}'"):
        add(p["time"], {"ev_kwh": p.get("ev_kwh")})

    rows = []
    for (y, m), s in sorted(months.items()):
        home = round(s.get("home", 0.0), 1)
        # live EV estimate can overcount — never let the car exceed the home total
        ev = min(round(s.get("ev_kwh", 0.0), 1), home)
        row = {
            # noon UTC on the 1st renders as the right month in any viewer TZ
            "ts": int(datetime(y, m, 1, 12, tzinfo=UTC).timestamp() * 1000),
            "home": home, "ev_kwh": ev, "house_kwh": round(home - ev, 1),
        }
        for k in ("solar", "from_grid", "to_grid", "from_pw"):
            row[k] = round(s.get(k, 0.0), 1)
        rows.append(row)

    return {"system": system, "months": rows}


def _daily_energy() -> dict:
    """Per-day, per-panel energy as {date: {solar:{tag}, home:{tag}, ev:{tag}}}.

    home includes the car; keeping everything per panel lets records follow the
    combined/panel-a/panel-b selector. Pre-live backfill (daily) + the complete
    kwh.http / kwh.enriched rollups.
    """
    tz = SETTINGS.tz()
    tzname = tz.key
    live_start = _hmax + 86_400_000 if _hmax else 0
    tags = [t for t in SETTINGS.systems() if t != "combined"]
    days: dict = {}

    def slot(ts_ms: int) -> dict:
        # +12h so a UTC-or-local midnight bucket lands mid-day → right local date
        d = datetime.fromtimestamp((ts_ms + 43_200_000) / 1000, tz).date()
        return days.setdefault(d, {"solar": {t: 0.0 for t in tags},
                                   "home": {t: 0.0 for t in tags},
                                   "ev": {t: 0.0 for t in tags}})

    # pre-live backfill, per panel (home is house-only → add the car back)
    for tag in tags:
        for r in _historical_rows(tag, 0):
            s = slot(r["ts"])
            s["solar"][tag] += r.get("solar", 0.0)
            s["home"][tag] += r["home"] + r.get("ev", 0.0)
            s["ev"][tag] += r.get("ev", 0.0)

    def q_daily(meas: str, sel: str):
        q = (f"SELECT {sel} FROM {meas} WHERE system <> 'combined' "
             f"AND time >= {live_start}ms GROUP BY time(1d), system fill(none)")
        try:
            return _query(q + f" TZ('{tzname}')")
        except HTTPException:  # no tzdata for this zone — UTC days are close enough
            return _query(q)

    # live per-panel solar/home
    for (_, tg), pts in q_daily('"kwh"."http"',
                                "sum(home) AS home, sum(solar) AS solar").items():
        st = tg.get("system")
        if st not in tags:
            continue
        for row in pts:
            s = slot(row["time"])
            if row.get("solar") is not None:
                s["solar"][st] += float(row["solar"])
            if row.get("home") is not None:
                s["home"][st] += float(row["home"])

    # live per-charger EV
    for (_, tg), pts in q_daily('"kwh"."enriched"', "sum(ev_kwh) AS ev_kwh").items():
        st = tg.get("system")
        if st not in tags:
            continue
        for row in pts:
            v = row.get("ev_kwh")
            if v is not None:
                slot(row["time"])["ev"][st] += float(v)

    return days


def _record_value(day: dict, metric: str, systems: list) -> float:
    """Metric value for a day, scoped to the given panel tags."""
    if metric == "solar":
        return sum(day["solar"][t] for t in systems)
    if metric == "home":  # all consumption incl. the cars
        return sum(day["home"][t] for t in systems)
    if metric == "house":  # home minus the cars
        return max(0.0, sum(day["home"][t] - day["ev"][t] for t in systems))
    return day["ev"].get(metric, 0.0)  # a specific charger (ignores system scope)


def _record_bucket(d: date, period: str):
    """(bucket_start_date, label) for the day d under a period granularity.

    Labels stay short so they fit one line on a phone; the period is already
    named above the list, and all current data is one year. Day/week drop the
    year; month/year keep it since they read as a year.
    """
    if period == "day":
        return d, f"{d:%b %-d}"
    if period == "week":
        start = d - timedelta(days=d.weekday())  # Monday
        end = start + timedelta(days=6)
        span = (f"{start:%b %-d}–{end.day}" if start.month == end.month
                else f"{start:%b %-d}–{end:%b %-d}")
        return start, span
    if period == "month":
        return d.replace(day=1), f"{d:%b %Y}"
    return d.replace(month=1, day=1), f"{d.year}"  # year


def _bucket_span(start: date, period: str):
    """(first_day, last_day) covered by a bucket, for completeness checks."""
    if period == "day":
        return start, start
    if period == "week":
        return start, start + timedelta(days=6)
    if period == "month":
        return start, start.replace(day=monthrange(start.year, start.month)[1])
    return date(start.year, 1, 1), date(start.year, 12, 31)  # year


@app.get("/api/records")
def records(metric: str = Query("solar"), period: str = Query("day"),
            system: str = Query("combined")):
    """Top 5 and bottom 5 of a metric over day/week/month/year buckets.

    metric: 'solar', 'house' (home minus the cars), or a charger tag (one car);
    solar/house follow `system` (combined or one panel). Only buckets fully
    covered by data and already ended count — a partial first or current bucket
    can't set a false record.
    """
    tags = [t for t in SETTINGS.systems() if t != "combined"]
    if metric not in ("solar", "house", "home", *tags):
        raise HTTPException(status_code=400, detail=f"unknown metric '{metric}'")
    if period not in ("day", "week", "month", "year"):
        raise HTTPException(status_code=400, detail=f"unknown period '{period}'")
    _validate_system(system)
    scope = tags if system == "combined" else [system]

    tz = SETTINGS.tz()
    days = _daily_energy()
    empty = {"metric": metric, "period": period, "system": system,
             "unit": "kWh", "top": [], "bottom": [], "count": 0}
    if not days:
        return empty
    today = datetime.now(tz).date()
    dmax = max(days)

    # This metric's data only starts once its equipment is online (e.g. solar
    # came up mid-Feb; a car's first charge is later). Leading days read 0 not
    # because they were low but because there was nothing to measure — start the
    # window at the first day with a real value so those can't win "least".
    active = [d for d, dd in days.items() if _record_value(dd, metric, scope) > 0]
    if not active:
        return empty
    dmin = min(active)

    buckets: dict = {}
    for d, dd in days.items():
        start, label = _record_bucket(d, period)
        b = buckets.setdefault(start, {"label": label, "val": 0.0})
        b["val"] += _record_value(dd, metric, scope)

    qualified = []
    for start, b in buckets.items():
        bstart, bend = _bucket_span(start, period)
        if bstart >= dmin and bend <= dmax and bend < today:
            qualified.append((start, b))
    if not qualified:
        return empty

    def shape(item) -> dict:
        start, b = item
        return {
            "value": round(b["val"], 1),
            "label": b["label"],
            "start": int(datetime(start.year, start.month, start.day,
                                  tzinfo=tz).timestamp() * 1000),
        }

    ranked = sorted(qualified, key=lambda kv: kv[1]["val"], reverse=True)  # high→low
    top = ranked[:5]
    # bottom = the lowest, ascending, excluding any already shown in top
    bottom = [x for x in reversed(ranked) if x not in top][:5]
    return {"metric": metric, "period": period, "system": system, "unit": "kWh",
            "top": [shape(x) for x in top],
            "bottom": [shape(x) for x in bottom],
            "count": len(qualified)}


def _short_date(d: datetime) -> str:
    return f"{d:%b} {d.day}".upper()


def _period_entry(s: datetime, e: datetime, now: datetime, estimated: bool,
                  bill: Optional[float] = None) -> dict:
    """Shape one billing cycle [s, e) for the picker."""
    is_current = s <= now < e
    end_ms = int(e.timestamp() * 1000) - 1
    last_day = e - timedelta(days=1)
    return {
        "key": s.strftime("%Y-%m-%d"),
        "label": (f"{_short_date(s)} – NOW" if is_current
                  else f"{_short_date(s)} – {_short_date(last_day)}"),
        "start": int(s.timestamp() * 1000),
        "end": end_ms,
        "current": is_current,
        "estimated": estimated,
        # actual per-cycle bill ($, negative = credit) if this cycle is billed
        "bill": bill,
    }


@app.get("/api/billing_periods")
def billing_periods(limit: int = Query(14, ge=1, le=36)):
    """Recent billing cycles (newest first) for the charging-split picker.

    Cycles come from the real bills (billing_cycles.json) when present, with the
    in-progress period rolled forward from the median cycle length; otherwise
    they fall back to synthetic billing_day-to-billing_day cycles. Either way the
    list stops where hourly charging data does: the pre-live backfill is
    daily-only, so a peak/off-peak split can't be computed for it.
    """
    now = datetime.now(SETTINGS.tz())
    tz = SETTINGS.tz()
    day = SETTINGS.billing_day()
    live_start = _hmax + 86_400_000 if _hmax else 0

    if BILLING_CYCLES:
        # newest first; stop once a cycle ends before live hourly data begins
        bounds = list(reversed(_billing_cycle_periods(now, tz)))
        running, carried = _running_balances()
        periods = []
        for s, e, estimated, bill in bounds:
            if int(e.timestamp() * 1000) - 1 < live_start:
                break
            entry = _period_entry(s, e, now, estimated, bill)
            bal = running.get(s.date())
            # balance = Total Due through a billed cycle; for the unbilled
            # in-progress cycle, prior_balance is what carries in (add estimate).
            entry["balance"] = round(bal, 2) if bal is not None else None
            entry["prior_balance"] = round(carried, 2)
            periods.append(entry)
            if len(periods) >= limit:
                break
        return {"source": "bills", "billing_day": day,
                "timezone": tz.key, "periods": periods}

    # Fallback: synthetic cycles from the billing_day setting (no bills entered).
    cur = _billing_start(now, day)
    periods = []
    for i in range(limit):
        s = _add_months(cur, -i)
        e = _add_months(s, 1)
        if int(e.timestamp() * 1000) - 1 < live_start:
            break
        periods.append(_period_entry(s, e, now, False))
    return {"source": "billing_day", "billing_day": day,
            "timezone": tz.key, "periods": periods}


@app.get("/api/tou")
def tou_split(
    range: str = Query("billing"),
    system: str = Query("combined"),
    start: Optional[int] = Query(None),
    end: Optional[int] = Query(None),
):
    """Per-charger EV energy (kWh) split into peak vs off-peak hours.

    Hourly kwh.enriched ev_kwh buckets are labelled by their wall-clock hour in
    the house timezone using the configured peak window (settings "tou",
    defaulting to 1PM-9PM daily). Hourly resolution is exactly enough: every
    peak boundary lands on the hour.

    Each hour's charge is also attributed to grid vs on-site (solar/battery)
    PER PANEL: the panel's own grid import (kwh.http from_grid) caps how much of
    that hour's charging could have come from the grid, so grid-sourced charge =
    min(import, ev_kwh); the remainder was solar/battery. The utility meters
    charging by clock time regardless of source (the utility reads the car), so
    "peak"/"off_peak" cover all charging; "*_grid" isolate what actually drew on
    the grid — a marginal attribution (the EV is treated as the swing load).
    """
    _validate_system(system)
    predicate, _, _ = _range_bounds(range, start, end)
    cfg = SETTINGS.tou()
    tz = SETTINGS.tz()

    tags = [t for t in SETTINGS.systems() if t != "combined"]
    if system != "combined":
        tags = [system]
    live_start = _hmax + 86_400_000 if _hmax else 0

    def _hourly(measurement: str, field: str) -> dict:
        """{ system: { hour_ts: summed field } } over the range (live data only)."""
        q = (
            f"SELECT sum({field}) AS {field} FROM {measurement} "
            f"WHERE {predicate} AND time >= {live_start}ms "
            f"GROUP BY time(1h), system fill(none)"
        )
        # The rollup CQ also groups by month/year tags, so one system can come
        # back as several series — a given hour bucket belongs to exactly one.
        out: dict = {t: {} for t in tags}
        for (_, tg), pts in _query(q).items():
            st = tg.get("system")
            if st not in out:
                continue
            for row in pts:
                v = row.get(field)
                if v is None:
                    continue
                ts = row.get("time")
                out[st][ts] = out[st].get(ts, 0.0) + max(float(v), 0.0)
        return out

    ev = _hourly('"kwh"."enriched"', "ev_kwh")       # charging per panel per hour
    imp = _hourly('"kwh"."http"', "from_grid")        # grid import, same panels/hours

    chargers = {}
    for tag in tags:
        peak = off = peak_grid = off_grid = 0.0
        for ts, kwh in ev[tag].items():
            # can't have sent more grid energy to the car than the panel imported
            grid = min(imp[tag].get(ts, 0.0), kwh)
            if tou.is_peak(datetime.fromtimestamp(ts / 1000, tz), cfg):
                peak += kwh
                peak_grid += grid
            else:
                off += kwh
                off_grid += grid
        total = peak + off
        grid_total = peak_grid + off_grid
        chargers[tag] = {
            "peak": round(peak, 2),
            "off_peak": round(off, 2),
            "total": round(total, 2),
            "peak_pct": round(peak / total * 100, 1) if total > 0 else 0.0,
            # grid-sourced only (solar/battery charge excluded)
            "peak_grid": round(peak_grid, 2),
            "off_peak_grid": round(off_grid, 2),
            "grid_total": round(grid_total, 2),
            "peak_grid_pct": (round(peak_grid / grid_total * 100, 1)
                              if grid_total > 0 else 0.0),
        }

    return {"range": range, "system": system, "timezone": tz.key,
            "tou": cfg, "chargers": chargers}


@app.get("/api/settings")
def get_settings():
    """Effective settings plus env defaults (for the reset affordance)."""
    return {"settings": SETTINGS.effective(), "defaults": settings_mod.env_defaults(),
            "home_url": config.HOME_URL}


@app.post("/api/settings")
def post_settings(patch: dict = Body(...)):
    """Partial update; persists to the settings volume and applies live."""
    try:
        return {"settings": SETTINGS.save(patch)}
    except settings_mod.ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        log.error("could not persist settings to %s: %s", SETTINGS.path, exc)
        raise HTTPException(
            status_code=500,
            detail="could not persist settings — is the ./data/webapp volume mounted?")


# HTML must always revalidate: the iOS home-screen web app otherwise keeps a
# stale index.html forever and never sees new ?v= cache-busters.
@app.middleware("http")
async def no_cache_html(request, call_next):
    response = await call_next(request)
    if "text/html" in response.headers.get("content-type", ""):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# Static SPA — mounted last so /api/* takes precedence.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=config.WEBAPP_PORT, log_level="info")
