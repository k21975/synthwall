# Synthwall — Multi-Gateway Powerwall Dashboard

Self-hosted energy dashboards for homes with **1 to 3 Tesla Powerwall gateways** — including split-service homes with two or three independent panels. Extends [jasonacox/Powerwall-Dashboard](https://github.com/jasonacox/Powerwall-Dashboard) with multi-gateway support, whole-home aggregation, a synthwave web app with a live settings page, and generated configs so any gateway count works out of the box.

Data collection is **100% local**: pypowerwall's TEDAPI mode talks to each gateway over your LAN using the gateway IP and the password printed on the unit. No Tesla account, no developer app, no cloud dependency.

---

## What You Get

**Synthwave web app** (the fork's centerpiece):
- Live power tiles (solar, home, grid, battery, self-powered %) refreshed every 5s
- Solar-vs-usage balance with AHEAD/BEHIND for any period
- Time ranges: today, 24h, 7 days, month, 30 days, year, **your electric-company billing period**, **lifetime**, or any **custom date range**
- Per-gateway cards and a combined/per-gateway toggle (hidden automatically on single-gateway systems)
- **Charging section** — a pie per car showing how much of its charging landed in **peak vs off-peak** hours, for any billing period from the dropdown. Peak defaults to 1PM–9PM every day and is editable in settings, with optional weekend/holiday carve-outs for tariffs that have them
- **Settings page (⚙)** — site name, per-gateway labels, electricity rates, battery capacities, house timezone, billing-cycle start day, peak-hour windows, and house-time vs local-time display; saved server-side and applied live, no restarts
- Installable as an iOS/Android home-screen app; updates itself when a new version ships

**Grafana dashboards**:
- Combined power-flow dashboard + one per gateway (animated flow, energy stats, savings, self-powered breakdown)
- Generated for your exact gateway count by `setup.sh`

**Metrics collected every 5 seconds, per gateway + combined:**
solar, home load, grid import/export, battery charge/discharge, state of charge, string data, frequency/voltage, self-sufficiency %, estimated EV load, and $ savings.

---

## Architecture

```
Gateway A ──► pypowerwall-a ──┐
Gateway B ──► pypowerwall-b ──┼──► Telegraf (system tags) ──► InfluxDB ──► Grafana
Gateway C ──► pypowerwall-c ──┘         ▲                        ▲
        (1-3, generated)            Enricher ────────────────────┘
                                (combined stream +          Web app (FastAPI)
                                 derived metrics)
```

Each gateway gets its own pypowerwall proxy and a `system` tag (`panel_a`/`panel_b`/`panel_c`). The enricher sums the per-gateway streams into a `combined` stream (capacity-weighted state of charge) and computes derived metrics. Everything downstream — InfluxDB continuous queries, Grafana, the web app — just filters by tag, so 1, 2, or 3 gateways flow through the same schema. Full decision record: [ARCHITECTURE.md](ARCHITECTURE.md).

**Stack:** InfluxDB 1.8 · pypowerwall (×N) · Telegraf · Grafana · Python enricher · FastAPI web app — all Docker Compose.

---

## Requirements

- Any Linux host with Docker + Docker Compose (Unraid, a NAS, a mini PC…)
- Each Tesla gateway wired to your LAN with a reserved/static IP
- Each gateway's hardware password (printed on the unit sticker)
- No internet access needed for data collection

---

## Quick Start

```bash
git clone https://github.com/k21975/synthwall.git
cd synthwall

# Interactive setup: asks your timezone, rates, HOW MANY GATEWAYS (1-3),
# and each gateway's IP/password/capacity. Generates the compose stack,
# telegraf config, and Grafana dashboards for your exact layout, then
# initializes InfluxDB and starts everything.
./setup.sh

# Optional: confirm live TEDAPI data from each gateway
./setup.sh -t
```

Then open:
- **Web app**: `http://<host>:8050`
- **Grafana**: `http://<host>:9000` (admin / your chosen password)

Data appears within a minute. Setup examples for 1, 2, and 3 gateways are identical — the only difference is your answer to the gateway-count prompt.

Guides: [docs/TEDAPI_SETUP.md](docs/TEDAPI_SETUP.md) · [docs/UNRAID_SETUP.md](docs/UNRAID_SETUP.md) · [docs/FAQ.md](docs/FAQ.md)

---

## Settings: what's live vs. what needs a re-run

| Change | How |
|---|---|
| Site name, gateway labels, rates, capacities, timezone, billing day, time display, peak hours | **Web app ⚙ settings page** — applied instantly |
| Gateway IP or password | Edit `pypowerwall-<x>.env`, then `docker compose -f powerwall.yml up -d --force-recreate pypowerwall-<x>` (once — see FAQ about rapid recreates) |
| Add/remove a gateway | Edit `gateways.json` + add/remove `pypowerwall-<x>.env`, run `./setup.sh -g`, then `docker compose -f powerwall.yml up -d` |
| Ports, data paths, poll interval | `compose.env`, then `up -d` |

The settings file lives in `data/webapp/settings.json` (mounted volume) and overrides the env defaults; both the web app and the enricher pick up edits live.

---

## Project Structure

```
powerwall-dashboard/
├── setup.sh                  # Interactive setup (1-3 gateways) + -g/-i/-u/-t modes
├── powerwall.yml             # GENERATED compose stack (2-gateway default shipped)
├── gateways.json             # Your gateway manifest (written by setup.sh, gitignored)
├── compose.env.sample        # Shared env template (rates, TZ, ports, capacities)
├── pypowerwall.env.sample    # Per-gateway credentials template
├── telegraf/telegraf.conf    # GENERATED polling config (system tags)
├── influxdb/influxdb.sql     # Schema + continuous queries (gateway-count agnostic)
├── enricher/                 # Combined stream + derived metrics service
├── webapp/                   # FastAPI + synthwave SPA (settings page lives here)
├── grafana/                  # Provisioning + GENERATED dashboards + branding
├── weather/                  # Optional weather411 config (sample provided)
├── tools/
│   ├── generate_stack.py     # Regenerates compose/telegraf/dashboards from gateways.json
│   ├── gen_dashboards.py     # Grafana dashboard builder (used by generate_stack)
│   ├── backup.sh             # Nightly InfluxDB backup with retention
│   ├── tesla_csv_import.py   # Backfill daily history from Tesla-app CSV exports
│   └── deploy-*.sh           # Remote-dev helpers (DEPLOY_HOST=<ssh-host>)
├── tz.sh / watchdog.sh       # Timezone bulk-update · health auto-restart
└── docs/                     # TEDAPI, Unraid, FAQ, upgrading
```

Files marked GENERATED carry a header saying so — edit `gateways.json` and run `./setup.sh -g` instead of editing them directly.

---

## Backups & Reliability

- All containers restart automatically (`unless-stopped`); InfluxDB gets a 30s graceful-stop window.
- `./setup.sh` offers a watchdog cron that restarts wedged services every 5 minutes.
- `tools/backup.sh` snapshots the InfluxDB database nightly with retention — schedule it; the time-series data is the only irreplaceable part of the stack.
- A host reboot loses only the metrics during the downtime (TEDAPI has no history API). See [docs/FAQ.md](docs/FAQ.md) for the reboot behavior rundown and the one warning that matters: **don't rapidly recreate the pypowerwall containers** (gateways lock out repeated auth attempts).

## Updating

```bash
git pull
./setup.sh -u
```

Details and breaking-change notes: [docs/UPGRADING.md](docs/UPGRADING.md)

---

## Optional: Weather Overlay

```bash
cp weather/weather411.conf.sample weather/weather411.conf
# add your free OpenWeatherMap API key + lat/lon (or let setup.sh prompt you)
docker compose -f powerwall.yml --profile weather up -d weather411
```

---

## Credits & License

MIT — see [LICENSE](LICENSE).

Built on [jasonacox/Powerwall-Dashboard](https://github.com/jasonacox/Powerwall-Dashboard) and [jasonacox/pypowerwall](https://github.com/jasonacox/pypowerwall) (both MIT); the Grafana power-flow dashboards are generated from the jasonacox source dashboard. This fork adds multi-gateway support without forking pypowerwall itself, and follows upstream color conventions: yellow `#FADE2A` solar · green `#73BF69` Powerwall · blue `#5794F2` home · purple `#B877D9` grid.
