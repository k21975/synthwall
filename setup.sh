#!/usr/bin/env bash
# setup.sh — Powerwall Dashboard setup (1-3 Tesla gateways)
# Mirrors jasonacox/Powerwall-Dashboard UX. Run this once on a fresh clone.
#
# Usage: ./setup.sh [options]
#   (no flags)  Full first-time setup — asks how many gateways you have,
#               creates .env files, generates the stack configs, checks
#               gateway connectivity, initializes InfluxDB, starts services.
#   -g          Regenerate stack configs (powerwall.yml, telegraf.conf,
#               Grafana dashboards) from gateways.json — after editing it
#   -i          (Re)initialize InfluxDB schema only
#   -u          Update — pull latest images, rebuild local images, restart
#   -t          TEDAPI test — run pypowerwall.tedapi against each configured
#               gateway to confirm live data before the full stack starts
#   -h          Show this help

set -euo pipefail

##############################################################################
# Helpers
##############################################################################

RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
step()    { echo -e "${CYAN}[STEP]${NC}  $*"; }
die()     { error "$*"; exit 1; }

require_cmd() { command -v "$1" &>/dev/null || die "Required command not found: $1"; }

# Run a python script with host python3, falling back to a dockerized python.
run_python() {
    if command -v python3 &>/dev/null; then
        python3 "$@"
    else
        docker run --rm -v "$SCRIPT_DIR":/work -w /work python:3.12-alpine python3 "$@"
    fi
}

LETTERS=(a b c)

##############################################################################
# Pre-flight
##############################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

INIT_DB=false; UPDATE=false; TEDAPI_TEST=false; REGEN=false

while getopts "gituh" opt; do
    case $opt in
        g) REGEN=true ;;
        i) INIT_DB=true ;;
        u) UPDATE=true ;;
        t) TEDAPI_TEST=true ;;
        h) sed -n 's/^# //p' "$0" | grep -A20 "^Usage:"; exit 0 ;;
        *) die "Unknown option: $opt. Run $0 -h for help." ;;
    esac
done

COMPOSE_CMD="docker compose"
if ! $REGEN; then
    # every mode except -g drives docker; -g only needs python3 (or docker as
    # a python fallback), so it works on a workstation too
    require_cmd docker
    require_cmd curl
    if ! docker compose version &>/dev/null 2>&1; then
        require_cmd docker-compose
        COMPOSE_CMD="docker-compose"
    fi
elif ! command -v python3 &>/dev/null && ! command -v docker &>/dev/null; then
    die "-g needs python3 or docker"
fi

##############################################################################
# Regenerate stack configs from gateways.json
##############################################################################

if $REGEN; then
    [[ -f gateways.json ]] || die "gateways.json not found — run ./setup.sh first"
    step "Regenerating powerwall.yml, telegraf.conf, and Grafana dashboards..."
    run_python tools/generate_stack.py
    info "Configs regenerated. Apply with:"
    info "  $COMPOSE_CMD -f powerwall.yml up -d"
    exit 0
fi

##############################################################################
# Update mode
##############################################################################

if $UPDATE; then
    info "Pulling latest images..."
    $COMPOSE_CMD -f powerwall.yml pull
    info "Rebuilding enricher + webapp..."
    $COMPOSE_CMD -f powerwall.yml build enricher webapp
    info "Restarting services..."
    $COMPOSE_CMD -f powerwall.yml up -d
    info "Update complete."
    exit 0
fi

##############################################################################
# InfluxDB schema re-init only
##############################################################################

if $INIT_DB; then
    info "Re-initializing InfluxDB schema..."
    docker exec influxdb influx -import -path=/var/lib/influxdb/influxdb.sql
    info "Schema initialized."
    exit 0
fi

##############################################################################
# TEDAPI connectivity / authentication test
##############################################################################

