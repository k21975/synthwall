"""
gen_dashboards.py — generate Grafana Power Flow dashboards for 1-3 gateways.

Builds one Combined dashboard plus one per-gateway dashboard from the
jasonacox Powerwall-Dashboard source JSON, adapted for this stack's schema.
Importable (used by tools/generate_stack.py) or runnable standalone:

    python3 tools/gen_dashboards.py            # 2 gateways, defaults
    python3 tools/gen_dashboards.py --count 3

Schema facts:
  - telegraf writes system=<tag> to raw.http → autogen.http (CQ).
  - A telegraf starlark processor splits from_grid/to_grid (site power) and
    from_pw/to_pw (battery power) into proper positive components.
  - enricher writes every gateway tag + combined to raw.enriched.
  - Live energy panels use raw.enriched (correct splits + combined).
  - Specialized CQs use SELECT mean(*) so fields gain a `mean_` prefix and the
    measurement name equals the policy: strings.strings, pod.pod, vitals.freq,
    pwtemps.pwtemps, fans.fans.
  - PW3 does NOT report: pwtemps, fans, string Inverter power → panels hidden.
"""
import argparse
import copy
import json
import re
from pathlib import Path

# ── Constants (rates/TZ are set per-run by generate()) ───────────────────────
RATE_IMPORT = 0.25
RATE_EXPORT = 0.08
TZ          = "America/Los_Angeles"
INFLUX_DS   = {"type": "influxdb", "uid": "P951FEA4DE68E13C5"}
SUN_DS      = {"type": "fetzerch-sunandmoon-datasource", "uid": "ffnmxduwuqvi8d"}
NAV_TAG     = "pwnav"

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = Path(__file__).resolve().parent / "jasonacox_dashboard_source.json"
DEFAULT_OUT = REPO_ROOT / "grafana" / "dashboards"

# Panels to delete entirely (empty on PW3, or replaced)
HIDE_PANEL_IDS = {
    34,   # Inverter Power — strings has no Inverter* fields on PW3
    86,   # Powerwall Temps and Fans — pwtemps/fans empty on PW3
}

# Specialized policies whose fields gained a mean_ prefix via SELECT mean(*)
MEAN_PREFIX_POLICIES = {"strings", "pod", "vitals", "pwtemps", "fans"}
# jasonacox (policy, measurement) → ours
MEAS_REMAP = {
    ("strings", "http"): ("strings", "strings"),
    ("pod",     "http"): ("pod",     "pod"),
    ("pwtemps", "http"): ("pwtemps", "pwtemps"),
    ("fans",    "http"): ("fans",    "fans"),
    ("vitals",  "http"): ("vitals",  "freq"),
}
VOLTAGE_FIELDS = {"solar_voltage": "solar", "home_voltage": "home",
                  "grid_voltage": "grid", "pw_voltage": "battery",
                  "battery_voltage": "battery"}
CURRENT_FIELDS = {"solar_current": "solar", "home_current": "home",
                  "grid_current": "grid", "pw_current": "battery",
                  "battery_current": "battery"}

GATEWAY_SPECIFIC = {"pod", "vitals", "strings", "pwtemps", "fans", "grid", "alerts"}

# ── SQL helpers ───────────────────────────────────────────────────────────────
SYSTEM_SQL_RE = re.compile(
    r'FROM\s+(?:"?autogen"?\."?http"?|autogen\.http'
    r'|"?kwh"?\."?http"?|kwh\.http'
    r'|"?daily"?\."?http"?|daily\.http'
    r'|"?raw"?\."?enriched"?|raw\.enriched)', re.IGNORECASE)
RAW_HTTP_RE = re.compile(r'FROM\s+"?raw"?\."?http"?', re.IGNORECASE)

def inject_system(sql, system):
    if f"system='{system}'" in sql:
        return sql
    return re.sub(r'\bWHERE\b', f"WHERE system='{system}' AND", sql, count=1,
                  flags=re.IGNORECASE)

