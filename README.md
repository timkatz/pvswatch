# PVS Watch — Self-Hosted SunPower PVS5/PVS6 Solar Monitor

A self-hosted, single-container web dashboard for SunPower **PVS5 / PVS6** solar gateways. Talks to the gateway over your LAN, records its time-series data to SQLite, and serves a real-time dashboard with charts, per-panel detail, SunVault battery status, and a live energy-flow diagram. No cloud dependency — your data stays on your network.

![Dashboard overview](docs/screenshots/dashboard-overview.png)

<table>
<tr>
<td width="60%"><img src="docs/screenshots/live-flow.gif" alt="Live power-flow diagram"></td>
<td width="40%"><img src="docs/screenshots/mobile-overview.png" alt="Mobile view"></td>
</tr>
<tr>
<td align="center"><sub>Live energy-flow diagram (LIVE tab)</sub></td>
<td align="center"><sub>Responsive mobile layout</sub></td>
</tr>
</table>

## Why this exists

In 2024, **SunPower filed for bankruptcy** and was acquired by **SunStrong Management** (Complete Solaria), which now operates the residential fleet. As part of that transition, the legacy **MySunPower web dashboard was retired** and the official mobile app became the only sanctioned interface for homeowners.

The PVS gateway under your stairs/garage still produces excellent local telemetry — it just no longer has a vendor-supported way to view it on a computer. This project fills that gap.

## ⚠️ PVS6 firmware compatibility (read this first)

The PVS6 historically exposed a local HTTP API on a hidden installer access point. SunPower **progressively restricted local API access** in firmware updates throughout 2024, and after the SunStrong acquisition there is **no official path to downgrade firmware**. This monitor only works if your gateway's current firmware still serves the local API.

**Known-working range:** Firmware versions through approximately **2024.04**. Firmware **~2024.06 and later** has been reported to lock down or remove the endpoints this project depends on.

**Test before deploying** — from any machine on the same LAN as the PVS:

```bash
# Replace LAST5 with the last 5 chars of your PVS *internal* serial.
curl -u "ssm_owner:LAST5" http://<PVS_IP>/cgi-bin/dl_cgi/devices/list
```

- ✅ Returns a JSON device list after 30–60 seconds → you're good
- ❌ Returns 401/403/404, hangs, or refuses connection → your firmware likely no longer exposes the local API and this project will not work for you

## Features

