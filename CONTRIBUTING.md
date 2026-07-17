# Contributing

Thanks for your interest! This project aims to stay small and dependable —
it runs unattended in people's homes.

## Ground rules

- **Don't edit generated files** (`powerwall.yml`, `telegraf/telegraf.conf`,
  `grafana/dashboards/*.json`) — change the generators in `tools/` and run
  `./setup.sh -g`. Generated files carry a header saying so.
- **Keep the stack stdlib-only where it is today.** The generators run on any
  python3 with no pip installs; the webapp/enricher dependencies are pinned in
  their `requirements.txt`.
- **Every gateway count matters.** Changes must work for 1, 2, and 3 gateways.
  Quick check: `python3 tools/generate_stack.py --count N --out /tmp/gen-nN`
  for each N, then `docker compose -f /tmp/gen-nN/powerwall.yml config -q`.
- **Live data is sacred.** Nothing may require dropping or rewriting InfluxDB
  data. Schema additions go through `influxdb/influxdb.sql` and must be
  backward compatible.
- **The pypowerwall containers are fragile by nature** — gateways lock out
  rapid repeated auth attempts. Avoid changes that cause them to restart or
  recreate as a side effect.

## Testing a change

```bash
# webapp: run locally against no InfluxDB (endpoints degrade to 502s)
cd webapp && SETTINGS_PATH=/tmp/settings.json GATEWAY_TAGS=panel_a,panel_b python3 app.py

# settings/API surface
curl localhost:8050/api/settings

# generator round-trip for all counts
for n in 1 2 3; do python3 tools/generate_stack.py --count $n --out /tmp/gen-n$n; done
```

## Pull requests

- One change per PR, with a short description of the user-visible effect.
- If you touched the webapp, bump `VERSION` and add a `CHANGELOG` entry in
  `webapp/static/app.js`, and bump the `?v=` cache-busters in `index.html`
  for the files you changed (installed home-screen apps update via these).
