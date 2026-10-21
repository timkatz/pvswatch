#!/usr/bin/env python3
"""
proxy.py — Flask proxy + static server for solar_dashboard.html

Proxies authenticated requests to SunPower PVS6 gateway, supplements PVS6
meter data from the varserver API, and serves the dashboard HTML with
server-side config baked in.

Features:
  - Background data refresh: caches PVS data and refreshes every 60s
  - Instant /devices response from cache (stale-while-revalidate)
  - PVS6 meter supplementation via varserver
  - Time-series history via SQLite (/history endpoint)
  - Clean URL: no query params needed, config from .env

Configuration is read from environment variables (set via .env file):
  PVS_IP         - PVS gateway IP address
  PVS_USER       - PVS auth username (default: ssm_owner)
  PVS_PASS       - PVS auth password (last 5 chars of internal serial)
  PORT           - Server port (default: 5001)
  TIMEOUT_SECS   - PVS request timeout (default: 120)
  REFRESH_SECS   - Background refresh interval (default: 300)
  NTFY_URL       - ntfy topic URL for stale-data alerts (optional)
  NTFY_TOKEN     - ntfy access token, if the topic requires auth (optional)
  NTFY_EMAIL     - Also have ntfy forward alerts to this email (optional)
  NTFY_FALLBACK_URL / NTFY_FALLBACK_TOKEN - Second ntfy server, used if the primary fails (optional)
  ALERT_STALE_MINS - Minutes without fresh data before alerting (default: 30)
  MIDSTATE_POLL_SECS - How often to check SunVault's grid/backup state (default: 60, 0 = off)
  ALERT_BATTERY_PCT - Alert when usable battery charge drops below this % (default: 20, 0 = off)
  LOG_LEVEL      - Python log level (default: INFO)
"""

from flask import Flask, request, Response, send_file, jsonify
import requests
import json
import math
import os
import time
import logging
import urllib3
import re
import threading
import sqlite3
from datetime import datetime, timedelta, timezone

# Ignore insecure HTTPS warnings for self-signed gateway certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Configuration from environment ──────────────────────────────────────
PVS_IP = os.environ.get("PVS_IP", "")
PVS_USER = os.environ.get("PVS_USER", "ssm_owner")
PVS_PASS = os.environ.get("PVS_PASS", "")
TIMEOUT_SECS = int(os.environ.get("TIMEOUT_SECS", "120"))
PORT = int(os.environ.get("PORT", "5001"))
HOST = os.environ.get("HOST", "0.0.0.0")
REFRESH_SECS = int(os.environ.get("REFRESH_SECS", "300"))
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "solar_history.db"))
# MOCK_PVS=1 swaps the live-gateway path for fixture-based responses + a
# pre-seeded synthetic history. Used by the local test rig (no LAN access
# to a real PVS needed). See fixtures/ and pvswatch.sh test.
MOCK_PVS = os.environ.get("MOCK_PVS", "0") == "1"
FIXTURES_DIR = os.environ.get("FIXTURES_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures"))

# Outage tracking. PVS/WiFi outages are derived from gaps in `readings`
# longer than OUTAGE_GAP_MINS. Grid power outages come from an apcupsd
# events log (optional; e.g. a UPS on the host running pvswatch).
OUTAGE_GAP_MINS = int(os.environ.get("OUTAGE_GAP_MINS", "20"))
UPS_EVENTS_FILE = os.environ.get("UPS_EVENTS_FILE", "").strip()

# Savings model: avoided cost = min(solar_kwh, home_kwh) * COST_PER_KWH
# CO2 model: solar_kwh * CO2_LBS_PER_KWH (full production offsets grid CO2)
# Stale-data alert: push to an ntfy topic when no successful PVS refresh
# for ALERT_STALE_MINS, and again when data comes back. Disabled if unset.
NTFY_URL = os.environ.get("NTFY_URL", "").strip()
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "").strip()
NTFY_EMAIL = os.environ.get("NTFY_EMAIL", "").strip()  # ntfy also forwards to this address
# Optional second ntfy server, used only if the primary fails (down,
# unreachable, auth error) — e.g. self-hosted primary + ntfy.sh fallback.
NTFY_FALLBACK_URL = os.environ.get("NTFY_FALLBACK_URL", "").strip()
NTFY_FALLBACK_TOKEN = os.environ.get("NTFY_FALLBACK_TOKEN", "").strip()
ALERT_STALE_MINS = int(os.environ.get("ALERT_STALE_MINS", "30"))
# Low-battery alert: usable charge below this % (0 disables). Recovery fires
# 5 points above it so a reading hovering at the line doesn't flap.
ALERT_BATTERY_PCT = float(os.environ.get("ALERT_BATTERY_PCT", "20"))
# SunVault grid/backup state (livedata midstate) is polled on its own, faster
# loop so a short outage isn't missed between 5-min refreshes.
MIDSTATE_POLL_SECS = int(os.environ.get("MIDSTATE_POLL_SECS", "60"))
COST_PER_KWH = float(os.environ.get("COST_PER_KWH", "0.30"))
CO2_LBS_PER_KWH = float(os.environ.get("CO2_LBS_PER_KWH", "0.85"))

# Logging
log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, log_level, logging.INFO), format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

def _sanitize_nonfinite(obj):
    """Recursively replace NaN/±Inf floats with None. Python's json module
    emits bare NaN/Infinity tokens, which browsers reject as invalid JSON.
    The PVS reports e.g. livedata soc=NaN right after a reboot before the
    battery has re-registered."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanitize_nonfinite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_nonfinite(v) for v in obj]
    return obj


def _json_dumps(obj, **kw):
    return json.dumps(_sanitize_nonfinite(obj), **kw)


app = Flask(__name__, static_folder=".")


class _SafeJSONProvider(app.json_provider_class):
    def dumps(self, obj, **kw):
        return super().dumps(_sanitize_nonfinite(obj), **kw)


app.json = _SafeJSONProvider(app)
sessions = {}

# ── SQLite time-series storage ─────────────────────────────────────────
_db_lock = threading.Lock()

def _init_db():
    """Create the history tables if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Main readings table — one row per refresh cycle
    c.execute("""CREATE TABLE IF NOT EXISTS readings (
        ts REAL PRIMARY KEY,
        production_kw REAL,
        consumption_kw REAL,
        net_kw REAL,
        lifetime_kwh REAL,
        sys_v REAL,
        l1_v REAL,
        l2_v REAL,
        freq_hz REAL,
        pf_production REAL,
        pf_consumption REAL,
        num_panels INTEGER,
        panels_working INTEGER,
        panels_error INTEGER
    )""")
    # Per-panel readings
    c.execute("""CREATE TABLE IF NOT EXISTS panel_readings (
        ts REAL,
        serial TEXT,
        panel_model TEXT,
        state TEXT,
        watts REAL,
        v_dc REAL,
        i_dc REAL,
        v_ac REAL,
        temp_c REAL,
        PRIMARY KEY (ts, serial)
    )""")
    # Grid power outages ingested from the UPS events log. end NULL = ongoing.
    c.execute("""CREATE TABLE IF NOT EXISTS grid_outages (
        start REAL PRIMARY KEY,
        end REAL,
        source TEXT
    )""")
    # SunVault MID state changes (livedata midstate), from _midstate_watch.
    c.execute("""CREATE TABLE IF NOT EXISTS midstate_events (
        ts REAL PRIMARY KEY,
        value INTEGER,
        prev INTEGER
    )""")
    # Idempotent migrations.
    # battery_*  added 2026-04-25 — sign: battery_kw > 0 = discharging.
    # home_lifetime_kwh / grid_lifetime_kwh added 2026-04-25 — cumulative
    # counters from livedata.site_load_en / net_en. Used for accurate period
    # totals (delta over window) instead of AVG×hours integration.
    for col, sql_type in (
        ("battery_kw", "REAL"),
        ("battery_soc", "REAL"),
        ("backup_min", "REAL"),
        ("battery_lifetime_kwh", "REAL"),
        ("home_lifetime_kwh", "REAL"),
        ("grid_lifetime_kwh", "REAL"),
        # pvs_uptime_s added 2026-10-19 — PVS dl_uptime (seconds since boot).
        # Lets /outages tell a PVS restart from a WiFi/network gap.
        ("pvs_uptime_s", "REAL"),
        # midstate added 2026-10-21 — livedata midstate (Hub+/MIDC state).
        # 2 = SunVault thinks the grid is down (the SunStrong app's outage
        # banner); 0/1 seen while grid-connected.
        ("midstate", "INTEGER"),
    ):
        try:
            c.execute(f"ALTER TABLE readings ADD COLUMN {col} {sql_type}")
            logger.info("Migrated readings: added column %s", col)
        except sqlite3.OperationalError:
            pass
    # lifetime_kwh on panel_readings — captures inverter ltea_3phsum_kwh.
    try:
        c.execute("ALTER TABLE panel_readings ADD COLUMN lifetime_kwh REAL")
        logger.info("Migrated panel_readings: added column lifetime_kwh")
    except sqlite3.OperationalError:
        pass
    # Cumulative-counter cleanup (#1): legacy rows stored 0 when the source
    # value was missing (panel in nighttime error/noop state, or livedata
    # absent). 0 is "no data," not a real reading — convert to NULL so MIN/MAX
    # skip the rows. Idempotent; no-op once the data is clean.
    cleanups = (
        ("panel_readings", "lifetime_kwh"),
        ("readings", "lifetime_kwh"),
        ("readings", "home_lifetime_kwh"),
        ("readings", "grid_lifetime_kwh"),
        ("readings", "battery_lifetime_kwh"),
    )
    for table, col in cleanups:
        try:
            r = c.execute(f"UPDATE {table} SET {col} = NULL WHERE {col} = 0")
            if r.rowcount:
                logger.info("Cleaned %s zero rows in %s.%s → NULL", r.rowcount, table, col)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()
    logger.info("History database initialized at %s", DB_PATH)


