#!/usr/bin/env bash
# watchdog.sh — Monitor service health and restart unhealthy containers
# Run this from cron every 5 minutes on the Unraid host, or as a one-shot check.
# Usage: ./watchdog.sh

set -euo pipefail

COMPOSE_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/powerwall.yml"
LOG_TAG="powerwall-watchdog"

log() { logger -t "$LOG_TAG" "$*" 2>/dev/null || echo "[$(date)] $*"; }

SERVICES=(influxdb pypowerwall-a pypowerwall-b telegraf enricher grafana)

for svc in "${SERVICES[@]}"; do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' "$svc" 2>/dev/null || echo "not_found")

    case "$STATUS" in
        healthy)
            ;;
        unhealthy)
            log "WARN: $svc is unhealthy — restarting"
            docker compose -f "$COMPOSE_FILE" restart "$svc"
            log "INFO: $svc restarted"
            ;;
        not_found)
            log "WARN: $svc container not found — starting"
            docker compose -f "$COMPOSE_FILE" up -d "$svc"
            ;;
        starting)
            log "INFO: $svc is still starting..."
            ;;
        *)
            log "WARN: $svc has unexpected health status: $STATUS"
            ;;
    esac
done