def sql_remap_specialized(sql):
    """Remap pod/strings/vitals/pwtemps/fans measurement names + mean_ prefix."""
    for jp, jm in [("strings", "http"), ("pod", "http"), ("pwtemps", "http"),
                   ("fans", "http"), ("vitals", "http")]:
        op, om = MEAS_REMAP[(jp, jm)]
        sql = re.sub(rf'"{jp}"\."{jm}"', f'"{op}"."{om}"', sql)
        sql = re.sub(rf'\b{jp}\.{jm}\b', f'{op}.{om}', sql)
    # Prefix uppercase-leading quoted field names with mean_ (PW1_..., A_Voltage, ISLAND_...)
    sql = re.sub(r'"(?!mean_)([A-Z][A-Za-z0-9_]*)"', r'"mean_\1"', sql)
    return sql

# ── Target builders (raw.enriched-based live panels) ──────────────────────────
def ts(ref, alias, query, ds=None):
    return {"refId": ref, "alias": alias, "rawQuery": True,
            "datasource": ds or INFLUX_DS, "query": query,
            "resultFormat": "time_series"}

def enriched_ts(ref, alias, expr, system):
    return ts(ref, alias,
              f'SELECT {expr} AS "{alias}" FROM "raw"."enriched" '
              f"WHERE system='{system}' AND $timeFilter "
              f"GROUP BY time($__interval) fill(null)")

def enriched_kwh_stat(ref, alias, field, system, sign=1):
    s = "" if sign == 1 else " * -1"
    return ts(ref, alias,
              f'SELECT integral("{field}"){s} / 3600000.0 AS "{alias}" '
              f'FROM "raw"."enriched" WHERE system=\'{system}\' AND $timeFilter')

def panel2_targets(system):
    return [
        enriched_ts("A", "Home Usage",   'mean("home")', system),
        enriched_ts("B", "Solar Energy", 'mean("solar")', system),
        enriched_ts("C", "Powerwall",    'mean("from_pw") - mean("to_pw")', system),
        enriched_ts("D", "Grid Usage",   'mean("from_grid") - mean("to_grid")', system),
        enriched_ts("E", "Charge",       'mean("soc")', system),
        {"refId": "F", "datasource": SUN_DS, "target": "sun_altitude", "hide": False},
    ]

STAT_FIELD_MAP = {
    4: ("Home Usage", "home", 1),  5: ("Solar Energy", "solar", 1),
    8: ("To Powerwall", "to_pw", 1), 6: ("From Powerwall", "from_pw", 1),
    7: ("To Grid", "to_grid", 1),  9: ("From Grid", "from_grid", 1),
}

def power_flow_html(ports):
    """Build the animated Power Flow iframe HTML for 1-3 gateways. Black bg."""
    cells = "\n".join(
        f'  <div class="pwCell"><iframe class="powerFrame" data-port="{p}" '
        f'src="/public/img/grafana_icon.svg">IFRAME unsupported</iframe></div>'
        for p in ports)
    return f'''<style>
div[class*="-headerContainer"] {{ position: relative; }}
div[class*="-title"] {{ position: absolute; left: 50%; transform: translateX(-50%); z-index: 1; pointer-events: none; }}
div[class*="-title"] * {{ pointer-events: auto; }}
div[class*="-menuItem"] {{ margin-left: auto !important; }}
div[class*="-flexGrow"] {{ display: none; }}
</style>
<style>
div.pwWrap {{ width: 100%; height: 100%; display: flex; background: #000 !important; }}
div.pwCell {{ flex: 1; position: relative; overflow: hidden; background: #000; }}
iframe.powerFrame {{
  width: 450px; height: 350px; transform: scale(0); transform-origin: center;
  position: absolute; left: 50%; top: 50%;
  background-color: #000 !important; border: none !important; outline: none !important;
  will-change: transform; backface-visibility: hidden;
}}
</style>
<div class="pwWrap">
{cells}
</div>
<script>
function pwScale() {{
  document.querySelectorAll('.powerFrame').forEach(function(f) {{
    var p = f.parentNode;
    var r = Math.min(1, p.offsetWidth / f.offsetWidth, p.offsetHeight / f.offsetHeight);
    f.style.transform = 'translate(-50%, -50%) scale(' + r + ')';
  }});
}}
window.addEventListener('resize', pwScale, false);
document.querySelectorAll('.powerFrame').forEach(function(f) {{
  f.onload = function() {{ this.parentNode.style.background = '#000'; pwScale(); }};
  f.src = window.location.protocol + '//' + window.location.hostname + ':' + f.dataset.port + '/';
}});
</script>'''

