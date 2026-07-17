# Unraid-Specific Setup Guide

This guide covers installing and running the dual-gateway Powerwall Dashboard on an Unraid server.

---

## Recommended File Location

Store all project files under Unraid's appdata share for automatic backup inclusion:

```
/mnt/user/appdata/powerwall-dashboard/
```

Create it and clone (or copy) the project there:

```bash
mkdir -p /mnt/user/appdata/powerwall-dashboard
cd /mnt/user/appdata/powerwall-dashboard
# Copy your project files here, or use git if you have it installed
```

---

## Port Assignments

These ports are assigned by default. Verify they don't conflict with other Unraid apps (check **Settings → Network Settings** and your Community Applications installs):

| Service | Default Port | Change in |
|---|---|---|
| InfluxDB | 8086 | `compose.env` → `INFLUXDB_PORT` |
| pypowerwall Panel A | 8675 | `compose.env` → `PW_A_PORT` |
| pypowerwall Panel B | 8676 | `compose.env` → `PW_B_PORT` |
| Grafana | 9000 | `compose.env` → `GRAFANA_PORT` |
| Weather411 (optional) | 8677 | `compose.env` → `WEATHER_PORT` |

Common Unraid port conflicts to watch for:
- Port 8086 may conflict with a self-hosted InfluxDB from Community Apps
- Port 9000 may conflict with Portainer

---

## Data Volume Paths

The persistent data directories are bind-mounted. With the defaults in `compose.env`:

```
./data/influxdb   →  /mnt/user/appdata/powerwall-dashboard/data/influxdb
./data/grafana    →  /mnt/user/appdata/powerwall-dashboard/data/grafana
./auth/panel_a    →  /mnt/user/appdata/powerwall-dashboard/auth/panel_a
./auth/panel_b    →  /mnt/user/appdata/powerwall-dashboard/auth/panel_b
```

These are created by `setup.sh`. If you prefer absolute paths, set in `compose.env`:

```
INFLUXDB_DATA_PATH=/mnt/user/appdata/powerwall-dashboard/data/influxdb
GRAFANA_DATA_PATH=/mnt/user/appdata/powerwall-dashboard/data/grafana
```

---

## Container User (UID/GID)

Unraid's default Docker user is `nobody` (UID 99, GID 100). pypowerwall defaults to UID/GID 1000. Set in `compose.env` to match your Unraid setup:

```
PW_USER=99
PW_GROUP=100
```

---

## Starting the Stack

### Option A: Unraid Terminal

```bash
cd /mnt/user/appdata/powerwall-dashboard
./setup.sh          # First-time setup: prompts for gateway IPs + passwords,
                    # checks connectivity, initializes DB, starts all services.
./setup.sh -t       # (Optional) test TEDAPI auth against both gateways
```

### Option B: Community Applications (Manual Compose)

1. Install the **Community Applications** plugin if not already installed.
2. Install **Docker Compose Manager** from Community Applications.
3. In Docker Compose Manager, add a new stack:
   - Name: `powerwall-dashboard`
   - Path: `/mnt/user/appdata/powerwall-dashboard/powerwall.yml`
4. Set environment variables in the manager UI or ensure `compose.env` is present.
5. Start the stack.

Note: Gateway credentials (`PW_HOST`, `PW_GW_PWD`) must still be set in the `.env` files before starting the stack — the Docker Compose Manager UI does not prompt for them. Run `./setup.sh` from the terminal for the interactive first-time setup, then manage the stack from the Compose Manager UI afterwards.

---

## Auto-Start on Boot

The containers use `restart: unless-stopped`, so they restart automatically after Unraid reboots — **as long as Docker is enabled**.

Verify Docker auto-start: **Settings → VM Manager → Enable Docker** should be checked.

If you use the Docker Compose Manager plugin, enable **Autostart** for the `powerwall-dashboard` stack in the plugin UI.

---

## Firewall / Network

Grafana (port 9000) is accessible from any device on your LAN. If you want to access it from outside your network:
- Set up a reverse proxy (e.g., Nginx Proxy Manager, available in Community Applications)
- Enable HTTPS in `grafana.env` (see the commented HTTPS section)
- Do **not** expose InfluxDB (8086) or pypowerwall ports (8675/8676) externally — they have no authentication by default

---

## InfluxDB Backup

### Automated backup (recommended)

Use Unraid's built-in backup or the CA Backup plugin to include `/mnt/user/appdata/powerwall-dashboard/data/influxdb` in your scheduled backups.

### Manual snapshot

```bash
# Stop InfluxDB (minimizes risk of partial writes during backup)
docker stop influxdb

# Copy the data directory
cp -a /mnt/user/appdata/powerwall-dashboard/data/influxdb \
      /mnt/user/backup/powerwall-influxdb-$(date +%Y%m%d)

# Restart
docker start influxdb
```

### InfluxDB native backup (online, no stop required)

```bash
docker exec influxdb influxd backup -portable /var/lib/influxdb/backup
cp -a /mnt/user/appdata/powerwall-dashboard/data/influxdb/backup \
      /mnt/user/backup/powerwall-influxdb-$(date +%Y%m%d)
```

---

## Checking Service Health

```bash
cd /mnt/user/appdata/powerwall-dashboard
docker compose -f powerwall.yml ps
docker compose -f powerwall.yml logs --tail=50 enricher
docker compose -f powerwall.yml logs --tail=50 telegraf
```

---

## Updating

```bash
cd /mnt/user/appdata/powerwall-dashboard
./setup.sh -u
```

This pulls the latest `pypowerwall`, `telegraf`, `influxdb`, and `grafana` images and restarts all services. The `enricher` image is rebuilt from source.

---

## Troubleshooting

**Grafana shows "No Data"**
1. Check `docker compose -f powerwall.yml ps` — all services should be `healthy`
2. Check that pypowerwall is authenticated: `curl http://localhost:8675/aggregates`
3. Check Telegraf is writing: `docker compose -f powerwall.yml logs telegraf | grep -i error`
4. Verify InfluxDB has data: `docker exec influxdb influx -database powerwall -execute "SELECT count(*) FROM \"raw\".\"http\" GROUP BY system"`

**pypowerwall returns empty / error**
- Run the TEDAPI test: `./setup.sh -t`
- Check gateway reachability: `curl -k --head https://<gateway-ip>`
- Check logs: `docker compose -f powerwall.yml logs pypowerwall-a`
- See [TEDAPI_SETUP.md](TEDAPI_SETUP.md) for detailed troubleshooting

**Port conflict on startup**
- Change the conflicting port in `compose.env` and restart: `docker compose -f powerwall.yml up -d`
