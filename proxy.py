#!/usr/bin/env python3
"""
proxy.py — Flask proxy + static server for solar_dashboard.html

Proxies authenticated requests to SunPower PVS6 gateway, supplements PVS6
meter data from the varserver API, and serves the dashboard HTML with
server-side config baked in.

Features:
  - Background data refresh: caches PVS data and refreshes every 30s
  - Instant /devices response from cache (stale-while-revalidate)
  - PVS6 meter supplementation via varserver
  - Clean URL: no query params needed, config from .env

Configuration is read from environment variables (set via .env file):
  PVS_IP         - PVS gateway IP address
  PVS_USER       - PVS auth username (default: ssm_owner)
  PVS_PASS       - PVS auth password (last 5 chars of internal serial)
  PORT           - Server port (default: 5001)
  TIMEOUT_SECS   - PVS request timeout (default: 60)
  REFRESH_SECS   - Background refresh interval (default: 30)
  LOG_LEVEL      - Python log level (default: INFO)
"""

from flask import Flask, request, Response, send_file
import requests
import json
import os
import time
import logging
import urllib3
import threading

# Ignore insecure HTTPS warnings for self-signed gateway certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Configuration from environment ──────────────────────────────────────
PVS_IP = os.environ.get("PVS_IP", "")
PVS_USER = os.environ.get("PVS_USER", "ssm_owner")
PVS_PASS = os.environ.get("PVS_PASS", "")
TIMEOUT_SECS = int(os.environ.get("TIMEOUT_SECS", "120"))
PORT = int(os.environ.get("PORT", "5001"))
HOST = os.environ.get("HOST", "0.0.0.0")
REFRESH_SECS = int(os.environ.get("REFRESH_SECS", "60"))

# Logging
log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, log_level, logging.INFO), format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder=".")
sessions = {}

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


# ── Varserver: supplement PVS6 meter data ─────────────────────────────
def _fetch_vars(sess, ip):
    """Fetch real-time data from the varserver API and return a dict of key->value."""
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
    """If meters are missing, inject synthetic PVS5-METER-P/C from varserver data."""
    try:
        data = json.loads(devices_json) if isinstance(devices_json, str) else devices_json
    except (json.JSONDecodeError, TypeError):
        return devices_json

    device_list = data.get("devices", [])
    types = {d.get("TYPE", "") for d in device_list}

    if "PVS5-METER-P" in types and "PVS5-METER-C" in types:
        return devices_json

    logger.info("Meters missing, supplementing from varserver")
    vars_data = _fetch_vars(sess, ip)
    if not vars_data:
        return devices_json

    if "PVS5-METER-C" not in types:
        meter_c = _build_meter_device(vars_data, 0, "c")
        if meter_c:
            device_list.append(meter_c)

    if "PVS5-METER-P" not in types:
        meter_p = _build_meter_device(vars_data, 1, "p")
        if meter_p:
            device_list.append(meter_p)

    data["devices"] = device_list
    return json.dumps(data)


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

        # Supplement with varserver meter data if needed
        supplemented = _supplement_devices(r.text, ip, sess)
        cache.set(supplemented)
        cache.set_error(None)
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


# ── Health check ──────────────────────────────────────────────────────
@app.route("/health")
def health():
    data = cache.get()
    age = time.time() - cache.last_fetch if cache.last_fetch else -1
    return Response(json.dumps({
        "status": "ok" if data else "no_data",
        "cache_age_seconds": round(age, 1) if age >= 0 else None,
        "last_error": cache.last_error,
        "fetching": cache.fetching,
    }), mimetype="application/json")


if __name__ == "__main__":
    _load_dashboard()

    # Start background data refresh
    if PVS_IP and PVS_PASS:
        t = threading.Thread(target=_background_refresh, daemon=True)
        t.start()
    else:
        logger.warning("PVS_IP or PVS_PASS not set — background refresh disabled, data will be fetched on-demand")

    logger.info("Starting solar proxy on %s:%s (timeout=%ss, refresh=%ss)", HOST, PORT, TIMEOUT_SECS, REFRESH_SECS)
    app.run(host=HOST, port=PORT)