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
  SOLAR_MONTHLY_COST - Fixed monthly solar payment (lease/PPA/loan) for the Net Cost card (default: 0)
  SOLAR_COST_START - First month (YYYY-MM) that payment applies to (optional)
  UTILITY_API_TOKEN - utilityapi.com API token; syncs utility bills + meter data (optional)
  UTILITY_API_SYNC_HOURS - How often to sync from UtilityAPI (default: 24, 0 = only on demand)
  UTILITY_API_REFRESH_DAY - Day of month to request one fresh collection (default: 24, 0 = off)
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

# Savings model: avoided cost = min(solar_kwh, home_kwh) * COST_PER_KWH,
# or with TOU_RATES set, the bill difference Σ (home - net import) × rate(t),
# which credits exports (NEM netting) and the battery's time shift.
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
# Time-of-use rate plan as JSON (all-in $/kWh, delivery + generation). Shape:
#   {"tz": "America/Los_Angeles", "adder": 0.05, "seasons": [
#     {"months": [6,7,8,9], "periods": [
#       {"name": "On peak", "rate": 0.62, "hours": [16, 21], "days": "weekday"},
#       {"name": "Off peak", "rate": 0.28}]}, ...]}
# The first matching period wins; one without "hours" is the season default.
# "days" is "weekday", "weekend" or omitted (every day). Holidays aren't modeled.
TOU_RATES_RAW = os.environ.get("TOU_RATES", "").strip()
CO2_LBS_PER_KWH = float(os.environ.get("CO2_LBS_PER_KWH", "0.85"))
# Fixed monthly solar payment (lease/PPA/loan), added to each utility bill
# in /finance. SOLAR_COST_START (YYYY-MM) limits it to bills from then on.
SOLAR_MONTHLY_COST = float(os.environ.get("SOLAR_MONTHLY_COST", "0") or 0)
SOLAR_COST_START = os.environ.get("SOLAR_COST_START", "").strip()
BILLS_DIR = os.path.join(os.path.dirname(DB_PATH), "bills")
# UtilityAPI (utilityapi.com) pulls bills and 15-min meter data from the
# utility. With a token set, pvswatch syncs every UTILITY_API_SYNC_HOURS.
UTILITY_API_TOKEN = os.environ.get("UTILITY_API_TOKEN", "").strip()
UTILITY_API_SYNC_HOURS = float(os.environ.get("UTILITY_API_SYNC_HOURS", "24") or 0)
UTILITY_API_BASE = "https://utilityapi.com/api/v2/"
# Ask UtilityAPI to collect fresh data from the utility once a calendar
# month, on or after this day (0 = never). UtilityAPI's first collection
# each month is free; later ones are billed, so this never runs twice in
# a month, even after a failure.
UTILITY_API_REFRESH_DAY = int(os.environ.get("UTILITY_API_REFRESH_DAY", "24") or 0)

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
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # bill PDF / Green Button uploads


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
    # Utility bills, one row per bill (keyed by the date it was prepared).
    # settlement = the NEM 12-month settlement charges included in amount.
    c.execute("""CREATE TABLE IF NOT EXISTS bills (
        bill_date TEXT PRIMARY KEY,
        period_start TEXT,
        period_end TEXT,
        amount REAL,
        settlement REAL,
        nem_month INTEGER,
        net_kwh REAL,
        import_kwh REAL,
        export_kwh REAL,
        peak_import_kwh REAL,
        base_charge REAL,
        source TEXT,
        file TEXT,
        notes TEXT,
        updated REAL
    )""")
    # rate_scale added 2026-10-22: that bill's energy rates / today's TOU_RATES.
    # rates added 2026-10-22: JSON {"summer:on": $/kWh, ...} from the bill.
    for col, typ in (("rate_scale", "REAL"), ("rates", "TEXT")):
        try:
            c.execute(f"ALTER TABLE bills ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    # Dated notes shown on the Net Cost chart (e.g. a billing plan change).
    c.execute("""CREATE TABLE IF NOT EXISTS annotations (
        date TEXT,
        label TEXT,
        detail TEXT,
        PRIMARY KEY (date, label)
    )""")
    # Monthly solar made / home used from another source (e.g. the solar
    # company's app) for months before pvswatch was recording.
    c.execute("""CREATE TABLE IF NOT EXISTS solar_months (
        month TEXT PRIMARY KEY,
        solar_kwh REAL,
        home_kwh REAL,
        source TEXT
    )""")
    # Meter intervals from the utility's Green Button download.
    c.execute("""CREATE TABLE IF NOT EXISTS utility_intervals (
        ts REAL PRIMARY KEY,
        secs INTEGER,
        delivered_kwh REAL,
        received_kwh REAL
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
        # midstate added 2026-10-21 — livedata midstate (Hub+/MID state).
        # 2 = on grid; see _midstate_kind.
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


# ── Time-of-use rates ────────────────────────────────────────────────
def _load_tou(raw):
    if not raw:
        return None
    try:
        from zoneinfo import ZoneInfo
        plan = json.loads(raw)
        plan["_tz"] = ZoneInfo(plan.get("tz", "UTC"))
        plan["adder"] = float(plan.get("adder", 0))
        for season in plan["seasons"]:
            season["months"] = set(season["months"])
            for p in season["periods"]:
                p["rate"] = float(p["rate"])
        return plan
    except Exception as e:
        logger.error("TOU_RATES is invalid, using flat COST_PER_KWH: %s", e)
        return None


TOU_PLAN = _load_tou(TOU_RATES_RAW)


def _tou_period(ts, plan=None):
    """(name, $/kWh) for an epoch under the TOU plan, or None without one."""
    plan = plan or TOU_PLAN
    if not plan:
        return None
    t = datetime.fromtimestamp(ts, plan["_tz"])
    weekend = t.weekday() >= 5
    for season in plan["seasons"]:
        if t.month not in season["months"]:
            continue
        for p in season["periods"]:
            days = p.get("days")
            if (days == "weekday" and weekend) or (days == "weekend" and not weekend):
                continue
            h = p.get("hours")
            if h and not (h[0] <= t.hour < h[1]):
                continue
            return p.get("name", ""), p["rate"] + plan["adder"]
    return None


def _tou_savings(conn, cutoff, end_ts, solar_kwh, elapsed_h):
    """Bill savings under NEM netting with time-of-use rates.

    Every solar kWh offsets grid energy at the rate in effect when it was
    made (used at home or exported), and the battery adds the spread between
    when it charges and when it discharges:
        solar_kwh x production-weighted rate + sum(battery_kw x rate x dt)
    solar_kwh comes from the lifetime counter (robust). The 5-min readings
    supply only the timing: they miss data gaps and pv_p goes stale between
    PVS scans, so their summed energy runs low. The battery term is scaled
    up by the share of the window the readings cover."""
    rows = conn.execute(
        "SELECT ts, production_kw, battery_kw FROM readings WHERE ts > ? AND ts <= ? ORDER BY ts",
        (cutoff, end_ts)).fetchall()
    cap = 2 * REFRESH_SECS
    pv_e = pv_cost = bat_cost = covered = 0.0
    for (ts, pv, bat), nxt in zip(rows, rows[1:]):
        per = _tou_period(ts)
        if per is None:
            continue
        dt = min(nxt[0] - ts, cap) / 3600
        covered += dt
        rate = per[1]
        if pv and pv > 0:
            pv_e += pv * dt
            pv_cost += pv * dt * rate
        if bat:
            bat_cost += bat * dt * rate  # + discharging (saves), - charging (costs)
    if pv_e > 0.01:
        pv_rate = pv_cost / pv_e
    else:  # no production in the readings: daytime average
        pv_rate = _tou_period(end_ts)[1] if _tou_period(end_ts) else COST_PER_KWH
    scale = elapsed_h / covered if covered > 0 else 0
    return solar_kwh * pv_rate + bat_cost * min(scale, 3)


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
        if os.path.exists(candidate) and _fixture_has_readings(candidate):
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


def _fixture_has_readings(path):
    """False for an empty or unreadable fixture, so seeding falls through to
    the next one instead of starting with no history."""
    try:
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        n = src.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        src.close()
    except sqlite3.Error:
        return False
    if not n:
        logger.warning("Fixture %s has no readings; trying the next one", path)
    return bool(n)


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


def _seed_mock_bills():
    """Synthetic bills for the Net Cost card in mock mode: 14 monthly bills,
    one NEM settlement, and a current bill whose period sits inside the
    seeded history so the no-solar estimate has data. Plus one note."""
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        if conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0] == 0:
            lo, hi = conn.execute("SELECT MIN(ts), MAX(ts) FROM readings").fetchone()
            hi = hi or time.time()
            lo = lo or hi - 5 * 86400
            day = lambda ts: datetime.fromtimestamp(ts, _local_tz()).strftime("%Y-%m-%d")
            start, end = lo + 86400, hi - 86400
            if end - start < 2 * 86400:
                start = end - 2 * 86400
            rows = [(day(hi), day(start), day(end), 120.0, None, 150.0, 20.0)]
            for i in range(1, 14):
                pe = start - 86400 - (i - 1) * 30 * 86400
                amount = 40.0 + (i * 17) % 50
                settle = 400.0 if i == 4 else None
                rows.append((day(pe + 4 * 86400), day(pe - 29 * 86400), day(pe),
                             amount + (settle or 0), settle, 100.0 + (i * 37) % 300, 20.0))
            conn.executemany("INSERT INTO bills (bill_date, period_start, period_end, amount, settlement, "
                             "net_kwh, base_charge, source, updated) VALUES (?, ?, ?, ?, ?, ?, ?, 'mock', ?)",
                             [r + (time.time(),) for r in rows])
            conn.execute("INSERT OR IGNORE INTO annotations (date, label, detail) VALUES (?, ?, ?)",
                         (day(start), "Billing switched to monthly", "Mock note"))
            conn.commit()
        conn.close()


def _background_refresh_mock():
    """Mock equivalent of _background_refresh."""
    logger.info("Starting mock refresh loop (every %ds)", REFRESH_SECS)
    _refresh_mock()
    while True:
        time.sleep(REFRESH_SECS)
        _refresh_mock()


# ── Bills & net cost ─────────────────────────────────────────────────
# One row per utility bill (uploaded PDF, Green Button, or manual entry).
# /finance rolls bills up by month / quarter / year / NEM year, adds the
# fixed solar payment, and estimates what the house would have cost with
# no solar, so "is solar saving me money?" has a number behind it.
BILL_COLS = ("bill_date", "period_start", "period_end", "amount", "settlement",
             "nem_month", "net_kwh", "import_kwh", "export_kwh",
             "peak_import_kwh", "base_charge", "source", "file", "notes", "rate_scale", "rates")
_DATE_RE = re.compile(r"^\d{4}-\d\d-\d\d$")


def _local_tz():
    if TOU_PLAN:
        return TOU_PLAN["_tz"]
    from zoneinfo import ZoneInfo
    return ZoneInfo("UTC")


def _us_date(s):
    """MM/DD/YY or MM/DD/YYYY -> YYYY-MM-DD."""
    m, d, y = (int(x) for x in s.split("/"))
    return f"{y + 2000 if y < 100 else y:04d}-{m:02d}-{d:02d}"


def _num(s):
    return float(s.replace(",", "").replace("$", ""))


def _parse_sce_bill(text):
    """Pull the fields /finance needs out of an SCE bill PDF's text.
    Anything not found is left out; the upload keeps what it can."""
    out = {}
    date = r"(\d\d?/\d\d?/\d\d(?:\d\d)?)"
    m = re.search(r"Date bill prepared\s*" + date, text)
    if m:
        out["bill_date"] = _us_date(m.group(1))
    m = re.search(r"Your new charges\s*(-?)\s*\$\s*([\d,]+\.\d\d)", text)
    if m:
        out["amount"] = -_num(m.group(2)) if m.group(1) else _num(m.group(2))
    m = re.search(r"Billing period:?\s*" + date + r"\s*to\s*" + date, text)
    if m:
        out["period_start"], out["period_end"] = _us_date(m.group(1)), _us_date(m.group(2))
    m = re.search(r"settlement charges are\s*(-?)\$\s*([\d,]+\.\d\d)", text)
    if m:
        out["settlement"] = -_num(m.group(2)) if m.group(1) else _num(m.group(2))
    m = re.search(r"\(NEM\)\s*month\s*(\d+)", text)
    if m:
        out["nem_month"] = int(m.group(1))
    m = re.search(r"net kWh:\s*(-?[\d,]+)", text)
    if m:
        out["net_kwh"] = _num(m.group(1))
    # Usage table: "<period> <consumption> <net generation> <total>" per TOU
    # period. On/mid peak are both 4-9 PM on TOU-D-PRIME.
    usage = text.split("Your past and current electricity usage", 1)
    if len(usage) == 2:
        block = usage[1].split("Details of your new charges", 1)[0]
        rows = re.findall(r"^(On peak|Mid peak|Off peak|Super off peak)\s+(-?[\d,]+)\s+(-?[\d,]+)\s+(-?[\d,]+)\s*$",
                          block, re.M | re.I)
        if rows:
            out["import_kwh"] = sum(_num(r[1]) for r in rows)
            out["export_kwh"] = -sum(_num(r[2]) for r in rows)
            out["peak_import_kwh"] = sum(_num(r[1]) for r in rows if r[0].lower() in ("on peak", "mid peak"))
    m = re.search(r"Base services charge\s+\d+\s*days\s*x\s*\$[\d.]+\s*\$([\d,]+\.\d\d)", text)
    if m:
        out["base_charge"] = _num(m.group(1))
    return out


def _pdf_text(data):
    import io
    from pypdf import PdfReader
    return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)


def _clean_bill(b):
    """Validate/coerce one bill dict from JSON. Returns (bill, error)."""
    if not isinstance(b, dict) or not _DATE_RE.match(str(b.get("bill_date", ""))):
        return None, "bill_date (YYYY-MM-DD) is required"
    out = {"bill_date": b["bill_date"]}
    for k in BILL_COLS[1:]:
        v = b.get(k)
        if v in (None, ""):
            out[k] = None
        elif k in ("period_start", "period_end"):
            if not _DATE_RE.match(str(v)):
                return None, f"{k} must be YYYY-MM-DD"
            out[k] = v
        elif k in ("source", "file", "notes", "rates"):
            out[k] = str(v)[:500]
        else:
            try:
                out[k] = int(v) if k == "nem_month" else float(v)
            except (TypeError, ValueError):
                return None, f"{k} must be a number"
    return out, None


def _upsert_bill(conn, bill, replace=False):
    """Insert or update a bill. By default fields left None keep their stored
    value (so a re-upload doesn't wipe notes); replace=True overwrites all."""
    cols = [k for k in BILL_COLS if k in bill]
    sets = ", ".join(f"{k}=excluded.{k}" if replace else f"{k}=COALESCE(excluded.{k}, {k})"
                     for k in cols if k != "bill_date")
    conn.execute(f"INSERT INTO bills ({', '.join(cols)}, updated) VALUES ({', '.join('?' * len(cols))}, ?) "
                 f"ON CONFLICT(bill_date) DO UPDATE SET {sets}, updated=excluded.updated",
                 [bill[k] for k in cols] + [time.time()])


def _parse_green_button(data):
    """Interval readings from a Green Button download: ESPI XML (the
    standard) or the utility's CSV. Returns [(ts, secs, delivered, received)]."""
    text = data.decode("utf-8-sig", errors="replace")
    out = []
    if text.lstrip().startswith("<"):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(text)
        flow, mult = 1, 0  # ReadingType applies to the IntervalBlocks after it
        for el in root.iter():
            tag = el.tag.rsplit("}", 1)[-1]
            if tag == "ReadingType":
                kids = {c.tag.rsplit("}", 1)[-1]: (c.text or "").strip() for c in el}
                flow = int(kids.get("flowDirection") or 1)
                mult = int(kids.get("powerOfTenMultiplier") or 0)
                uom = int(kids.get("uom") or 72)
                if uom == 72:  # Wh
                    mult -= 3
            elif tag == "IntervalReading":
                kids = {c.tag.rsplit("}", 1)[-1]: c for c in el}
                tp = kids.get("timePeriod")
                if tp is None or "value" not in kids:
                    continue
                t = {c.tag.rsplit("}", 1)[-1]: c.text for c in tp}
                kwh = float(kids["value"].text) * 10 ** mult
                received = flow == 19 or kwh < 0
                out.append((float(t["start"]), int(t["duration"]),
                            0.0 if received else kwh, abs(kwh) if received else 0.0))
        return out
    # CSV: "YYYY-MM-DD HH:MM[:SS] to YYYY-MM-DD HH:MM[:SS]","<kWh>" rows,
    # in sections whose headers say whether they're delivered or received.
    tz = _local_tz()
    stamp = r"(\d{4}-\d\d-\d\d[ T]\d\d:\d\d(?::\d\d)?)"
    row = re.compile(stamp + r"\s*(?:to|-)\s*" + stamp + r'"?\s*,\s*"?(-?[\d.]+)')
    received = False
    for line in text.splitlines():
        m = row.search(line)
        if not m:
            low = line.lower()
            if "received" in low or "generat" in low or "export" in low:
                received = True
            elif "delivered" in low or "consum" in low:
                received = False
            continue
        a, b = (datetime.fromisoformat(x.replace(" ", "T")).replace(tzinfo=tz).timestamp() for x in m.group(1, 2))
        kwh = float(m.group(3))
        rec = received or kwh < 0
        out.append((a, int(b - a), 0.0 if rec else kwh, abs(kwh) if rec else 0.0))
    return out


def _period_bounds(bill, tz):
    t0 = datetime.strptime(bill["period_start"], "%Y-%m-%d").replace(tzinfo=tz).timestamp()
    t1 = datetime.strptime(bill["period_end"], "%Y-%m-%d").replace(tzinfo=tz).timestamp() + 86400
    return t0, t1


def _counter_delta(conn, col, t0, t1, slack=36 * 3600):
    """A lifetime counter's growth over [t0, t1), scaled up when the first or
    last reading is a little inside the window. None without readings near
    both ends."""
    a = conn.execute(f"SELECT ts, {col} FROM readings WHERE ts >= ? AND ts < ? AND {col} IS NOT NULL "
                     "ORDER BY ts LIMIT 1", (t0, t0 + slack)).fetchone()
    b = conn.execute(f"SELECT ts, {col} FROM readings WHERE ts > ? AND ts <= ? AND {col} IS NOT NULL "
                     "ORDER BY ts DESC LIMIT 1", (t1 - slack, t1)).fetchone()
    if not a or not b or b[0] - a[0] < (t1 - t0) / 2:
        return None
    return (b[1] - a[1]) * (t1 - t0) / (b[0] - a[0])


def _interval_net(conn, t0, t1, tz):
    """(net, delivered, received, delivered 4-9 PM) kWh from utility meter
    intervals if they cover at least 90% of the window."""
    rows = conn.execute("SELECT ts, secs, delivered_kwh, received_kwh FROM utility_intervals "
                        "WHERE ts >= ? AND ts < ?", (t0, t1)).fetchall()
    if sum(r[1] for r in rows) < 0.9 * (t1 - t0):
        return None
    d = sum(r[2] for r in rows)
    rcv = sum(r[3] for r in rows)
    peak = sum(r[2] for r in rows if 16 <= datetime.fromtimestamp(r[0], tz).hour < 21)
    return d - rcv, d, rcv, peak


def _load_rates(conn, t0=None, t1=None):
    """Home-use-weighted TOU rate from pvswatch readings: one number for the
    window, or with no window a {calendar month: rate} map over all
    history (the typical rate for months pvswatch didn't record)."""
    q, args = "SELECT ts, consumption_kw FROM readings", ()
    if t0 is not None:
        q, args = q + " WHERE ts >= ? AND ts < ?", (t0, t1)
    rows = conn.execute(q + " ORDER BY ts", args).fetchall()
    acc = {}
    tz = _local_tz()
    for (ts, kw), nxt in zip(rows, rows[1:]):
        if not kw or kw <= 0:
            continue
        dt = min(nxt[0] - ts, 2 * REFRESH_SECS) / 3600
        per = _tou_period(ts)
        key = 0 if t0 is not None else datetime.fromtimestamp(ts, tz).month
        e, c = acc.get(key, (0.0, 0.0))
        acc[key] = (e + kw * dt, c + kw * dt * (per[1] if per else COST_PER_KWH))
    rates = {k: c / e for k, (e, c) in acc.items() if e > 0}
    return rates.get(0) if t0 is not None else rates


def _months_solar(conn, bill, col="solar_kwh"):
    """Solar (or home, col="home_kwh") kWh for a bill period from
    solar_months, prorated by days. None unless every month the period
    touches is on file (solar falls back to pvswatch's own monthly total)."""
    d0 = datetime.strptime(bill["period_start"], "%Y-%m-%d").date()
    d1 = datetime.strptime(bill["period_end"], "%Y-%m-%d").date()
    have = dict(conn.execute(f"SELECT month, {col} FROM solar_months WHERE {col} IS NOT NULL"))
    tz = _local_tz()
    total, day = 0.0, d0
    while day <= d1:
        key = day.strftime("%Y-%m")
        nxt = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
        if key not in have and col != "solar_kwh":
            return None
        if key not in have:
            # Not on file: pvswatch's own total for that calendar month.
            m0 = datetime(day.year, day.month, 1, tzinfo=tz).timestamp()
            m1 = datetime(nxt.year, nxt.month, 1, tzinfo=tz).timestamp()
            got = _counter_delta(conn, "lifetime_kwh", m0, m1)
            if got is None:
                return None
            have[key] = got
        in_month = (nxt - day.replace(day=1)).days
        span = (min(nxt, d1 + timedelta(days=1)) - day).days
        total += have[key] * span / in_month
        day = nxt
    return total


def _no_solar_estimate(conn, bill, tz, typical_rates=None):
    """What a bill period would have cost with no solar. Home use = solar
    made + net kWh from the grid, priced at the TOU rate weighted by when
    the home uses power, plus the bill's fixed base charge, which a
    no-solar home pays too. Battery losses count as home use, so this runs
    a little high.

    Solar comes from the inverter lifetime counter when pvswatch covers the
    period; otherwise from solar_months (e.g. the solar company's app),
    priced at the typical rate pvswatch has seen for that calendar month."""
    if not bill.get("period_start") or not bill.get("period_end"):
        return None
    net = bill.get("net_kwh")
    if net is None:
        # Older bills with only a total: home use straight from the
        # monthly backfill instead of solar + net.
        home = _months_solar(conn, bill, "home_kwh")
        if not home:
            return None
        t0, t1 = _period_bounds(bill, tz)
        net = home - (_months_solar(conn, bill) or 0)
    t0, t1 = _period_bounds(bill, tz)
    solar = _counter_delta(conn, "lifetime_kwh", t0, t1)
    basis = "pvswatch"
    if solar is not None:
        rate = _load_rates(conn, t0, t1)
    else:
        solar = _months_solar(conn, bill)
        if solar is None:
            return None
        basis = "monthly"
        mid = datetime.fromtimestamp((t0 + t1) / 2, tz).month
        typical_rates = typical_rates or {}
        rate = typical_rates.get(mid)
        if rate is None and typical_rates:
            # No history for that calendar month: average the months pvswatch
            # has seen in the same TOU season, else all of them.
            season = next((se["months"] for se in (TOU_PLAN or {}).get("seasons", []) if mid in se["months"]), set())
            mates = [v for m, v in typical_rates.items() if m in season] or list(typical_rates.values())
            rate = sum(mates) / len(mates)
    # Today's TOU rates, scaled to the rates on this bill when known.
    rate = (rate or COST_PER_KWH) * (bill.get("rate_scale") or 1)
    home = solar + net
    if home <= 0:
        return None
    return {"solar_kwh": solar, "home_kwh": home, "rate": rate, "basis": basis,
            "rates": "bill" if bill.get("rate_scale") else "today",
            "cost": home * rate + (bill.get("base_charge") or 0)}


def _spread_settlements(bills):
    """Under annual NEM the year's energy charges land on one settlement
    bill. Give each bill its share ("accrued") so months compare fairly:
    a settlement is split over the bills of its NEM year by net kWh used
    (equal shares when kWh are missing)."""
    for b in bills:
        b["accrued"] = b["amount"] - (b.get("settlement") or 0)
    prev = -1
    for i, s in enumerate(bills):
        if not s.get("settlement"):
            continue
        # The NEM year: bills since the previous settlement, at most 12 and
        # all ending within a year before this one.
        cutoff = str(int(s["period_end"][:4]) - 1) + s["period_end"][4:]
        group = [b for b in bills[max(prev + 1, i - 11):i + 1] if b["period_end"] > cutoff]
        kwh = [b.get("net_kwh") for b in group]
        if None in kwh or sum(kwh) <= 0:
            kwh = [1.0] * len(group)
        for b, w in zip(group, kwh):
            b["accrued"] += s["settlement"] * w / sum(kwh)
        prev = i


def _group_key(period_end, group, nem_end):
    y, m = int(period_end[:4]), int(period_end[5:7])
    if group == "quarter":
        return f"{y}-Q{(m - 1) // 3 + 1}", (y, (m - 1) // 3)
    if group == "year":
        return str(y), (y,)
    if group == "nemyear":
        ny = y if m <= nem_end else y + 1
        return f"NEM {ny}", (ny,)
    return f"{y}-{m:02d}", (y, m)


def _finance(group="month", spread=True):
    tz = _local_tz()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    bills = [dict(r) for r in conn.execute(
        "SELECT * FROM bills WHERE amount IS NOT NULL ORDER BY COALESCE(period_end, bill_date)")]
    notes = [dict(r) for r in conn.execute("SELECT date, label, detail FROM annotations ORDER BY date")]
    typical = _load_rates(conn) if conn.execute("SELECT 1 FROM solar_months LIMIT 1").fetchone() else {}
    for b in bills:
        b["period_end"] = b["period_end"] or b["bill_date"]
        if b.get("period_start"):
            iv = _interval_net(conn, *_period_bounds(b, tz), tz)
            if iv:
                for k, v in zip(("net_kwh", "import_kwh", "export_kwh", "peak_import_kwh"), iv):
                    if b[k] is None:
                        b[k] = round(v, 1)
        b["no_solar"] = _no_solar_estimate(conn, b, tz, typical)
        b["solar_payment"] = SOLAR_MONTHLY_COST if (not SOLAR_COST_START or b["period_end"][:7] >= SOLAR_COST_START) else 0.0
    conn.close()
    _spread_settlements(bills)
    sett = [b for b in bills if b.get("settlement")]
    nem_end = int(sett[-1]["period_end"][5:7]) if sett else 12

    rows, by_key = [], {}
    for b in bills:
        key, sort = _group_key(b["period_end"], group, nem_end)
        r = by_key.get(key)
        if r is None:
            r = by_key[key] = {"key": key, "sort": sort, "bills": [], "sce_cash": 0.0, "sce": 0.0,
                               "solar_payment": 0.0, "no_solar": 0.0, "no_solar_bills": 0,
                               "paid_with_data": 0.0, "net_kwh": None, "import_kwh": None,
                               "export_kwh": None, "peak_import_kwh": None, "settlement": 0.0,
                               "start": b.get("period_start") or b["bill_date"], "end": b["period_end"]}
            rows.append(r)
        sce = b["accrued"] if spread else b["amount"]
        r["bills"].append(b["bill_date"])
        r.setdefault("_totals", []).append(sce + b["solar_payment"])
        r["sce_cash"] += b["amount"]
        r["sce"] += sce
        r["solar_payment"] += b["solar_payment"]
        r["settlement"] += b.get("settlement") or 0
        r["end"] = b["period_end"]
        for k in ("net_kwh", "import_kwh", "export_kwh", "peak_import_kwh"):
            if b.get(k) is not None:
                r[k] = (r[k] or 0) + b[k]
        if b["no_solar"]:
            r["no_solar"] += b["no_solar"]["cost"]
            r["no_solar_bills"] += 1
            r["paid_with_data"] += sce + b["solar_payment"]
    expected = {"month": 1, "quarter": 3, "year": 12, "nemyear": 12}[group]
    for i, r in enumerate(rows):
        r["total"] = r["sce"] + r["solar_payment"]
        n = len(r["bills"])
        r["expected_bills"] = expected
        # The newest period is still filling in; compare it with the same
        # number of bills from the earlier period ("so far").
        r["in_progress"] = i == len(rows) - 1 and n < expected
        r["coverage"] = "full" if r["no_solar_bills"] == n else "partial" if r["no_solar_bills"] else "none"
        r["saved"] = r["no_solar"] - r["paid_with_data"] if r["no_solar_bills"] else None
        if not r["no_solar_bills"]:
            r["no_solar"] = None
        prev = rows[i - 1] if i else None
        ly = by_key.get(_shift_year(r["key"], group))
        # Compare only like with like: full periods, or the period in
        # progress against the same number of bills before. A period with
        # bills missing (before history starts) isn't compared.
        if r["in_progress"]:
            cmp = lambda o: sum(o["_totals"][:n]) if len(o["_totals"]) >= n else None
        elif n < expected:
            cmp = lambda o: None
        else:
            cmp = lambda o: o["total"] if len(o["bills"]) >= expected else None
        r["prev_total"] = cmp(prev) if prev else None
        r["yoy_total"] = cmp(ly) if ly else None
        r["prev_saved"] = prev["saved"] if prev else None
        r["yoy_saved"] = ly["saved"] if ly else None

    for r in rows:
        r.pop("_totals", None)

    # Headline over the last 12 billed months.
    last12 = bills[-12:]
    prior12 = bills[-24:-12]
    with_data = [b for b in last12 if b["no_solar"]]
    sce_of = (lambda b: b["accrued"] if spread else b["amount"])
    paid = sum(sce_of(b) + b["solar_payment"] for b in with_data)
    est = sum(b["no_solar"]["cost"] for b in with_data)
    headline = {
        "months": len(with_data),
        "paid": paid, "no_solar": est, "saved": est - paid,
        "per_month": (est - paid) / len(with_data) if with_data else None,
        "sce": sum(sce_of(b) for b in with_data),
        "solar_payment": sum(b["solar_payment"] for b in with_data),
        "net_cost_12": sum(sce_of(b) + b["solar_payment"] for b in last12) if last12 else None,
        "net_cost_prior_12": sum(sce_of(b) + b["solar_payment"] for b in prior12) if len(prior12) == 12 else None,
        "bills_12": len(last12),
    }
    # This year so far vs. the same stretch of last year, and since the
    # first bill. Savings sum only bills with an estimate.
    def _saved(bs):
        est = [b for b in bs if b["no_solar"]]
        paid = sum(sce_of(b) + b["solar_payment"] for b in est)
        ns = sum(b["no_solar"]["cost"] for b in est)
        return {"saved": ns - paid if est else None, "paid": paid, "no_solar": ns,
                "bills": len(bs), "with_data": len(est)}
    if bills:
        year = int(bills[-1]["period_end"][:4])
        cur = [b for b in bills if int(b["period_end"][:4]) == year]
        prev = [b for b in bills if int(b["period_end"][:4]) == year - 1][:len(cur)]
        headline["ytd"] = {"year": year, **_saved(cur),
                           "prev": {"year": year - 1, **_saved(prev)} if len(prev) == len(cur) else None}
        life = _saved(bills)
        life["since"] = bills[0].get("period_start") or bills[0]["bill_date"]
        life["per_month"] = life["saved"] / life["with_data"] if life["with_data"] else None
        headline["lifetime"] = life
    for b in bills:
        if b["no_solar"]:
            b["no_solar"] = {k: round(v, 3) if isinstance(v, float) else v for k, v in b["no_solar"].items()}
    return {
        "group": group, "spread": spread,
        "solar_monthly_cost": SOLAR_MONTHLY_COST, "solar_cost_start": SOLAR_COST_START or None,
        "rate_model": "tou" if TOU_PLAN else "flat",
        "nem_settlement_month": nem_end if sett else None,
        "headline": headline, "rows": rows, "annotations": notes, "bills": bills,
        "utilityapi": {"enabled": bool(UTILITY_API_TOKEN), "last_sync": _uapi["last_sync"],
                       "last_error": _uapi["last_error"], "refresh_day": UTILITY_API_REFRESH_DAY,
                       **{k: v for k, v in _uapi_state().items() if k.startswith("refresh_")}},
    }


def _shift_year(key, group):
    if key.startswith("NEM "):
        return f"NEM {int(key[4:]) - 1}"
    return str(int(key[:4]) - 1) + key[4:]


# ── Time-of-use schedule + rate history (/tou) ───────────────────────
RATE_LABELS = {"summer:on": "Summer on peak", "summer:mid": "Summer mid peak", "summer:off": "Summer off peak",
               "winter:mid": "Winter mid peak", "winter:off": "Winter off peak", "winter:super": "Winter super off peak"}


def _tou_segments(day):
    """[{start, end (hours), name, rate}] for one local date under the plan.
    Rates are delivery + generation, without the plan's per-kWh adder, so
    they compare with the rates printed on bills."""
    tz = TOU_PLAN["_tz"]
    segs = []
    for h in range(24):
        ts = datetime(day.year, day.month, day.day, h, 30, tzinfo=tz).timestamp()
        name, rate = _tou_period(ts)
        rate = round(rate - TOU_PLAN["adder"], 4)
        if segs and segs[-1]["name"] == name:
            segs[-1]["end"] = h + 1
        else:
            segs.append({"start": h, "end": h + 1, "name": name, "rate": rate})
    return segs


def _rate_history():
    """Per-year average of each TOU rate from bills, the plan's rates now,
    and the dates a rate changed between consecutive bills."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT period_start, rates FROM bills WHERE rates IS NOT NULL "
                        "ORDER BY period_start").fetchall()
    conn.close()
    years, last, changes = {}, {}, []
    for start, raw in rows:
        try:
            rates = json.loads(raw)
        except ValueError:
            continue
        for k, v in rates.items():
            years.setdefault(start[:4], {}).setdefault(k, []).append(v)
            if k in last and abs(v - last[k]) >= 0.0005:
                changes.append({"date": start, "key": k, "label": RATE_LABELS.get(k, k),
                                "from": last[k], "to": v})
            last[k] = v
    now = {}
    if TOU_PLAN:
        names = {"on peak": "on", "mid peak": "mid", "off peak": "off", "super off peak": "super"}
        for se in TOU_PLAN["seasons"]:
            season = "summer" if 7 in se["months"] else "winter"
            for p in se["periods"]:
                key = names.get(p.get("name", "").lower())
                if key:
                    now[f"{season}:{key}"] = p["rate"]
    keys = [k for k in RATE_LABELS if k in now or any(k in y for y in years.values())]
    return {
        "years": sorted(years),
        "rows": [{"key": k, "label": RATE_LABELS[k], "now": now.get(k),
                  "by_year": {y: round(sum(v[k]) / len(v[k]), 4) for y, v in years.items() if k in v}}
                 for k in keys],
        "changes": changes,
    }


@app.route("/tou", methods=["GET"])
def tou():
    if not TOU_PLAN:
        return jsonify({"enabled": False, "history": _rate_history()})
    tz = TOU_PLAN["_tz"]
    now = datetime.now(tz)
    today = _tou_segments(now.date())
    # The other kind of day (weekday vs weekend) when its schedule differs.
    other_day = now.date() + timedelta(days=1)
    while (other_day.weekday() >= 5) == (now.weekday() >= 5):
        other_day += timedelta(days=1)
    other = _tou_segments(other_day)
    cur = next(sg for sg in today if sg["start"] <= now.hour < sg["end"])
    # Next change, looking into tomorrow if today has none left.
    nxt, until = None, None
    for day_off in range(0, 2):
        segs = today if day_off == 0 else _tou_segments(now.date() + timedelta(days=1))
        for sg in segs:
            start = datetime(now.year, now.month, now.day, tzinfo=tz) + timedelta(days=day_off, hours=sg["start"])
            if start > now and sg["name"] != cur["name"]:
                nxt, until = sg, start.timestamp()
                break
        if nxt:
            break
    rates = sorted({(sg["rate"], sg["name"]) for sg in today})
    return jsonify({
        "enabled": True,
        "season": "summer" if now.month in next(se["months"] for se in TOU_PLAN["seasons"] if 7 in se["months"]) else "winter",
        "adder": TOU_PLAN["adder"],
        "now": {**cur, "until": until, "next": nxt,
                "cheapest": cur["rate"] == rates[0][0], "priciest": cur["rate"] == rates[-1][0]},
        "today": today,
        "today_label": "weekends" if now.weekday() >= 5 else "weekdays",
        "other": other if [(s_["start"], s_["name"]) for s_ in other] != [(s_["start"], s_["name"]) for s_ in today] else None,
        "other_label": "weekends" if other_day.weekday() >= 5 else "weekdays",
        "history": _rate_history(),
    })


@app.route("/finance", methods=["GET"])
def finance():
    group = request.args.get("group", "month")
    if group not in ("month", "quarter", "year", "nemyear"):
        return jsonify({"error": "group must be month, quarter, year or nemyear"}), 400
    return jsonify(_finance(group, request.args.get("spread", "1") != "0"))


@app.route("/bills", methods=["GET"])
def bills_list():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    out = [dict(r) for r in conn.execute("SELECT * FROM bills ORDER BY bill_date DESC")]
    conn.close()
    return jsonify({"bills": out})


@app.route("/bills", methods=["POST"])
def bills_save():
    """Add or update bills. multipart with file= (one or more bill PDFs, parsed
    and stored under DATA_DIR/bills), or JSON: one bill or a list. JSON with
    ?replace=1 overwrites every field (the edit form); otherwise blank fields
    keep their stored values."""
    results = []
    if request.files:
        os.makedirs(BILLS_DIR, exist_ok=True)
        for f in request.files.getlist("file"):
            data = f.read()
            if not data.startswith(b"%PDF"):
                results.append({"file": f.filename, "error": "not a PDF"})
                continue
            try:
                parsed = _parse_sce_bill(_pdf_text(data))
            except Exception as e:
                logger.warning("Bill PDF %s unreadable: %s", f.filename, e)
                results.append({"file": f.filename, "error": "couldn't read the PDF"})
                continue
            if "bill_date" not in parsed or "amount" not in parsed:
                results.append({"file": f.filename, "error": "couldn't find the bill date and amount",
                                "parsed": parsed})
                continue
            name = parsed["bill_date"] + ".pdf"
            with open(os.path.join(BILLS_DIR, name), "wb") as fh:
                fh.write(data)
            parsed.update(source="pdf", file=name)
            with _db_lock:
                conn = sqlite3.connect(DB_PATH)
                _upsert_bill(conn, parsed)
                conn.commit()
                conn.close()
            results.append({"file": f.filename, "bill": parsed})
        return jsonify({"results": results})
    body = request.get_json(silent=True)
    items = body if isinstance(body, list) else [body]
    replace = request.args.get("replace") == "1"
    clean = []
    for item in items:
        bill, err = _clean_bill(item)
        if err:
            return jsonify({"error": err, "bill": item}), 400
        if bill["amount"] is None and replace:
            return jsonify({"error": "amount is required", "bill": item}), 400
        clean.append(bill)
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        for bill in clean:
            if not replace:
                bill = {k: v for k, v in bill.items() if v is not None}
            _upsert_bill(conn, bill, replace)
        conn.commit()
        conn.close()
    return jsonify({"saved": len(clean)})


@app.route("/bills/<bill_date>", methods=["DELETE"])
def bills_delete(bill_date):
    if not _DATE_RE.match(bill_date):
        return jsonify({"error": "bad date"}), 400
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        n = conn.execute("DELETE FROM bills WHERE bill_date = ?", (bill_date,)).rowcount
        conn.commit()
        conn.close()
    return jsonify({"deleted": n})


@app.route("/solar-months", methods=["GET"])
def solar_months_list():
    conn = sqlite3.connect(DB_PATH)
    out = [{"month": m, "solar_kwh": s_, "home_kwh": h, "source": src} for m, s_, h, src in
           conn.execute("SELECT month, solar_kwh, home_kwh, source FROM solar_months ORDER BY month")]
    conn.close()
    return jsonify({"months": out})


@app.route("/solar-months", methods=["POST"])
def solar_months_save():
    """Monthly solar made (and optionally home used) for months pvswatch
    didn't record: a list of {month: YYYY-MM, solar_kwh, home_kwh, source}."""
    body = request.get_json(silent=True)
    items = body if isinstance(body, list) else [body]
    rows = []
    for it in items:
        if not isinstance(it, dict) or not re.match(r"^\d{4}-\d\d$", str(it.get("month", ""))):
            return jsonify({"error": "month (YYYY-MM) is required", "item": it}), 400
        try:
            solar = float(it["solar_kwh"])
            home = float(it["home_kwh"]) if it.get("home_kwh") not in (None, "") else None
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "solar_kwh must be a number", "item": it}), 400
        rows.append((it["month"], solar, home, str(it.get("source") or "manual")[:60]))
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.executemany("INSERT OR REPLACE INTO solar_months (month, solar_kwh, home_kwh, source) "
                         "VALUES (?, ?, ?, ?)", rows)
        conn.commit()
        conn.close()
    return jsonify({"saved": len(rows)})


@app.route("/annotations", methods=["POST"])
def annotations_save():
    a = request.get_json(silent=True) or {}
    if not _DATE_RE.match(str(a.get("date", ""))) or not str(a.get("label", "")).strip():
        return jsonify({"error": "date (YYYY-MM-DD) and label are required"}), 400
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT OR REPLACE INTO annotations (date, label, detail) VALUES (?, ?, ?)",
                     (a["date"], str(a["label"]).strip()[:120], str(a.get("detail") or "")[:1000]))
        conn.commit()
        conn.close()
    return jsonify({"saved": 1})


@app.route("/annotations", methods=["DELETE"])
def annotations_delete():
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        n = conn.execute("DELETE FROM annotations WHERE date = ? AND label = ?",
                         (request.args.get("date", ""), request.args.get("label", ""))).rowcount
        conn.commit()
        conn.close()
    return jsonify({"deleted": n})


@app.route("/usage", methods=["POST"])
def usage_upload():
    """Green Button download (XML or CSV) -> utility_intervals. Fills in
    net/import/export kWh for bills that don't have them."""
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    try:
        rows = _parse_green_button(f.read())
    except Exception as e:
        logger.warning("Green Button file %s unreadable: %s", f.filename, e)
        return jsonify({"error": "couldn't read that file"}), 400
    if not rows:
        return jsonify({"error": "no interval readings found in that file"}), 400
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.executemany("INSERT OR REPLACE INTO utility_intervals (ts, secs, delivered_kwh, received_kwh) "
                         "VALUES (?, ?, ?, ?)", rows)
        conn.commit()
        conn.close()
    return jsonify({"intervals": len(rows), "start": min(r[0] for r in rows),
                    "end": max(r[0] + r[1] for r in rows),
                    "delivered_kwh": round(sum(r[2] for r in rows), 1),
                    "received_kwh": round(sum(r[3] for r in rows), 1)})


# ── UtilityAPI sync ──────────────────────────────────────────────────
_uapi = {"last_sync": None, "last_error": None, "bills": 0, "intervals": 0, "running": False}


def _uapi_state_file():
    return os.path.join(os.path.dirname(DB_PATH), "utilityapi_state.json")


def _uapi_state():
    try:
        with open(_uapi_state_file()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _uapi_save_state(**kw):
    st = _uapi_state()
    st.update(kw)
    with open(_uapi_state_file(), "w") as f:
        json.dump(st, f)


def _uapi_meters():
    return [m["uid"] for page in _uapi_get("meters") for m in page.get("meters", [])
            if not m.get("is_archived")]


def _uapi_refresh_due(now=None):
    """True once per calendar month, on or after UTILITY_API_REFRESH_DAY."""
    if UTILITY_API_REFRESH_DAY <= 0:
        return False
    t = datetime.fromtimestamp(now or time.time(), _local_tz())
    return t.day >= UTILITY_API_REFRESH_DAY and _uapi_state().get("refresh_month") != t.strftime("%Y-%m")


def _uapi_refresh():
    """Request a 2-month collection for each meter, then wait for it to
    finish (up to an hour) and sync. The month is recorded before the
    request, so a failure is never retried into a paid collection."""
    month = datetime.now(_local_tz()).strftime("%Y-%m")
    # The first pull asks for the full 36 months (older bills and their
    # rates); after that, 2 months is enough to catch the newest bill.
    months = 2 if _uapi_state().get("backfill_done") else 36
    _uapi_save_state(refresh_month=month, refresh_ts=time.time(), refresh_error=None)
    try:
        meters = _uapi_meters()
        r = requests.post(UTILITY_API_BASE + "meters/historical-collection",
                          headers={"Authorization": "Bearer " + UTILITY_API_TOKEN},
                          json={"meters": meters, "collection_duration": months}, timeout=60)
        r.raise_for_status()
        if months == 36:
            _uapi_save_state(backfill_done=True)
        logger.info("UtilityAPI: requested a %d-month collection for %d meter(s)", months, len(meters))
    except Exception as e:
        _uapi_save_state(refresh_error=str(e)[:300])
        logger.warning("UtilityAPI collection request failed (not retried this month): %s", e)
        return
    for _ in range(30):
        time.sleep(120)
        try:
            pending = [m for page in _uapi_get("meters") for m in page.get("meters", [])
                       if m["uid"] in meters and m.get("status") == "pending"]
        except Exception:
            continue
        if not pending:
            break
    _uapi_sync()


def _uapi_get(url):
    """GET a UtilityAPI path or full `next` URL; yields each page."""
    while url:
        if not url.startswith("http"):
            url = UTILITY_API_BASE + url
        r = requests.get(url, headers={"Authorization": "Bearer " + UTILITY_API_TOKEN}, timeout=60)
        r.raise_for_status()
        page = r.json()
        yield page
        url = page.get("next")


def _tou_key(name):
    """('summer'|'winter', 'on'|'mid'|'off'|'super') from a bill line name
    like "Delivery - Energy-Summer - On peak" or "Generation - Super OfPk -
    Winter", or None."""
    n = re.sub(r"[^a-z]", "", (name or "").lower())
    season = "summer" if "summer" in n else "winter" if "winter" in n else None
    if not season:
        return None
    per = "super" if "super" in n else "on" if "onpeak" in n else "mid" if "mid" in n else \
        "off" if ("offpeak" in n or "ofpk" in n) else None
    return (season, per) if per else None


def _rate_scale(delivery, generation):
    """How a bill's energy rates compare with today's TOU_RATES: the mean
    ratio of (delivery + generation) $/kWh per season/period, from
    {(season, period): rate} dicts. None without a plan or a match."""
    if not TOU_PLAN:
        return None
    names = {"on": "on peak", "mid": "mid peak", "off": "off peak", "super": "super off peak"}
    ratios = []
    for key, d in delivery.items():
        g = generation.get(key)
        if g is None:
            continue
        months = range(6, 10) if key[0] == "summer" else (10, 11, 12, 1, 2, 3, 4, 5)
        season = next((se for se in TOU_PLAN["seasons"] if se["months"] & set(months)), None)
        plan = next((p["rate"] for p in (season or {}).get("periods", [])
                     if p.get("name", "").lower() == names[key[1]]), None)
        if plan:
            ratios.append((d + g) / plan)
    return sum(ratios) / len(ratios) if ratios else None


def _uapi_bill(b):
    """A UtilityAPI bill -> a bills row. Its end date is exclusive."""
    base = b.get("base") or {}
    nem = b.get("sce_nem_details") or {}
    items = b.get("line_items") or []
    end = datetime.fromisoformat(base["bill_end_date"]) - timedelta(days=1)
    row = {
        "bill_date": base["bill_statement_date"][:10],
        "period_start": base["bill_start_date"][:10],
        "period_end": end.strftime("%Y-%m-%d"),
        "amount": base.get("bill_total_cost"),
        "net_kwh": base.get("bill_total_kwh"),
        "import_kwh": nem.get("total_forward"),
        "export_kwh": nem.get("total_reverse"),
        "source": "utilityapi",
    }
    if nem.get("current_nem_month"):
        row["nem_month"] = int(nem["current_nem_month"])
    base_charge = sum(i.get("cost") or 0 for i in items if "base services charge" in (i.get("name") or "").lower())
    if base_charge:
        row["base_charge"] = base_charge
    # SCE puts the NEM 12-month settlement on the settlement bill as a
    # "Billing adjustment" line.
    settle = sum(i.get("cost") or 0 for i in items if (i.get("name") or "").lower() == "billing adjustment")
    if settle:
        row["settlement"] = settle
    # The bill's own per-kWh rates, so past months are priced at the rates
    # they actually had rather than today's.
    # Under annual NEM the delivery energy lines sit in the tracked charges.
    tracked = [i for blk in (b.get("sce_tracked_charges") or []) for i in blk.get("line_items", [])]
    deliv = {k: i["rate"] for i in items + tracked if i.get("rate") and (k := _tou_key(i.get("name")))
             and "delivery" in (i.get("name") or "").lower()}
    gen = {k: i["rate"] for sup in (b.get("suppliers") or []) for i in sup.get("supplier_line_items", [])
           if i.get("rate") and (k := _tou_key(i.get("name")))}
    row["rate_scale"] = _rate_scale(deliv, gen)
    both = {f"{k[0]}:{k[1]}": round(deliv[k] + gen[k], 5) for k in deliv if k in gen}
    if both:
        row["rates"] = json.dumps(both)
    return {k: v for k, v in row.items() if v is not None}


def _uapi_sync():
    """Pull every active meter's bills and new 15-min intervals."""
    if _uapi["running"]:
        return dict(_uapi)
    _uapi["running"] = True
    try:
        meters = _uapi_meters()
        n_bills = n_iv = 0
        for uid in meters:
            rows = [_uapi_bill(b) for page in _uapi_get(f"bills?meters={uid}") for b in page.get("bills", [])]
            conn = sqlite3.connect(DB_PATH)
            last = conn.execute("SELECT MAX(ts) FROM utility_intervals").fetchone()[0]
            conn.close()
            start = datetime.fromtimestamp((last or time.time() - 400 * 86400) - 2 * 86400, timezone.utc)
            ivs = []
            for page in _uapi_get(f"intervals?meters={uid}&start={start.strftime('%Y-%m-%dT%H:%M:%SZ')}"):
                for block in page.get("intervals", []):
                    for rd in block.get("readings", []):
                        a = datetime.fromisoformat(rd["start"]).timestamp()
                        b = datetime.fromisoformat(rd["end"]).timestamp()
                        dp = {d["type"]: d["value"] for d in rd.get("datapoints", [])}
                        net = rd.get("kwh") or 0
                        fwd = dp.get("fwd", max(net, 0))
                        rev = dp.get("rev", max(-net, 0))
                        ivs.append((a, int(b - a), fwd, rev))
            with _db_lock:
                conn = sqlite3.connect(DB_PATH)
                for row in rows:
                    _upsert_bill(conn, row)
                conn.executemany("INSERT OR REPLACE INTO utility_intervals (ts, secs, delivered_kwh, received_kwh) "
                                 "VALUES (?, ?, ?, ?)", ivs)
                conn.commit()
                conn.close()
            n_bills += len(rows)
            n_iv += len(ivs)
        _uapi.update(last_sync=time.time(), last_error=None, bills=n_bills, intervals=n_iv)
        logger.info("UtilityAPI sync: %d bills, %d new intervals from %d meter(s)", n_bills, n_iv, len(meters))
    except Exception as e:
        _uapi["last_error"] = str(e)[:300]
        logger.warning("UtilityAPI sync failed: %s", e)
    finally:
        _uapi["running"] = False
    return dict(_uapi)


def _uapi_loop():
    logger.info("UtilityAPI sync enabled (every %gh; monthly collection on day %d)",
                UTILITY_API_SYNC_HOURS, UTILITY_API_REFRESH_DAY)
    while True:
        if _uapi_refresh_due():
            _uapi_refresh()
        elif UTILITY_API_SYNC_HOURS > 0 and time.time() - (_uapi["last_sync"] or 0) >= UTILITY_API_SYNC_HOURS * 3600:
            _uapi_sync()
        time.sleep(3600)


@app.route("/utilityapi/sync", methods=["POST"])
def utilityapi_sync():
    if not UTILITY_API_TOKEN:
        return jsonify({"error": "UTILITY_API_TOKEN is not set"}), 400
    return jsonify(_uapi_sync())


@app.before_request
def _same_origin_writes():
    """Bills and notes are written only from the dashboard itself. Browsers
    tag requests from other sites (Sec-Fetch-Site); refuse those. Checked
    this way rather than Origin vs Host because the Tailscale proxy rewrites
    Host. Non-browser clients (curl) send no header and pass."""
    if request.method in ("POST", "DELETE"):
        if request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            return jsonify({"error": "cross-site write refused"}), 403


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
            savings_dollars = (_tou_savings(conn, cutoff, end_ts, solar_kwh, elapsed_h) if TOU_PLAN
                               else avoided_kwh * COST_PER_KWH)
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
                "savings_model": "tou" if TOU_PLAN else "flat",
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
    # "since" is only meaningful if we saw the change happen. The first
    # reading after the watch started (prev NULL) just marks when we began.
    since = _midstate["since"]
    if not events or events[0]["value"] != v or events[0]["prev"] is None:
        since = None
    return Response(json.dumps({
        "midstate": v,
        "kind": _midstate_kind(v),
        "since": since,
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
        _seed_mock_bills()
        t = threading.Thread(target=_background_refresh_mock, daemon=True)
        t.start()
    elif PVS_IP and PVS_PASS:
        t = threading.Thread(target=_background_refresh, daemon=True)
        t.start()
        if MIDSTATE_POLL_SECS > 0:
            threading.Thread(target=_midstate_watch, daemon=True).start()
        if UTILITY_API_TOKEN and (UTILITY_API_SYNC_HOURS > 0 or UTILITY_API_REFRESH_DAY > 0):
            threading.Thread(target=_uapi_loop, daemon=True).start()
    else:
        logger.warning("PVS_IP or PVS_PASS not set — background refresh disabled, data will be fetched on-demand")

    logger.info("Starting solar proxy on %s:%s (timeout=%ss, refresh=%ss)", HOST, PORT, TIMEOUT_SECS, REFRESH_SECS)
    app.run(host=HOST, port=PORT)