- **Real-time hero KPIs** — Lifetime solar, plus Solar / Home / Battery / Grid for the selected window (LIVE / 1H / 6H / 24H / 7D / 30D / 90D / 1Y / ALL)
- **Energy independence %** — `solar_kwh ÷ home_kwh` for the selected window
- **Live power-flow diagram** — Home Assistant-style 4-node SVG with animated dots showing where energy is moving right now
- **POWER ↔ ENERGY chart toggle** — line chart of kW over time, or stacked bar chart of kWh per period
- **SunVault battery section** — state of charge, charge/discharge power, backup-time-remaining
- **Per-panel inverter status** — click a panel for a drill-down chart of its production over day/week/month
- **Savings card** — dollars saved, lbs CO₂ avoided, trees-equivalent, miles-not-driven, gallons-not-used (configurable rate / emissions factor)
- **Net Cost & Savings card** — your utility bills plus a fixed monthly solar payment, next to an estimate of what the house would have cost with no solar. Answers "is solar actually saving me money?" at a glance, with month / quarter / year / NEM-year views, change vs. the previous period and vs. last year, and dated notes on the chart. Bills come from uploaded PDFs (SCE layout parsed automatically), manual entry, or a [UtilityAPI](https://utilityapi.com) sync
- **Nighttime detection** — when DC powerline comms drop after dark, panels report `STATE=error` even though nothing is wrong; the dashboard recognizes this pattern and shows "🌙 Nighttime idle" instead of a red banner
- **Optional Tailscale sidecar** — secure HTTPS access from anywhere on your tailnet without opening a router port

## Architecture

```
Browser ──► (optional Tailscale HTTPS) ──► Flask proxy (5001) ──► PVS gateway (HTTPS)
                                                  │
                                          SQLite history DB
```

- **`proxy.py`** — Flask app: handles HTTP Basic auth against the PVS, caches `/devices` data, runs a background poll thread that records to SQLite, exposes the JSON API the dashboard consumes, and serves the dashboard HTML
- **`solar_dashboard.html`** — Single-file HTML/CSS/JS dashboard (Chart.js for the time-series chart, vanilla SVG for the flow diagram)
- **`docker-compose.yml`** — The monitor container plus an optional Tailscale sidecar
- **SQLite** — `solar_history.db` in the data volume; one row per refresh in `readings`, plus per-panel rows in `panel_readings`

A background thread inside `proxy.py` polls the PVS every `REFRESH_SECS` (default 300s = 5 min). The PVS scan cycle is ~40–60 min so meter values change at most that often, but the `/sys/livedata/` endpoint refreshes every few seconds and is the freshest source for kW readings.

## Quick start

### Prerequisites

- A SunPower **PVS5 or PVS6** with firmware that still exposes the local HTTP API (see compatibility section above)
- Network reachability from the host running this container to the PVS gateway IP
- Docker + Docker Compose
- The PVS internal serial number (see below)

### Find your PVS internal serial number

The PVS auth password is **not** the serial printed on the unit's label — it's the **last 5 characters of the internal serial**. Find it from any LAN-connected machine:

```bash
curl http://<PVS_IP>/cgi-bin/dl_cgi/getvarserver?vars=/sys/info/serialnum
# or:
curl 'http://<PVS_IP>/vars?name=/sys/info/serialnum'
```

The response will look like `ZTxxxxxxxxxxxxxxxxx`. Take the **last 5 characters** — that's your `PVS_PASS`.

### 1. Configure

```bash
cp .env.example .env
# Edit .env and set at minimum:
#   PVS_IP   = your PVS LAN IP (e.g. 192.168.1.50)
#   PVS_PASS = the last 5 chars of the internal serial
```

### 2. Start the container

**Option A — convenience wrapper (recommended):**

```bash
./pvswatch.sh start    # builds and starts
./pvswatch.sh logs     # follow logs
./pvswatch.sh open     # open the dashboard in your browser
```

**Option B — Docker Compose directly:**

```bash
docker compose up -d --build
```

**Option C — pull the prebuilt image** (skip the local build):

A multi-arch (`linux/amd64`, `linux/arm64`) image is published to GitHub Container Registry on every release tag. Edit `docker-compose.yml` to replace the `build: .` line on the `pvswatch` service with:

```yaml
image: ghcr.io/timkatz/pvswatch:latest
```

then:

```bash
docker compose pull && docker compose up -d
```

Pin to a specific release if you'd rather not auto-roll: `ghcr.io/timkatz/pvswatch:2026.04.25.3` (full version), `:2026.04.25` (latest release of that day), or `:2026` (latest of that year).

The dashboard is now at `http://localhost:5001/` (or whatever `DASHBOARD_PORT` you set).

The first request can take 60–90 seconds — the PVS `/devices/list` endpoint is famously slow, and the proxy waits for the first poll to complete before serving data.

### 3. (Optional) HTTPS access from anywhere via Tailscale

The included `tailscale` sidecar in `docker-compose.yml` will expose the dashboard at `https://<TS_HOSTNAME>.<your-tailnet>.ts.net/` from any device on your tailnet. On first run, check the sidecar logs for the auth URL:

```bash
docker logs pvswatch-tailscale
```

If you don't want Tailscale, simply remove or comment out the `tailscale:` service in `docker-compose.yml`.

## Configuration

All settings come from `.env`. Required fields are marked.

| Variable | Default | Description |
|---|---|---|
| `PVS_IP` | — | **Required.** PVS gateway IP on your LAN |
| `PVS_USER` | `ssm_owner` | PVS auth username (always this for SunPower) |
| `PVS_PASS` | — | **Required.** Last 5 chars of internal serial |
| `DASHBOARD_PORT` | `5001` | Host port for the dashboard |
| `TIMEOUT_SECS` | `120` | PVS request timeout (`/devices/list` is slow) |
| `REFRESH_SECS` | `300` | Background poll interval (5 min recommended) |
| `DATA_DIR` | `./data` | Bind-mount path for SQLite + Tailscale state |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `COST_PER_KWH` | `0.30` | Utility rate ($/kWh) for the Savings card |
| `CO2_LBS_PER_KWH` | `0.85` | Grid emissions factor for the Savings card |
| `NTFY_URL` | — | Optional [ntfy](https://ntfy.sh) topic URL. Push alert when the PVS stops responding, and again when it recovers |
| `NTFY_TOKEN` | — | Optional ntfy access token for protected topics |
| `NTFY_EMAIL` | — | Optional: ntfy also forwards each alert to this email address |
| `NTFY_FALLBACK_URL` / `NTFY_FALLBACK_TOKEN` | — | Optional second ntfy server, tried only if the primary fails |
| `TOU_RATES` | — | Optional time-of-use plan as JSON (see `.env.example`). Makes "Cost saved" the real bill difference by time of day; overrides `COST_PER_KWH` |
| `ALERT_STALE_MINS` | `30` | Minutes without fresh PVS data before alerting |
| `MIDSTATE_POLL_SECS` | `60` | How often to check SunVault's grid/backup state; alerts on every change (`0` = off) |
| `SOLAR_MONTHLY_COST` | `0` | Fixed monthly solar payment (lease / PPA / loan), added to each bill on the Net Cost card |
| `SOLAR_COST_START` | — | First month (`YYYY-MM`) that payment applies to; blank = every bill |
| `UTILITY_API_TOKEN` | — | Optional [UtilityAPI](https://utilityapi.com) API token. Syncs utility bills and 15-min meter data daily |
| `UTILITY_API_SYNC_HOURS` | `24` | UtilityAPI sync interval (`0` = only via `POST /utilityapi/sync`) |
| `UTILITY_API_REFRESH_DAY` | `24` | Day of the month to ask UtilityAPI for one fresh collection from the utility (`0` = off). Runs at most once per calendar month, since only the first collection each month is free |
| `ALERT_BATTERY_PCT` | `20` | Alert when usable battery charge drops below this % (`0` = off); recovery alert at +5 |
| `OUTAGE_GAP_MINS` | `20` | A gap in history longer than this counts as a "PVS offline" outage |
| `UPS_LOG_DIR` | — | Host directory with `apcupsd.events` (e.g. `/var/log`) for grid power outage tracking |
| `TS_HOSTNAME` | `pvswatch` | Tailscale device name (sidecar only) |
| `TS_AUTHKEY` | — | Optional Tailscale auth key for unattended startup |

⚠️ **Polling rate**: don't set `REFRESH_SECS` lower than 300 (5 min). Each poll involves the slow `/devices/list` call (~30–45 s); going faster causes overlapping requests and added load on the gateway.

## API endpoints

The Flask app exposes these JSON endpoints — useful for Home Assistant, Grafana, scripts, etc.

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard HTML |
| `GET /devices` | Latest cached PVS device data; includes a top-level `battery` object (`p_kw`, `soc`, `backup_min`, `lifetime_kwh`) and `grid` object (`p_kw`, `lifetime_kwh`). Sign convention: `battery.p_kw > 0` = discharging; `grid.p_kw > 0` = importing |
| `GET /history?range=24h` | Bucketed time-series + a `period_totals` block (kWh per source, savings, CO₂, independence %, etc.). Ranges: `1h`, `6h`, `24h`, `7d`, `30d`, `90d`, `1y`, `all` |
| `GET /history/panels?range=24h` | Per-panel raw readings for the range (large response) |
| `GET /panel/<serial>/history?range=24h` | Per-panel drilldown: bucketed kWh, latest snapshot, total — used by the dashboard panel modal |
| `GET /outages?range=24h` | PVS-offline (history gaps) and grid power (UPS) outages in the window, newest first, plus per-kind count/total. PVS events carry a `cause` from the PVS uptime: `pvs_restart` (rebooted / lost power) or `network` (stayed up, so WiFi/network); `null` for history recorded before uptime tracking |
| `GET /finance?group=month` | Net cost report: bills grouped by `month`, `quarter`, `year` or `nemyear`, each with utility cost, solar payment, no-solar estimate, savings and change vs. the previous period / last year, plus a 12-bill headline and dated notes. `spread=0` shows NEM settle-ups as billed instead of spread over their NEM year |
| `GET /bills`, `POST /bills`, `DELETE /bills/<date>` | List / add / delete utility bills. POST takes bill PDFs (multipart `file=`) or JSON |
| `POST /usage` | Import a Green Button usage download (XML or CSV) |
| `POST /annotations`, `DELETE /annotations` | Dated notes shown on the Net Cost chart |
| `POST /utilityapi/sync` | Run a UtilityAPI sync now |
| `GET /health` | Cache freshness, history row count, and `history_earliest` timestamp |

## Database schema

SQLite, schema created on first start:

- **`readings`** — one row per poll: `production_kw`, `consumption_kw`, `net_kw`, `lifetime_kwh`, voltage / frequency / power-factor, panel health counts, and battery columns (`battery_kw`, `battery_soc`, `backup_min`, `battery_lifetime_kwh`, `home_lifetime_kwh`, `grid_lifetime_kwh`)
- **`panel_readings`** — one row per inverter per poll: `serial`, `model`, `state`, `watts`, DC/AC voltage, current, temperature, `lifetime_kwh`
- **`bills`** — one row per utility bill (`bill_date` key): billing period, `amount`, NEM `settlement` included in it, net / import / export kWh, fixed `base_charge`, `source`
- **`utility_intervals`** — meter readings from Green Button or UtilityAPI: `ts`, `secs`, `delivered_kwh`, `received_kwh`
- **`annotations`** — dated notes (`date`, `label`, `detail`)

Schema migrations are idempotent `ALTER TABLE` calls in `_init_db()` — safe to run on existing databases.

**Sign conventions** (verified empirically):
- `net_kw > 0` = importing from grid; `< 0` = exporting (matches PVS `net_p`)
- `battery_kw > 0` = discharging; `< 0` = charging

## Local management script

`pvswatch.sh` is a thin wrapper around `docker compose`:

```
build     Build (or rebuild) the Docker image
start     Start the container (builds first if needed)
stop      Stop and remove the container
restart   Stop and start the container
status    Show container status and recent logs
logs      Follow container logs
url       Print the dashboard URL
open      Open the dashboard in your browser
update    git pull origin and rebuild the container
help      Show this help
```

## Troubleshooting

### "Dashboard not loaded" / 500 on first request
The first poll takes 30–90 s because `/devices/list` is slow. Wait, then refresh.

### 401/403 from the PVS
- Confirm `PVS_PASS` is the **last 5 of the internal serial**, not the label serial
- Confirm you can reach the PVS at `PVS_IP` from the host running the container (`ping`, `curl`)
- Confirm your firmware version still allows local API access (see compatibility section)

### Connection refused / hangs
- Verify the PVS is on the same network and the IP hasn't changed (set a DHCP reservation)
- The container runs on Docker's default bridge — confirm the host (and its bridge NAT) can reach the PVS at `PVS_IP`
- Some PVS units only expose the API on the **installer port** (an Ethernet jack inside the unit) rather than the customer-facing LAN; in that case you may need a small Linux box (Raspberry Pi works) attached to that port

### Dashboard shows "🌙 Nighttime idle"
This is normal. SunPower micro-inverters communicate over the DC powerline; once panels stop producing, comms drop and every panel reports `STATE=error`. The dashboard detects this pattern and treats it as expected nighttime behavior.

### Polling is loading my PVS too much
Increase `REFRESH_SECS` in `.env` and restart. The default is 5 min, which is comparable to what the SunPower app does. Don't go below 300.

## Related projects

If PVS Watch's standalone dashboard isn't quite what you're after, the PVS community has built other tools that talk to the same local API and may be a better fit:

- **[smcneece/ha-esunpower](https://github.com/smcneece/ha-esunpower)** — A native Home Assistant integration. If you already run HA, this surfaces production / consumption / battery / per-panel data as HA sensors directly, no proxy in between. PVS Watch and ha-esunpower can run side-by-side against the same PVS without conflict.
- **[thomastech/SunPower-Web-Monitor](https://github.com/thomastech/SunPower-Web-Monitor)** — The original lightweight web monitor that this project descended from.
- **[scott1howard-cba/SunPower-PVS-Exporter](https://github.com/scott1howard-cba/SunPower-PVS-Exporter)** — Prometheus exporter for the PVS local API; pair with Grafana for time-series dashboards.

## Credits

The original PVS dashboard concept was popularized by **[thomastech/SunPower-Web-Monitor](https://github.com/thomastech/SunPower-Web-Monitor)** — early versions of this project started from that codebase. PVS Watch has since diverged significantly (rewritten Flask proxy, SQLite history, SunVault battery support, energy-flow diagram, period-totals API, per-panel drilldown, etc.) but the lineage is acknowledged.

Reverse-engineering of PVS endpoints draws on years of community work in the [SunPower local-API discussions](https://github.com/scott1howard-cba/SunPower-PVS-Exporter/) and various forum threads.

## Contributing

Issues and PRs welcome. If you've successfully tested with a specific firmware version (working *or* not), please open an issue noting your firmware build so we can keep the compatibility section accurate.

## Disclaimer

This project is **not affiliated with, endorsed by, or sponsored by** SunPower Corporation, SunStrong Management, Complete Solaria, or any of their subsidiaries. "SunPower", "PVS", "PVS5", "PVS6", and "SunVault" are trademarks of their respective owners. This is an independent, community-developed monitoring tool. Use at your own risk; the authors accept no liability for any damage to your equipment or warranty implications of accessing the local API.

## License

MIT — see [LICENSE](LICENSE).
