# Upgrading

## Standard upgrade

```bash
git pull
./setup.sh -u
```

`-u` pulls the latest images, rebuilds the two locally-built images (enricher,
webapp), and does a rolling `up -d`. Your data, env files, `gateways.json`,
and `data/webapp/settings.json` are untouched.

Notes:
- `up -d` only recreates containers whose config/image changed. The
  pypowerwall containers rarely change; when they do, that is the **one**
  recreate they get (see the FAQ lockout warning).
- If the release notes mention regenerated configs, run `./setup.sh -g` after
  pulling so `powerwall.yml` / `telegraf.conf` / dashboards match your
  `gateways.json` again — then `up -d`.

## Schema changes

Additions to `influxdb/influxdb.sql` (new continuous queries, retention
policies) are applied with:

```bash
./setup.sh -i
```

This re-imports the schema file; existing data is never dropped. Continuous
queries only run forward — release notes will say if a backfill command is
needed for history.

## Rollback

Images are tagged `latest`, so the reliable rollback is git:

```bash
git log --oneline          # find the previous release
git checkout <commit>
./setup.sh -u
```

Your InfluxDB data is forward-compatible; the settings file has a `version`
field and old app versions ignore unknown keys.