# ── mean_ prefix for query-builder targets ───────────────────────────────────
def prefix_mean_qb(target):
    sel = target.get("select")
    if not sel:
        return
    for part in sel:
        for f in part:
            if f.get("type") == "field":
                fn = f.get("params", [""])[0]
                if fn and not fn.startswith("mean_"):
                    f["params"] = ["mean_" + fn]

def remap_voltage_current(target):
    if target.get("policy") != "autogen" or target.get("measurement") != "http":
        return
    sel = target.get("select", [[]])
    if not sel or not sel[0]:
        return
    fld = sel[0][0].get("params", [""])[0]
    if fld in VOLTAGE_FIELDS:
        target["measurement"] = "voltage"
        for part in sel:
            for f in part:
                if f.get("type") == "field":
                    f["params"] = [VOLTAGE_FIELDS.get(f["params"][0], f["params"][0])]
    elif fld in CURRENT_FIELDS:
        target["measurement"] = "current"
        for part in sel:
            for f in part:
                if f.get("type") == "field":
                    f["params"] = [CURRENT_FIELDS.get(f["params"][0], f["params"][0])]

def adapt_target(target, system):
    if not isinstance(target, dict):
        return
    if "target" in target:           # sun/moon style — handled per-panel
        target["hide"] = True
        return
    target["datasource"] = INFLUX_DS
    # weather measurement (autogen.weather) has NO system tag — never filter by system
    if target.get("measurement") == "weather":
        return
    policy = target.get("policy", "")

    new_pm = MEAS_REMAP.get((policy, target.get("measurement", "http")))
    if new_pm:
        target["policy"], target["measurement"] = new_pm
    remap_voltage_current(target)

    if target.get("rawQuery") is True and target.get("query", "").strip():
        sql = target["query"].strip()
        # Specialized measurement raw SQL → remap names + mean_ prefix
        if re.search(r'(pod|strings|pwtemps|fans|vitals)\.(http|pod|strings|pwtemps|fans|freq)|'
                     r'"(pod|strings|pwtemps|fans|vitals)"', sql, re.IGNORECASE):
            target["query"] = sql_remap_specialized(sql)
            return
        if RAW_HTTP_RE.search(sql):
            target["query"] = inject_system(
                re.sub(r'(FROM\s+)"?raw"?\."?http"?', r'\1"raw"."enriched"', sql,
                       flags=re.IGNORECASE), system)
        elif SYSTEM_SQL_RE.search(sql):
            target["query"] = inject_system(sql, system)
        return

    # Query-builder
    if policy in MEAN_PREFIX_POLICIES:
        prefix_mean_qb(target)
    if policy in GATEWAY_SPECIFIC:
        if system != "combined":
            tags = [t for t in target.get("tags", []) if t.get("key") != "system"]
            tags.insert(0, {"key": "system", "operator": "=", "value": system})
            target["tags"] = tags
        return
    tags = [t for t in target.get("tags", []) if t.get("key") != "system"]
    nt = {"key": "system", "operator": "=", "value": system}
    if tags:
        tags[0] = dict(tags[0]); tags[0].setdefault("condition", "AND")
        tags.insert(0, nt)
    else:
        tags = [nt]
    target["tags"] = tags