if $TEDAPI_TEST; then
    echo ""
    step "Running TEDAPI connectivity test against configured gateways..."
    FOUND=false
    for L in "${LETTERS[@]}"; do
        ENVF="pypowerwall-${L}.env"
        [[ -f "$ENVF" ]] || continue
        FOUND=true
        HOST=$(grep '^PW_HOST=' "$ENVF" | cut -d= -f2)
        GWD=$(grep '^PW_GW_PWD=' "$ENVF" | cut -d= -f2)
        LBL=$(grep '^PW_LABEL=' "$ENVF" | cut -d= -f2)
        LBL="${LBL:-panel_$L}"
        [[ -z "$HOST" ]] && { warn "$LBL: PW_HOST not set in $ENVF — skipping"; continue; }

        step "$LBL — connectivity: curl -k --head https://${HOST}"
        if curl -sk --head --max-time 5 "https://${HOST}" -o /dev/null -w "HTTP %{http_code}\n"; then
            info "$LBL gateway at ${HOST} is reachable."
        else
            error "$LBL gateway at ${HOST} is NOT reachable. Check IP, cable, and firewall."
            continue
        fi

        step "$LBL — TEDAPI auth test (python -m pypowerwall.tedapi)"
        docker run --rm \
            -e PW_HOST="${HOST}" \
            -e PW_GW_PWD="${GWD}" \
            jasonacox/pypowerwall:latest \
            python3 -m pypowerwall.tedapi \
            && info "$LBL TEDAPI auth: OK" \
            || warn "$LBL TEDAPI auth: FAILED — check gateway password"
    done
    $FOUND || warn "No pypowerwall-*.env files found — run ./setup.sh first."
    exit 0
fi

##############################################################################
# Full first-time setup
##############################################################################

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║              Powerwall Dashboard Setup                       ║"
echo "║     1-3 Tesla gateways · TEDAPI local LAN · no cloud         ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# ── Check for existing config ─────────────────────────────────────────────────
for f in compose.env gateways.json pypowerwall-a.env pypowerwall-b.env pypowerwall-c.env influxdb.env grafana.env; do
    if [[ -f "$f" ]]; then
        warn "$f already exists — skipping that step (delete it first to reconfigure)"
    fi
done

# ── Site-wide settings ────────────────────────────────────────────────────────
TZ=""
RATE_IMPORT="" RATE_EXPORT="" BASELINE="" SITE_NAME=""
if [[ ! -f compose.env ]]; then
    DEFAULT_TZ=$(timedatectl show --property=Timezone --value 2>/dev/null || echo "America/Los_Angeles")
    read -rp "Enter your timezone [${DEFAULT_TZ}]: " TZ_INPUT
    TZ="${TZ_INPUT:-$DEFAULT_TZ}"
    if ! timedatectl list-timezones 2>/dev/null | grep -q "^${TZ}$"; then
        warn "Timezone '$TZ' not found in system list — using anyway."
    fi
    info "Timezone: $TZ"

    read -rp "Site name shown in the web dashboard [POWERWALL]: " SITE_NAME
    SITE_NAME="${SITE_NAME:-POWERWALL}"

    read -rp "Import electricity rate (\$/kWh) [0.25]: " RATE_IMPORT
    RATE_IMPORT="${RATE_IMPORT:-0.25}"
    read -rp "Export (sell-back) rate (\$/kWh) [0.08]: " RATE_EXPORT
    RATE_EXPORT="${RATE_EXPORT:-0.08}"

    read -rp "Estimated baseline home load without EVs (W) [800]: " BASELINE
    BASELINE="${BASELINE:-800}"
fi

