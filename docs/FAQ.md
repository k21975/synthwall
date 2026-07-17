# FAQ & Troubleshooting

## Setup

**"No Data" in Grafana or the web app after setup**
1. Run `./setup.sh -t` — it checks each gateway's reachability and TEDAPI auth.
2. `docker compose -f powerwall.yml ps` — everything should be `healthy` after ~2 minutes.
3. `docker logs pypowerwall-a --tail 20` — look for auth errors (wrong password) or
   "Failed to connect to Powerwall with all modes" (usually a wrong `PW_RSA_KEY_PATH`
   or unreachable IP, **not** a lockout).
4. `docker logs telegraf --tail 20` — connection refused here means the pypowerwall
   containers aren't healthy yet.

**Gateway unreachable**
The gateway must be wired to your LAN with a reserved IP. Test from the Docker host:
`curl -k --head https://<gateway-ip>` — any HTTP response (even 4xx) means reachable.
See [TEDAPI_SETUP.md](TEDAPI_SETUP.md) for the full pre-flight.

**I have multiple Powerwalls but only one gateway**
That's a 1-gateway install — the gateway aggregates its Powerwalls. Enter the
combined usable capacity (e.g. 27 kWh for two PW3s) when setup asks.

## Operations

**⚠️ Never rapidly restart/recreate the pypowerwall containers.**
Tesla gateways lock out clients that hammer authentication. Several
`--force-recreate` cycles in quick succession can lock out your gateways for
hours (it has happened). Batch config changes and recreate **once**. A normal
host reboot is one reconnection and is safe.

**What happens when the host reboots?**
Everything restarts automatically (`restart: unless-stopped`; on Unraid also
enable stack autostart in Compose Manager). TEDAPI auth keys persist in
`data/auth-*`, so gateways reconnect without re-registering. The only loss is
the metrics during the downtime — TEDAPI has no history API, so that gap is
permanent. Daily totals can be patched from a Tesla-app CSV export with
`tools/tesla_csv_import.py` if you ever need to.

**Backups**
`tools/backup.sh` takes a portable InfluxDB snapshot into `backups/` and keeps
7 days by default. Schedule it nightly (cron, or User Scripts on Unraid). The
InfluxDB data is the only irreplaceable state in the stack.

**Restoring a backup**
```bash
tar -xzf backups/influxdb-<stamp>.tgz -C backups/
docker cp backups/influxdb-<stamp> influxdb:/tmp/pw-restore
docker exec influxdb influxd restore -portable -db powerwall -newdb powerwall_restored /tmp/pw-restore
# verify, then swap it in:
docker exec influxdb influx -execute "DROP DATABASE powerwall"   # ⚠️ point of no return
docker exec influxdb influx -execute 'CREATE DATABASE powerwall'
docker exec influxdb influx -database powerwall_restored -execute 'SELECT * INTO "powerwall".."http" FROM "http" GROUP BY *'
# simpler in practice: restore straight over a fresh install before starting telegraf
```

**Changing a gateway's IP or password**
Edit `pypowerwall-<x>.env`, then recreate that one container **once**:
`docker compose -f powerwall.yml up -d --force-recreate pypowerwall-<x>`

**Adding a third gateway later**
Add the gateway to `gateways.json`, create `pypowerwall-c.env` from the sample,
add `PW_C_CAPACITY_KWH` to `compose.env` and update `GATEWAY_TAGS`, then:
`./setup.sh -g && docker compose -f powerwall.yml up -d`

## Web app

**Rates/labels changed but Grafana savings didn't**
The web app recomputes savings from raw kWh, so it reacts instantly. The
enricher's *stored* savings field (used by some Grafana panels) is computed at
write time — rate changes apply going forward only. The Grafana dashboards'
rate constants are baked at generation time; re-run `./setup.sh -g` to refresh.

**Billing period shows the wrong window**
Set your billing-cycle start day (1–28) in the ⚙ settings page. The BILLING
PERIOD range runs from the most recent occurrence of that day (house-timezone
midnight) to now.

**Times look shifted**
The ⚙ settings page has "SHOW TIMES IN": *my local time* (viewer's browser) or
*house time* (the configured house timezone). Range boundaries like "today"
always follow house time; this setting only affects display.

**The phone app shows an old version**
The app checks for updates when foregrounded and every 15 minutes. If it's
stuck on something ancient (pre-2.20), force-quit it once or re-add it to the
home screen.
