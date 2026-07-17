#!/usr/bin/env python3
"""
enricher.py — Powerwall Dashboard derived metrics service.

Reads per-system raw data from InfluxDB (one stream per configured gateway,
see GATEWAY_TAGS), computes:
  - self_sufficiency %
  - grid_dependency %
  - ev_load (W) — estimated EV charging power
  - savings ($/interval) — based on configurable rates
  - combined metrics (sum / capacity-weighted SOC over reporting gateways)

Writes results back to InfluxDB with the per-gateway system tags + "combined".
Runs every ENRICHER_INTERVAL seconds with exponential backoff on failures.

Rates and capacities come from env defaults, overridden live by the webapp
settings file (read-only mount) when it changes.
"""

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from influxdb import InfluxDBClient
from influxdb.exceptions import InfluxDBClientError, InfluxDBServerError

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("enricher")

_shutdown = False


def handle_signal(signum, frame):
    global _shutdown
    log.info("Shutdown signal received")
    _shutdown = True


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


def health_check():
    """Called by Docker healthcheck — raises if InfluxDB is unreachable."""
    client = InfluxDBClient(
        host=config.INFLUXDB_HOST,
        port=config.INFLUXDB_PORT,
        database=config.INFLUXDB_DATABASE,
        timeout=5,
    )
    client.ping()
    client.close()


class LiveSettings:
    """Rates + per-gateway capacities, refreshed from the webapp settings
    file when its mtime changes. Env values are the fallback."""

    def __init__(self):
        self.path = Path(config.SETTINGS_PATH)
        self._mtime = None
        self.rate_import = config.RATE_IMPORT
        self.rate_export = config.RATE_EXPORT
        self.capacities = dict(zip(config.GATEWAY_TAGS, config.GATEWAY_CAPACITIES))

    def refresh(self):
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return  # no settings file (volume absent or nothing saved yet)
        if mtime == self._mtime:
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable settings file %s: %s", self.path, exc)
            return
        self._mtime = mtime
        try:
            self.rate_import = float(data.get("rate_import", config.RATE_IMPORT))
            self.rate_export = float(data.get("rate_export", config.RATE_EXPORT))
            for gw in data.get("gateways", []):
                tag = gw.get("tag")
                if tag in self.capacities and gw.get("capacity_kwh"):
                    self.capacities[tag] = float(gw["capacity_kwh"])
        except (TypeError, ValueError) as exc:
            log.warning("bad values in settings file %s: %s", self.path, exc)
            return
        log.info("settings reloaded: import=$%.4f export=$%.4f capacities=%s",
                 self.rate_import, self.rate_export, self.capacities)


SETTINGS = LiveSettings()


def _query_latest(client: InfluxDBClient, system: str) -> Optional[dict]:
    """Return the most recent 1-minute-mean row for the given system tag."""
    q = (
        f"SELECT last(home) AS home, last(solar) AS solar, "
        f"last(from_pw) AS from_pw, last(to_pw) AS to_pw, "
        f"last(from_grid) AS from_grid, last(to_grid) AS to_grid, "
        f"last(percentage) AS percentage "
        f"FROM \"autogen\".\"http\" "
        f"WHERE system = '{system}' "
        f"AND time > now() - 5m"
    )
    try:
        result = client.query(q)
        points = list(result.get_points())
        return points[0] if points else None
    except (InfluxDBClientError, InfluxDBServerError) as exc:
        log.warning("Query failed for system=%s: %s", system, exc)
        return None


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _compute_panel(data: dict, system: str, capacity_kwh: float) -> list[dict]:
    """Compute derived metrics for a single panel and return InfluxDB point dicts."""
    home = data.get("home") or 0.0
    solar = data.get("solar") or 0.0
    # telegraf starlark processor already split these into positive components
    from_pw = max(0.0, data.get("from_pw") or 0.0)      # discharge (power from Powerwall)
    to_pw = max(0.0, data.get("to_pw") or 0.0)          # charge (power to Powerwall)
    from_grid = max(0.0, data.get("from_grid") or 0.0)  # import
    to_grid = max(0.0, data.get("to_grid") or 0.0)      # export
    soc = _clamp(data.get("percentage") or 0.0, 0.0, 100.0)

    clean_supply = solar + from_pw
    self_sufficiency = _clamp((clean_supply / home * 100) if home > 0 else 100.0)
    grid_dependency = 100.0 - self_sufficiency

    ev_load = max(0.0, home - config.BASELINE_HOME_LOAD_W) if home > config.EV_DETECTION_THRESHOLD_W else 0.0

    interval_h = config.ENRICHER_INTERVAL / 3600.0
    savings = (to_grid * SETTINGS.rate_export - from_grid * SETTINGS.rate_import) * interval_h

    soc_kwh = soc / 100.0 * capacity_kwh

    fields = {
        "self_sufficiency": self_sufficiency,
        "grid_dependency": grid_dependency,
        "ev_load": ev_load,
        "savings": savings,
        "soc_kwh": soc_kwh,
        "home": home,
        "solar": solar,
        "from_pw": from_pw,
        "to_pw": to_pw,
        "from_grid": from_grid,
        "to_grid": to_grid,
        "soc": soc,
    }

    return [{
        "measurement": "enriched",
        "tags": {"system": system},
        "time": datetime.now(timezone.utc).isoformat(),
        "fields": fields,
    }]


