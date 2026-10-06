"""config.py — webapp configuration from environment variables.

Mirrors enricher/config.py so both services read the same env conventions.
"""

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

# Electricity rates ($/kWh) — used for period savings on the totals endpoint.
# These are env defaults; the settings page (settings.py) can override them live.
RATE_IMPORT = _float("RATE_IMPORT", 0.25)
RATE_EXPORT = _float("RATE_EXPORT", 0.08)
# Fixed monthly charge ($) added to the estimated bill (utility customer charge).
FIXED_CHARGE = _float("FIXED_CHARGE", 10.0)

# Gateway system tags in display order. Infrastructure-owned (written by
# setup.sh to match the generated telegraf/compose configs) — the settings
# page maps display labels onto these but can never add or remove one.
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
TOTAL_CAPACITY_KWH = sum(GATEWAY_CAPACITIES)

# Site identity shown in the webapp header; editable live from the settings page.
SITE_NAME = os.environ.get("SITE_NAME", "POWERWALL")
SITE_TAGLINE = os.environ.get("SITE_TAGLINE", "")

# Timezone for "to date" range boundaries; editable live from the settings page.
TZ = os.environ.get("TZ", "America/Los_Angeles")

# Where user-edited settings persist (mounted volume in compose).
SETTINGS_PATH = os.environ.get("SETTINGS_PATH", "/data/settings.json")

# Port the uvicorn server listens on inside the container.
WEBAPP_PORT = _int("WEBAPP_PORT", 8050)

# Origins whose pages may read /api/* from a browser (CORS), comma-separated,
# e.g. "https://home.example.com" for a separate home dashboard page. Empty =
# no cross-origin access, the default.
DASHBOARD_ORIGINS = [o.strip().rstrip("/") for o in
                     os.environ.get("DASHBOARD_ORIGINS", "").split(",")
                     if o.strip()]
# Where the header's HOME button links: the first origin above. Empty = no button.
HOME_URL = DASHBOARD_ORIGINS[0] if DASHBOARD_ORIGINS else ""
