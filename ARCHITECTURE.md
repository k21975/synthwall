# Architecture Decision Record: Dual-Gateway Powerwall Dashboard

**Date:** 2026-05-19  
**Last updated:** 2026-05-20 — switched from FleetAPI to local wired TEDAPI  
**Status:** Accepted  
**Context:** Aggregating two independent Tesla Powerwall 3 systems (each on a separate 200A panel, combined 400A service) into a single Grafana dashboard on an Unraid server. Both gateways are hardwired to the LAN with reserved IPs and confirmed reachable over HTTPS.

---

## Options Evaluated

### Option A — Two pypowerwall proxies + Python aggregation service
Two `pypowerwall` containers (ports 8675 and 8676), plus a Python service that polls both proxies and writes per-system and combined metrics to a single InfluxDB.

### Option B — Two independent pypowerwall + Telegraf stacks with tags
Two complete stacks (pypowerwall + Telegraf each), both writing into the same InfluxDB with `system=panel_a` / `system=panel_b` tags. Aggregation done in Grafana/InfluxDB queries only.

### Option C — Single custom Python poller via FleetAPI / TEDAPI directly
One Python service that authenticates to each gateway directly (either FleetAPI cloud or TEDAPI local), normalizes data, and writes everything to InfluxDB with system tags, bypassing pypowerwall entirely.

### Option D — Enhanced Option A (Selected)
Two pypowerwall proxies + **single Telegraf instance** polling both (with per-gateway system tags) + InfluxDB continuous queries for combined rollups + lightweight Python enricher for derived metrics only.

---

## Decision: Option D — Enhanced Option A

### Architecture Diagram

```
  Local LAN (wired ethernet, reserved IPs, HTTPS/TEDAPI)
  ┌───────────────────┐          ┌───────────────────┐
  │   Gateway A       │          │   Gateway B       │
  │   (Panel A)       │          │   (Panel B)       │
  │   192.168.x.x     │          │   192.168.x.x     │
  └────────┬──────────┘          └────────┬──────────┘
           │ HTTPS + gateway pwd           │ HTTPS + gateway pwd
           ▼                              ▼
  ┌───────────────┐         ┌───────────────┐
  │ pypowerwall-a │         │ pypowerwall-b │
  │  port 8675    │         │  port 8676    │
  └───────┬───────┘         └───────┬───────┘
          │                          │
          └────────────┬─────────────┘
                       │  polls both every 5s
                       ▼         with system tags
               ┌───────────────┐
               │    Telegraf   │
               │ (single inst) │
               └───────┬───────┘
                       │
                       ▼
               ┌───────────────┐
               │   InfluxDB    │◄─── Enricher (derived metrics,
               │               │               combined totals,
               │  CQs compute  │               self-sufficiency,
               │  combined raw │               EV load)
               └───────┬───────┘
                       │
                       ▼
               ┌───────────────┐
               │    Grafana    │
               │  port 9000    │
               └───────────────┘

  [Sense placeholder — see SENSE_INTEGRATION.md]
```

### Why Not the Other Options

**Option B (two Telegraf instances)** was rejected because:
- Duplicate Telegraf overhead with identical config except for the target host
- No place to compute combined metrics without Grafana `math` transforms, which are brittle and don't persist to InfluxDB (no historical combined rollups)
- Two Telegraf health surfaces to monitor instead of one

**Option C (custom FleetAPI poller)** was rejected because:
- Reimplements TEDAPI authentication, session management, and retry logic that pypowerwall already handles
- Requires maintaining low-level gateway HTTPS protocol handling rather than using pypowerwall's stable REST API abstraction
- Higher maintenance burden; pypowerwall upstream improvements (new Powerwall firmware support, etc.) would not flow through

**Pure Option A (separate Python aggregation service polling proxies)** was rejected in favor of Option D because:
- Telegraf already knows how to poll HTTP endpoints with tags — adding a Python middle layer just for collection is redundant
- Keeps collection in Telegraf (single tool, unified config) and reserves Python for computation that Telegraf can't do

---

## Option D: Detailed Design

### Data Source: Local Wired TEDAPI

Both gateways are connected via **wired ethernet** to the home LAN with reserved DHCP IPs. pypowerwall operates in TEDAPI mode (`PW_HOST` + `PW_GW_PWD`), authenticating directly to each gateway over HTTPS using the hardware password printed on the unit.

**Why TEDAPI over wired ethernet, not FleetAPI cloud:**
- No Tesla developer account, OAuth app, or internet dependency for data collection
- Lower latency — sub-second round trip vs. cloud API polling
- No token refresh failures or cloud rate limits
- Works even if the Tesla cloud or FleetAPI is temporarily unavailable
- TEDAPI provides higher-fidelity data including string-level solar, temperatures, and pod metrics

**Firmware note:** As of firmware 25.10.0, Tesla removed LAN routing via `192.168.91.1` (the old WiFi-side gateway address). This installation is unaffected because it uses the **physical wired ethernet port** on each gateway, which has a dedicated reserved IP independent of the WiFi-side address.

### Data Collection Layer (Telegraf)

A single Telegraf instance contains two sets of `[[inputs.http]]` blocks — one per gateway — each with a `[inputs.http.tags]` stanza that injects `system = "panel_a"` or `system = "panel_b"`. Every measurement written to InfluxDB carries this tag, making gateway-level filtering trivial:

```
WHERE system = 'panel_a'        # Panel A only
WHERE system = 'panel_b'        # Panel B only  
WHERE system = 'combined'       # Enricher-written combined metrics
```

If one gateway's pypowerwall goes down, Telegraf simply stops writing for that `system` tag — the other continues unaffected.

### Aggregation Layer (InfluxDB Continuous Queries)

InfluxDB continuous queries (CQs) compute 1-minute averages and hourly/daily/monthly rollups exactly as jasonacox does, but scoped per `system` tag, operating on the per-gateway `http` measurement (`panel_a` / `panel_b`).

The `system=combined` view is computed by the **enricher** (not by CQs), which sums `panel_a` and `panel_b` for power fields and weights SOC by capacity, then writes a `combined` series into the `enriched` measurement. The enricher writes only to the `raw` policy (3-day retention), so `cq_enriched_autogen` downsamples the enriched power-flow fields (`home, solar, from_pw, to_pw, from_grid, to_grid, soc, soc_kwh`) into `autogen.enriched` at 1-minute resolution — this is what retains combined **and** per-panel power history beyond 3 days. Grafana history panels read from `autogen.enriched`; real-time (`now()-10m`) panels read from `raw.enriched` for freshest resolution.

### Enricher Service (Python)

A small Python service (`enricher/`) runs on a 60-second interval and writes derived metrics that require computation across fields:

- **Self-sufficiency %**: `(solar + from_pw) / home * 100` (clamped 0–100)
- **Grid dependency %**: `100 - self_sufficiency`
- **EV charger load**: estimated from home load vs. baseline (configurable threshold), written as a separate measurement
- **Savings estimate**: `to_grid * rate_export + (home - from_grid) * rate_import`
- **Combined SOC (kWh)**: `soc_a * capacity_a + soc_b * capacity_b` (capacity from .env)

The enricher reads from InfluxDB (latest raw values) and writes back to InfluxDB. It uses exponential backoff when either gateway is unreachable and continues writing for the available gateway.

### Storage Layer (InfluxDB 1.8)

Retention policies and measurement schema are extended from jasonacox with one addition: the `system` tag on every measurement. No schema change is required for existing jasonacox measurements — the tag is additive.

| Retention Policy | Duration | Purpose |
|---|---|---|
| `raw` | 3 days | 5-second Telegraf writes + 60s enricher writes (`enriched`) |
| `autogen` | forever | 1-minute means of `http` and `enriched` power fields (CQ) |
| `kwh` | forever | Hourly kWh integrals (CQ) |
| `daily` | forever | Daily sums (CQ) |
| `monthly` | forever | Monthly sums (CQ) |
| `strings` | forever | Solar string data |
| `pwtemps` | forever | Powerwall temperatures |
| `enriched` | forever | Derived-metric rollups (self-sufficiency, savings, ev_kwh) in `kwh`/`daily`/`monthly`; full-res enricher output is in `raw` (3d), power downsampled to `autogen` (forever) |

### Failure Modes

| Scenario | Behavior |
|---|---|
| Gateway A pypowerwall down | Telegraf stops writing `system=panel_a`; Panel A dashboard shows "no data"; Panel B and combined (from last-known) continue |
| Gateway B pypowerwall down | Symmetric with above |
| Gateway A loses LAN connectivity | pypowerwall-a logs HTTPS errors, retries; Telegraf receives errors from pypowerwall-a and stops writing panel_a metrics |
| Gateway B loses LAN connectivity | Symmetric with above |
| Enricher down | Derived metrics (self-sufficiency, savings) stale; raw metrics unaffected |
| InfluxDB down | Telegraf buffers up to `metric_buffer_limit` writes in memory; enricher retries with backoff |
| Both gateways unreachable | Dashboard shows "no data"; InfluxDB historical data intact |

### Compatibility with jasonacox

This architecture is a superset of jasonacox:
- Same core stack: pypowerwall + Telegraf + InfluxDB + Grafana
- Same pypowerwall TEDAPI mode and REST API endpoints
- Same InfluxDB measurement names (with added `system` tag)
- Same Grafana port (9000) and color conventions
- `setup.sh` mirrors jasonacox's interactive flow

---

## Evaluation Matrix

| Criterion | Option A | Option B | Option C | **Option D** |
|---|---|---|---|---|
| Reliability | ★★★★ | ★★★★ | ★★★ | **★★★★★** |
| Maintainability | ★★★ | ★★★★ | ★★ | **★★★★★** |
| Grafana query complexity | ★★★★ | ★★ | ★★★★ | **★★★★★** |
| Sense future-proofing | ★★★★ | ★★★ | ★★★ | **★★★★★** |
| jasonacox compatibility | ★★★★ | ★★★★ | ★ | **★★★★★** |
| Complexity (lower=better) | ★★★ | ★★★★ | ★★ | **★★★★** |

---

## Consequences

- **Positive**: One Telegraf config to maintain; failure isolation by tag; combined historical data stored (not computed); enricher is a small, testable Python module
- **Negative**: Enricher adds a 6th container; combined metrics have 60s latency vs. 5s for raw metrics (acceptable for dashboard use)
- **Mitigated**: If Enricher is down, Grafana can still compute self-sufficiency at query time as a fallback (documented in dashboard annotations)
