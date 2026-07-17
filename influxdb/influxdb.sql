# influxdb.sql — Powerwall Dashboard schema (1-3 gateways)
# Imported by setup.sh via: docker exec influxdb influx -import -path=/var/lib/influxdb/influxdb.sql
# Every measurement carries a `system` tag: panel_a [| panel_b [| panel_c]] | combined.
# The continuous queries GROUP BY system, so any gateway count flows through
# without schema changes. Combined metrics are written by the enricher every 60s.
#
# NOTE: influx -import executes one line at a time — all DDL must be single-line.
# CREATE CONTINUOUS QUERY blocks are written as single lines for this reason.

# DDL

CREATE DATABASE powerwall

# Retention Policies
CREATE RETENTION POLICY "raw"      ON "powerwall" DURATION 3d   REPLICATION 1
CREATE RETENTION POLICY "autogen"  ON "powerwall" DURATION INF  REPLICATION 1 DEFAULT
CREATE RETENTION POLICY "strings"  ON "powerwall" DURATION INF  REPLICATION 1
CREATE RETENTION POLICY "pwtemps"  ON "powerwall" DURATION INF  REPLICATION 1
CREATE RETENTION POLICY "vitals"   ON "powerwall" DURATION INF  REPLICATION 1
CREATE RETENTION POLICY "kwh"      ON "powerwall" DURATION INF  REPLICATION 1
CREATE RETENTION POLICY "daily"    ON "powerwall" DURATION INF  REPLICATION 1
CREATE RETENTION POLICY "monthly"  ON "powerwall" DURATION INF  REPLICATION 1
CREATE RETENTION POLICY "grid"     ON "powerwall" DURATION INF  REPLICATION 1
CREATE RETENTION POLICY "pod"      ON "powerwall" DURATION INF  REPLICATION 1
CREATE RETENTION POLICY "alerts"   ON "powerwall" DURATION INF  REPLICATION 1
CREATE RETENTION POLICY "fans"     ON "powerwall" DURATION INF  REPLICATION 1
CREATE RETENTION POLICY "enriched" ON "powerwall" DURATION INF  REPLICATION 1

# Continuous Queries — 1-minute means
CREATE CONTINUOUS QUERY "cq_autogen" ON "powerwall" BEGIN SELECT mean(home) AS home, mean(solar) AS solar, mean(from_pw) AS from_pw, mean(to_pw) AS to_pw, mean(from_grid) AS from_grid, mean(to_grid) AS to_grid, mean(percentage) AS percentage INTO "autogen"."http" FROM "raw"."http" GROUP BY time(1m), system, month, year END
CREATE CONTINUOUS QUERY "cq_autogen_current" ON "powerwall" BEGIN SELECT mean(home_current) AS home, mean(solar_current) AS solar, mean(battery_current) AS battery, mean(grid_current) AS grid INTO "autogen"."current" FROM "raw"."http" GROUP BY time(1m), system, month, year END
CREATE CONTINUOUS QUERY "cq_autogen_voltage" ON "powerwall" BEGIN SELECT mean(home_voltage) AS home, mean(solar_voltage) AS solar, mean(battery_voltage) AS battery, mean(grid_voltage) AS grid INTO "autogen"."voltage" FROM "raw"."http" GROUP BY time(1m), system, month, year END

# Continuous Queries — Hourly kWh
CREATE CONTINUOUS QUERY "cq_kwh" ON "powerwall" BEGIN SELECT integral(home)/3600000 AS home, integral(solar)/3600000 AS solar, integral(from_pw)/3600000 AS from_pw, integral(to_pw)/3600000 AS to_pw, integral(from_grid)/3600000 AS from_grid, integral(to_grid)/3600000 AS to_grid INTO "kwh"."http" FROM "autogen"."http" GROUP BY time(1h), system, month, year END

# Continuous Queries — Daily sums
CREATE CONTINUOUS QUERY "cq_daily" ON "powerwall" BEGIN SELECT sum(home) AS home, sum(solar) AS solar, sum(from_pw) AS from_pw, sum(to_pw) AS to_pw, sum(from_grid) AS from_grid, sum(to_grid) AS to_grid INTO "daily"."http" FROM "kwh"."http" GROUP BY time(1d), system, month, year END