# ── Gateways ──────────────────────────────────────────────────────────────────
GW_COUNT=0
declare -a GW_HOST GW_PWD GW_CAP GW_LABEL
if [[ ! -f gateways.json ]]; then
    echo ""
    echo "── Gateways (TEDAPI local LAN) ────────────────────────────────"
    echo "   One entry per Tesla gateway. A gateway with multiple"
    echo "   Powerwalls behind it is still ONE gateway — enter the"
    echo "   combined battery capacity when asked."
    echo ""
    while true; do
        read -rp "How many Tesla gateways does your system have? [1-3]: " GW_COUNT
        [[ "$GW_COUNT" =~ ^[123]$ ]] && break
        warn "Please enter 1, 2, or 3."
    done

    for ((i = 0; i < GW_COUNT; i++)); do
        L="${LETTERS[$i]}"; U=$(echo "$L" | tr 'a-z' 'A-Z')
        echo ""
        echo "── Gateway ${U} ────────────────────────────────────────────────"
        read -rp "Gateway ${U} LAN IP (e.g. 192.168.1.10$i): " IP
        [[ -z "$IP" ]] && die "Gateway ${U} IP is required."
        read -rp "Gateway ${U} hardware password (from sticker on unit): " -s PWD_IN; echo ""
        [[ -z "$PWD_IN" ]] && die "Gateway ${U} password is required."
        read -rp "Usable battery capacity behind gateway ${U} (kWh) [13.5]: " CAP
        CAP="${CAP:-13.5}"
        read -rp "Display name for gateway ${U} [PANEL ${U}]: " LBL
        LBL="${LBL:-PANEL ${U}}"

        step "Checking connectivity to gateway ${U} at https://${IP} ..."
        if curl -sk --head --max-time 5 "https://${IP}" -o /dev/null; then
            info "Gateway ${U} is reachable."
        else
            warn "Gateway ${U} at ${IP} did not respond. Check IP and cable."
            warn "Continuing anyway — you can fix the IP in pypowerwall-${L}.env and restart."
        fi

        GW_HOST[$i]="$IP"; GW_PWD[$i]="$PWD_IN"; GW_CAP[$i]="$CAP"; GW_LABEL[$i]="$LBL"
    done

    # gateways.json — infrastructure manifest consumed by the generator (-g).
    # No passwords here; those go only into the per-gateway env files.
    {
        echo '{'
        echo '  "gateways": ['
        for ((i = 0; i < GW_COUNT; i++)); do
            L="${LETTERS[$i]}"
            COMMA=","; [[ $i -eq $((GW_COUNT - 1)) ]] && COMMA=""
            printf '    {"tag": "panel_%s", "host": "%s", "label": "%s"}%s\n' \
                "$L" "${GW_HOST[$i]}" "${GW_LABEL[$i]}" "$COMMA"
        done
        echo '  ]'
        echo '}'
    } > gateways.json
    info "gateways.json written."

    # Per-gateway env files from the generic sample
    for ((i = 0; i < GW_COUNT; i++)); do
        L="${LETTERS[$i]}"; U=$(echo "$L" | tr 'a-z' 'A-Z')
        cp pypowerwall.env.sample "pypowerwall-${L}.env"
        sed -i.bak \
            -e "s|PW_HOST=REPLACE_GATEWAY_IP|PW_HOST=${GW_HOST[$i]}|" \
            -e "s|PW_GW_PWD=REPLACE_GATEWAY_PASSWORD|PW_GW_PWD=${GW_PWD[$i]}|" \
            -e "s|PW_TIMEZONE=America/Los_Angeles|PW_TIMEZONE=${TZ:-America/Los_Angeles}|" \
            -e "s|PW_LABEL=REPLACE_LABEL|PW_LABEL=Panel_${U}|" \
            "pypowerwall-${L}.env"
        rm -f "pypowerwall-${L}.env.bak"
        info "pypowerwall-${L}.env written."
    done
fi

# ── compose.env ───────────────────────────────────────────────────────────────
if [[ ! -f compose.env ]]; then
    TAGS="panel_a"
    [[ $GW_COUNT -ge 2 ]] && TAGS="panel_a,panel_b"
    [[ $GW_COUNT -ge 3 ]] && TAGS="panel_a,panel_b,panel_c"

    cp compose.env.sample compose.env
    sed -i.bak \
        -e "s|TZ=America/Los_Angeles|TZ=${TZ}|" \
        -e "s|GATEWAY_TAGS=panel_a,panel_b|GATEWAY_TAGS=${TAGS}|" \
        -e "s|SITE_NAME=POWERWALL|SITE_NAME=${SITE_NAME}|" \
        -e "s|RATE_IMPORT=0.25|RATE_IMPORT=${RATE_IMPORT}|" \
        -e "s|RATE_EXPORT=0.08|RATE_EXPORT=${RATE_EXPORT}|" \
        -e "s|BASELINE_HOME_LOAD_W=800|BASELINE_HOME_LOAD_W=${BASELINE}|" \
        compose.env
    for ((i = 0; i < GW_COUNT; i++)); do
        U=$(echo "${LETTERS[$i]}" | tr 'a-z' 'A-Z')
        sed -i.bak "s|PW_${U}_CAPACITY_KWH=13.5|PW_${U}_CAPACITY_KWH=${GW_CAP[$i]}|" compose.env
    done
    rm -f compose.env.bak
    info "compose.env written."
