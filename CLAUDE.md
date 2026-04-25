# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

- **Data collection**: A background thread in proxy.py fetches from PVS every `REFRESH_SECS` (300s / 5 min default) and records to SQLite. Runs 24/7 regardless of browser — it's a Python thread in the Flask process, not tied to HTTP connections. The PVS scan cycle is ~40-60 min, so meter values only change that often, but `/sys/livedata/` updates every few seconds so 5 min polling does pick up fresher kW numbers in practice.
- **PVS is slow**: The `/cgi-bin/dl_cgi/devices/list` endpoint takes 30-45s to respond. Timeout is 120s. First dashboard load waits up to 90s for cached data.
- **Network mode**: `network_mode: host` on Unraid for direct LAN access to PVS gateway.

## Key PVS6 Quirks

- **Authentication**: PVS6 requires HTTP Basic auth with `ssm_owner` and the last 5 chars of the internal serial number (not the label serial). Password is stored in `.env`.
- **Data freshness**: The PVS6 has different data endpoints with different update cadences:
  - `/sys/livedata/` — Real-time power (pv_p, site_load_p, net_p). Updates every few seconds but values only change on the PVS scan cycle (~40-60 min). This is the freshest source for kW readings.
  - `/sys/devices/transfer_switch/` — Voltage data. Updates every ~5 minutes.
  - `/sys/devices/meter/` — Full meter data (power, voltage, PF, frequency). **Only updates on PVS scan cycle** (~40-60 min). Stale for hours between scans. Do NOT rely on this for real-time values.
  - `/cgi-bin/dl_cgi/devices/list` — Device list including inverters. Takes 30-45 seconds to respond. Contains panel states but no real-time power data for inverters.
- **Powerline comms**: SunPower inverters use DC powerline communication. After dark, panels report `STATE=error, OPERATION=noop` because comms need DC voltage. This is normal nighttime behavior — the dashboard detects this pattern (all panels error + near-zero production) and shows "🌙 Nighttime idle" instead of a red error banner.
- **Refresh interval**: 5 min (300s, see `REFRESH_SECS`). Community standard for PVS monitoring — the SunPower app polls similarly. Each poll makes 3-4 HTTP requests including the slow `/devices/list` (~30-45s), so the PVS spends ~10-15% of its time answering you. Harmless but noticeable. Don't go below 5 min or requests will start overlapping.
- **Meter supplementation**: PVS6 sometimes omits meter devices from `/devices/list`. The proxy fetches from `/vars?match=/&fmt=obj&cache=mdata` (varserver) and injects synthetic `PVS5-METER-P/C` devices when missing, then overrides power values from livedata.
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

`sunpower.sh` is the local management wrapper around `docker compose`: `start`, `stop`, `restart`, `status`, `logs`, `url`, `open`, `build`, `update` (the last fetches upstream `solar_dashboard.html` from the original `thomastech/SunPower-Web-Monitor` repo and rebuilds; it does NOT auto-overwrite our customized `proxy.py` — it diffs and asks). Port 5001 by default locally; 5002 in the Your-server `docker-compose.yml` since 5001 is taken there.

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard HTML |
| `GET /devices` | Current PVS device data (JSON, from cache). Includes a top-level `battery` object: `{ p_kw, soc, backup_min, lifetime_kwh, midstate }` (sign convention: `p_kw > 0` = discharging). |
| `GET /history?range=24h` | Aggregated time-series with auto-resampling; also returns `period_totals` (see below). |
| `GET /history/panels?range=24h` | Per-panel raw readings (all panels, all rows in range — large response). |
| `GET /panel/<serial>/history?range=24h` | Per-panel drilldown: bucketed kWh per period (5-min/1h/6h/1d auto by range), latest snapshot, total_kwh. Used by panel modal. |
| `GET /health` | Cache status, history entry count, `history_earliest` (used by dashboard to gate which range tabs are enabled) |

History ranges: `1h`, `6h`, `24h`, `7d`, `30d`, `90d`, `1y`, `all` (where `all` is computed dynamically from earliest reading).

Resampling aims for ~300 data points per range.

`period_totals` shape (when there's data in the window):

```jsonc
{
  "solar_kwh": 50.4,        // production over period (lifetime_kwh delta when available)
  "home_kwh": 10.9,
  "battery_net_kwh": -29.9, // signed: positive = net discharge, negative = net charge
  "grid_net_kwh": 12.2,     // signed: positive = exported, negative = imported (PVS net_p convention)
  "grid_import_kwh": 0,     // = max(0, -grid_net_kwh)
  "grid_export_kwh": 12.2,  // = max(0,  grid_net_kwh)
  "avoided_kwh": 10.9,      // min(solar_kwh, home_kwh)
  "savings_dollars": 3.27,  // avoided_kwh × COST_PER_KWH
  "co2_lbs": 42.8,          // solar_kwh × CO2_LBS_PER_KWH
  "trees_equivalent": 0.9,  // co2_lbs / 48
  "miles_not_driven": 48,   // co2_lbs / 0.89
  "gallons_not_used": 2.2,  // co2_lbs / 19.6
  "independence_pct": 462,  // solar_kwh / home_kwh × 100
  "period_start": 1777..., "period_end": 1777..., "elapsed_hours": 24.0
}
```

## Database Schema

Two SQLite tables in `solar_history.db`:

- **`readings`** — One row per refresh: production/consumption kW, **net_kw (PVS net_p convention: + = exporting to grid, - = importing)** verified via energy balance `pv + battery_discharge - load = net_p`, voltage, frequency, power factor, panel health counts, and battery columns: `battery_kw` (`+` = discharging, `-` = charging), `battery_soc` (0–1), `backup_min`, `battery_lifetime_kwh`. Battery columns added 2026-04-25 via idempotent `ALTER TABLE` in `_init_db`.
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
| `REFRESH_SECS` | `300` | Background refresh interval (5 min) |
| `DATA_DIR` | `./data` | SQLite persistence volume mount |
| `TS_HOSTNAME` | `sunstrong` | Tailscale hostname |

## Repository

- GitHub: https://github.com/timkatz/sunstrong (private)
- Git ignores: `.env`, `.env.*`, `solar_history.db`, `*.pdf`