# Continuous Queries — Monthly sums
CREATE CONTINUOUS QUERY "cq_monthly" ON "powerwall" BEGIN SELECT sum(home) AS home, sum(solar) AS solar, sum(from_pw) AS from_pw, sum(to_pw) AS to_pw, sum(from_grid) AS from_grid, sum(to_grid) AS to_grid INTO "monthly"."http" FROM "daily"."http" GROUP BY time(30d), system, month, year END

# Continuous Queries — Solar strings, temperatures, vitals
CREATE CONTINUOUS QUERY "cq_strings" ON "powerwall" BEGIN SELECT mean(*) INTO "strings"."strings" FROM "raw"."strings" GROUP BY time(1m), system, month, year END
CREATE CONTINUOUS QUERY "cq_pwtemps" ON "powerwall" BEGIN SELECT mean(*) INTO "pwtemps"."pwtemps" FROM "raw"."pwtemps" GROUP BY time(1m), system END
CREATE CONTINUOUS QUERY "cq_vitals"  ON "powerwall" BEGIN SELECT mean(*) INTO "vitals"."freq" FROM "raw"."freq" GROUP BY time(1m), system END

# Continuous Queries — Grid status, pod, alerts, fans
CREATE CONTINUOUS QUERY "cq_grid"   ON "powerwall" BEGIN SELECT min(from_grid) AS from_grid, min(to_grid) AS to_grid INTO "grid"."http" FROM "raw"."http" GROUP BY time(1m), system END
CREATE CONTINUOUS QUERY "cq_pod"    ON "powerwall" BEGIN SELECT mean(*) INTO "pod"."pod" FROM "raw"."pod" GROUP BY time(1m), system END
CREATE CONTINUOUS QUERY "cq_alerts" ON "powerwall" BEGIN SELECT max(*) INTO "alerts"."alerts" FROM "raw"."alerts" GROUP BY time(2m), system END
CREATE CONTINUOUS QUERY "cq_fans"   ON "powerwall" BEGIN SELECT mean(*) INTO "fans"."fans" FROM "raw"."fans" GROUP BY time(1m), system END

# Continuous Queries — Enriched 1-minute power means (preserves power-flow fields beyond raw's 3d window)
CREATE CONTINUOUS QUERY "cq_enriched_autogen" ON "powerwall" BEGIN SELECT mean(home) AS home, mean(solar) AS solar, mean(from_pw) AS from_pw, mean(to_pw) AS to_pw, mean(from_grid) AS from_grid, mean(to_grid) AS to_grid, mean(soc) AS soc, mean(soc_kwh) AS soc_kwh INTO "autogen"."enriched" FROM "raw"."enriched" GROUP BY time(1m), system END

# Continuous Queries — Enriched derived metrics rollups
CREATE CONTINUOUS QUERY "cq_enriched_hourly"   ON "powerwall" BEGIN SELECT mean(self_sufficiency) AS self_sufficiency, mean(grid_dependency) AS grid_dependency, integral(ev_load)/3600000 AS ev_kwh, sum(savings) AS savings INTO "kwh"."enriched" FROM "raw"."enriched" GROUP BY time(1h), system, month, year END
CREATE CONTINUOUS QUERY "cq_enriched_daily"    ON "powerwall" BEGIN SELECT mean(self_sufficiency) AS self_sufficiency, sum(ev_kwh) AS ev_kwh, sum(savings) AS savings INTO "daily"."enriched" FROM "kwh"."enriched" GROUP BY time(1d), system, month, year END
CREATE CONTINUOUS QUERY "cq_enriched_monthly"  ON "powerwall" BEGIN SELECT mean(self_sufficiency) AS self_sufficiency, sum(ev_kwh) AS ev_kwh, sum(savings) AS savings INTO "monthly"."enriched" FROM "daily"."enriched" GROUP BY time(30d), system, month, year END