def _cumulative(v):
    """Coerce a cumulative-counter value (kWh, etc.) to float, returning None
    for missing/zero. A 0 here almost always means "no data this poll" (e.g.
    panel in nighttime error/noop state) rather than a real zero — see #1.
    Storing None instead lets SQL MIN/MAX skip the rows naturally."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _pvs_uptime_now(pvs_dev):
    """PVS uptime in seconds as of now. dl_uptime is only refreshed with the
    rest of the device list (DATATIME, UTC "YYYY,MM,DD,HH,MM,SS"), so add the
    time elapsed since then. None if the PVS entry has no uptime."""
    try:
        up = float(pvs_dev["dl_uptime"])
    except (TypeError, ValueError, KeyError):
        return None
    try:
        dt = datetime.strptime(pvs_dev.get("DATATIME", ""), "%Y,%m,%d,%H,%M,%S").replace(tzinfo=timezone.utc)
        up += max(0.0, time.time() - dt.timestamp())
    except ValueError:
        pass
    return up


def _record_reading(devices_json_str, livedata=None):
    """Parse device data and record a time-series reading.
    If livedata is provided, uses fresh pv_p/site_load_p values."""
    try:
        data = json.loads(devices_json_str) if isinstance(devices_json_str, str) else devices_json_str
    except (json.JSONDecodeError, TypeError):
        return

    devices = data.get("devices", [])
    meter_p = next((d for d in devices if d.get("TYPE") == "PVS5-METER-P"), None)
    meter_c = next((d for d in devices if d.get("TYPE") == "PVS5-METER-C"), None)
    inverters = [d for d in devices if d.get("TYPE") == "SOLARBRIDGE"]

    # Battery (SunVault/Equinox) — pulled from livedata only.
    # Sign convention: battery_kw > 0 = discharging, < 0 = charging.
    battery_kw = float(livedata.get("ess_p", 0)) if livedata else 0
    battery_soc = float(livedata.get("soc", 0)) if livedata else 0
    # Prefer the response's battery.soc, which falls back to the per-unit
    # average when livedata's aggregate is stuck at 0 (see _inject_battery_units).
    resp_soc = (data.get("battery") or {}).get("soc")
    if isinstance(resp_soc, (int, float)) and math.isfinite(resp_soc):
        battery_soc = float(resp_soc)
    backup_min = float(livedata.get("backupTimeRemaining", 0)) if livedata else 0
    midstate = (data.get("battery") or {}).get("midstate")
    battery_lifetime_kwh = _cumulative(livedata.get("ess_en")) if livedata else None
    home_lifetime_kwh = _cumulative(livedata.get("site_load_en")) if livedata else None
    grid_lifetime_kwh = _cumulative(livedata.get("net_en")) if livedata else None

    # Prefer livedata for real-time power, fall back to meter values
    if livedata:
        production = float(livedata.get("pv_p", 0))
        consumption = float(livedata.get("site_load_p", 0))
        net = float(livedata.get("net_p", 0))
        lifetime = _cumulative(livedata.get("pv_en"))
        # Fresh voltage from transfer switch if available
        if livedata.get("v12_v") is not None:
            sys_v = float(livedata["v12_v"])
        if livedata.get("v1n_v") is not None:
            l1_v = float(livedata["v1n_v"])
        if livedata.get("v2n_v") is not None:
            l2_v = float(livedata["v2n_v"])
    else:
        p_kw = float(meter_p.get("p_3phsum_kw", 0) if meter_p else 0)
        c_kw = float(meter_c.get("p_3phsum_kw", 0) if meter_c else 0)
        production = p_kw if p_kw > 0.005 else 0
        consumption = c_kw if c_kw > 0.005 else 0
        net = production - consumption
        lifetime = _cumulative(meter_p.get("net_ltea_3phsum_kwh") if meter_p else None)
    sys_v = float(meter_c.get("v12_v", 0) if meter_c else 0)
    l1_v = float(meter_c.get("v1n_v", 0) if meter_c else 0)
    l2_v = float(meter_c.get("v2n_v", 0) if meter_c else 0)
    freq = float(meter_c.get("freq_hz", 0) if meter_c else 0)
    pf_p = float(meter_p.get("tot_pf_rto", 0) if meter_p else 0)
    pf_c = float(meter_c.get("tot_pf_rto", 0) if meter_c else 0)
    pvs_uptime = _pvs_uptime_now(next((d for d in devices if d.get("DEVICE_TYPE") == "PVS"), None))
    num_panels = len(inverters)
    panels_working = sum(1 for i in inverters if (i.get("STATEDESCR") or i.get("STATE") or "").lower() == "working")
    panels_error = num_panels - panels_working

    ts = time.time()
    now = datetime.now(timezone.utc).isoformat()

    try:
        with _db_lock:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("""INSERT OR REPLACE INTO readings
                (ts, production_kw, consumption_kw, net_kw, lifetime_kwh,
                 sys_v, l1_v, l2_v, freq_hz, pf_production, pf_consumption,
                 num_panels, panels_working, panels_error,
                 battery_kw, battery_soc, backup_min, battery_lifetime_kwh,
                 home_lifetime_kwh, grid_lifetime_kwh, pvs_uptime_s, midstate)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, production, consumption, net, lifetime,
                 sys_v, l1_v, l2_v, freq, pf_p, pf_c,
                 num_panels, panels_working, panels_error,
                 battery_kw, battery_soc, backup_min, battery_lifetime_kwh,
                 home_lifetime_kwh, grid_lifetime_kwh, pvs_uptime, midstate))
            # Per-panel data
            for inv in inverters:
                serial = inv.get("SERIAL", "unknown")
                state = inv.get("STATEDESCR") or inv.get("STATE") or "unknown"
                kw = float(inv.get("p_3phsum_kw", 0) or 0)
                watts = round(kw * 1000)
                v_dc = float(inv.get("v_mppt1_v", 0) or 0)
                i_dc = float(inv.get("i_mppt1_a", 0) or 0)
                v_ac = float(inv.get("vln_3phavg_v", 0) or 0)
                temp = float(inv.get("t_htsnk_degc", 0) or 0)
                lifetime = _cumulative(inv.get("ltea_3phsum_kwh"))
                c.execute("""INSERT OR REPLACE INTO panel_readings
                    (ts, serial, panel_model, state, watts, v_dc, i_dc, v_ac, temp_c, lifetime_kwh)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ts, serial, inv.get("PANEL") or inv.get("MODEL") or "", state, watts, v_dc, i_dc, v_ac, temp, lifetime))
            conn.commit()
            conn.close()
    except Exception as e:
        logger.warning("Failed to record history: %s", e)


# ── Data cache ─────────────────────────────────────────────────────────
class DataCache:
    """Thread-safe cache for PVS device data. Refreshes in background."""
    def __init__(self):
        self._lock = threading.Lock()
        self._data = None
        self._last_fetch = 0
        self._fetching = False
        self._last_error = None

    def get(self):
        with self._lock:
            return self._data

    def set(self, data):
        with self._lock:
            self._data = data
            self._last_fetch = time.time()

    @property
    def last_fetch(self):
        with self._lock:
            return self._last_fetch

    @property
    def last_error(self):
        with self._lock:
            return self._last_error

    @property
    def fetching(self):
        with self._lock:
            return self._fetching

    @fetching.setter
    def fetching(self, val):
        with self._lock:
            self._fetching = val

    def set_error(self, err):
        with self._lock:
            self._last_error = err


cache = DataCache()

# ── Dashboard HTML (loaded once at startup) ───────────────────────────
DASHBOARD_HTML = None


def _load_dashboard():
    """Read solar_dashboard.html and inject server-side config so the
    dashboard works at / with no URL parameters visible in the browser.
    The dashboard JS reads from window.__SOLAR_CONFIG__ and merges it
    with URL params, so no query string is needed."""
    global DASHBOARD_HTML
    with open("solar_dashboard.html", "r") as f:
        DASHBOARD_HTML = f.read()

    if PVS_IP:
        # Only the IP (shown in the system info card). Never inject user/pass:
        # the browser doesn't need them and they'd leak into page source and
        # request logs.
        inject = (
            "<script>\n"
            "  // Server-side config injected by proxy from .env\n"
            f"  window.__SOLAR_CONFIG__ = {{ ip: {json.dumps(PVS_IP)} }};\n"
            "</script>\n"
        )
        head_pos = DASHBOARD_HTML.find("<head>")
        if head_pos != -1:
            insert_pos = head_pos + len("<head>")
            DASHBOARD_HTML = DASHBOARD_HTML[:insert_pos] + "\n" + inject + DASHBOARD_HTML[insert_pos:]

    logger.info("Dashboard HTML loaded (%d bytes), config: ip=%s user=%s pass=%s",
                len(DASHBOARD_HTML), PVS_IP, PVS_USER,
                "set" if PVS_PASS else "not set")


# ── Varserver: fetch live and cached data ─────────────────────────────
def _fetch_vars(sess, ip):
    """Fetch all varserver data and return a dict of key->value.
    Includes both cached meter data and real-time livedata."""
    try:
        r = sess.get(
            f"https://{ip}/vars?match=/&fmt=obj&cache=mdata",
            verify=False,
            timeout=TIMEOUT_SECS,
        )
        if r.status_code != 200:
            logger.warning("Varserver returned HTTP %s", r.status_code)
            return {}
        return r.json()
    except Exception as e:
        logger.warning("Varserver fetch error: %s", e)
        return {}


def _fetch_ess_vars(sess, ip):
    """Fetch just /sys/devices/ess/ vars (per-unit battery detail).
    Lightweight — ~40 keys total for a 2-unit SunVault."""
    try:
        r = sess.get(
            f"https://{ip}/vars?match=/sys/devices/ess/&fmt=obj",
            verify=False,
            timeout=15,
        )
        if r.status_code != 200:
            return {}
        return r.json()
    except Exception as e:
        logger.debug("ESS vars fetch error: %s", e)
        return {}


def _fetch_livedata(sess, ip):
    """Fetch real-time power data from /sys/livedata/ vars.
    This is the fast, fresh endpoint — updated every few seconds.
    Also includes /sys/devices/transfer_switch/ for fresh voltage data.
    Returns a dict with pv_p, site_load_p, net_p, pv_en, site_load_en, etc.
    plus v1n_v, v2n_v, v12_v from the transfer switch if available."""
    try:
        # Fetch both livedata and transfer switch voltage in one request
        r = sess.get(
            f"https://{ip}/vars?match=/sys/livedata/&fmt=obj",
            verify=False,
            timeout=30,
        )
        if r.status_code != 200:
            logger.warning("Livedata returned HTTP %s", r.status_code)
            return {}
        raw = r.json()
        # Strip the /sys/livedata/ prefix
        livedata = {k.replace("/sys/livedata/", ""): v for k, v in raw.items() if k.startswith("/sys/livedata/")}

        # Also fetch transfer switch data for fresh voltage (separate fast request)
        try:
            r2 = sess.get(
                f"https://{ip}/vars?match=/sys/devices/transfer_switch/&fmt=obj",
                verify=False,
                timeout=15,
            )
            if r2.status_code == 200:
                ts_data = r2.json()
                # Extract voltage values and add to livedata
                for k, v in ts_data.items():
                    if "v1nV" in k or "v2nV" in k or "v1nGridV" in k or "v2nGridV" in k:
                        # Map: /sys/devices/transfer_switch/0/v1nV → v1n_v (fresh)
                        short_key = k.split("/")[-1]
                        # Convert camelCase to snake_case for dashboard compatibility
                        key_map = {
                            "v1nV": "v1n_v",
                            "v2nV": "v2n_v",
                            "v1nGridV": "v1n_grid_v",
                            "v2nGridV": "v2n_grid_v",
                        }
                        if short_key in key_map:
                            livedata[key_map[short_key]] = v
                # Compute v12_v from v1n + v2n
                v1n = livedata.get("v1n_v")
                v2n = livedata.get("v2n_v")
                if v1n is not None and v2n is not None:
                    livedata["v12_v"] = float(v1n) + float(v2n)
                # Also add timestamp
                ts_eps = ts_data.get("/sys/devices/transfer_switch/0/msmtEps", "")
                if ts_eps:
                    livedata["voltage_time"] = ts_eps
        except Exception as e:
            logger.debug("Transfer switch voltage fetch error: %s", e)

        return livedata
    except Exception as e:
        logger.warning("Livedata fetch error: %s", e)
        return {}


def _build_meter_device(vars_data, meter_index, suffix):
    """Build a PVS5-METER-P/C device dict from varserver data."""
    prefix = f"/sys/devices/meter/{meter_index}/"
    m = {k.replace(prefix, ""): v for k, v in vars_data.items() if k.startswith(prefix)}
    if not m:
        return None

    device = {
        "ISDETAIL": True,
        "SERIAL": m.get("sn", f"PVS6M-{suffix}"),
        "TYPE": f"PVS5-METER-{suffix.upper()}",
        "STATE": "working",
        "STATEDESCR": "Working",
        "MODEL": m.get("prodMdlNm", f"PVS6M0400{suffix}"),
        "DESCR": f"Power Meter {suffix.upper()}",
        "DEVICE_TYPE": "Power Meter",
        "p_3phsum_kw": m.get("p3phsumKw", "0"),
        "p1_kw": m.get("p1Kw", "0"),
        "p2_kw": m.get("p2Kw", "0"),
        "net_ltea_3phsum_kwh": m.get("netLtea3phsumKwh", "0"),
        "pos_ltea_3phsum_kwh": m.get("posLtea3phsumKwh", "0"),
        "neg_ltea_3phsum_kwh": m.get("negLtea3phsumKwh", "0"),
        "v12_v": m.get("v12V", "0"),
        "v1n_v": m.get("v1nV", "0"),
        "v2n_v": m.get("v2nV", "0"),
        "i1_a": m.get("i1A", "0"),
        "i2_a": m.get("i2A", "0"),
        "freq_hz": m.get("freqHz", "0"),
        "tot_pf_rto": m.get("totPfRto", "0"),
        "q3phsum_kvar": m.get("q3phsumKvar", "0"),
        "s3phsum_kva": m.get("s3phsumKva", "0"),
        "ct_scl_fctr": m.get("ctSclFctr", "0"),
        "subtype": suffix.lower(),
        "CURTIME": m.get("msmtEps", ""),
    }
    return device


def _supplement_devices(devices_json, ip, sess):
    """If meters are missing, inject synthetic PVS5-METER-P/C from varserver data.
    Also overrides meter power values with fresh livedata from /sys/livedata/.
    Returns (json_str, livedata_dict)."""
    try:
        data = json.loads(devices_json) if isinstance(devices_json, str) else devices_json
    except (json.JSONDecodeError, TypeError):
        return devices_json, {}

    device_list = data.get("devices", [])
    types = {d.get("TYPE", "") for d in device_list}

    # Fetch fresh livedata for real-time power values
    livedata = _fetch_livedata(sess, ip)
    pv_p = float(livedata.get("pv_p", 0))
    site_load_p = float(livedata.get("site_load_p", 0))
    net_p = float(livedata.get("net_p", 0))
    pv_en = float(livedata.get("pv_en", 0))
    site_load_en = float(livedata.get("site_load_en", 0))
    ld_time = livedata.get("time", "")

    if livedata:
        logger.info("Livedata: pv=%.3fkW load=%.3fkW net=%.3fkW (age=%s)",
                     pv_p, site_load_p, net_p,
                     f"{(time.time()-float(ld_time)):.0f}s" if ld_time else "?")

    # Override existing meter power values with livedata
    ld_ts = None
    if ld_time:
        try:
            ld_ts = float(ld_time)
        except (ValueError, TypeError):
            ld_ts = None

    for d in device_list:
        dtype = d.get("TYPE", "")
        if dtype == "PVS5-METER-P" and livedata:
            d["p_3phsum_kw"] = str(pv_p)
            d["net_ltea_3phsum_kwh"] = str(pv_en)
            if ld_ts is not None:
                d["CURTIME"] = datetime.fromtimestamp(ld_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                logger.debug("Meter-P CURTIME overridden to %s", d["CURTIME"])
        elif dtype == "PVS5-METER-C" and livedata:
            d["p_3phsum_kw"] = str(site_load_p)
            if ld_ts is not None:
                d["CURTIME"] = datetime.fromtimestamp(ld_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                logger.debug("Meter-C CURTIME overridden to %s", d["CURTIME"])

    # If meters already present in device list, return now
    if "PVS5-METER-P" in types and "PVS5-METER-C" in types:
        _inject_battery(data, livedata)
        _inject_battery_units(data, _fetch_ess_vars(sess, ip))
        return _json_dumps(data), livedata

    # Meters missing — supplement from full varserver data
    logger.info("Meters missing, supplementing from varserver")
    vars_data = _fetch_vars(sess, ip)
    if not vars_data:
        return _json_dumps(data), livedata

    if "PVS5-METER-C" not in types:
        meter_c = _build_meter_device(vars_data, 0, "c")
        if meter_c:
            # Override with livedata if available
            if livedata:
                meter_c["p_3phsum_kw"] = str(site_load_p)
                if ld_ts is not None:
                    meter_c["CURTIME"] = datetime.fromtimestamp(ld_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            device_list.append(meter_c)

    if "PVS5-METER-P" not in types:
        meter_p = _build_meter_device(vars_data, 1, "p")
        if meter_p:
            # Override with livedata if available
            if livedata:
                meter_p["p_3phsum_kw"] = str(pv_p)
                meter_p["net_ltea_3phsum_kwh"] = str(pv_en)
                if ld_ts is not None:
                    meter_p["CURTIME"] = datetime.fromtimestamp(ld_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            device_list.append(meter_p)

    # Second pass: override any supplemented meters with livedata
    # Extract fresh voltage from livedata (transfer switch data)
    v12 = livedata.get("v12_v")
    v1n = livedata.get("v1n_v")
    v2n = livedata.get("v2n_v")

    for d in device_list:
        dtype = d.get("TYPE", "")
        if dtype == "PVS5-METER-P" and livedata:
            d["p_3phsum_kw"] = str(pv_p)
            d["net_ltea_3phsum_kwh"] = str(pv_en)
            if ld_ts is not None:
                d["CURTIME"] = datetime.fromtimestamp(ld_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        elif dtype == "PVS5-METER-C" and livedata:
            d["p_3phsum_kw"] = str(site_load_p)
            if ld_ts is not None:
                d["CURTIME"] = datetime.fromtimestamp(ld_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            # Override voltage with fresh transfer switch data
            if v12 is not None:
                d["v12_v"] = str(v12)
            if v1n is not None:
                d["v1n_v"] = str(v1n)
            if v2n is not None:
                d["v2n_v"] = str(v2n)

    data["devices"] = device_list
    _inject_battery(data, livedata)
    # vars_data was fetched above for the meter supplement; reuse it for
    # per-unit battery details so we don't make a second varserver call.
    _inject_battery_units(data, vars_data)
    return _json_dumps(data), livedata


def _inject_battery(data, livedata):
    """Surface battery (SunVault) data + grid (net_p) + per-unit battery
    detail on the response dict so the dashboard doesn't need a second
    request. Sign conventions:
      battery p_kw   > 0 = discharging, < 0 = charging
      grid    p_kw   > 0 = importing,   < 0 = exporting (PVS net_p)
    """
    if not livedata:
        return
    try:
        data["battery"] = {
            "p_kw": float(livedata.get("ess_p", 0)),
            "soc": float(livedata.get("soc", 0)),
            "backup_min": float(livedata.get("backupTimeRemaining", 0)),
            "lifetime_kwh": float(livedata.get("ess_en", 0)),
            "midstate": int(float(livedata.get("midstate", 0))) if livedata.get("midstate") is not None else None,
        }
    except (ValueError, TypeError) as e:
        logger.debug("Battery data parse error: %s", e)

    # Authoritative grid power (revenue meter), so the dashboard doesn't
    # have to derive it from energy balance with measurement-noise drift.
    try:
        data["grid"] = {
            "p_kw": float(livedata.get("net_p", 0)),
            "lifetime_kwh": float(livedata.get("net_en", 0)),
        }
    except (ValueError, TypeError):
        pass


def _inject_battery_units(data, vars_data):
    """Add data.battery.units = [...] from /sys/devices/ess/{0,1}/ varserver
    keys. SunVault has 2 units; some installs may have more or fewer."""
    if not vars_data or "battery" not in data:
        return
    units = []
    # Discover unit indices by scanning the keys
    indices = sorted({
        int(k.split("/")[4])
        for k in vars_data.keys()
        if k.startswith("/sys/devices/ess/") and k.split("/")[4].isdigit()
    })
    for i in indices:
        prefix = f"/sys/devices/ess/{i}/"
        u = {k.replace(prefix, ""): v for k, v in vars_data.items() if k.startswith(prefix)}
        if not u:
            continue
        def f(key, default=None):
            try: return float(u[key])
            except (KeyError, ValueError, TypeError): return default
        units.append({
            "index": i,
            "model": u.get("prodMdlNm"),
            "serial": u.get("sn"),
            "p_kw": f("p3phsumKw", 0),
            # customerSocVal is the usable charge the SunStrong app shows
            # (0% = at the hidden reserve). socVal is the raw cell charge,
            # ~15 points higher. Older firmware may lack the customer value.
            "soc": f("customerSocVal", f("socVal", 0)),
            "raw_soc": f("socVal"),
            "soh": f("sohVal"),
            "op_mode": u.get("opMode"),
            "v_batt": f("vBattV"),
            "temp_c": f("maxTBattCellDegc"),  # max cell temp = warmest part
            "temp_inv_c": f("tInvtrDegc"),
            "lifetime_charged_kwh": f("posLtea3phsumKwh"),
            "lifetime_discharged_kwh": f("negLtea3phsumKwh"),
            "chrg_limit_kw": f("chrgLimitPmaxKw"),
            "dischrg_limit_kw": f("dischrgLimPmaxKw"),
            "msmt_eps": u.get("msmtEps"),
        })
    if units:
        data["battery"]["units"] = units
        # The PVS's aggregate livedata soc can sit at 0 (or NaN) for hours
        # after the battery re-registers, while each unit already reports a
        # real value. Fall back to the unit average in that case. A real 0
        # (all units at 0) is left alone.
        agg = data["battery"].get("soc")
        unit_socs = [u["soc"] for u in units
                     if u["soc"] is not None and math.isfinite(u["soc"])]
        agg_ok = agg is not None and math.isfinite(agg)
        if unit_socs and (not agg_ok or (agg == 0 and max(unit_socs) > 0)):
            data["battery"]["soc"] = sum(unit_socs) / len(unit_socs)


# ── PVS authentication ────────────────────────────────────────────────
def _get_session(ip, user, passwd):
    """Get or create an authenticated session for the PVS."""
    sess = sessions.get(ip)
    if sess:
        return sess, None

    logger.info("Authenticating with %s...", ip)
    sess = requests.Session()
    try:
        r = sess.get(f"https://{ip}/auth?login", auth=(user, passwd), verify=False, timeout=TIMEOUT_SECS)
    except Exception as e:
        return None, f"Login error: {e}"

    if r.status_code != 200 or not sess.cookies:
        return None, f"Authentication failed (HTTP {r.status_code})."

    sessions[ip] = sess
    logger.info("Authenticated successfully with %s", ip)
    return sess, None


def _reauth(ip, user, passwd):
    """Force a new authentication."""
    logger.info("Re-authenticating %s...", ip)
    try:
        new_sess = requests.Session()
        r = new_sess.get(f"https://{ip}/auth?login", auth=(user, passwd), verify=False, timeout=TIMEOUT_SECS)
    except Exception as e:
        return None, f"Re-login error: {e}"

    if r.status_code == 200 and new_sess.cookies:
        sessions[ip] = new_sess
        return new_sess, None

    return None, f"Authentication failed (HTTP {r.status_code})."


# ── Background data refresh ───────────────────────────────────────────
def _refresh_data():
    """Fetch fresh data from PVS and update the cache. Called by background thread."""
    ip = PVS_IP
    user = PVS_USER
    passwd = PVS_PASS

    if not ip or not passwd:
        logger.warning("PVS_IP or PVS_PASS not set, skipping data refresh")
        return

    cache.fetching = True
    try:
        sess, err = _get_session(ip, user, passwd)
        if err:
            # Try re-auth if session was stale
            sess, err = _reauth(ip, user, passwd)
            if err:
                logger.error("Data refresh failed: %s", err)
                cache.set_error(err)
                return

        # Fetch device list
        try:
            r = sess.get(f"https://{ip}/cgi-bin/dl_cgi/devices/list", verify=False, timeout=TIMEOUT_SECS)
        except Exception as e:
            # Session may be stale — try re-auth
            logger.warning("Device fetch error: %s, re-authenticating...", e)
            sessions.pop(ip, None)
            sess, err = _reauth(ip, user, passwd)
            if err:
                cache.set_error(err)
                return
            r = sess.get(f"https://{ip}/cgi-bin/dl_cgi/devices/list", verify=False, timeout=TIMEOUT_SECS)

        if r.status_code in (401, 403):
            sess, err = _reauth(ip, user, passwd)
            if err:
                cache.set_error(err)
                return
            r = sess.get(f"https://{ip}/cgi-bin/dl_cgi/devices/list", verify=False, timeout=TIMEOUT_SECS)

        if r.status_code != 200:
            logger.error("Data refresh: HTTP %s from PVS", r.status_code)
            cache.set_error(f"HTTP {r.status_code}")
            return

        # Supplement with varserver meter data and livedata overrides
        supplemented, livedata = _supplement_devices(r.text, ip, sess)
        cache.set(supplemented)
        cache.set_error(None)

        # Record in time-series database
        _record_reading(supplemented, livedata=livedata)

        logger.info("Data refreshed (%d bytes)", len(supplemented))

    except Exception as e:
        logger.error("Data refresh error: %s", e)
        cache.set_error(str(e))
    finally:
        cache.fetching = False


# ── Stale-data alerting (ntfy) ─────────────────────────────────────────
_STARTED_AT = time.time()
_stale_since = None  # epoch of last good data when we alerted; None = not alerted


def _post_ntfy(url, token, title, message, priority, tags):
    """POST one alert; returns True on success."""
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    if NTFY_EMAIL:
        headers["Email"] = NTFY_EMAIL
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=15)
        if r.status_code == 400 and "Email" in headers:
            # e.g. ntfy.sh rejects email from anonymous publishers — don't let
            # that swallow the push itself.
            logger.warning("ntfy rejected email forwarding (%s); sending push only", r.text[:120])
            headers.pop("Email")
            r = requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=15)
        if r.status_code >= 300:
            logger.warning("ntfy alert to %s failed: HTTP %s", url.split("//")[-1].split("/")[0], r.status_code)
            return False
        return True
    except Exception as e:
        logger.warning("ntfy alert to %s failed: %s", url.split("//")[-1].split("/")[0], e)
        return False


def _send_ntfy(title, message, priority="default", tags=""):
    """Send to NTFY_URL; if that fails, try NTFY_FALLBACK_URL."""
    if _post_ntfy(NTFY_URL, NTFY_TOKEN, title, message, priority, tags):
        return True
    if NTFY_FALLBACK_URL:
        logger.info("Retrying alert via fallback ntfy server")
        return _post_ntfy(NTFY_FALLBACK_URL, NTFY_FALLBACK_TOKEN, title, message, priority, tags)
    return False


def _fmt_duration(secs):
    mins = int(secs // 60)
    if mins >= 48 * 60:
        return f"{secs / 86400:.1f}d"
    return f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins}m"


def _check_stale_alert():
    """Alert once when the PVS has been unreachable for ALERT_STALE_MINS,
    and once more when data starts flowing again."""
    global _stale_since
    if not NTFY_URL:
        return
    now = time.time()
    last_ok = cache.last_fetch or _STARTED_AT
    stale_secs = now - last_ok
    if _stale_since is None and stale_secs >= ALERT_STALE_MINS * 60:
        _stale_since = last_ok
        err = cache.last_error or "no response"
        logger.warning("No PVS data for %s — sending alert", _fmt_duration(stale_secs))
        _send_ntfy("PVS offline",
                   f"No data from the PVS for {_fmt_duration(stale_secs)}. Last error: {err}",
                   priority="high", tags="warning")
    elif _stale_since is not None and stale_secs < ALERT_STALE_MINS * 60:
        down = last_ok - _stale_since
        _stale_since = None
        why = _recovery_cause(down)
        logger.info("PVS data back after %s (%s) — sending recovery", _fmt_duration(down), why)
        _send_ntfy("PVS back online", f"Data is flowing again after {_fmt_duration(down)}. {why}",
                   tags="white_check_mark")


def _battery_alert_file():
    return os.path.join(os.path.dirname(DB_PATH) or ".", "battery_alert_state")


def _check_battery_alert():
    """Alert once when usable battery charge drops below ALERT_BATTERY_PCT,
    and once when it climbs back above it + 5. The "alerted" flag is kept in
    a file next to the DB so a deploy or reboot doesn't re-send it."""
    if not NTFY_URL or ALERT_BATTERY_PCT <= 0:
        return
    try:
        soc = json.loads(cache.get() or "{}").get("battery", {}).get("soc")
    except (TypeError, json.JSONDecodeError, AttributeError):
        return
    if soc is None or not math.isfinite(soc):
        return
    pct = soc * 100
    flag = _battery_alert_file()
    alerted = os.path.exists(flag)
    if not alerted and pct < ALERT_BATTERY_PCT:
        logger.warning("Battery at %.0f%% — sending low-battery alert", pct)
        if _send_ntfy("Battery low",
                      f"SunVault usable charge is {pct:.0f}% (alert below {ALERT_BATTERY_PCT:.0f}%). "
                      "It should stay above the backup reserve. Check the SunStrong app.",
                      priority="high", tags="battery"):
            open(flag, "w").write(f"{time.time()}\n")
    elif alerted and pct >= ALERT_BATTERY_PCT + 5:
        logger.info("Battery back to %.0f%% — sending recovery", pct)
        if _send_ntfy("Battery recovered", f"SunVault usable charge is back to {pct:.0f}%.",
                      tags="white_check_mark"):
            os.remove(flag)


def _recovery_cause(down_secs):
    """One-line cause for the recovery alert, from the PVS's current uptime."""
    try:
        devices = json.loads(cache.get() or "{}").get("devices", [])
        up = _pvs_uptime_now(next((d for d in devices if d.get("DEVICE_TYPE") == "PVS"), None))
    except (TypeError, json.JSONDecodeError):
        up = None
    if up is None:
        return "Cause unknown (no PVS uptime)."
    if up < down_secs:
        return f"The PVS restarted {_fmt_duration(up)} ago (power loss or reboot)."
    return f"The PVS stayed up the whole time ({_fmt_duration(up)} uptime), so it was a WiFi/network problem."


# ── SunVault grid/backup state (midstate) ────────────────────────────
# livedata midstate is the Hub+ MID (main grid contactor) state. Observed:
# 0 before the Hub+ was commissioned (grid up), 2 grid-connected. SunPower
# names the normal state MID_STATE_CLOSED; the backup/islanded value hasn't
# been observed yet. So: 2 = on grid, 0 = unknown, anything else = backup.
MIDSTATE_ON_GRID, MIDSTATE_UNKNOWN = 2, 0
_midstate = {"value": None, "since": None, "checked": None}


def _midstate_kind(v):
    if v is None or v == MIDSTATE_UNKNOWN:
        return "unknown"
    return "on_grid" if v == MIDSTATE_ON_GRID else "backup"


def _midstate_record(value, now=None):
    """Record a midstate reading: on change, store an event and alert."""
    now = now or time.time()
    _midstate["checked"] = now
    prev = _midstate["value"]
    if value == prev and _midstate["since"] is not None:
        return
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        if prev is None:
            # First reading since start: carry "since" over from the stored
            # history if the state hasn't changed across the restart.
            row = conn.execute("SELECT ts, value FROM midstate_events ORDER BY ts DESC LIMIT 1").fetchone()
            if row and row[1] == value:
                _midstate.update(value=value, since=row[0])
                conn.close()
                return
            prev = row[1] if row else None
        conn.execute("INSERT OR REPLACE INTO midstate_events (ts, value, prev) VALUES (?, ?, ?)",
                     (now, value, prev))
        conn.commit()
        conn.close()
    _midstate.update(value=value, since=now)
    logger.info("SunVault midstate %s -> %s (%s)", prev, value, _midstate_kind(value))
    if prev is None:
        return  # first ever reading, nothing changed
    kind = _midstate_kind(value)
    if kind == "backup":
        _send_ntfy("SunVault on backup power",
                   f"The Hub+ disconnected from the grid (state {prev} -> {value}). "
                   "The battery is powering the house.", priority="high", tags="zap")
    elif kind == "on_grid":
        _send_ntfy("SunVault back on grid", f"The Hub+ reconnected to the grid (state {prev} -> {value}).",
                   tags="white_check_mark")
    else:
        _send_ntfy("SunVault state unknown",
                   f"The Hub+ state changed {prev} -> {value} (unknown). Check the SunStrong app.",
                   tags="grey_question")


def _midstate_watch():
    """Poll livedata midstate every MIDSTATE_POLL_SECS (a fast varserver read)."""
    logger.info("SunVault state watch every %ds", MIDSTATE_POLL_SECS)
    while True:
        try:
            sess, err = _get_session(PVS_IP, PVS_USER, PVS_PASS)
            if sess:
                r = sess.get(f"https://{PVS_IP}/vars?name=/sys/livedata/midstate&fmt=obj",
                             verify=False, timeout=30)
                if r.status_code in (401, 403):
                    sessions.pop(PVS_IP, None)
                elif r.status_code == 200:
                    v = r.json().get("/sys/livedata/midstate")
                    if v is not None:
                        _midstate_record(int(float(v)))
        except Exception as e:
            logger.debug("midstate poll failed: %s", e)
        time.sleep(MIDSTATE_POLL_SECS)


def _background_refresh():
    """Background thread that periodically refreshes PVS data."""
    logger.info("Starting background refresh (every %ds)", REFRESH_SECS)
    if NTFY_URL:
        logger.info("Stale-data alerts enabled (after %d min)", ALERT_STALE_MINS)
        if ALERT_BATTERY_PCT > 0:
            logger.info("Low-battery alerts enabled (below %.0f%%)", ALERT_BATTERY_PCT)
    if UPS_EVENTS_FILE:
        logger.info("Grid outage tracking from %s", UPS_EVENTS_FILE)
    # Initial fetch
    _refresh_data()
    _ingest_ups_events()
    while True:
        time.sleep(REFRESH_SECS)
        _refresh_data()
        _check_stale_alert()
        _check_battery_alert()
        _ingest_ups_events()


# ── Mock mode (MOCK_PVS=1) ────────────────────────────────────────────
# Synthesizes /devices and history from fixtures so the dashboard runs
# without a real PVS gateway. Used by the local test rig.

MOCK_DEVICES_TEMPLATE = None
MOCK_LIVEDATA_TEMPLATE = None


def _load_mock_fixtures():
    """Load JSON fixtures into module globals on startup."""
    global MOCK_DEVICES_TEMPLATE, MOCK_LIVEDATA_TEMPLATE
    with open(os.path.join(FIXTURES_DIR, "devices_list.json")) as f:
        MOCK_DEVICES_TEMPLATE = json.load(f)
    with open(os.path.join(FIXTURES_DIR, "livedata.json")) as f:
        MOCK_LIVEDATA_TEMPLATE = json.load(f)
    logger.info("Loaded mock fixtures from %s", FIXTURES_DIR)


def _mock_curve(ts):
    """Return (production_kw, consumption_kw, battery_kw) for a given epoch.
    Deterministic: solar bell during daylight, evening consumption bump,
    battery charges around peak production and discharges in the evening.
    Battery sign: + = discharging, - = charging (matches PVS ess_p)."""
    import math
    hod = (ts % 86400) / 3600.0
    if 6 <= hod <= 18:
        x = (hod - 12) / 6.0
        production = max(0.0, 5.0 * (1 - x * x))
    else:
        production = 0.0
    consumption = 1.0
    if 18 <= hod <= 23:
        consumption += 2.0
    if 6 <= hod <= 8:
        consumption += 0.5
    if production > 3.0:
        battery_kw = -1.5  # charging
    elif consumption > 2.5 and production < 0.5:
        battery_kw = 1.5   # discharging
    else:
        battery_kw = 0.0
    return production, consumption, battery_kw


def _refresh_mock():
    """Populate cache + record a history row from fixtures, modulating power
    values from a deterministic time-of-day curve so LIVE flow looks alive."""
    devices_data = json.loads(json.dumps(MOCK_DEVICES_TEMPLATE))  # deep copy
    livedata = dict(MOCK_LIVEDATA_TEMPLATE)

    now = time.time()
    pv, cons, bat = _mock_curve(now)
    net = cons - pv - bat  # net_p convention: + = importing

    livedata["pv_p"] = round(pv, 3)
    livedata["site_load_p"] = round(cons, 3)
    livedata["net_p"] = round(net, 3)
    livedata["ess_p"] = round(bat, 3)
    livedata["time"] = str(int(now))
    for d in devices_data["devices"]:
        if d.get("DEVICE_TYPE") == "PVS":
            d["dl_uptime"] = str(int(now - _STARTED_AT) + 86400)

    # Mirror power into the devices list so the dashboard's panel/meter
    # views see consistent values.
    inverters = [d for d in devices_data["devices"] if d.get("TYPE") == "SOLARBRIDGE"]
    per_panel = pv / max(1, len(inverters))
    for d in devices_data["devices"]:
        t = d.get("TYPE")
        if t == "PVS5-METER-P":
            d["p_3phsum_kw"] = f"{pv:.3f}"
        elif t == "PVS5-METER-C":
            d["p_3phsum_kw"] = f"{cons:.3f}"
        elif t == "SOLARBRIDGE":
            d["p_3phsum_kw"] = f"{per_panel:.3f}"
            if per_panel > 0.005:
                d["STATE"] = "working"
                d["STATEDESCR"] = "Working"
            else:
                d["STATE"] = "error"
                d["STATEDESCR"] = "Communicating"

    _inject_battery(devices_data, livedata)
    cache.set(json.dumps(devices_data))
    cache.set_error(None)
    _record_reading(json.dumps(devices_data), livedata=livedata)


def _seed_mock_history():
    """Seed history on first start in mock mode. Prefers a real-data fixture
    (fixtures/history_seed.sqlite, anonymized + trimmed by
    scripts/build_history_fixture.py) if present, otherwise falls back to a
    synthetic time-of-day curve. Idempotent: skipped if any rows exist."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if c.execute("SELECT COUNT(*) FROM readings").fetchone()[0] > 0:
        conn.close()
        logger.info("Mock seed skipped — readings table already has data")
        return

    # Two-tier fixture: prefer .live (auto-refreshed from asgard before each
    # test run) over the committed baseline. Both are gated by FIXTURES_DIR.
    for name in ("history_seed.live.sqlite", "history_seed.sqlite"):
        candidate = os.path.join(FIXTURES_DIR, name)
        if os.path.exists(candidate):
            logger.info("Seeding from fixture %s", candidate)
            conn.close()
            _seed_from_fixture(candidate)
            return

    logger.info("No history_seed fixture found in %s — using synthetic curves", FIXTURES_DIR)
    import math
    DAYS = 5
    INTERVAL = 300
    rows_per_day = 86400 // INTERVAL
    total_rows = DAYS * rows_per_day
    now = time.time()
    start_ts = now - DAYS * 86400

    lt_solar = 12000.0
    lt_home = 8000.0
    lt_grid_net = -3500.0
    lt_battery = 1200.0

    rows = []
    for i in range(total_rows):
        ts = start_ts + i * INTERVAL
        pv, cons, bat = _mock_curve(ts)
        net_kw = cons - pv - bat
        delta_h = INTERVAL / 3600.0
        lt_solar += pv * delta_h
        lt_home += cons * delta_h
        lt_grid_net += net_kw * delta_h
        lt_battery += abs(bat) * delta_h * 0.5
        hod = (ts % 86400) / 3600.0
        soc = 0.7 + 0.25 * math.sin(2 * math.pi * hod / 24.0 - 1.0)
        soc = max(0.4, min(0.95, soc))
        panels_working = 16 if pv > 0.05 else 0
        panels_error = 0 if pv > 0.05 else 16
        rows.append((
            ts, pv, cons, net_kw, lt_solar,
            240.1, 120.05, 120.05, 60.0, 1.0, 0.99,
            16, panels_working, panels_error,
            bat, soc, 720,
            lt_battery, lt_home, lt_grid_net,
        ))

    panel_serials = [f"E00121935016M{i:03d}" for i in range(1, 17)]
    panel_rows = []
    # One panel snapshot per hour (not every 5 min) — keeps the seeded DB
    # small while still giving the panel-drilldown view enough buckets.
    for i in range(0, total_rows, 12):
        ts = start_ts + i * INTERVAL
        pv, _, _ = _mock_curve(ts)
        per_panel_w = (pv * 1000) / 16
        for serial in panel_serials:
            jitter = ((hash(serial) % 100) - 50) / 1000.0
            w = max(0.0, per_panel_w * (1 + jitter))
            state = "working" if w > 5 else "error"
            lifetime = 700 + (ts - start_ts) / 86400.0 * 8 + (hash(serial) % 50)
            panel_rows.append((
                ts, serial, "SPR-X22-360-D-AC", state, w,
                44.0 + (hash(serial) % 30) / 100.0,
                w / 240.0,
                240.05,
                38.0 + (hash(serial + "t") % 50) / 10.0,
                lifetime,
            ))

    with _db_lock:
        c.executemany("""INSERT OR REPLACE INTO readings
            (ts, production_kw, consumption_kw, net_kw, lifetime_kwh,
             sys_v, l1_v, l2_v, freq_hz, pf_production, pf_consumption,
             num_panels, panels_working, panels_error,
             battery_kw, battery_soc, backup_min, battery_lifetime_kwh,
             home_lifetime_kwh, grid_lifetime_kwh)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
        c.executemany("""INSERT OR REPLACE INTO panel_readings
            (ts, serial, panel_model, state, watts, v_dc, i_dc, v_ac, temp_c, lifetime_kwh)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", panel_rows)
        conn.commit()
    conn.close()
    logger.info("Seeded mock history: %d readings, %d panel rows over %d days",
                total_rows, len(panel_rows), DAYS)


def _seed_from_fixture(fixture_path):
    """Copy readings + panel_readings from the fixture into the runtime DB,
    adding time.time() to each ts. The fixture stores normalized timestamps
    where the most-recent row has ts = 0 and older rows are negative
    (see scripts/build_history_fixture.py), so the offset to apply is
    simply the current epoch — the newest row lands at "now"."""
    src = sqlite3.connect(f"file:{fixture_path}?mode=ro", uri=True)
    max_orig = src.execute("SELECT MAX(ts) FROM readings").fetchone()[0]
    src.close()
    if max_orig is None:
        logger.warning("Fixture %s has no readings; skipping seed", fixture_path)
        return
    offset = time.time()  # newest row (ts=0) → now; older rows (ts<0) → past

    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        # ATTACH path is sqlite-quoted (single quotes, doubled to escape).
        conn.execute(f"ATTACH DATABASE '{fixture_path.replace(chr(39), chr(39)*2)}' AS src")
        cur = conn.execute(
            "INSERT INTO readings ("
            "ts, production_kw, consumption_kw, net_kw, lifetime_kwh, "
            "sys_v, l1_v, l2_v, freq_hz, pf_production, pf_consumption, "
            "num_panels, panels_working, panels_error, "
            "battery_kw, battery_soc, backup_min, "
            "battery_lifetime_kwh, home_lifetime_kwh, grid_lifetime_kwh) "
            "SELECT "
            "ts + ?, production_kw, consumption_kw, net_kw, lifetime_kwh, "
            "sys_v, l1_v, l2_v, freq_hz, pf_production, pf_consumption, "
            "num_panels, panels_working, panels_error, "
            "battery_kw, battery_soc, backup_min, "
            "battery_lifetime_kwh, home_lifetime_kwh, grid_lifetime_kwh "
            "FROM src.readings",
            (offset,),
        )
        n_readings = cur.rowcount
        cur.close()
        cur = conn.execute(
            "INSERT INTO panel_readings ("
            "ts, serial, panel_model, state, watts, "
            "v_dc, i_dc, v_ac, temp_c, lifetime_kwh) "
            "SELECT "
            "ts + ?, serial, panel_model, state, watts, "
            "v_dc, i_dc, v_ac, temp_c, lifetime_kwh "
            "FROM src.panel_readings",
            (offset,),
        )
        n_panel = cur.rowcount
        cur.close()
        conn.commit()
        # No DETACH — closing the connection releases the attached DB.
        # Calling DETACH while the INSERT cursors are still in flight
        # raises "database src is locked".
        conn.close()
    logger.info("Seeded mock history from fixture: %d readings, %d panel rows (ts offset %+ds)",
                n_readings, n_panel, int(offset))


def _seed_mock_outages():
    """One synthetic 4-min grid outage, 30 min before the newest seeded
    reading (so it lands inside the charted data even when the fixture has
    a long gap), for the Outages card and chart bands in mock mode."""
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        if conn.execute("SELECT COUNT(*) FROM grid_outages").fetchone()[0] == 0:
            latest = conn.execute("SELECT MAX(ts) FROM readings").fetchone()[0] or time.time()
            start = latest - 30 * 60
            conn.execute("INSERT INTO grid_outages (start, end, source) VALUES (?, ?, 'mock')",
                         (start, start + 240))
            conn.commit()
        conn.close()


def _background_refresh_mock():
    """Mock equivalent of _background_refresh."""
    logger.info("Starting mock refresh loop (every %ds)", REFRESH_SECS)
    _refresh_mock()
    while True:
        time.sleep(REFRESH_SECS)
        _refresh_mock()


# ── CORS ──────────────────────────────────────────────────────────────
@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    return resp


# ── Dashboard routes ──────────────────────────────────────────────────
@app.route("/favicon.svg")
def favicon_svg():
    return send_file("docs/favicon.svg", mimetype="image/svg+xml")


@app.route("/favicon-32.png")
def favicon_png_32():
    return send_file("docs/favicon-32.png", mimetype="image/png")


@app.route("/apple-touch-icon.png")
def apple_touch_icon():
    return send_file("docs/apple-touch-icon.png", mimetype="image/png")


@app.route("/favicon.ico")
def favicon_ico():
    # Modern browsers prefer the SVG declared in the HTML <link>, but tabs
    # opened by URL still request /favicon.ico — serve the 32px PNG which
    # is acceptable to all major browsers as an "ico" response.
    return send_file("docs/favicon-32.png", mimetype="image/png")


@app.route("/")
def index():
    if DASHBOARD_HTML is None:
        return Response("Dashboard not loaded", status=500)
    resp = Response(DASHBOARD_HTML, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/solar_dashboard.html")
def dashboard():
    if DASHBOARD_HTML is None:
        return Response("Dashboard not loaded", status=500)
    return Response(DASHBOARD_HTML, mimetype="text/html")


# ── Device data route ─────────────────────────────────────────────────
@app.route("/devices", methods=["GET", "OPTIONS"])
def devices():
    if request.method == "OPTIONS":
        return Response("", status=200)

    # Serve from cache — instant response
    data = cache.get()
    if data:
        return Response(data, mimetype="application/json")

    # No cached data yet — wait briefly for background thread
    # The PVS /devices/list endpoint can take 30-60s to respond,
    # so instead of blocking here, we wait for the background refresh.
    for _ in range(90):  # wait up to 90s
        time.sleep(1)
        data = cache.get()
        if data:
            return Response(data, mimetype="application/json")
        if cache.last_error and not cache.fetching:
            return Response(f"PVS error: {cache.last_error}", status=502)

    return Response("Timed out waiting for PVS data.", status=504)


# ── Time windows ──────────────────────────────────────────────────────
RANGE_SECS = {
    "1m": 60, "1h": 3600, "6h": 21600, "24h": 86400,
    "7d": 604800, "30d": 2592000, "90d": 7776000, "1y": 31536000,
}


def _resolve_window(range_str, start_param=None, end_param=None):
    """Return (cutoff, end_ts, range_secs) for a ?range= / ?start=&end= query.
    "all" = since first reading. Raises ValueError on bad start/end."""
    if start_param and end_param:
        cutoff = float(start_param)
        end_ts = float(end_param)
        return cutoff, end_ts, max(60, int(end_ts - cutoff))
    end_ts = time.time()
    if range_str == "all":
        try:
            conn0 = sqlite3.connect(DB_PATH)
            earliest_row = conn0.execute("SELECT MIN(ts) FROM readings").fetchone()
            conn0.close()
            range_secs = max(60, int(end_ts - (earliest_row[0] or end_ts)))
        except Exception:
            range_secs = 86400
    else:
        range_secs = RANGE_SECS.get(range_str, 86400)
    return end_ts - range_secs, end_ts, range_secs


# ── History API ────────────────────────────────────────────────────────
@app.route("/history", methods=["GET"])
def history():
    """Return time-series data for charts.

    Query params:
      range  - Time range: 1m, 1h, 6h, 24h, 7d, 30d, 90d, 1y, all (default: 24h)
      resample - Resample interval in seconds (default: auto based on range)
    """
    range_str = request.args.get("range", "24h")
    resample = request.args.get("resample")
    # Optional explicit window — used by the dashboard for calendar-based
    # ranges ("today", "yesterday") so the browser's local-tz midnight
    # boundaries are authoritative.
    start_param = request.args.get("start")
    end_param = request.args.get("end")

    try:
        cutoff, end_ts, range_secs = _resolve_window(range_str, start_param, end_param)
    except ValueError:
        return Response("Invalid start/end timestamps", status=400)

    # Auto-resample: aim for ~200-400 points max
    if resample:
        sample_secs = int(resample)
    else:
        target_points = 300
        sample_secs = max(60, range_secs // target_points)
        # Round up to nice intervals
        if sample_secs <= 60:
            sample_secs = 60
        elif sample_secs <= 300:
            sample_secs = (sample_secs // 60 + 1) * 60
        elif sample_secs <= 600:
            sample_secs = 300
        elif sample_secs <= 1800:
            sample_secs = 600
        elif sample_secs <= 7200:
            sample_secs = 1800
        else:
            sample_secs = 3600

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        # Use bucketed query for resampling
        bucket_expr = f"CAST((ts / {sample_secs}) AS INTEGER) * {sample_secs}"

        rows = c.execute(f"""
            SELECT
                {bucket_expr} as bucket,
                AVG(production_kw) as production_kw,
                AVG(consumption_kw) as consumption_kw,
                AVG(net_kw) as net_kw,
                AVG(battery_kw) as battery_kw,
                AVG(battery_soc) as battery_soc,
                MAX(NULLIF(lifetime_kwh, 0)) as lifetime_kwh,
                AVG(sys_v) as sys_v,
                AVG(l1_v) as l1_v,
                AVG(l2_v) as l2_v,
                AVG(freq_hz) as freq_hz,
                AVG(pf_production) as pf_production,
                AVG(pf_consumption) as pf_consumption,
                AVG(num_panels) as num_panels,
                AVG(panels_working) as panels_working,
                AVG(panels_error) as panels_error,
                COUNT(*) as samples
            FROM readings
            WHERE ts > ? AND ts <= ?
            GROUP BY bucket
            ORDER BY bucket
        """, (cutoff, end_ts)).fetchall()

        # Period totals — compute kWh for the actual data span (not the requested
        # window, which may extend before our earliest reading). Prefer
        # cumulative-counter deltas (exact, drift-free) over AVG×hours
        # integration, which is lossy under high variance (e.g. outage spikes).
        # Use first/last (time-ordered) instead of MIN/MAX: a single garbage poll
        # from the varserver (e.g. site_load_en briefly jumping +1283 kWh, seen
        # 2026-05-05) blows out MAX, but first/last ignore mid-window spikes.
        totals_row = c.execute("""
            SELECT
                (SELECT lifetime_kwh FROM readings
                 WHERE ts > :cutoff AND ts <= :end_ts AND lifetime_kwh > 0
                 ORDER BY ts ASC LIMIT 1) as solar_first_v,
                (SELECT lifetime_kwh FROM readings
                 WHERE ts > :cutoff AND ts <= :end_ts AND lifetime_kwh > 0
                 ORDER BY ts DESC LIMIT 1) as solar_last_v,
                (SELECT ts FROM readings
                 WHERE ts > :cutoff AND ts <= :end_ts AND lifetime_kwh > 0
                 ORDER BY ts ASC LIMIT 1) as solar_lt_t_start,
                (SELECT ts FROM readings
                 WHERE ts > :cutoff AND ts <= :end_ts AND lifetime_kwh > 0
                 ORDER BY ts DESC LIMIT 1) as solar_lt_t_end,
                (SELECT home_lifetime_kwh FROM readings
                 WHERE ts > :cutoff AND ts <= :end_ts AND home_lifetime_kwh > 0
                 ORDER BY ts ASC LIMIT 1) as home_first_v,
                (SELECT home_lifetime_kwh FROM readings
                 WHERE ts > :cutoff AND ts <= :end_ts AND home_lifetime_kwh > 0
                 ORDER BY ts DESC LIMIT 1) as home_last_v,
                (SELECT ts FROM readings
                 WHERE ts > :cutoff AND ts <= :end_ts AND home_lifetime_kwh > 0
                 ORDER BY ts ASC LIMIT 1) as home_lt_t_start,
                (SELECT ts FROM readings
                 WHERE ts > :cutoff AND ts <= :end_ts AND home_lifetime_kwh > 0
                 ORDER BY ts DESC LIMIT 1) as home_lt_t_end,
                AVG(production_kw) as avg_prod,
                AVG(consumption_kw) as avg_cons,
                AVG(net_kw) as avg_net,
                AVG(battery_kw) as avg_battery,
                MIN(ts) as t_start,
                MAX(ts) as t_end,
                COUNT(*) as n,
                COUNT(NULLIF(lifetime_kwh, 0)) as n_solar_lt,
                COUNT(NULLIF(home_lifetime_kwh, 0)) as n_home_lt
            FROM readings WHERE ts > :cutoff AND ts <= :end_ts
        """, {"cutoff": cutoff, "end_ts": end_ts}).fetchone()

        totals = None
        if totals_row and totals_row["n"] and totals_row["n"] > 1:
            t_start = totals_row["t_start"] or 0
            t_end = totals_row["t_end"] or 0
            elapsed_h = max(1 / 3600, (t_end - t_start) / 3600)
            n_rows = totals_row["n"] or 0
            window_span = max(1, t_end - t_start)
            # Trust a lifetime-counter delta only if its populated rows span
            # *both* most of the window's row count *and* most of the window's
            # time. Row-count alone wasn't enough — a column that was added
            # mid-window can hit the row threshold (most recent rows have it)
            # while only covering ~half the window's time, producing a delta
            # that grossly understates the true period total. Falling back to
            # AVG×hours integration always covers the full elapsed time.
            def coverage_ok(n_lt, lt_t_start, lt_t_end):
                if (n_lt or 0) < max(2, int(n_rows * 0.8)):
                    return False
                if not (lt_t_start and lt_t_end):
                    return False
                return (lt_t_end - lt_t_start) / window_span >= 0.95

            # Sanity-cap a lifetime-counter delta against AVG×hours. first/last
            # ignores mid-window spikes; this catches the edge case where the
            # spike sits at the very first or last populated row. Generous
            # factor (4×) avoids rejecting legitimate deltas when AVG is
            # depressed by sparse sampling.
            def trust_delta(lt_kwh, avg_kw):
                avg_kwh = max(0, (avg_kw or 0) * elapsed_h)
                if avg_kwh < 0.5:
                    return True  # AVG too small to compare against
                return lt_kwh <= avg_kwh * 4 + 1

            solar_first = totals_row["solar_first_v"]
            solar_last = totals_row["solar_last_v"]
            solar_lt = max(0, (solar_last or 0) - (solar_first or 0)) if (solar_first and solar_last) else 0
            solar_kwh = solar_lt if (coverage_ok(
                totals_row["n_solar_lt"], totals_row["solar_lt_t_start"], totals_row["solar_lt_t_end"]
            ) and trust_delta(solar_lt, totals_row["avg_prod"])) else 0
            if solar_kwh < 0.01:
                solar_kwh = max(0, (totals_row["avg_prod"] or 0) * elapsed_h)

            home_first = totals_row["home_first_v"]
            home_last = totals_row["home_last_v"]
            home_lt = max(0, (home_last or 0) - (home_first or 0)) if (home_first and home_last) else 0
            home_kwh = home_lt if (coverage_ok(
                totals_row["n_home_lt"], totals_row["home_lt_t_start"], totals_row["home_lt_t_end"]
            ) and trust_delta(home_lt, totals_row["avg_cons"])) else 0
            if home_kwh < 0.01:
                home_kwh = max(0, (totals_row["avg_cons"] or 0) * elapsed_h)
            # PVS net_p convention: positive = importing from grid, negative = exporting.
            # Verified empirically (2026-04-25):
            #   During outage pv=0.02 load=12.34 → net_kw=+12.32 (importing 12.3 kW)
            #   Sunny day      pv=4.02 load=1.12 → net_kw=-2.89  (exporting 2.9 kW)
            net_kwh = (totals_row["avg_net"] or 0) * elapsed_h
            battery_net_kwh = (totals_row["avg_battery"] or 0) * elapsed_h  # +discharge, -charge

            grid_import_kwh = max(0, net_kwh)
            grid_export_kwh = max(0, -net_kwh)
            avoided_kwh = min(solar_kwh, home_kwh)  # solar that offset grid use
            savings_dollars = avoided_kwh * COST_PER_KWH
            co2_lbs = solar_kwh * CO2_LBS_PER_KWH
            independence_pct = (solar_kwh / home_kwh * 100) if home_kwh > 0.01 else None

            totals = {
                "solar_kwh": round(solar_kwh, 2),
                "home_kwh": round(home_kwh, 2),
                "battery_net_kwh": round(battery_net_kwh, 2),
                "grid_net_kwh": round(net_kwh, 2),
                "grid_import_kwh": round(grid_import_kwh, 2),
                "grid_export_kwh": round(grid_export_kwh, 2),
                "avoided_kwh": round(avoided_kwh, 2),
                "savings_dollars": round(savings_dollars, 2),
                "co2_lbs": round(co2_lbs, 1),
                "trees_equivalent": round(co2_lbs / 48.0, 1),
                "miles_not_driven": round(co2_lbs / 0.89, 0),
                "gallons_not_used": round(co2_lbs / 19.6, 1),
                "independence_pct": round(independence_pct, 0) if independence_pct is not None else None,
                "period_start": t_start,
                "period_end": t_end,
                "elapsed_hours": round(elapsed_h, 2),
            }

        result = {
            "range": range_str,
            "range_secs": range_secs,
            "resample_seconds": sample_secs,
            "readings": [dict(r) for r in rows],
            "period_totals": totals,
        }

        conn.close()
        return jsonify(result)

    except Exception as e:
        logger.error("History query error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/history/panels", methods=["GET"])
def history_panels():
    """Return per-panel time-series data for a specific time range."""
    range_str = request.args.get("range", "24h")
    range_map = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000}
    range_secs = range_map.get(range_str, 86400)
    cutoff = time.time() - range_secs

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        rows = c.execute("""
            SELECT ts, serial, panel_model, state, watts, v_dc, i_dc, v_ac, temp_c, lifetime_kwh
            FROM panel_readings
            WHERE ts > ?
            ORDER BY ts
        """, (cutoff,)).fetchall()

        result = {
            "range": range_str,
            "panels": [dict(r) for r in rows]
        }
        conn.close()
        return jsonify(result)

    except Exception as e:
        logger.error("Panel history query error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/panel/<serial>/history", methods=["GET"])
def panel_history(serial):
    """Per-panel drilldown: bucketed energy (kWh) per period.

    Query params:
      range  - 1h, 6h, 24h, 7d, 30d (default: 24h)
      bucket - bucket size in seconds (default: auto — 5 min for 1-6h ranges,
               1 hour for 24h, 1 day for 7d+)
    """
    range_str = request.args.get("range", "24h")
    range_map = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000}
    range_secs = range_map.get(range_str, 86400)
    cutoff = time.time() - range_secs

    bucket_param = request.args.get("bucket")
    if bucket_param:
        bucket_secs = int(bucket_param)
    else:
        # Auto-bucket: aim for 24-30 buckets per range
        if range_secs <= 21600:    bucket_secs = 300       # 5 min
        elif range_secs <= 86400:  bucket_secs = 3600      # 1 hour
        elif range_secs <= 604800: bucket_secs = 21600     # 6 hour
        else:                      bucket_secs = 86400     # 1 day

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        # Latest snapshot for header info
        latest_row = c.execute("""
            SELECT ts, panel_model, state, watts, v_dc, i_dc, v_ac, temp_c, lifetime_kwh
            FROM panel_readings WHERE serial = ?
            ORDER BY ts DESC LIMIT 1
        """, (serial,)).fetchone()

        if not latest_row:
            return jsonify({"error": "panel not found", "serial": serial}), 404

        # Bucketed energy
        bucket_expr = f"CAST((ts / {bucket_secs}) AS INTEGER) * {bucket_secs}"
        rows = c.execute(f"""
            SELECT
                {bucket_expr} as bucket,
                AVG(watts) as avg_w,
                COUNT(*) as samples,
                MAX(NULLIF(lifetime_kwh, 0)) as lifetime_max,
                MIN(NULLIF(lifetime_kwh, 0)) as lifetime_min,
                AVG(v_dc) as avg_v_dc,
                AVG(temp_c) as avg_temp_c
            FROM panel_readings
            WHERE ts > ? AND serial = ?
            GROUP BY bucket
            ORDER BY bucket
        """, (cutoff, serial)).fetchall()

        # Compute kWh per bucket: prefer lifetime delta, fall back to avg power × hours
        h_per_bucket = bucket_secs / 3600
        buckets = []
        for r in rows:
            d = dict(r)
            lifetime_delta = (d.get("lifetime_max") or 0) - (d.get("lifetime_min") or 0)
            if lifetime_delta > 0.001:
                d["kwh"] = round(lifetime_delta, 4)
                d["source"] = "lifetime"
            else:
                d["kwh"] = round(((d.get("avg_w") or 0) / 1000) * h_per_bucket, 4)
                d["source"] = "integration"
            buckets.append(d)

        # Period total
        total_kwh = sum(b["kwh"] for b in buckets)

        conn.close()
        return jsonify({
            "serial": serial,
            "range": range_str,
            "bucket_seconds": bucket_secs,
            "latest": dict(latest_row),
            "total_kwh": round(total_kwh, 3),
            "buckets": buckets,
        })
    except Exception as e:
        logger.error("Panel history query error: %s", e)
        return jsonify({"error": str(e)}), 500


# ── Health check ──────────────────────────────────────────────────────
# ── Outage tracking ───────────────────────────────────────────────────
_UPS_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4})\s+(.*)$")


def _ingest_ups_events():
    """Parse the apcupsd events log into grid_outages. The log is usually on
    tmpfs and lost on host reboot, so we persist each outage as we see it.
    Idempotent: keyed on start time; an ongoing outage (end NULL) gets its
    end filled in on a later pass."""
    if not UPS_EVENTS_FILE or not os.path.isfile(UPS_EVENTS_FILE):
        return
    try:
        with open(UPS_EVENTS_FILE, "r", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        logger.debug("UPS events read failed: %s", e)
        return
    outages, open_start, startups = [], None, []
    for line in lines:
        m = _UPS_LINE.match(line.strip())
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S %z").timestamp()
        except ValueError:
            continue
        msg = m.group(2)
        if "startup succeeded" in msg:
            startups.append(ts)
        if msg.startswith("Power failure"):
            if open_start is None:
                open_start = ts
        elif open_start is not None and (msg.startswith("Mains returned")
                                         or msg.startswith("Power is back")
                                         or "startup succeeded" in msg):
            # A daemon restart mid-outage means the host lost power and came
            # back — treat that as the end.
            outages.append((open_start, ts))
            open_start = None
    if open_start is not None:
        outages.append((open_start, None))
    if not outages and not startups:
        return
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.executemany(
            "INSERT INTO grid_outages (start, end, source) VALUES (?, ?, 'ups') "
            "ON CONFLICT(start) DO UPDATE SET end = COALESCE(excluded.end, grid_outages.end)",
            outages)
        # The host rebooting wipes the log, so the "Power failure" line for an
        # outage stored earlier is gone and the loop above can't close it.
        # The first daemon startup after it marks the end instead.
        for ts in sorted(startups):
            conn.execute(
                "UPDATE grid_outages SET end = ? WHERE end IS NULL AND start < ?",
                (ts, ts))
        conn.commit()
        conn.close()


def _classify_gap(a, ua, b, ub):
    """Cause of a data gap from PVS uptime (dl_uptime) on either side.
    If the PVS booted during the gap (uptime after < gap length, or uptime
    went backwards) it restarted — power loss or reboot. If it stayed up the
    whole time, the PVS was fine and the problem was WiFi/network.
    Returns (cause, restart_at): cause is "pvs_restart", "network" or None
    (no uptime recorded, e.g. history from before 2026-10-19)."""
    if ub is None:
        return None, None
    if ub < (b - a) or (ua is not None and ub < ua):
        return "pvs_restart", b - ub
    return "network", None


def _pvs_outages(conn, cutoff, end_ts):
    """PVS-offline events overlapping the window: gaps in `readings` longer
    than OUTAGE_GAP_MINS, plus any shorter gap where the PVS uptime shows it
    rebooted. Returns (start, end, cause, restart_at); start = last good
    reading, end = first reading back (None if ongoing)."""
    gap = OUTAGE_GAP_MINS * 60
    prev = conn.execute(
        "SELECT ts, pvs_uptime_s FROM readings WHERE ts < ? ORDER BY ts DESC LIMIT 1", (cutoff,)).fetchone()
    rows = conn.execute(
        "SELECT ts, pvs_uptime_s FROM readings WHERE ts >= ? AND ts <= ? ORDER BY ts", (cutoff, end_ts)).fetchall()
    if prev is not None:
        rows.insert(0, prev)
    out = []
    for (a, ua), (b, ub) in zip(rows, rows[1:]):
        cause, restart_at = _classify_gap(a, ua, b, ub)
        if b - a > gap or cause == "pvs_restart":
            out.append((a, b, cause, restart_at))
    if rows and end_ts >= time.time() - 60 and time.time() - rows[-1][0] > gap:
        out.append((rows[-1][0], None, None, None))
    return out


@app.route("/outages", methods=["GET"])
def outages():
    """PVS-offline (data gap) and grid power (UPS) outages in a window.
    Same ?range= / ?start=&end= semantics as /history. Newest first."""
    try:
        cutoff, end_ts, _ = _resolve_window(request.args.get("range", "24h"),
                                            request.args.get("start"), request.args.get("end"))
    except ValueError:
        return Response("Invalid start/end timestamps", status=400)
    now = time.time()
    events = []
    conn = sqlite3.connect(DB_PATH)
    try:
        for a, b, cause, restart_at in _pvs_outages(conn, cutoff, end_ts):
            events.append(("pvs", a, b, cause, restart_at))
        for a, b in conn.execute(
                "SELECT start, end FROM grid_outages WHERE start <= ? AND COALESCE(end, ?) >= ?",
                (end_ts, now, cutoff)):
            events.append(("grid", a, b, None, None))
    finally:
        conn.close()
    items = []
    summary = {"pvs": {"count": 0, "total_s": 0, "by_cause": {"network": 0, "pvs_restart": 0, "unknown": 0}},
               "grid": {"count": 0, "total_s": 0}}
    for kind, a, b, cause, restart_at in sorted(events, key=lambda e: e[1], reverse=True):
        dur = (b if b is not None else now) - a
        items.append({"kind": kind, "start": a, "end": b, "duration_s": round(dur), "ongoing": b is None,
                      "cause": cause, "restart_at": restart_at})
        summary[kind]["count"] += 1
        summary[kind]["total_s"] += round(dur)
        if kind == "pvs":
            summary["pvs"]["by_cause"][cause or "unknown"] += 1
    return jsonify({
        "outages": items,
        "summary": summary,
        "grid_tracking": bool(UPS_EVENTS_FILE and os.path.isfile(UPS_EVENTS_FILE)) or MOCK_PVS,
        "gap_threshold_min": OUTAGE_GAP_MINS,
        "period_start": cutoff,
        "period_end": end_ts,
    })


@app.route("/sunvault", methods=["GET"])
def sunvault_state():
    """SunVault grid/backup state from the fast midstate watch, plus the
    last few changes."""
    if MOCK_PVS and _midstate["value"] is None:
        v = MOCK_LIVEDATA_TEMPLATE.get("midstate") if MOCK_LIVEDATA_TEMPLATE else None
        if v is not None:
            _midstate.update(value=int(float(v)), since=time.time() - 3600, checked=time.time())
    try:
        conn = sqlite3.connect(DB_PATH)
        events = [{"ts": t, "value": v, "prev": p, "kind": _midstate_kind(v)} for t, v, p in conn.execute(
            "SELECT ts, value, prev FROM midstate_events ORDER BY ts DESC LIMIT 20")]
        conn.close()
    except sqlite3.Error:
        events = []
    v = _midstate["value"]
    return Response(json.dumps({
        "midstate": v,
        "kind": _midstate_kind(v),
        "since": _midstate["since"],
        "checked": _midstate["checked"],
        "events": events,
    }), mimetype="application/json")


@app.route("/health")
def health():
    data = cache.get()
    age = time.time() - cache.last_fetch if cache.last_fetch else -1

    # Count history entries
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        count = c.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        earliest = c.execute("SELECT MIN(ts) FROM readings").fetchone()[0]
        conn.close()
    except Exception:
        count = 0
        earliest = None

    return Response(json.dumps({
        "status": "ok" if data else "no_data",
        "cache_age_seconds": round(age, 1) if age >= 0 else None,
        "last_error": cache.last_error,
        "fetching": cache.fetching,
        "history_entries": count,
        "history_earliest": datetime.fromtimestamp(earliest, tz=timezone.utc).isoformat() if earliest else None,
    }), mimetype="application/json")


if __name__ == "__main__":
    _init_db()
    _load_dashboard()

    if MOCK_PVS:
        logger.info("MOCK_PVS=1 — fixture mode, no PVS gateway calls will be made")
        _load_mock_fixtures()
        _seed_mock_history()
        _seed_mock_outages()
        t = threading.Thread(target=_background_refresh_mock, daemon=True)
        t.start()
    elif PVS_IP and PVS_PASS:
        t = threading.Thread(target=_background_refresh, daemon=True)
        t.start()
        if MIDSTATE_POLL_SECS > 0:
            threading.Thread(target=_midstate_watch, daemon=True).start()
    else:
        logger.warning("PVS_IP or PVS_PASS not set — background refresh disabled, data will be fetched on-demand")

    logger.info("Starting solar proxy on %s:%s (timeout=%ss, refresh=%ss)", HOST, PORT, TIMEOUT_SECS, REFRESH_SECS)
    app.run(host=HOST, port=PORT)