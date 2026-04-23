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
  REFRESH_SECS   - Background refresh interval (default: 3600)
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
REFRESH_SECS = int(os.environ.get("REFRESH_SECS", "3600"))
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "solar_history.db"))

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
    conn.commit()
    conn.close()
    logger.info("History database initialized at %s", DB_PATH)


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

    # Prefer livedata for real-time power, fall back to meter values
    if livedata:
        production = float(livedata.get("pv_p", 0))
        consumption = float(livedata.get("site_load_p", 0))
        net = float(livedata.get("net_p", 0))
        lifetime = float(livedata.get("pv_en", 0))
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
        lifetime = float(meter_p.get("net_ltea_3phsum_kwh", 0) if meter_p else 0)
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
    now = datetime.utcnow().isoformat()

    try:
        with _db_lock:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("""INSERT OR REPLACE INTO readings
                (ts, production_kw, consumption_kw, net_kw, lifetime_kwh,
                 sys_v, l1_v, l2_v, freq_hz, pf_production, pf_consumption,
                 num_panels, panels_working, panels_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, production, consumption, net, lifetime,
                 sys_v, l1_v, l2_v, freq, pf_p, pf_c,
                 num_panels, panels_working, panels_error))
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
                c.execute("""INSERT OR REPLACE INTO panel_readings
                    (ts, serial, panel_model, state, watts, v_dc, i_dc, v_ac, temp_c)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ts, serial, inv.get("PANEL") or inv.get("MODEL") or "", state, watts, v_dc, i_dc, v_ac, temp))
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
    return json.dumps(data), livedata


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


# ── CORS ──────────────────────────────────────────────────────────────
@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    return resp


# ── Dashboard routes ──────────────────────────────────────────────────
@app.route("/")
def index():
    if DASHBOARD_HTML is None:
        return Response("Dashboard not loaded", status=500)
    return Response(DASHBOARD_HTML, mimetype="text/html")


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
      range  - Time range: 1h, 6h, 24h, 7d, 30d (default: 24h)
      resample - Resample interval in seconds (default: auto based on range)
    """
    range_str = request.args.get("range", "24h")
    resample = request.args.get("resample")

    # Parse range
    range_map = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000}
    range_secs = range_map.get(range_str, 86400)

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

    cutoff = time.time() - range_secs

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
                MAX(lifetime_kwh) as lifetime_kwh,
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
            WHERE ts > ?
            GROUP BY bucket
            ORDER BY bucket
        """, (cutoff,)).fetchall()

        result = {
            "range": range_str,
            "resample_seconds": sample_secs,
            "readings": [dict(r) for r in rows]
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
            SELECT ts, serial, panel_model, state, watts, v_dc, i_dc, v_ac, temp_c
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

    # Start background data refresh
    if PVS_IP and PVS_PASS:
        t = threading.Thread(target=_background_refresh, daemon=True)
        t.start()
    else:
        logger.warning("PVS_IP or PVS_PASS not set — background refresh disabled, data will be fetched on-demand")

    logger.info("Starting solar proxy on %s:%s (timeout=%ss, refresh=%ss)", HOST, PORT, TIMEOUT_SECS, REFRESH_SECS)
    app.run(host=HOST, port=PORT)