# ── Panel adapter ─────────────────────────────────────────────────────────────
def adapt_panel(panel, system, ports):
    pid   = panel.get("id")
    ptype = panel.get("type", "")
    panel["datasource"] = INFLUX_DS

    if ptype == "text":   # Power Flow animation
        panel["options"]["content"] = power_flow_html(ports)
        panel["transparent"] = True
        return

    if pid == 2:          # Energy Usage — Mixed (InfluxDB + Sun&Moon)
        panel["datasource"] = {"type": "datasource", "uid": "-- Mixed --"}
        panel["targets"] = panel2_targets(system)
        leg = panel.setdefault("options", {}).setdefault("legend", {})
        leg["displayMode"] = "table"; leg["calcs"] = ["max"]
        leg["placement"] = "bottom";  leg["showLegend"] = True
        return

    if pid in STAT_FIELD_MAP:
        title, field, sign = STAT_FIELD_MAP[pid]
        panel["targets"] = [enriched_kwh_stat("A", title, field, system, sign)]
        # Force always-kWh display (predefined kwatth auto-scales to Wh/MWh)
        fc = panel.setdefault("fieldConfig", {}).setdefault("defaults", {})
        fc["unit"] = "kWh"; fc["decimals"] = 1
        return

    if pid == 69:         # Net Grid Usage
        panel["targets"] = [ts("A", "Net Grid",
            f'SELECT (integral("from_grid") - integral("to_grid")) / 3600000.0 AS "Net Grid" '
            f'FROM "raw"."enriched" WHERE system=\'{system}\' AND $timeFilter')]
        fc = panel.setdefault("fieldConfig", {}).setdefault("defaults", {})
        fc["unit"] = "kWh"; fc["decimals"] = 1
        return

    if pid == 70:         # % Solar Powered → Net Solar (solar - home)
        panel["title"] = "Net Solar"
        panel["targets"] = [ts("A", "Net Solar",
            f'SELECT (integral("solar") - integral("home")) / 3600000.0 AS "Net Solar" '
            f'FROM "raw"."enriched" WHERE system=\'{system}\' AND $timeFilter')]
        fc = panel.setdefault("fieldConfig", {})
        fc["defaults"] = {
            "unit": "kWh", "decimals": 1,
            "color": {"mode": "thresholds"},
            "thresholds": {"mode": "absolute", "steps": [
                {"color": "blue", "value": None},    # net negative (more home) → matches Home Usage
                {"color": "yellow", "value": 0},      # net positive (more solar) → matches Solar Energy
            ]},
            "mappings": [],
        }
        fc["overrides"] = []
        opts = panel.setdefault("options", {})
        opts["colorMode"] = "value"
        opts["reduceOptions"] = {"calcs": ["lastNotNull"], "fields": "", "values": False}
        opts["textMode"] = "value"; opts["graphMode"] = "none"
        return

    if pid == 43:         # Self-Powered pie → raw.enriched
        b = f"system='{system}'"
        panel["targets"] = [
            ts("A", "Solar",
               f'SELECT integral("solar")/3600000 - integral("to_pw")/3600000 '
               f'- integral("to_grid")/3600000 FROM "raw"."enriched" WHERE {b} AND $timeFilter'),
            ts("B", "Battery",
               f'SELECT integral("from_pw")/3600000 FROM "raw"."enriched" WHERE {b} AND $timeFilter'),
            ts("C", "Grid",
               f'SELECT integral("from_grid")/3600000 FROM "raw"."enriched" WHERE {b} AND $timeFilter'),
        ]
        return

    if pid == 78:         # Savings (combined uses daily.http backfill)
        rp = "daily" if system == "combined" else "kwh"
        b, ri, rx = f"system='{system}'", str(RATE_IMPORT), str(RATE_EXPORT)
        panel["targets"] = [
            ts("A", "From Powerwall Savings",
               f'SELECT fp FROM (SELECT sum("from_pw")*{ri} AS fp FROM "{rp}"."http" WHERE {b} AND $timeFilter)'),
            ts("B", "Solar Savings",
               f'SELECT s-(tp+tg) FROM (SELECT sum("solar")*{ri} AS s, sum("to_pw")*{ri} AS tp, sum("to_grid")*{ri} AS tg FROM "{rp}"."http" WHERE {b} AND $timeFilter)'),
            ts("C", "Grid Export Revenue",
               f'SELECT tgg FROM (SELECT sum("to_grid")*{rx} AS tgg FROM "{rp}"."http" WHERE {b} AND $timeFilter)'),
            ts("D", "Total Savings",
               f'SELECT tgg+(s-tp-tg)+fp FROM (SELECT sum("to_pw")*{ri} AS tp, sum("solar")*{ri} AS s, sum("to_grid")*{ri} AS tg, sum("from_pw")*{ri} AS fp, sum("to_grid")*{rx} AS tgg FROM "{rp}"."http" WHERE {b} AND $timeFilter)'),
        ]
        return

    if pid == 93:         # Current State — round to 2 decimals in SQL, keep mappings
        flds = [("A", "Home", "home"), ("B", "Solar", "solar"),
                ("C", "Battery", "from_pw"), ("D", "Grid", "from_grid")]
        # Battery: from_pw - to_pw (signed); Grid: from_grid - to_grid (signed)
        exprs = {
            "home":     'last("home")',
            "solar":    'last("solar")',
            "from_pw":  'last("from_pw") - last("to_pw")',
            "from_grid":'last("from_grid") - last("to_grid")',
        }
        panel["targets"] = [
            ts(ref, alias,
               f'SELECT round(({exprs[f]}) / 10) / 100 AS "{alias}" FROM "raw"."enriched" '
               f"WHERE system='{system}' AND time >= now() - 10m")
            for ref, alias, f in flds
        ]
        return

    if pid == 72:         # Grid Power (replacement for Grid Status)
        panel["title"] = "Grid Power"
        panel["targets"] = [
            ts("A", "Grid Import (W)",
               f'SELECT mean("from_grid") AS "Grid Import (W)" FROM "raw"."enriched" '
               f"WHERE system='{system}' AND $timeFilter GROUP BY time($__interval) fill(null)"),
            ts("B", "Grid Export (W)",
               f'SELECT mean("to_grid") * -1 AS "Grid Export (W)" FROM "raw"."enriched" '
               f"WHERE system='{system}' AND $timeFilter GROUP BY time($__interval) fill(null)"),
        ]
        fc = panel.setdefault("fieldConfig", {}).setdefault("defaults", {})
        fc["unit"] = "watt"
        panel["fieldConfig"]["overrides"] = [
            {"matcher": {"id": "byName", "options": "Grid Import (W)"},
             "properties": [{"id": "color", "value": {"fixedColor": "purple", "mode": "fixed"}}]},
            {"matcher": {"id": "byName", "options": "Grid Export (W)"},
             "properties": [{"id": "color", "value": {"fixedColor": "#FCEE09", "mode": "fixed"}}]},
        ]
        return

    if pid == 88:         # Dashboard footer → Battery SOC %
        panel["title"] = "Battery Charge"
        panel["targets"] = [ts("A", "Battery Charge",
            f'SELECT last("soc") AS "Battery Charge" FROM "raw"."enriched" '
            f"WHERE system='{system}' AND time >= now() - 10m")]
        fc = panel.setdefault("fieldConfig", {}).setdefault("defaults", {})
        fc["unit"] = "percent"; fc["decimals"] = 1
        opts = panel.setdefault("options", {})
        opts["reduceOptions"] = {"calcs": ["lastNotNull"], "fields": "", "values": False}
        opts["colorMode"] = "value"; opts["textMode"] = "value_and_name"
        return

    for t in panel.get("targets", []):
        adapt_target(t, system)
        if system == "combined" and t.get("policy") == "kwh" and t.get("measurement") == "http":
            t["policy"] = "daily"   # combined has no kwh.http; daily has backfill

    for sub in panel.get("panels", []):
        adapt_panel(sub, system, ports)