def _compute_combined(panels: list[tuple[str, dict]]) -> list[dict]:
    """Sum reporting gateways into the combined stream (capacity-weighted SOC).

    `panels` is [(system_tag, latest_row)] for whichever gateways reported;
    works identically for 1, 2, or 3 gateways.
    """
    def total(k, positive=False):
        vals = ((d.get(k) or 0.0) for _, d in panels)
        return sum(max(0.0, v) for v in vals) if positive else sum(vals)

    home = total("home")
    solar = total("solar")
    # telegraf starlark processor already split these into positive components
    from_pw = total("from_pw", positive=True)
    to_pw = total("to_pw", positive=True)
    from_grid = total("from_grid", positive=True)
    to_grid = total("to_grid", positive=True)

    total_cap = sum(SETTINGS.capacities.get(tag, 13.5) for tag, _ in panels)
    soc_kwh_total = sum(
        _clamp(d.get("percentage") or 0.0, 0.0, 100.0) / 100.0
        * SETTINGS.capacities.get(tag, 13.5)
        for tag, d in panels
    )
    soc_weighted = (soc_kwh_total / total_cap * 100.0) if total_cap > 0 else 0.0

    clean_supply = solar + from_pw
    self_sufficiency = _clamp((clean_supply / home * 100) if home > 0 else 100.0)
    grid_dependency = 100.0 - self_sufficiency

    ev_load = sum(
        max(0.0, (d.get("home") or 0.0) - config.BASELINE_HOME_LOAD_W)
        for _, d in panels
    )

    interval_h = config.ENRICHER_INTERVAL / 3600.0
    savings = (to_grid * SETTINGS.rate_export - from_grid * SETTINGS.rate_import) * interval_h

    fields = {
        "self_sufficiency": self_sufficiency,
        "grid_dependency": grid_dependency,
        "ev_load": ev_load,
        "savings": savings,
        "soc_kwh": soc_kwh_total,
        "soc": soc_weighted,
        "home": home,
        "solar": solar,
        "from_pw": from_pw,
        "to_pw": to_pw,
        "from_grid": from_grid,
        "to_grid": to_grid,
        "total_capacity_kwh": total_cap,
    }

    return [{
        "measurement": "enriched",
        "tags": {"system": "combined"},
        "time": datetime.now(timezone.utc).isoformat(),
        "fields": fields,
    }]


def run_once(client: InfluxDBClient) -> bool:
    """Execute one enrichment cycle. Returns True if any data was written."""
    SETTINGS.refresh()

    points = []
    reporting: list[tuple[str, dict]] = []

    for tag in config.GATEWAY_TAGS:
        data = _query_latest(client, tag)
        if data:
            points.extend(_compute_panel(data, tag, SETTINGS.capacities.get(tag, 13.5)))
            reporting.append((tag, data))
            log.debug("%s: home=%.0fW solar=%.0fW soc=%.1f%%", tag,
                      data.get("home", 0), data.get("solar", 0), data.get("percentage", 0))
        else:
            log.warning("No recent data for %s — skipping %s enrichment", tag, tag)

    if reporting:
        points.extend(_compute_combined(reporting))
        if len(reporting) < len(config.GATEWAY_TAGS):
            log.info("combined uses %d of %d gateways this cycle",
                     len(reporting), len(config.GATEWAY_TAGS))

    if points:
        try:
            client.write_points(points, retention_policy="raw", time_precision="s")
            log.info("Wrote %d enriched points", len(points))
            return True
        except (InfluxDBClientError, InfluxDBServerError) as exc:
            log.error("Failed to write enriched points: %s", exc)
            return False

    return False


def main():
    log.info("Enricher starting (interval=%ds, gateways=%s)",
             config.ENRICHER_INTERVAL, ",".join(config.GATEWAY_TAGS))
    log.info("Rates: import=$%.4f/kWh export=$%.4f/kWh", SETTINGS.rate_import, SETTINGS.rate_export)
    log.info("Capacity: %s", " ".join(f"{t}={c:.1f}kWh" for t, c in SETTINGS.capacities.items()))

    backoff = 5
    client = None

    while not _shutdown:
        try:
            if client is None:
                client = InfluxDBClient(
                    host=config.INFLUXDB_HOST,
                    port=config.INFLUXDB_PORT,
                    database=config.INFLUXDB_DATABASE,
                    timeout=10,
                )
                client.ping()
                log.info("Connected to InfluxDB at %s:%d", config.INFLUXDB_HOST, config.INFLUXDB_PORT)
                backoff = 5

            run_once(client)
            backoff = 5

        except Exception as exc:
            log.error("Enricher error: %s — retrying in %ds", exc, backoff)
            if client:
                try:
                    client.close()
                except Exception:
                    pass
                client = None
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
            continue

        # Sleep in small increments so shutdown signal is responsive
        for _ in range(config.ENRICHER_INTERVAL):
            if _shutdown:
                break
            time.sleep(1)

    if client:
        client.close()
    log.info("Enricher stopped")


if __name__ == "__main__":
    main()
