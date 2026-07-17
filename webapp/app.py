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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import requests.exceptions
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from influxdb import InfluxDBClient
from influxdb.exceptions import InfluxDBClientError, InfluxDBServerError

import config
import settings as settings_mod

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("webapp")

app = FastAPI(title="Powerwall Dashboard", docs_url=None, redoc_url=None)

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

UTC = ZoneInfo("UTC")

# range token -> (kind, key, GROUP BY interval).
#   rolling:  trailing window ending now.
#   todate:   from local start-of-period to now (settings-page timezone).
#   billing:  from the most recent billing-cycle start day (settings) to now.
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
        day = SETTINGS.billing_day()
        s = now.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
        if s > now:  # cycle-start day hasn't arrived yet this month
            s = (s - timedelta(days=day + 1)).replace(day=day)
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


@app.get("/api/history")
def history(
    range: str = Query("24h"),
    system: str = Query("combined"),
    start: Optional[int] = Query(None),
    end: Optional[int] = Query(None),
):
    """Down-sampled power + SOC series from autogen.enriched (INF retention)."""
    _validate_system(system)
    predicate, interval, start_ms = _range_bounds(range, start, end)

    sel = ", ".join(f"mean({f}) AS {f}" for f in (*POWER_FIELDS, "soc"))
    q = (
        f"SELECT {sel} FROM \"autogen\".\"enriched\" "
        f"WHERE system = '{system}' AND {predicate} "
        f"GROUP BY time({interval}) fill(none)"
    )
    result = _query(q)
    points = list(result.get_points())

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
    """Energy totals (kWh) over the period via integral on autogen.enriched."""
    _validate_system(system)
    predicate, _, start_ms = _range_bounds(range, start, end)

    # One query grouped by system: the requested system's totals plus the
    # per-gateway breakdown that feeds the tile split panels.
    # integral(W, 1s) = W·s = J ; /3.6e6 = kWh
    sel = ", ".join(f"integral({f}, 1s) / 3600000 AS {f}" for f in POWER_FIELDS)
    q = (
        f"SELECT {sel} FROM \"autogen\".\"enriched\" "
        f"WHERE {predicate} GROUP BY system"
    )
    known = SETTINGS.systems()
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


@app.get("/api/settings")
def get_settings():
    """Effective settings plus env defaults (for the reset affordance)."""
    return {"settings": SETTINGS.effective(), "defaults": settings_mod.env_defaults()}


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