def prune_hidden(panels):
    out = []
    for p in panels:
        if p.get("id") in HIDE_PANEL_IDS:
            continue
        if p.get("panels"):
            p["panels"] = prune_hidden(p["panels"])
        out.append(p)
    return out

# ── Dashboard builder ─────────────────────────────────────────────────────────
def make_dashboard(src, v):
    system, uid, title, ports = v["system"], v["uid"], v["title"], v["ports"]
    d = copy.deepcopy(src)
    d.update({"title": title, "uid": uid,
              "description": f"Powerwall Power Flow — {system}",
              "version": 1, "tags": ["powerwall", "power-flow", NAV_TAG, system]})
    for k in ("__inputs", "__elements", "__requires"):
        d.pop(k, None)

    # Dashboard switcher dropdown (Combined / per-gateway / Historical)
    d["links"] = [{
        "asDropdown": True, "title": "Dashboards", "type": "dashboards",
        "tags": [NAV_TAG], "includeVars": False, "keepTime": True,
        "targetBlank": False, "icon": "external link", "tooltip": "",
        "url": ""
    }]

    d["templating"] = {"list": [
        {"name": "avg_buy_per_kwh", "type": "constant", "label": "Buy $/kWh",
         "query": str(RATE_IMPORT),
         "current": {"text": str(RATE_IMPORT), "value": str(RATE_IMPORT)}, "hide": 2},
        {"name": "avg_sell_per_kwh", "type": "constant", "label": "Sell $/kWh",
         "query": str(RATE_EXPORT),
         "current": {"text": str(RATE_EXPORT), "value": str(RATE_EXPORT)}, "hide": 2},
        {"name": "tz", "type": "constant", "label": "Timezone",
         "query": TZ, "current": {"text": TZ, "value": TZ}, "hide": 2},
    ]}

    d["panels"] = prune_hidden(d.get("panels", []))
    for panel in d["panels"]:
        adapt_panel(panel, system, ports)
    return d