fi

# ── Generate stack configs (powerwall.yml, telegraf.conf, dashboards) ─────────
step "Generating stack configs for your gateway count..."
run_python tools/generate_stack.py
info "Stack configs generated."

# ── Weather (optional) ────────────────────────────────────────────────────────
if [[ ! -f weather/weather411.conf ]]; then
    echo ""
    read -rp "Enable weather overlay? Needs a free OpenWeatherMap API key (y/N): " WANT_WEATHER
    if [[ "${WANT_WEATHER,,}" == "y" ]]; then
        read -rp "OpenWeatherMap API key: " OWM_KEY
        read -rp "Latitude (decimal): " OWM_LAT
        read -rp "Longitude (decimal): " OWM_LON
        cp weather/weather411.conf.sample weather/weather411.conf
        sed -i.bak \
            -e "s|APIKEY = your_openweathermap_api_key_here|APIKEY = ${OWM_KEY}|" \
            -e "s|LAT = 37.7749|LAT = ${OWM_LAT}|" \
            -e "s|LON = -122.4194|LON = ${OWM_LON}|" \
            weather/weather411.conf
        rm -f weather/weather411.conf.bak
        info "weather/weather411.conf written. Start it later with:"
        info "  $COMPOSE_CMD -f powerwall.yml --profile weather up -d weather411"
    fi
fi

# ── InfluxDB env ───────────────────────────────────────────────────────────────
[[ -f influxdb.env ]] || { cp influxdb.env.sample influxdb.env; info "influxdb.env written."; }

# ── Grafana env ────────────────────────────────────────────────────────────────
if [[ ! -f grafana.env ]]; then
    cp grafana.env.sample grafana.env
    read -rp "Grafana admin password [admin]: " GF_PASS
    GF_PASS="${GF_PASS:-admin}"
    sed -i.bak "s|GF_SECURITY_ADMIN_PASSWORD=admin|GF_SECURITY_ADMIN_PASSWORD=${GF_PASS}|" grafana.env
    rm -f grafana.env.bak
    info "grafana.env written."
fi

# ── Create data directories ────────────────────────────────────────────────────
mkdir -p data/influxdb data/grafana data/webapp
for ((i = 0; i < ${GW_COUNT:-3}; i++)); do
    mkdir -p "data/auth-${LETTERS[$i]}"
done
chmod 755 data/influxdb data/grafana
# The webapp container runs as uid 1000 and persists settings.json here
chown 1000:1000 data/webapp 2>/dev/null || chmod 777 data/webapp
info "Data directories created."

