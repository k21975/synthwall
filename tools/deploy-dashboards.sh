#!/bin/bash
# Deploy Grafana dashboard JSONs to the docker host and restart Grafana.
# For remote development where the stack runs on another machine (NAS, server).
# Usage:
#   ./tools/deploy-dashboards.sh          — deploy all dashboards
#   ./tools/deploy-dashboards.sh 01 05    — deploy only dashboards 01 and 05
# Config (env or edit defaults):
#   DEPLOY_HOST — ssh host/alias of the docker host
#   DEPLOY_DIR  — project directory on that host
set -euo pipefail

UNRAID="${DEPLOY_HOST:-unraid}"
PW_DIR="${DEPLOY_DIR:-/mnt/user/appdata/powerwall-dashboard}"
DASH_DIR="$(cd "$(dirname "$0")/../grafana/dashboards" && pwd)"

if [ $# -eq 0 ]; then
    FILES=("$DASH_DIR"/*.json)
    SYNC_ALL=true
else
    FILES=()
    SYNC_ALL=false
    for n in "$@"; do
        match=("$DASH_DIR"/${n}-*.json)
        [ ${#match[@]} -gt 0 ] && FILES+=("${match[@]}") || echo "warning: no match for '$n'"
    done
fi

NAMES=()
for f in "${FILES[@]}"; do NAMES+=("$(basename "$f")"); done

echo "Deploying: ${NAMES[*]}"
scp "${FILES[@]}" "$UNRAID:/tmp/"
# shellcheck disable=SC2145
ssh "$UNRAID" "
  for f in ${NAMES[*]}; do cp /tmp/\$f $PW_DIR/grafana/dashboards/\$f; done
  if [ '$SYNC_ALL' = 'true' ]; then
    for f in $PW_DIR/grafana/dashboards/*.json; do
      name=\$(basename \$f)
      case \" ${NAMES[*]} \" in *\" \$name \"*) ;; *) echo \"removing \$name\"; rm \$f ;; esac
    done
  fi
  chmod 644 $PW_DIR/grafana/dashboards/*.json
  cd $PW_DIR && docker compose -f powerwall.yml restart grafana
  echo done
"
