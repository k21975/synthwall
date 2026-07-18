"""settings.py — display + billing settings, persisted over env defaults.

Layering: env vars (config.py) provide the defaults; a JSON file on a mounted
volume (SETTINGS_PATH) holds edits made from the settings page and wins over
env. The gateway tag set itself is infrastructure (GATEWAY_TAGS env, written
by setup.sh to match the generated telegraf/compose configs) — the page can
rename labels and adjust capacities but never add or remove a gateway.

The enricher reads the same JSON file (read-only mount) for rates/capacities,
so both services stay consistent without any API coupling.
"""

import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import config

log = logging.getLogger("webapp.settings")

SCHEMA_VERSION = 1

_MAX_SITE_NAME = 32
_MAX_TAGLINE = 64
_MAX_LABEL = 24
_MAX_RATE = 10.0       # $/kWh sanity ceiling
_MAX_CAPACITY = 1000.0  # kWh sanity ceiling


class ValidationError(ValueError):
    """A settings patch failed validation; message is user-facing."""


def _default_label(tag: str, index: int) -> str:
    # panel_a -> "PANEL A"; unrecognized tag shapes get a positional label.
    if tag.startswith("panel_") and len(tag) == len("panel_a"):
        return f"PANEL {tag[-1].upper()}"
    return f"PANEL {index + 1}"


def _default_tagline(n: int) -> str:
    return {1: "POWERWALL GATEWAY",
            2: "DUAL GATEWAY // POWERWALL",
            3: "TRIPLE GATEWAY // POWERWALL"}.get(n, "POWERWALL")


def env_defaults() -> dict:
    """Settings as they stand with no saved file — pure env/config defaults."""
    return {
        "version": SCHEMA_VERSION,
        "site_name": config.SITE_NAME,
        "tagline": config.SITE_TAGLINE or _default_tagline(len(config.GATEWAY_TAGS)),
        "timezone": config.TZ,
        # how the UI renders times: viewer's browser TZ or the house TZ above
        "time_display": "local",
        # day of month (1-28) the electric-company billing cycle starts
        "billing_day": 1,
        "rate_import": config.RATE_IMPORT,
        "rate_export": config.RATE_EXPORT,
        "gateways": [
            {"tag": tag,
             "label": _default_label(tag, i),
             "charger_label": f"CHARGER {i + 1}",
             "capacity_kwh": config.GATEWAY_CAPACITIES[i]}
            for i, tag in enumerate(config.GATEWAY_TAGS)
        ],
    }


def _require_str(patch: dict, key: str, max_len: int, allow_empty: bool = False) -> str:
    v = patch[key]
    if not isinstance(v, str):
        raise ValidationError(f"{key} must be a string")
    v = v.strip()
    if not v and not allow_empty:
        raise ValidationError(f"{key} cannot be empty")
    if len(v) > max_len:
        raise ValidationError(f"{key} must be at most {max_len} characters")
    return v


