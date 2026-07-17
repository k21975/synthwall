#!/usr/bin/env bash
# tz.sh — Update timezone across all config files
# Usage: ./tz.sh "America/New_York"

set -euo pipefail

OLD_TZ="${1:-}"
NEW_TZ="${2:-}"

if [[ -z "$NEW_TZ" ]]; then
    NEW_TZ="$OLD_TZ"
    OLD_TZ=$(grep "^TZ=" compose.env 2>/dev/null | cut -d= -f2 || echo "America/Los_Angeles")
fi

if [[ -z "$NEW_TZ" ]]; then
    echo "Usage: $0 [old_tz] <new_tz>"
    echo "Example: $0 'America/New_York'"
    exit 1
fi

echo "Changing timezone from '$OLD_TZ' to '$NEW_TZ' ..."

FILES=(compose.env pypowerwall-a.env pypowerwall-b.env telegraf/telegraf.conf)

for f in "${FILES[@]}"; do
    [[ -f "$f" ]] && sed -i.bak "s|${OLD_TZ}|${NEW_TZ}|g" "$f" && rm -f "${f}.bak" && echo "  Updated: $f"
done

echo "Done. Restart services: docker compose -f powerwall.yml restart"
