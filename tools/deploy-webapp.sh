#!/bin/bash
# Deploy the custom web dashboard (webapp/) to Unraid and rebuild its container.
#
# Copies the webapp source + the updated powerwall.yml to Unraid, then rebuilds
# and restarts ONLY the webapp service (no other containers are touched — this is
# important: never recreate the pypowerwall-* proxies casually, see STATUS.md).
#
# Usage:
#   ./tools/deploy-webapp.sh            — sync source, rebuild + restart webapp
#   ./tools/deploy-webapp.sh --static   — sync only static/ assets (no rebuild;
#                                         requires the image to bind-mount static,
#                                         otherwise use the default full rebuild)
# Config (env or edit defaults):
#   DEPLOY_HOST — ssh host/alias of the docker host
#   DEPLOY_DIR  — project directory on that host
set -euo pipefail

UNRAID="${DEPLOY_HOST:-unraid}"
PW_DIR="${DEPLOY_DIR:-/mnt/user/appdata/powerwall-dashboard}"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "Syncing webapp/ -> $UNRAID:$PW_DIR/webapp"
rsync -az --delete \
  --exclude '__pycache__' --exclude '*.pyc' \
  "$SRC_DIR/webapp/" "$UNRAID:$PW_DIR/webapp/"

echo "Syncing powerwall.yml"
scp "$SRC_DIR/powerwall.yml" "$UNRAID:$PW_DIR/powerwall.yml"

echo "Rebuilding + restarting webapp container"
ssh "$UNRAID" "cd $PW_DIR && docker compose -f powerwall.yml up -d --build webapp && \
  echo '--- webapp logs ---' && sleep 3 && docker logs --tail 15 webapp"

echo "done — webapp restarted on $UNRAID"
