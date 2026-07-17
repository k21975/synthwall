# TEDAPI Setup — Local Wired Ethernet

This dashboard uses pypowerwall's **TEDAPI mode** to talk directly to each Powerwall 3 gateway over your local wired ethernet. No Tesla developer account, no OAuth, no internet dependency for data collection.

---

## What You Need

For each gateway:
1. **Reserved LAN IP** — static/DHCP-reserved so the address never changes
2. **Gateway hardware password** — printed on the sticker on the Powerwall 3 unit (or visible in the Tesla app under Gateway → Settings → Advanced)
3. **Wired ethernet connection** from each gateway to your LAN switch

That's it. No Tesla account credentials are required.

---

## Pre-flight: Verify Gateway Reachability

Before running `setup.sh`, confirm each gateway responds on HTTPS:

```bash
# Panel A
curl -k --head https://192.168.x.x

# Panel B
curl -k --head https://192.168.x.x
```

You should get an `HTTP/2 404` (or similar non-connection-error response). The 404 on the root path is expected — the gateway's web server is running but has no root handler. What matters is that the TCP handshake and TLS negotiation succeed.

---

## Setup

Run the interactive setup script:

```bash
./setup.sh
```

When prompted, enter:
- **Gateway LAN IP** for Panel A (e.g. `192.168.1.100`)
- **Gateway hardware password** for Panel A
- Same for Panel B

The script writes these into `pypowerwall-a.env` and `pypowerwall-b.env` and immediately checks connectivity.

---

## TEDAPI Authentication Test

After setup, you can validate that pypowerwall can authenticate to each gateway and retrieve live data:

```bash
./setup.sh -t
```

This runs `python3 -m pypowerwall.tedapi` inside a temporary pypowerwall container for each gateway, prints the raw TEDAPI response, and exits. Expected output includes live power flow values.

If this fails, check:
1. Is the gateway IP correct and reachable? (`curl -k --head https://<IP>`)
2. Is the gateway password correct? (check the sticker — it is case-sensitive)
3. Is there a firewall rule blocking the Docker host from the gateway IP?

---

## How TEDAPI Authentication Works

pypowerwall in TEDAPI mode:
1. Connects to `https://<PW_HOST>/tedapi/v1` over TLS (self-signed cert — `-k` skips verification)
2. Sends the gateway password in the Authorization header
3. Receives a session token valid for the container's lifetime
4. Uses the token for all subsequent data requests

No tokens are written to disk. If the container restarts, pypowerwall re-authenticates automatically on the next poll.

---

## Firmware Note: 192.168.91.1 is Gone

As of Powerwall firmware **25.10.0**, Tesla removed the LAN routing that previously allowed access via `192.168.91.1` (the WiFi-side gateway address). This installation is **not affected** because it uses the **physical wired ethernet port** on each gateway, which has its own independent LAN IP. The wired port remains accessible.

If you ever need to access a gateway without its wired ethernet port, you would need to return to FleetAPI (cloud) mode. See the archived `docs/FLEETAPI_SETUP.md.archived` for reference.

---

## Changing a Gateway IP or Password

Edit the relevant `.env` file:

```bash
# Example: change Panel A gateway IP
nano pypowerwall-a.env
# Update PW_HOST= and optionally PW_GW_PWD=

# Restart only the affected container
docker compose -f powerwall.yml restart pypowerwall-a
```

Also update `compose.env` (`PW_HOST_A` / `PW_GW_PWD_A`) to keep it in sync for `setup.sh -t`.

---

## Troubleshooting

**`curl -k --head https://<IP>` hangs or times out**
- Verify the IP is correct: check your router's DHCP table for the MAC address on the gateway's ethernet port
- Verify the cable is plugged into the **wired ethernet port** on the gateway, not the WiFi antenna port
- Try pinging the IP first: `ping -c4 <IP>`

**`./setup.sh -t` fails with authentication error**
- Double-check the gateway password character by character — it is case-sensitive and can contain special characters
- Try entering the password manually: passwords with `$`, `!`, or `\` may need to be escaped in shell input

**Grafana shows "No Data" after setup**
- Wait 2 minutes — the pypowerwall container takes up to 60s to authenticate and begin serving
- Check pypowerwall logs: `docker compose -f powerwall.yml logs pypowerwall-a`
- Verify Telegraf is writing: `docker compose -f powerwall.yml logs telegraf | grep -i error`
- Query InfluxDB directly: `docker exec influxdb influx -database powerwall -execute "SHOW MEASUREMENTS"`