# ── Seed webapp settings from the setup answers ────────────────────────────────
# (if compose.env pre-existed, the prompts were skipped — read values from it)
[[ -z "$TZ" ]] && TZ=$(grep '^TZ=' compose.env | cut -d= -f2)
[[ -z "$RATE_IMPORT" ]] && RATE_IMPORT=$(grep '^RATE_IMPORT=' compose.env | cut -d= -f2)
[[ -z "$RATE_EXPORT" ]] && RATE_EXPORT=$(grep '^RATE_EXPORT=' compose.env | cut -d= -f2)
[[ -z "$SITE_NAME" ]] && SITE_NAME=$(grep '^SITE_NAME=' compose.env | cut -d= -f2)
SITE_NAME="${SITE_NAME:-POWERWALL}"
if [[ ! -f data/webapp/settings.json && ${GW_COUNT:-0} -gt 0 ]]; then
    case $GW_COUNT in
        1) TAGLINE="POWERWALL GATEWAY" ;;
        2) TAGLINE="DUAL GATEWAY // POWERWALL" ;;
        3) TAGLINE="TRIPLE GATEWAY // POWERWALL" ;;
    esac
    {
        echo '{'
        echo '  "version": 1,'
        echo "  \"site_name\": \"${SITE_NAME}\","
        echo "  \"tagline\": \"${TAGLINE}\","
        echo "  \"timezone\": \"${TZ}\","
        echo "  \"rate_import\": ${RATE_IMPORT},"
        echo "  \"rate_export\": ${RATE_EXPORT},"
        echo '  "gateways": ['
        for ((i = 0; i < GW_COUNT; i++)); do
            COMMA=","; [[ $i -eq $((GW_COUNT - 1)) ]] && COMMA=""
            printf '    {"tag": "panel_%s", "label": "%s", "capacity_kwh": %s}%s\n' \
                "${LETTERS[$i]}" "${GW_LABEL[$i]}" "${GW_CAP[$i]}" "$COMMA"
        done
        echo '  ]'
        echo '}'
    } > data/webapp/settings.json
    chown 1000:1000 data/webapp/settings.json 2>/dev/null || true
    info "Webapp settings seeded (editable later via the ⚙ settings page)."
fi

# ── Watchdog cron (optional) ───────────────────────────────────────────────────
echo ""
read -rp "Install the health watchdog as a cron job (restarts wedged services every 5 min)? (y/N): " WANT_WD
if [[ "${WANT_WD,,}" == "y" ]]; then
    CRON_LINE="*/5 * * * * ${SCRIPT_DIR}/watchdog.sh >> ${SCRIPT_DIR}/watchdog.log 2>&1"
    if crontab -l 2>/dev/null | grep -qF "watchdog.sh"; then
        info "Watchdog cron entry already present."
    else
        (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab - \
            && info "Watchdog installed (every 5 minutes)." \
            || warn "Could not edit crontab — add manually: $CRON_LINE"
    fi
    warn "Unraid note: crontab does not survive reboots — use the User Scripts plugin instead (see docs/UNRAID_SETUP.md)."
fi

# ── Build local images ─────────────────────────────────────────────────────────
info "Building enricher + webapp containers..."
$COMPOSE_CMD -f powerwall.yml build enricher webapp

# ── Start InfluxDB first, initialize schema ────────────────────────────────────
info "Starting InfluxDB..."
$COMPOSE_CMD -f powerwall.yml up -d influxdb

info "Waiting for InfluxDB to be healthy..."
for i in $(seq 1 30); do
    if docker exec influxdb curl -sf http://localhost:8086/health &>/dev/null; then
        info "InfluxDB is healthy."
        break
    fi
    [[ $i -eq 30 ]] && die "InfluxDB failed to become healthy after 30 attempts"
    sleep 3
done

info "Initializing InfluxDB schema..."
docker exec influxdb influx -import -path=/var/lib/influxdb/influxdb.sql
info "Schema initialized."

# ── Start all services ─────────────────────────────────────────────────────────
info "Starting all services..."
$COMPOSE_CMD -f powerwall.yml up -d

HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Setup complete!                                             ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║                                                              ║"
printf "║  Web dashboard: http://%-37s║\n" "${HOST_IP}:${WEBAPP_PORT:-8050}  "
printf "║  Grafana:       http://%-37s║\n" "${HOST_IP}:${GRAFANA_PORT:-9000}  "
echo "║  Grafana login: admin / (your chosen password)               ║"
echo "║                                                              ║"
echo "║  Data should appear within ~1 minute.                        ║"
echo "║  If panels show 'No Data', run the TEDAPI test:              ║"
echo "║    ./setup.sh -t                                             ║"
echo "║                                                              ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
info "Check service health:  docker compose -f powerwall.yml ps"
info "View live logs:        docker compose -f powerwall.yml logs -f"
