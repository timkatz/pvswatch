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
  LOG_LEVEL      - Python log level (default: INFO)
"""

from flask import Flask, request, Response, send_file, jsonify
import requests
import json
import os
import time
import logging
import urllib3
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

# Savings model: avoided cost = min(solar_kwh, home_kwh) * COST_PER_KWH
# CO2 model: solar_kwh * CO2_LBS_PER_KWH (full production offsets grid CO2)
COST_PER_KWH = float(os.environ.get("COST_PER_KWH", "0.30"))
CO2_LBS_PER_KWH = float(os.environ.get("CO2_LBS_PER_KWH", "0.85"))

# Logging
log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, log_level, logging.INFO), format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder=".")
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
    backup_min = float(livedata.get("backupTimeRemaining", 0)) if livedata else 0
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
                 home_lifetime_kwh, grid_lifetime_kwh)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, production, consumption, net, lifetime,
                 sys_v, l1_v, l2_v, freq, pf_p, pf_c,
                 num_panels, panels_working, panels_error,
                 battery_kw, battery_soc, backup_min, battery_lifetime_kwh,
                 home_lifetime_kwh, grid_lifetime_kwh))
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

    if PVS_IP and PVS_PASS:
        inject = (
            "<script>\n"
            "  // Server-side config injected by proxy from .env\n"
            f"  // ip={PVS_IP} user={PVS_USER} pass=****\n"
            "  window.__SOLAR_CONFIG__ = {\n"
            f"    ip: \"{PVS_IP}\",\n"
            f"    user: \"{PVS_USER}\",\n"
            f"    pass: \"{PVS_PASS}\"\n"
            "  };\n"
            "</script>\n"
        )
        head_pos = DASHBOARD_HTML.find("<head>")
        if head_pos != -1:
            insert_pos = head_pos + len("<head>")
            DASHBOARD_HTML = DASHBOARD_HTML[:insert_pos] + "\n" + inject + DASHBOARD_HTML[insert_pos:]

    logger.info("Dashboard HTML loaded (%d bytes), config: ip=%s user=%s pass=****%s",
                len(DASHBOARD_HTML), PVS_IP, PVS_USER,
                PVS_PASS[-2:] if len(PVS_PASS) >= 2 else "N/A")


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
        return json.dumps(data), livedata

    # Meters missing — supplement from full varserver data
    logger.info("Meters missing, supplementing from varserver")
    vars_data = _fetch_vars(sess, ip)
    if not vars_data:
        return json.dumps(data), livedata

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
    return json.dumps(data), livedata


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
            "soc": f("socVal", 0),
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


def _background_refresh():
    """Background thread that periodically refreshes PVS data."""
    logger.info("Starting background refresh (every %ds)", REFRESH_SECS)
    # Initial fetch
    _refresh_data()
    while True:
        time.sleep(REFRESH_SECS)
        _refresh_data()


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

    # Parse range. "all" = since first reading.
    range_map = {
        "1m": 60, "1h": 3600, "6h": 21600, "24h": 86400,
        "7d": 604800, "30d": 2592000, "90d": 7776000, "1y": 31536000,
    }
    if start_param and end_param:
        try:
            cutoff = float(start_param)
            end_ts = float(end_param)
            range_secs = max(60, int(end_ts - cutoff))
        except ValueError:
            return Response("Invalid start/end timestamps", status=400)
    elif range_str == "all":
        # Compute span from earliest reading
        try:
            conn0 = sqlite3.connect(DB_PATH)
            earliest_row = conn0.execute("SELECT MIN(ts) FROM readings").fetchone()
            conn0.close()
            range_secs = max(60, int(time.time() - (earliest_row[0] or time.time())))
        except Exception:
            range_secs = 86400
        end_ts = time.time()
        cutoff = end_ts - range_secs
    else:
        range_secs = range_map.get(range_str, 86400)
        end_ts = time.time()
        cutoff = end_ts - range_secs

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
        totals_row = c.execute("""
            SELECT
                MAX(NULLIF(lifetime_kwh, 0)) - MIN(NULLIF(lifetime_kwh, 0)) as solar_from_lifetime,
                MAX(NULLIF(home_lifetime_kwh, 0)) - MIN(NULLIF(home_lifetime_kwh, 0)) as home_from_lifetime,
                AVG(production_kw) as avg_prod,
                AVG(consumption_kw) as avg_cons,
                AVG(net_kw) as avg_net,
                AVG(battery_kw) as avg_battery,
                MIN(ts) as t_start,
                MAX(ts) as t_end,
                COUNT(*) as n,
                COUNT(NULLIF(lifetime_kwh, 0)) as n_solar_lt,
                COUNT(NULLIF(home_lifetime_kwh, 0)) as n_home_lt,
                MIN(CASE WHEN lifetime_kwh IS NOT NULL AND lifetime_kwh != 0 THEN ts END) as solar_lt_t_start,
                MAX(CASE WHEN lifetime_kwh IS NOT NULL AND lifetime_kwh != 0 THEN ts END) as solar_lt_t_end,
                MIN(CASE WHEN home_lifetime_kwh IS NOT NULL AND home_lifetime_kwh != 0 THEN ts END) as home_lt_t_start,
                MAX(CASE WHEN home_lifetime_kwh IS NOT NULL AND home_lifetime_kwh != 0 THEN ts END) as home_lt_t_end
            FROM readings WHERE ts > ? AND ts <= ?
        """, (cutoff, end_ts)).fetchone()

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

            solar_kwh = (totals_row["solar_from_lifetime"] or 0) if coverage_ok(
                totals_row["n_solar_lt"], totals_row["solar_lt_t_start"], totals_row["solar_lt_t_end"]
            ) else 0
            if solar_kwh < 0.01:
                solar_kwh = max(0, (totals_row["avg_prod"] or 0) * elapsed_h)
            home_kwh = (totals_row["home_from_lifetime"] or 0) if coverage_ok(
                totals_row["n_home_lt"], totals_row["home_lt_t_start"], totals_row["home_lt_t_end"]
            ) else 0
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
        t = threading.Thread(target=_background_refresh_mock, daemon=True)
        t.start()
    elif PVS_IP and PVS_PASS:
        t = threading.Thread(target=_background_refresh, daemon=True)
        t.start()
    else:
        logger.warning("PVS_IP or PVS_PASS not set — background refresh disabled, data will be fetched on-demand")

    logger.info("Starting solar proxy on %s:%s (timeout=%ss, refresh=%ss)", HOST, PORT, TIMEOUT_SECS, REFRESH_SECS)
    app.run(host=HOST, port=PORT)