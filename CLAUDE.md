# CLAUDE.md — SunStrong

## Project Overview

SunStrong is a Dockerized solar monitor for **SunPower PVS5/PVS6** gateway systems. It consists of:

- **`proxy.py`** — Flask server that authenticates with the PVS gateway, proxies device data, records time-series history to SQLite, and serves the dashboard
- **`solar_dashboard.html`** — Single-file dashboard UI (HTML/CSS/JS with Chart.js)
- **`docker-compose.yml`** — Two services: the monitor app + a Tailscale sidecar for HTTPS

## Architecture

```
Browser → Tailscale (HTTPS :443) → proxy.py (:5002) → PVS gateway (HTTPS :443)
                                         ↓
                                  SQLite (solar_history.db)
```

- **Data collection**: A background thread in proxy.py fetches from PVS every `REFRESH_SECS` (900s/15min) and records to SQLite. Runs 24/7 regardless of browser — it's a Python thread in the Flask process, not tied to HTTP connections. The PVS scan cycle is ~40-60 min, so polling faster gives diminishing returns.
- **PVS is slow**: The `/cgi-bin/dl_cgi/devices/list` endpoint takes 30-45s to respond. Timeout is 120s. First dashboard load waits up to 90s for cached data.
- **Network mode**: `network_mode: host` on Unraid for direct LAN access to PVS gateway.

## Key PVS6 Quirks

- **Authentication**: PVS6 requires HTTP Basic auth with `ssm_owner` and the last 5 chars of the internal serial number (not the label serial). Password is stored in `.env`.
- **Powerline comms**: SunPower inverters use DC powerline communication. After dark, panels report `STATE=error, OPERATION=noop` because comms need DC voltage. This is normal nighttime behavior — the dashboard detects this pattern (all panels error + near-zero production) and shows "🌙 Nighttime idle" instead of a red error banner.
- **Meter supplementation**: PVS6 sometimes omits meter devices from `/devices/list`. The proxy fetches from `/vars?match=/&fmt=obj&cache=mdata` (varserver) and injects synthetic `PVS5-METER-P/C` devices when missing.
- **Session management**: The proxy maintains a requests Session with cookies. On 401/403 or connection errors, it re-authenticates automatically.

## Deployment

### Your-server (Unraid)

```bash
cd /mnt/user/appdata/sunstrong/repo
docker compose up -d --build
```

- Port 5002 (5001 is taken by GluetunVPN on your-server)
- Data: `/mnt/user/appdata/sunstrong/data/solar_history.db`
- Tailscale state: `/mnt/user/appdata/sunstrong/tailscale-state/`
- First Tailscale launch requires browser auth — check `docker logs sunstrong-tailscale` for the URL
- Remote access: `https://sunstrong.your-tailnet.ts.net/`

### Local (macOS)

```bash
cp .env.example .env  # Edit with PVS credentials
./sunpower.sh start
```

Uses `uv run` locally. Port 5001 by default (not inside Docker).

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard HTML |
| `GET /devices` | Current PVS device data (JSON, served from cache) |
| `GET /history?range=24h` | Aggregated time-series with auto-resampling |
| `GET /history/panels?range=24h` | Per-panel raw readings |
| `GET /health` | Cache status, history entry count |

History ranges: `1h`, `6h`, `24h`, `7d`, `30d`

Resampling aims for ~300 data points per range.

## Database Schema

Two SQLite tables in `solar_history.db`:

- **`readings`** — One row per refresh: production/consumption kW, voltage, frequency, power factor, panel health counts
- **`panel_readings`** — Per-inverter: serial, model, state, watts, DC/AC voltage, current, temperature

Timestamps are Unix epochs (REAL). Both tables use PRIMARY KEY constraints for dedup.

## Making Changes

### proxy.py changes
1. Edit locally
2. `git commit` + `git push`
3. On your-server: `cd /mnt/user/appdata/sunstrong/repo && git pull && docker compose up -d --build`

### solar_dashboard.html changes
Same workflow — it's loaded from disk at startup, so a container rebuild picks up changes.

### Database migrations
SQLite schema is created on first start. For schema changes, add a migration in `_init_db()` using `ALTER TABLE` with try/except for idempotency.

## Environment Variables

Key variables in `.env`:

| Variable | Default | Notes |
|---|---|---|
| `PVS_IP` | — | Required. PVS gateway IP on LAN |
| `PVS_USER` | `ssm_owner` | SunPower auth username |
| `PVS_PASS` | — | Last 5 chars of internal serial |
| `DASHBOARD_PORT` | `5002` | 5002 on your-server (5001 elsewhere) |
| `TIMEOUT_SECS` | `120` | PVS is slow |
| `REFRESH_SECS` | `900` | Background refresh interval (15 min) |
| `DATA_DIR` | `./data` | SQLite persistence volume mount |
| `TS_HOSTNAME` | `sunstrong` | Tailscale hostname |

## Repository

- GitHub: https://github.com/timkatz/sunstrong (private)
- Git ignores: `.env`, `.env.*`, `solar_history.db`, `*.pdf`