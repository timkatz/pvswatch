# SunPower Web Monitor

Dockerized web dashboard for monitoring SunPower PVS5/PVS6 solar systems on your local network.

## Quick Start

```bash
# 1. Copy and edit config
cp .env.example .env
# Edit .env with your PVS IP, serial number, etc.

# 2. Build and start
./sunpower.sh start

# 3. Open in browser
./sunpower.sh open
```

That's it — visit `http://localhost:5001/` and you'll see your solar dashboard. No URL parameters needed.

## Configuration

All config lives in `.env`:

| Variable | Default | Description |
|---|---|---|
| `PVS_IP` | — | PVS gateway IP address (required) |
| `PVS_USER` | `ssm_owner` | PVS auth username |
| `PVS_PASS` | — | PVS auth password (last 5 chars of internal serial) |
| `PVS_SERIAL` | — | Full internal serial (for reference) |
| `DASHBOARD_PORT` | `5001` | Host port for the dashboard |
| `TIMEOUT_SECS` | `60` | PVS request timeout |
| `LOG_LEVEL` | `INFO` | Python log level |

### Finding your PVS internal serial number

The password is **not** the serial printed on the unit's label — it's the last 5 characters of the PVS **internal** serial number. Find it by visiting:

```
http://<PVS_IP>/vars?name=/sys/info/serialnum
```

The value will look like `ZT00000000000000000` — take the last 5 characters (`W4730`) as the `PVS_PASS`.

## Commands

```bash
./sunpower.sh start     # Start the container (builds first if needed)
./sunpower.sh stop      # Stop the container
./sunpower.sh restart    # Restart the container
./sunpower.sh logs       # Follow container logs
./sunpower.sh status     # Show status and recent logs
./sunpower.sh url        # Print the dashboard URL
./sunpower.sh open       # Open dashboard in browser
./sunpower.sh update     # Pull latest dashboard HTML from GitHub and rebuild
./sunpower.sh build      # Build/rebuild the Docker image
./sunpower.sh help       # Show help
```

## How It Works

1. **`.env`** holds your PVS IP, username, and password
2. **`proxy.py`** (Flask) reads config from env, handles HTTPS authentication with the PVS gateway, and injects config into the dashboard HTML
3. Visiting `http://localhost:5001/` serves the dashboard with all params pre-configured — clean URL, no query string needed
4. The dashboard JS calls `/devices` on the same origin, which the proxy forwards to the PVS with auth
5. All communication is **local** — no cloud dependency

For **old firmware** (no auth required), the proxy falls back to plain HTTP without credentials.

## Files

| File | Purpose |
|---|---|
| `proxy.py` | Flask auth proxy + dashboard server |
| `solar_dashboard.html` | Dashboard UI (single HTML file) |
| `Dockerfile` | Container image definition |
| `docker-compose.yml` | Container orchestration (reads from `.env`) |
| `sunpower.sh` | Management script |
| `.env` | Your config (not tracked in git) |
| `.env.example` | Config template |
| `requirements.txt` | Python dependencies |

## Network

The PVS gateway is on your LAN (e.g. `192.168.1.50`). Docker Desktop on macOS routes container traffic to the host LAN automatically. If you have connectivity issues, uncomment `network_mode: host` in `docker-compose.yml` (and remove the `ports` section).