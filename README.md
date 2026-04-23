# SunStrong — SunPower PVS5/PVS6 Solar Monitor

Dockerized web dashboard for monitoring SunPower PVS5/PVS6 solar systems. Features real-time monitoring, time-series history with interactive charts, and Tailscale HTTPS exposure.

**Live**: https://sunstrong.your-tailnet.ts.net/

## Quick Start

### Local (macOS)

```bash
# 1. Copy and edit config
cp .env.example .env
# Edit .env with your PVS IP, serial number, etc.

# 2. Build and start
./sunpower.sh start

# 3. Open in browser
./sunpower.sh open
```

### Your-server (Unraid)

```bash
# On your-server
cd /mnt/user/appdata/sunstrong/repo
docker compose up -d --build
```

The first Tailscale launch requires browser authentication — check `docker logs sunstrong-tailscale` for the auth URL.

## Features

- **Real-time dashboard** — production, consumption, net power, voltage, frequency
- **Inverter panel status** — per-panel detail with nighttime detection (PVS6 reports panels as "Error" after dark — this is normal and shown as 🌙 Idle)
- **Time-series history** — SQLite-backed, records every refresh cycle
- **Interactive charts** — Chart.js power graph with 1H / 6H / 24H / 7D / 30D range selection
- **Tailscale HTTPS** — secure remote access via tailnet with automatic TLS
- **PVS6 meter supplementation** — injects meter data from the varserver API when missing

## Configuration

All config lives in `.env`:

| Variable | Default | Description |
|---|---|---|
| `PVS_IP` | — | PVS gateway IP address (required) |
| `PVS_USER` | `ssm_owner` | PVS auth username |
| `PVS_PASS` | — | PVS auth password (last 5 chars of internal serial) |
| `PVS_SERIAL` | — | Full internal serial (for reference) |
| `DASHBOARD_PORT` | `5002` | Host port for the dashboard |
| `TIMEOUT_SECS` | `120` | PVS request timeout (dl_cgi can be very slow) |
| `REFRESH_SECS` | `60` | Background refresh interval |
| `DATA_DIR` | `./data` | Directory for SQLite history database |
| `TS_HOSTNAME` | `sunstrong` | Tailscale hostname |
| `LOG_LEVEL` | `INFO` | Python log level |

### Finding your PVS internal serial number

The password is **not** the serial printed on the unit's label — it's the last 5 characters of the PVS **internal** serial number. Find it by visiting:

```
http://<PVS_IP>/vars?name=/sys/info/serialnum
```

The value will look like `ZT00000000000000000` — take the last 5 characters (`W4730`) as the `PVS_PASS`.

## API Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard HTML |
| `GET /devices` | Current PVS device data (JSON) |
| `GET /history?range=24h` | Time-series readings with auto-resampling |
| `GET /history/panels?range=24h` | Per-panel history |
| `GET /health` | Cache status, history count |

History ranges: `1h`, `6h`, `24h`, `7d`, `30d`

## Architecture

```
 Browser → Tailscale (HTTPS) → Flask proxy (auth) → PVS gateway (HTTPS)
                                      ↓
                              SQLite history DB
```

1. **`proxy.py`** (Flask) reads config from env, handles HTTPS auth with the PVS gateway, injects config into the dashboard HTML
2. Background thread refreshes PVS data every 60s, records to SQLite, caches for instant `/devices` responses
3. Dashboard JS calls `/devices` and `/history` on the same origin
4. Tailscale sidecar provides HTTPS with auto-TLS at `sunstrong.<tailnet>.ts.net`
5. All communication is **local** — no cloud dependency

## Local Commands

```bash
./sunpower.sh start     # Start the container (builds first if needed)
./sunpower.sh stop      # Stop the container
./sunpower.sh restart    # Restart the container
./sunpower.sh logs       # Follow container logs
./sunpower.sh status     # Show status and recent logs
./sunpower.sh url        # Print the dashboard URL
./sunpower.sh open       # Open dashboard in browser
./sunpower.sh build      # Build/rebuild the Docker image
./sunpower.sh help       # Show help
```

## Files

| File | Purpose |
|---|---|
| `proxy.py` | Flask auth proxy + history recording + dashboard server |
| `solar_dashboard.html` | Dashboard UI with Chart.js (single HTML file) |
| `Dockerfile` | Container image definition |
| `docker-compose.yml` | Container orchestration + Tailscale sidecar |
| `sunpower.sh` | Local management script |
| `.env` | Your config (not tracked in git) |
| `.env.example` | Config template |

## Network

The PVS gateway is on your LAN (e.g. `192.168.1.50`). The container uses `network_mode: host` on Unraid for direct LAN access. On macOS, Docker Desktop routes container traffic automatically.