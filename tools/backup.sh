#!/usr/bin/env bash
# backup.sh — nightly InfluxDB backup with retention.
#
# Takes a portable snapshot of the powerwall database (the irreplaceable part
# of this stack — everything else regenerates) into backups/, then prunes old
# archives. Grafana state (dashboards are provisioned; only user prefs live in
# the volume) and the settings file are tiny and included too.
#
# Usage:
#   ./tools/backup.sh              # one-shot backup
#   ./tools/backup.sh 14           # keep 14 days instead of the default 7
#
# Schedule nightly (cron):
#   0 3 * * * /path/to/powerwall-dashboard/tools/backup.sh >> /path/to/powerwall-dashboard/backups/backup.log 2>&1
# Unraid: crontab does not survive reboots — use the User Scripts plugin
# (see docs/UNRAID_SETUP.md).
set -euo pipefail

KEEP_DAYS="${1:-7}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$ROOT/backups"
WORK="$DEST/influxdb-$STAMP"

mkdir -p "$DEST"

echo "[$(date '+%F %T')] backing up InfluxDB → $WORK.tgz"

# Portable backup runs inside the container, then lands in the host workdir.
docker exec influxdb influxd backup -portable -database powerwall /tmp/pw-backup
docker cp influxdb:/tmp/pw-backup "$WORK"
docker exec influxdb rm -rf /tmp/pw-backup

# Include the webapp settings file (tiny, restores the settings page state)
[ -f "$ROOT/data/webapp/settings.json" ] && cp "$ROOT/data/webapp/settings.json" "$WORK/"

tar -czf "$WORK.tgz" -C "$DEST" "$(basename "$WORK")"
rm -rf "$WORK"

# Retention: drop archives older than KEEP_DAYS
find "$DEST" -name 'influxdb-*.tgz' -mtime "+$KEEP_DAYS" -delete

SIZE=$(du -h "$WORK.tgz" | cut -f1)
COUNT=$(ls "$DEST"/influxdb-*.tgz 2>/dev/null | wc -l | tr -d ' ')
echo "[$(date '+%F %T')] done: $WORK.tgz ($SIZE) — $COUNT archive(s) retained"

# Restore procedure (documented here so it survives with the backups):
#   tar -xzf influxdb-<stamp>.tgz
#   docker cp influxdb-<stamp> influxdb:/tmp/pw-restore
#   docker exec influxdb influxd restore -portable -db powerwall -newdb powerwall_restored /tmp/pw-restore
#   ...then verify and swap: see docs/FAQ.md "Restoring a backup"
