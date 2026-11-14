# pvswatch API v1

Everything a pvswatch screen shows comes from these endpoints, already worked out. The web dashboard and the native apps both read them, so no client repeats the math.

Base URL: wherever pvswatch runs, e.g. `https://pvswatch.your-tailnet.ts.net`. There's no auth of its own; keep it on a private network (Tailscale) only.

Sign conventions follow the PVS:

- grid `kw > 0` is importing, `< 0` exporting
- battery `kw > 0` is discharging, `< 0` charging
- `soc` is 0–1

Writes (`POST`/`DELETE`) are refused when a browser marks them cross-site (`Sec-Fetch-Site`). Native apps and `curl` send no such header.

## Endpoints

| Method | Path | What it returns |
|---|---|---|
| GET | `/api/v1/now` | The live screen: power, flow, battery, house sides, panels, electrical, system (below) |
| GET | `/api/v1/history?range=24h` | Time series + `period_totals`. Ranges: `1h 6h 24h 7d 30d 90d 1y all`, or `start`/`end` epochs |
| GET | `/api/v1/history/panels?range=24h` | Raw per-panel readings in the window |
| GET | `/api/v1/panels/<serial>/history?range=24h` | One panel: bucketed kWh, latest snapshot, total |
| GET | `/api/v1/outages?range=24h` | PVS-offline and grid outages, newest first, with a summary |
| GET | `/api/v1/sunvault` | Grid/backup state (`kind`: `on_grid` / `backup` / `unknown`), recent changes, `battery_mode` |
| POST | `/api/v1/battery/mode` | `{"mode": "SELF_CONSUMPTION" \| "ENERGY_ARBITRAGE" \| "BACKUP_ONLY"}`. Writes it to the PVS and reads it back. 400 on an unknown mode, 502 if the PVS refuses or the value doesn't stick |
| GET | `/api/v1/finance?group=month&spread=1` | Bills, no-solar estimates, net cost and savings |
| GET | `/api/v1/tou` | Rate plan, today's time-of-use segments, current period |
| GET | `/api/v1/health` | Cache status, history count, `history_earliest` |
| GET | `/api/v1/settings` | Editable settings with defaults, the battery mode, and what's configured (below) |
| PATCH | `/api/v1/settings` | `{KEY: value, …}`; `null` resets a key to its `.env` default. All-or-nothing: 400 on any bad value |

Battery modes, as the SunStrong app names them: `SELF_CONSUMPTION` is Self-Supply, `ENERGY_ARBITRAGE` is Cost Savings, `BACKUP_ONLY` is Reserve.

## `/api/v1/now`

`503 {"error": …}` until the first PVS fetch lands.

```jsonc
{
  "ts": 1794682636.9,          // when this was built
  "fetched_at": 1794682602.9,  // when pvswatch last fetched the PVS
  "data_time": 1794682501,     // the PVS meter's own timestamp
  "solar_kw": 1.377,
  "home_kw": 1.868,
  "grid": { "kw": 1.836, "direction": "importing", "source": "meter" },  // direction: importing | exporting | balanced (±50 W); source: meter | derived
  "battery": {                 // null when there's no battery reading in the last 10 min
    "kw": -1.345, "soc": 0.99, "backup_min": 744,
    "state": "charging",       // charging | discharging | idle (±50 W)
    "charge_source": "grid",   // solar | solar+grid | grid, while charging; else null
    "fetched_at": 1794682602.9,
    "stale": false,            // true = this refresh had no battery block, showing the last good one
    "mode": "ENERGY_ARBITRAGE", "mode_label": "Cost Savings"
  },
  "house_sides": {             // home load per 120 V leg; null without per-leg meter data
    "l1_kw": 0.475, "l2_kw": 1.384, "gap_kw": 0.909,
    "balanced": false,         // gap under 0.2 kW
    "battery_cover_kw": 0.85   // what an import-only PCS lets the battery cover (~2x the lighter leg − 0.1)
  },
  "flow": {
    "paths": {                 // kW along each arrow of the flow diagram
      "solar_home": 1.377, "solar_battery": 0, "solar_grid": 0,
      "battery_home": 0, "battery_grid": 0,
      "grid_home": 0.491, "grid_battery": 1.345
    },
    "headline": "Solar is powering your home · Grid is supplying your home · Grid is charging the battery"
  },
  "lifetime_kwh": 49204.0,
  "electrical": { "system_v": 236.4, "l1_v": 118.7, "l2_v": 117.6, "freq_hz": 60.0, "pf_solar": 0.994, "pf_home": 0.835 },
  "grid_state": { "kind": "on_grid", "midstate": 2 },
  "panels": {
    "mode": "producing",       // producing | night | idle | comms_error | none
    "summary": "27 panels",
    "count": 27, "errors": 0,
    "items": [{
      "serial": "<serial>", "model": "SPR-M425-H-AC", "firmware": "4.21.4", "state": "Working",
      "status": "ok",          // ok | idle | error
      "low": false,            // producing under 85% of the best panel
      "w": 312, "ac_v": 239.5, "dc_v": 38.9, "dc_a": 6.9, "temp_c": 38, "lifetime_kwh": 1743.5
    }]
  },
  "system": { "model": "PV Supervisor PVS6", "serial": "<serial>", "hw": "6.02", "firmware": "…", "state": "working",
              "mem_kib": 88824, "panel_id": 0, "ct_type": "NET_CONSUMPTION_LOADSIDE" }
}
```

After dark every SunPower inverter reports `STATE=error, OPERATION=noop`, because powerline comms need DC from the panels. With near-zero production, `/now` reports that as `panels.mode = "night"` with each panel `idle`, not as errors.

## `/api/v1/settings`

`.env` gives the defaults. A change is stored in the database and takes effect at once, with no restart. Secrets (PVS password, tokens, ntfy URLs) stay in `.env` only and are never returned.

```jsonc
{
  "settings": [
    { "key": "ALERT_STALE_MINS", "group": "Alerts", "label": "Alert when the PVS is silent for",
      "type": "int", "min": 5, "max": 1440, "unit": "min",
      "value": 30, "default": 30, "overridden": false }
    // ALERT_BATTERY_PCT, OUTAGE_GAP_MINS, COST_PER_KWH, TOU_RATES (type "json"),
    // CO2_LBS_PER_KWH, SOLAR_MONTHLY_COST, SOLAR_COST_START ("YYYY-MM"), UTILITY_API_SYNC_HOURS
  ],
  "battery_mode": { "value": "ENERGY_ARBITRAGE",
                    "options": { "SELF_CONSUMPTION": "Self-Supply", "ENERGY_ARBITRAGE": "Cost Savings", "BACKUP_ONLY": "Reserve" } },
  "status": { "mock": false, "pvs_configured": true, "alerts": true, "alerts_fallback": true,
              "ups_tracking": true, "utility_api": true, "refresh_secs": 300 }
}
```

Group settings by `group` and render the input from `type`, `min`, `max` and `unit`. The battery mode is changed with `POST /api/v1/battery/mode`, not here.

The legacy paths (`/devices`, `/history`, `/tou`, …) still work while the dashboard moves to v1.