def _require_num(value, key: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{key} must be a number")
    v = float(value)
    if not (lo <= v <= hi):
        raise ValidationError(f"{key} must be between {lo} and {hi}")
    return v


def _validate_patch(patch: dict, current: dict) -> dict:
    """Validate a partial update against the current settings; return the merged dict."""
    if not isinstance(patch, dict):
        raise ValidationError("settings payload must be an object")

    allowed = {"site_name", "tagline", "timezone", "time_display", "billing_day",
               "rate_import", "rate_export", "gateways"}
    unknown = set(patch) - allowed
    if unknown:
        raise ValidationError(f"unknown settings: {', '.join(sorted(unknown))}")

    merged = json.loads(json.dumps(current))  # deep copy

    if "site_name" in patch:
        merged["site_name"] = _require_str(patch, "site_name", _MAX_SITE_NAME)
    if "tagline" in patch:
        merged["tagline"] = _require_str(patch, "tagline", _MAX_TAGLINE, allow_empty=True)
    if "timezone" in patch:
        tz = _require_str(patch, "timezone", 64)
        try:
            ZoneInfo(tz)
        except Exception:
            raise ValidationError(f"unknown timezone '{tz}'")
        merged["timezone"] = tz
    if "time_display" in patch:
        if patch["time_display"] not in ("local", "house"):
            raise ValidationError("time_display must be 'local' or 'house'")
        merged["time_display"] = patch["time_display"]
    if "billing_day" in patch:
        v = patch["billing_day"]
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 28:
            raise ValidationError("billing_day must be a whole number from 1 to 28")
        merged["billing_day"] = v
    if "rate_import" in patch:
        merged["rate_import"] = _require_num(patch["rate_import"], "rate_import", 0.0, _MAX_RATE)
    if "rate_export" in patch:
        merged["rate_export"] = _require_num(patch["rate_export"], "rate_export", 0.0, _MAX_RATE)

    if "gateways" in patch:
        if not isinstance(patch["gateways"], list):
            raise ValidationError("gateways must be a list")
        by_tag = {g["tag"]: g for g in merged["gateways"]}
        for item in patch["gateways"]:
            if not isinstance(item, dict) or "tag" not in item:
                raise ValidationError("each gateway entry needs a tag")
            tag = item["tag"]
            if tag not in by_tag:
                raise ValidationError(
                    f"unknown gateway '{tag}' — gateways are fixed by the "
                    f"installation (GATEWAY_TAGS); only labels and capacities "
                    f"can be edited here")
            extra = set(item) - {"tag", "label", "charger_label", "capacity_kwh"}
            if extra:
                raise ValidationError(f"unknown gateway fields: {', '.join(sorted(extra))}")
            if "label" in item:
                by_tag[tag]["label"] = _require_str(item, "label", _MAX_LABEL)
            if "charger_label" in item:
                by_tag[tag]["charger_label"] = _require_str(item, "charger_label", _MAX_LABEL)
            if "capacity_kwh" in item:
                by_tag[tag]["capacity_kwh"] = _require_num(
                    item["capacity_kwh"], "capacity_kwh", 0.1, _MAX_CAPACITY)

    merged["version"] = SCHEMA_VERSION
    return merged


class Settings:
    """Effective settings = saved file merged over env defaults, thread-safe."""

    def __init__(self, path: Optional[str] = None):
        self.path = Path(path or config.SETTINGS_PATH)
        self._lock = threading.Lock()
        self._data: dict = {}
        self._tz: ZoneInfo = ZoneInfo("UTC")
        self.reload()

    def reload(self) -> None:
        defaults = env_defaults()
        saved = {}
        try:
            saved = json.loads(self.path.read_text())
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable settings file %s: %s", self.path, exc)
        if not isinstance(saved, dict):
            saved = {}

        merged = defaults
        for key in ("site_name", "tagline", "timezone", "time_display",
                    "billing_day", "rate_import", "rate_export"):
            if key in saved:
                merged[key] = saved[key]
        # Per-gateway overrides apply by tag; tags no longer configured are
        # dropped (a stale file must not resurrect a removed gateway).
        saved_gw = {g.get("tag"): g for g in saved.get("gateways", [])
                    if isinstance(g, dict)}
        for gw in merged["gateways"]:
            override = saved_gw.get(gw["tag"], {})
            for key in ("label", "charger_label", "capacity_kwh"):
                if key in override:
                    gw[key] = override[key]

        try:
            tz = ZoneInfo(str(merged["timezone"]))
        except Exception:
            log.warning("invalid timezone %r in settings; falling back to %s",
                        merged["timezone"], config.TZ)
            merged["timezone"] = config.TZ
            tz = ZoneInfo(config.TZ)

        with self._lock:
            self._data = merged
            self._tz = tz

    def effective(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def save(self, patch: dict) -> dict:
        """Validate + merge a partial update, persist atomically, return effective."""
        with self._lock:
            merged = _validate_patch(patch, self._data)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(merged, indent=2) + "\n")
            os.replace(tmp, self.path)
            self._data = merged
            self._tz = ZoneInfo(merged["timezone"])
        log.info("settings saved: %s", ", ".join(sorted(patch)))
        return self.effective()

    # ---- accessors used by app.py ----

    def systems(self) -> tuple:
        with self._lock:
            return ("combined", *(g["tag"] for g in self._data["gateways"]))

    def tz(self) -> ZoneInfo:
        with self._lock:
            return self._tz

    def rate_import(self) -> float:
        with self._lock:
            return float(self._data["rate_import"])

    def rate_export(self) -> float:
        with self._lock:
            return float(self._data["rate_export"])

    def billing_day(self) -> int:
        with self._lock:
            day = self._data.get("billing_day", 1)
            return day if isinstance(day, int) and 1 <= day <= 28 else 1