def variants_for(gateways):
    """Dashboard variants: Combined always; per-gateway ones only when N > 1
    (for a single gateway, combined IS that gateway — no point duplicating)."""
    all_ports = [g["port"] for g in gateways]
    out = [{"system": "combined", "uid": "pw-flow-combined",
            "title": "Powerwall — Combined Power Flow", "ports": all_ports,
            "file": "06-power-flow-combined.json"}]
    if len(gateways) > 1:
        for i, g in enumerate(gateways):
            slug = g["tag"].replace("_", "-")
            out.append({
                "system": g["tag"],
                "uid": f"pw-flow-{slug}",
                "title": f"Powerwall — {g['label']} Power Flow",
                "ports": [g["port"]],
                "file": f"{7 + i:02d}-power-flow-{slug}.json",
            })
    return out


def generate(gateways, rate_import, rate_export, tz,
             src_path=DEFAULT_SRC, out_dir=DEFAULT_OUT):
    """Write dashboards for the given gateways; returns written file paths.

    gateways: [{"tag": "panel_a", "label": "Panel A", "port": 8675}, ...]
    """
    global RATE_IMPORT, RATE_EXPORT, TZ
    RATE_IMPORT, RATE_EXPORT, TZ = rate_import, rate_export, tz

    with open(src_path) as f:
        src = json.load(f)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for v in variants_for(gateways):
        dash = make_dashboard(src, v)
        path = out_dir / v["file"]
        with open(path, "w") as f:
            json.dump(dash, f, indent=2)
        written.append(path)
        print(f"Wrote {path.name}  ({len(json.dumps(dash)) // 1024}KB)")
    return written


def default_gateways(count):
    return [{"tag": f"panel_{chr(ord('a') + i)}",
             "label": f"Panel {chr(ord('A') + i)}",
             "port": 8675 + i} for i in range(count)]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, default=2, choices=(1, 2, 3))
    ap.add_argument("--rate-import", type=float, default=RATE_IMPORT)
    ap.add_argument("--rate-export", type=float, default=RATE_EXPORT)
    ap.add_argument("--tz", default=TZ)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()
    generate(default_gateways(args.count), args.rate_import, args.rate_export,
             args.tz, out_dir=args.out)
    print("Done.")
