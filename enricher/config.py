"""config.py — enricher configuration from environment variables."""

import os


def _float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


def _int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


INFLUXDB_HOST = os.environ.get("INFLUXDB_HOST", "influxdb")
INFLUXDB_PORT = _int("INFLUXDB_PORT", 8086)
INFLUXDB_DATABASE = os.environ.get("INFLUXDB_DATABASE", "powerwall")

RATE_IMPORT = _float("RATE_IMPORT", 0.25)
RATE_EXPORT = _float("RATE_EXPORT", 0.08)

# Gateway system tags in order (set by setup.sh to match telegraf/compose).
GATEWAY_TAGS = [t.strip() for t in
                os.environ.get("GATEWAY_TAGS", "panel_a,panel_b").split(",")
                if t.strip()]

# Usable battery capacity (kWh) behind each gateway, positional A/B/C.
PW_A_CAPACITY_KWH = _float("PW_A_CAPACITY_KWH", 13.5)
PW_B_CAPACITY_KWH = _float("PW_B_CAPACITY_KWH", 13.5)
PW_C_CAPACITY_KWH = _float("PW_C_CAPACITY_KWH", 13.5)
_CAPS = (PW_A_CAPACITY_KWH, PW_B_CAPACITY_KWH, PW_C_CAPACITY_KWH)
GATEWAY_CAPACITIES = [_CAPS[i] if i < len(_CAPS) else 13.5
                      for i in range(len(GATEWAY_TAGS))]

# Settings file written by the webapp settings page (read-only mount);
# overrides rates/capacities live when present.
SETTINGS_PATH = os.environ.get("SETTINGS_PATH", "/data/settings.json")

ENRICHER_INTERVAL = _int("ENRICHER_INTERVAL", 60)

# Watts below which we don't try to attribute load to EV charging.
# A home with no EVs charging might consume 300-800W baseline; adjust to match yours.
EV_DETECTION_THRESHOLD_W = _float("EV_DETECTION_THRESHOLD_W", 1200.0)

# Estimated baseline non-EV home load (W). EV load = home - baseline when above threshold.
# Set this to your typical idle consumption (HVAC off, no EV, lights only).
BASELINE_HOME_LOAD_W = _float("BASELINE_HOME_LOAD_W", 800.0)
