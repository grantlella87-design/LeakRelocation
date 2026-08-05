"""
Fast non-ArcPy historic leak relocation workflow.
Uses ArcGIS REST + GeoPandas/Shapely instead of ArcPy geoprocessing.
Reads MA historic leaks, MA distribution pipes, MA service pipes, supplements leak attributes from the shared CSV,
matches each leak to the nearest pipe with exact diameter and material/family match, snaps the leak to the matched pipe,
and writes a GeoPackage with relocated points, offset guide lines, and an audit table.
"""

import datetime as dt
import glob
import json
import math
import numbers
import os
import re
import shutil as _shutil
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import webbrowser
from collections import defaultdict
from concurrent import futures
from http.server import BaseHTTPRequestHandler, HTTPServer

import geopandas as gpd
import keyring
import pandas as pd
import requests
from shapely.geometry import LineString, MultiLineString, Point, shape
from shapely.ops import nearest_points
from shapely.strtree import STRtree

# The `leakrelocation` package sits next to this file. Adding the script's own
# directory to sys.path lets it import cleanly whether it is run from the repo
# or copied to the shared drive - provided the package folder travels with it.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# === Native CRS patch: per-layer MapServer spatial reference ===
# Each source layer must get its own native CRS from the actual MapServer layer being queried.
# Do not force all layers to EPSG:2249 before matching.
from pyproj import CRS

from leakrelocation import config
from leakrelocation.matching import (
    clean,
    diameter_matches,
    matched_radius_from_distance,
    material_label,
    material_matches,
    normalize_key,
    parse_number,
    pressure_matches,
    resolve_field_name,
    route_layers,
)

LAYER_NATIVE_CRS_CONFIG = {
    "historic_leaks": {"url_global": "HIST_LEAK_URL"},
    "distribution_pipes": {"url_global": "DISTRIBUTION_PIPE_URL"},
    "service_pipes": {"url_global": "SERVICE_PIPE_URL"},
}

ANALYSIS_CRS_LAYER_KEY = "distribution_pipes"
_NATIVE_CRS_CACHE = {}


def _spatial_reference_from_layer_json(layer_json):
    sr = (
        layer_json.get("extent", {}).get("spatialReference")
        or layer_json.get("spatialReference")
        or {}
    )
    if sr.get("wkt"):
        return CRS.from_wkt(sr["wkt"]), sr
    wkid = sr.get("latestWkid") or sr.get("wkid")
    if wkid:
        return CRS.from_epsg(int(wkid)), sr
    raise RuntimeError(
        f"Layer metadata did not include a usable spatialReference: {sr}"
    )


def get_layer_native_crs(session, layer_key):
    if layer_key in _NATIVE_CRS_CACHE:
        return _NATIVE_CRS_CACHE[layer_key]
    if layer_key not in LAYER_NATIVE_CRS_CONFIG:
        raise KeyError(f"Unknown layer_key for native CRS lookup: {layer_key}")
    cfg = LAYER_NATIVE_CRS_CONFIG[layer_key]
    url_global = cfg["url_global"]
    layer_url = globals().get(url_global)
    if not layer_url:
        raise RuntimeError(f"Missing layer URL global {url_global} for {layer_key}")
    layer_json = request_json(session, layer_url, {"f": "json"})
    crs, _sr = _spatial_reference_from_layer_json(layer_json)
    _NATIVE_CRS_CACHE[layer_key] = crs
    print(
        f"{layer_key}: native CRS assigned from MapServer [{layer_url}]: {crs.name}",
        flush=True,
    )
    return crs


def assign_native_crs(session, layer_key, gdf):
    crs = get_layer_native_crs(session, layer_key)
    if gdf is None or len(gdf) == 0:
        return gdf
    existing = getattr(gdf, "crs", None)
    if existing is None:
        print(
            f"{layer_key}: dataframe CRS was empty; assigning native CRS {crs.name}",
            flush=True,
        )
    else:
        print(
            f"{layer_key}: overriding dataframe CRS [{existing}] with native CRS [{crs.name}]",
            flush=True,
        )
    return gdf.set_crs(crs, allow_override=True)


def get_analysis_crs(session):
    return get_layer_native_crs(session, ANALYSIS_CRS_LAYER_KEY)


def to_analysis_crs(session, layer_key, gdf):
    if gdf is None or len(gdf) == 0:
        return gdf
    native = get_layer_native_crs(session, layer_key)
    analysis = get_analysis_crs(session)
    if gdf.crs is None:
        gdf = gdf.set_crs(native, allow_override=True)
    if gdf.crs != analysis:
        print(
            f"{layer_key}: projecting from [{gdf.crs}] to analysis CRS [{analysis.name}]",
            flush=True,
        )
        return gdf.to_crs(analysis)
    print(f"{layer_key}: already in analysis CRS [{analysis.name}]", flush=True)
    return gdf


def normalize_loaded_layers_to_analysis_crs(
    session, historic_leaks, distribution_pipes, service_pipes
):
    historic_leaks = assign_native_crs(session, "historic_leaks", historic_leaks)
    distribution_pipes = assign_native_crs(
        session, "distribution_pipes", distribution_pipes
    )
    service_pipes = assign_native_crs(session, "service_pipes", service_pipes)
    historic_leaks = to_analysis_crs(session, "historic_leaks", historic_leaks)
    distribution_pipes = to_analysis_crs(
        session, "distribution_pipes", distribution_pipes
    )
    service_pipes = to_analysis_crs(session, "service_pipes", service_pipes)
    analysis_crs = get_analysis_crs(session)
    print(f"Analysis CRS for all spatial matching: {analysis_crs.name}", flush=True)
    return historic_leaks, distribution_pipes, service_pipes, analysis_crs
    # === End native CRS patch ===


SUPPLEMENTAL_CSV = str(config.SUPPLEMENTAL_CSV)
OUTPUT_FOLDER = str(config.PROJECT_DIR)
OUTPUT_GPKG = str(config.OUTPUT_GPKG)
HIST_LEAK_URL = config.HIST_LEAK_URL
DISTRIBUTION_PIPE_URL = config.DISTRIBUTION_PIPE_URL
SERVICE_PIPE_URL = config.SERVICE_PIPE_URL

WHERE_MA = config.WHERE_MA
TARGET_CRS = None  # Native CRS patch: do not force EPSG:2249 before matching
INITIAL_RADIUS_FT = config.INITIAL_RADIUS_FT
RADIUS_INCREMENT_FT = config.RADIUS_INCREMENT_FT
MAX_RADIUS_FT = config.MAX_RADIUS_FT
REQUIRE_PRESSURE_MATCH = config.REQUIRE_PRESSURE_MATCH
ALLOW_MATERIAL_FAMILY_FALLBACK = config.ALLOW_MATERIAL_FAMILY_FALLBACK
USE_MULTIPROCESSING = True
WORKERS = max(1, min(8, (os.cpu_count() or 4) - 1))
REQUEST_PAGE_SIZE = config.REQUEST_PAGE_SIZE

USE_OBJECTID_BATCH_DOWNLOAD = True
OBJECTID_BATCH_SIZE = config.OBJECTID_BATCH_SIZE
OBJECTID_DOWNLOAD_WORKERS = config.OBJECTID_DOWNLOAD_WORKERS
REQUEST_TIMEOUT_SECONDS = config.REQUEST_TIMEOUT_SECONDS
VERIFY_SSL = config.VERIFY_SSL

PORTAL_ROOT = config.PORTAL_ROOT
PORTAL_AUTHORIZE_URL = config.PORTAL_AUTHORIZE_URL
PORTAL_TOKEN_URL = config.PORTAL_TOKEN_URL
ARCGIS_CLIENT_ID = config.ARCGIS_CLIENT_ID
ARCGIS_REDIRECT_URI = config.ARCGIS_REDIRECT_URI
KEYRING_SERVICE = config.KEYRING_SERVICE
KEYRING_ACCESS_TOKEN_USER = config.KEYRING_ACCESS_TOKEN_USER
KEYRING_ACCESS_TOKEN_EXPIRES_USER = config.KEYRING_ACCESS_TOKEN_EXPIRES_USER
TOKEN_EXPIRY_SAFETY_SECONDS = config.TOKEN_EXPIRY_SAFETY_SECONDS
MESSAGE_EVERY_N_LEAKS = 1000
VERBOSE_PER_LEAK = False

USE_LAYER_CACHE = config.USE_LAYER_CACHE
LAYER_CACHE_FOLDER = str(config.LAYER_CACHE_DIR)
FORCE_LAYER_REFRESH = config.FORCE_LAYER_REFRESH
DELTA_REFRESH_SAFETY_SECONDS = config.DELTA_REFRESH_SAFETY_SECONDS
MODIFIED_FIELD_CANDIDATES = [
    "LASTUPDATE",
    "lastupdate",
    "LastUpdate",
    "last_edited_date",
    "EditDate",
    "UPDATEDATE",
    "MODIFIEDDATE",
]
LEAK_KEY_CANDIDATES = [
    "LMSLEAKNUMBER",
    "LMSLEAKSUMBER",
    "LEAKNUMBER",
    "LeakNumber",
    "Name",
    "name",
]
PIPE_DIAMETER_CANDIDATES = [
    "nominaldiameter",
    "NOMINALDIAMETER",
    "NominalDiameter",
    "diameter",
    "DIAMETER",
    "outsidediameter",
    "OUTSIDEDIAMETER",
]
PIPE_MATERIAL_CANDIDATES = [
    "assettype_material",
    "ASSETTYPE_MATERIAL",
    "AssetType_Material",
    "material",
    "MATERIAL",
    "ASSETTYPE",
    "assettype",
]
PIPE_PRESSURE_CANDIDATES = [
    "operatingpressure",
    "OPERATINGPRESSURE",
    "OperatingPressure",
    "maopdesign",
    "MAOPDESIGN",
    "MAOPDesign",
    "pressure",
    "PRESSURE",
    "op_pressure",
    "OP_PRESSURE",
]
SUPP_KEY_CANDIDATES = ["LeakNumber", "Name", "LMSLEAKNUMBER", "LEAKNUMBER"]
SUPP_DIAMETER_CANDIDATES = ["Diameter", "Abdn_Diameter_Main", "Abdn_Diameter_Service"]
SUPP_MATERIAL_CANDIDATES = [
    "LeakMaterialType",
    "Abdn_Material",
    "Adbn_Material_Main",
    "Abdn_Material_Service",
]
SUPP_PRESSURE_CANDIDATES = [
    "Pressure",
    "OperatingPressure",
    "MAOP",
    "MAOPDesign",
    "op_pressure",
]
SUPP_FACILITY_CANDIDATES = ["FacilityType", "PipeType"]
GLOBALID_CANDIDATES = ["GlobalID", "GLOBALID", "globalid"]
OBJECTID_CANDIDATES = ["OBJECTID", "ObjectID", "objectid", "FID", "OID"]
WORKER_TREES = None


def log(text):
    print(str(text), flush=True)


def step(text):
    log(f"\n--- {text} ---")


def warn(text):
    log(f"WARNING: {text}")


def fail(text):
    raise RuntimeError(str(text))


def verbose(text):
    if VERBOSE_PER_LEAK:
        log(text)


def detail(text):
    """Diagnostic output. Hidden unless LEAKRELOCATION_VERBOSE is set.

    Field resolution, TLS/proxy setup and outFields lists are useful when
    something is wrong and noise the rest of the time.
    """
    if config.VERBOSE:
        log(text)


_TIMINGS = []


def timed(label):
    """Context manager recording elapsed time for a stage."""
    from contextlib import contextmanager

    @contextmanager
    def _timer():
        started = time.time()
        try:
            yield
        finally:
            _TIMINGS.append((label, time.time() - started))

    return _timer()


def report_timings():
    if not config.TIMINGS or not _TIMINGS:
        return
    step("Stage timings")
    width = max(len(label) for label, _ in _TIMINGS)
    for label, seconds in _TIMINGS:
        log(f"  {label:<{width}}  {seconds:8.1f}s")
    log(f"  {'total':<{width}}  {sum(s for _, s in _TIMINGS):8.1f}s")


def resolved_field(columns, candidates, required=False, label="field"):
    available = list(columns)
    resolved = resolve_field_name(available, candidates)
    if resolved is not None:
        detail(f"Resolved {label}: {resolved}")
        return resolved
    if required:
        fail(
            f"Could not resolve required {label}. Candidates={candidates}. Available={available}"
        )
    # The caller reports what the missing field means for the run; listing every
    # candidate here only repeats it.
    detail(f"Could not resolve optional {label}. Candidates={candidates}")
    return None


def ensure_output_folder():
    if not os.path.isdir(OUTPUT_FOLDER):
        os.makedirs(OUTPUT_FOLDER)
        log(f"Created output folder: {OUTPUT_FOLDER}")
    else:
        detail(f"Output folder exists: {OUTPUT_FOLDER}")


def make_session():
    try:
        import truststore

        truststore.inject_into_ssl()
        detail("Injected Windows certificate store through truststore.")
    except Exception as ex:
        warn(f"truststore injection failed: {ex}")
    for proxy_var in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
        if proxy_var in os.environ:
            os.environ.pop(proxy_var, None)
            detail(f"Removed runtime proxy environment variable: {proxy_var}")
    os.environ["NO_PROXY"] = (
        "gis.nationalgrid.com,.nationalgrid.com,localhost,127.0.0.1"
    )
    os.environ["no_proxy"] = os.environ["NO_PROXY"]
    session = requests.Session()
    session.trust_env = True
    session.headers.update({"User-Agent": "HistoricLeakRelocationGeoPandas/1.0"})
    return session


def keyring_get(name):
    try:
        return keyring.get_password(KEYRING_SERVICE, name)
    except Exception as ex:
        warn(f"Keyring read failed for {name}: {ex}")
        return None


def keyring_set(name, value):
    try:
        keyring.set_password(KEYRING_SERVICE, name, str(value))
    except Exception as ex:
        warn(f"Keyring write failed for {name}: {ex}")


def cached_access_token():
    token = keyring_get(KEYRING_ACCESS_TOKEN_USER)
    expires_raw = keyring_get(KEYRING_ACCESS_TOKEN_EXPIRES_USER)

    if not token or not expires_raw:
        log("No cached ArcGIS Portal access token found in Windows Credential Manager.")
        return ""

    try:
        expires_epoch = float(expires_raw)
    except Exception:
        log("Cached ArcGIS Portal access token expiration value is invalid.")
        return ""

    if time.time() + TOKEN_EXPIRY_SAFETY_SECONDS < expires_epoch:
        log("Using cached ArcGIS Portal access token from Windows Credential Manager.")
        return token

    log("Cached ArcGIS Portal access token is expired or inside safety window.")
    return ""


def clear_cached_access_token():
    keyring_set(KEYRING_ACCESS_TOKEN_USER, "")
    keyring_set(KEYRING_ACCESS_TOKEN_EXPIRES_USER, "0")


def extract_oauth_code(value):
    value = clean(value)

    if not value:
        return ""

    if "code=" in value:
        try:
            parsed = urllib.parse.urlparse(value)
            query = urllib.parse.parse_qs(parsed.query)
            code = query.get("code", [""])[0]

            if code:
                return clean(code)
        except Exception:
            pass

    match = re.search(r"[?&]code=([^&\s]+)", value)

    if match:
        return clean(urllib.parse.unquote(match.group(1)))

    return value


def chrome_time_from_epoch(epoch_seconds):
    return int((float(epoch_seconds) + 11644473600) * 1000000)


def browser_history_paths():
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = []

    if local:
        candidates.extend(
            [
                os.path.join(
                    local, "Microsoft", "Edge", "User Data", "Default", "History"
                ),
                os.path.join(
                    local, "Microsoft", "Edge", "User Data", "Profile 1", "History"
                ),
                os.path.join(
                    local, "Google", "Chrome", "User Data", "Default", "History"
                ),
                os.path.join(
                    local, "Google", "Chrome", "User Data", "Profile 1", "History"
                ),
            ]
        )

        candidates.extend(
            glob.glob(
                os.path.join(
                    local, "Microsoft", "Edge", "User Data", "Profile *", "History"
                )
            )
        )
        candidates.extend(
            glob.glob(
                os.path.join(
                    local, "Google", "Chrome", "User Data", "Profile *", "History"
                )
            )
        )

    deduped = []

    for path in candidates:
        if path and path not in deduped and os.path.isfile(path):
            deduped.append(path)

    return deduped


def try_extract_oob_code_from_browser_history_since(start_epoch_seconds):
    min_chrome_time = chrome_time_from_epoch(start_epoch_seconds)

    for history_path in browser_history_paths():
        temp_path = os.path.join(
            tempfile.gettempdir(),
            f"arcgis_oauth_history_{abs(hash(history_path))}.sqlite",
        )

        try:
            _shutil.copy2(history_path, temp_path)

            conn = sqlite3.connect(temp_path)

            try:
                rows = conn.execute(
                    """
                    SELECT url, last_visit_time
                    FROM urls
                    WHERE url LIKE ?
                      AND last_visit_time >= ?
                    ORDER BY last_visit_time DESC
                    LIMIT 25
                    """,
                    ("%gis.nationalgrid.com%oauth2/approval%code=%", min_chrome_time),
                ).fetchall()
            finally:
                conn.close()

            for url, _last_visit_time in rows:
                code = extract_oauth_code(url)

                if code:
                    log(
                        "Captured fresh ArcGIS OAuth approval code from browser history."
                    )
                    return code

        except Exception:
            continue

    return ""


def wait_for_fresh_oob_code_from_history(start_epoch_seconds, seconds_to_wait=180):
    for _ in range(seconds_to_wait):
        code = try_extract_oob_code_from_browser_history_since(start_epoch_seconds)

        if code:
            return code

        time.sleep(1)

    return ""


def get_clipboard_text():
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        value = root.clipboard_get()
        root.destroy()
        return clean(value)
    except Exception:
        return ""


def get_oob_authorization_code(start_epoch_seconds):
    # Primary path: silently capture a new OOB approval URL from Edge/Chrome history.
    code = wait_for_fresh_oob_code_from_history(start_epoch_seconds)

    if code:
        return code

        # Fallback only: if silent capture fails, then use whatever user gives us.
    log(
        "Could not automatically capture a fresh ArcGIS OAuth code from browser history."
    )
    log(
        "Fallback: copy the approval URL or code from the browser, then press Enter here."
    )

    input("After copying the ArcGIS approval code or URL, press Enter: ")

    code = extract_oauth_code(get_clipboard_text())

    if code:
        log("Read ArcGIS authorization code from clipboard fallback.")
        return code

    return extract_oauth_code(
        input("Paste ArcGIS authorization code or approval URL: ").strip()
    )


LOOPBACK_SUCCESS_PAGE = b"""<!doctype html>
<html><head><meta charset="utf-8"/><title>Signed in</title></head>
<body style="font-family:Arial,sans-serif;padding:2em">
<p id="msg">Signed in. Closing...</p>
<script>
// Opened by the sign-in redirect, so this window may close itself.
window.close();
// If the browser refuses (some block close() on tabs it did not script-open),
// leave a short instruction rather than the raw authorization code.
setTimeout(function(){document.getElementById('msg').textContent =
  'Signed in. You can close this tab.';}, 400);
</script>
</body></html>"""


def capture_loopback_authorization_code(timeout_seconds=180):
    """Serve the OAuth redirect on loopback and return the authorization code.

    Replaces reading the code off the out-of-band page, which left a tab showing
    "SUCCESS code=..." that could not be closed programmatically because the
    page belonged to the portal. Here the redirect lands on a page this process
    serves, so it can close itself and never displays the code.

    Returns None if the redirect never arrives, so the caller can fall back.
    """
    captured = {}
    ready = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = (query.get("code") or [None])[0]
            error = (query.get("error_description") or query.get("error") or [None])[0]
            if code:
                captured["code"] = code
            elif error:
                captured["error"] = error
            else:
                # Favicon and similar noise; not the redirect.
                self.send_response(204)
                self.end_headers()
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(LOOPBACK_SUCCESS_PAGE)))
            self.end_headers()
            self.wfile.write(LOOPBACK_SUCCESS_PAGE)
            ready.set()

        def log_message(self, *args):
            """Silence the default per-request logging to stderr."""

    try:
        server = HTTPServer(("127.0.0.1", config.LOOPBACK_OAUTH_PORT), Handler)
    except OSError as exc:
        warn(f"Could not listen on {config.LOOPBACK_REDIRECT_URI}: {exc}")
        return None

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not ready.wait(timeout_seconds):
            warn("Sign-in did not complete within the timeout.")
            return None
    finally:
        server.shutdown()
        server.server_close()

    if captured.get("error"):
        fail(f"ArcGIS sign-in failed: {captured['error']}")
    return captured.get("code")


def interactive_access_token(session):
    cached = cached_access_token()

    if cached:
        return cached

    env_token = (
        os.environ.get("GIS_AccessToken")
        or os.environ.get("GIS_ACCESS_TOKEN")
        or os.environ.get("ARCGIS_ACCESS_TOKEN")
        or os.environ.get("ARCGIS_TOKEN")
    )

    if env_token and env_token.strip():
        log("Using ArcGIS access token from environment variable.")
        return env_token.strip()

    if not ARCGIS_CLIENT_ID:
        fail("ARCGIS_CLIENT_ID is not set.")

    def authorize_url_for(redirect_uri):
        return PORTAL_AUTHORIZE_URL + "?" + urllib.parse.urlencode({
            "client_id": ARCGIS_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "expiration": "20160",
        })

    auth_code = None
    redirect_uri = ARCGIS_REDIRECT_URI

    if config.USE_LOOPBACK_OAUTH:
        log("Opening browser to sign in to ArcGIS.")
        detail(f"Redirect URI: {config.LOOPBACK_REDIRECT_URI}")
        webbrowser.open(authorize_url_for(config.LOOPBACK_REDIRECT_URI), new=1, autoraise=True)
        auth_code = capture_loopback_authorization_code()
        if auth_code:
            redirect_uri = config.LOOPBACK_REDIRECT_URI
        else:
            warn(
                "Loopback sign-in did not complete. If the portal app registration "
                f"does not list {config.LOOPBACK_REDIRECT_URI} as a redirect URI, add it, "
                "or set LEAKRELOCATION_LOOPBACK_OAUTH=0 to use the out-of-band page."
            )

    if not auth_code:
        log("Opening browser for ArcGIS user authentication (out-of-band).")
        detail(f"Redirect URI: {ARCGIS_REDIRECT_URI}")
        auth_start_epoch = time.time()
        webbrowser.open(authorize_url_for(ARCGIS_REDIRECT_URI), new=1, autoraise=True)
        auth_code = get_oob_authorization_code(auth_start_epoch)
        redirect_uri = ARCGIS_REDIRECT_URI

    if not auth_code:
        fail("No authorization code was captured.")

    token_payload = {
        "f": "json",
        "client_id": ARCGIS_CLIENT_ID,
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": redirect_uri,
    }

    response = session.post(
        PORTAL_TOKEN_URL,
        data=token_payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
        verify=VERIFY_SSL,
    )

    response.raise_for_status()
    data = response.json()

    if "error" in data:
        fail(
            f"ArcGIS OAuth token request failed: {json.dumps(data['error'], indent=2)}"
        )

    token = data.get("access_token") or data.get("token")

    if not token:
        fail("ArcGIS OAuth response did not include access_token/token.")

    expires_epoch = time.time() + float(data.get("expires_in") or 3600)

    keyring_set(KEYRING_ACCESS_TOKEN_USER, token)
    keyring_set(KEYRING_ACCESS_TOKEN_EXPIRES_USER, expires_epoch)

    log("Saved ArcGIS Portal access token to Windows Credential Manager.")
    return token


def get_arcgis_token(session=None):
    active_session = session if session is not None else requests.Session()
    return interactive_access_token(active_session)


def apply_pipe_domain_out_fields(url, params):
    """Ensure DNV pipe-layer queries also return ASSETGROUP and ASSETTYPE.

    Pipe material classification comes from the decoded ASSETGROUP + ASSETTYPE
    subtype domains. The DNV `material` field is Grade/characteristic data and
    is not the material class used for relocation assessment, so any pipe query
    that asks for attribute columns must carry the domain fields as well.

    Returns `params` unchanged unless this is a pipe-layer query whose
    outFields requests attributes but omits ASSETTYPE.
    """
    if not isinstance(params, dict):
        return params

    url_text = str(url).lower()
    if "/mapserver/6" not in url_text and "/mapserver/7" not in url_text:
        return params

    out_field_key = None
    for key in params:
        if str(key).lower() == "outfields":
            out_field_key = key
            break
    if out_field_key is None:
        return params

    out_fields = params.get(out_field_key)
    if not isinstance(out_fields, str):
        return params

    lowered = out_fields.lower()
    wants_attributes = (
        "nominaldiameter" in lowered
        or "material" in lowered
        or "operatingpressure" in lowered
    )
    if not wants_attributes or "assettype" in lowered:
        return params

    updated = dict(params)
    updated[out_field_key] = out_fields + ",ASSETGROUP,ASSETTYPE"
    log("Appended DNV pipe domain fields to outFields: ASSETGROUP,ASSETTYPE")
    return updated


def request_json(session, url, params=None):
    params = apply_pipe_domain_out_fields(url, params)
    request_params = dict(params or {})

    token = getattr(session, "_arcgis_access_token", None)
    if not token:
        token = get_arcgis_token(session)
        session._arcgis_access_token = token

    if token and "token" not in request_params:
        request_params["token"] = token

    response = session.get(
        url, params=request_params, timeout=REQUEST_TIMEOUT_SECONDS, verify=VERIFY_SSL
    )

    response.raise_for_status()
    data = response.json()

    if "error" in data:
        error = data.get("error", {})
        code = str(error.get("code"))

        if code in ("498", "499"):
            warn(
                "ArcGIS access token was rejected. Clearing cached token and forcing one fresh login."
            )
            clear_cached_access_token()
            if hasattr(session, "_arcgis_access_token"):
                delattr(session, "_arcgis_access_token")

            request_params["token"] = get_arcgis_token(session)
            session._arcgis_access_token = request_params["token"]

            response = session.get(
                url,
                params=request_params,
                timeout=REQUEST_TIMEOUT_SECONDS,
                verify=VERIFY_SSL,
            )

            response.raise_for_status()
            data = response.json()

            if "error" in data:
                fail(
                    f"ArcGIS REST error after refreshed access token from {url}: {json.dumps(data.get('error', {}), indent=2)}"
                )
        else:
            fail(f"ArcGIS REST error from {url}: {json.dumps(error, indent=2)}")

    return data


def request_json_post(session, url, params):
    request_params = dict(params or {})

    token = request_params.get("token") or getattr(
        session, "_arcgis_access_token", None
    )

    if not token:
        token = get_arcgis_token(session)
        session._arcgis_access_token = token

    if token and "token" not in request_params:
        request_params["token"] = token

    response = session.post(
        url, data=request_params, timeout=REQUEST_TIMEOUT_SECONDS, verify=VERIFY_SSL
    )

    if response.status_code >= 400:
        snippet = response.text[:1000] if response.text else ""
        fail(
            f"ArcGIS POST failed for {url}. HTTP {response.status_code}. Response snippet: {snippet}"
        )

    data = response.json()

    if "error" in data:
        error = data.get("error", {})
        code = str(error.get("code"))

        if code in ("498", "499"):
            warn(
                "ArcGIS access token was rejected during POST. Clearing cached token and forcing one fresh login."
            )
            clear_cached_access_token()
            if hasattr(session, "_arcgis_access_token"):
                delattr(session, "_arcgis_access_token")
            request_params["token"] = get_arcgis_token(session)
            session._arcgis_access_token = request_params["token"]

            response = session.post(
                url,
                data=request_params,
                timeout=REQUEST_TIMEOUT_SECONDS,
                verify=VERIFY_SSL,
            )

            if response.status_code >= 400:
                snippet = response.text[:1000] if response.text else ""
                fail(
                    f"ArcGIS POST failed after refreshed token for {url}. HTTP {response.status_code}. Response snippet: {snippet}"
                )

            data = response.json()

            if "error" in data:
                fail(
                    f"ArcGIS REST POST error after refreshed access token from {url}: {json.dumps(data.get('error', {}), indent=2)}"
                )
        else:
            fail(f"ArcGIS REST POST error from {url}: {json.dumps(error, indent=2)}")

    return data


def layer_metadata(session, layer_url):
    data = request_json(session, layer_url, {"f": "json"})
    fields = data.get("fields", [])
    object_id_field = data.get("objectIdField") or next(
        (f.get("name") for f in fields if f.get("type") == "esriFieldTypeOID"), None
    )
    spatial_reference = (
        data.get("extent", {}).get("spatialReference")
        or data.get("spatialReference")
        or {}
    )
    wkid = spatial_reference.get("latestWkid") or spatial_reference.get("wkid")
    max_record_count = int(data.get("maxRecordCount") or REQUEST_PAGE_SIZE)
    page_size = (
        min(REQUEST_PAGE_SIZE, max_record_count)
        if max_record_count > 0
        else REQUEST_PAGE_SIZE
    )
    return {
        "object_id_field": object_id_field,
        "wkid": wkid,
        "fields": fields,
        "page_size": page_size,
    }


def esri_point_to_geom(geometry):
    if not geometry:
        return None
    if "x" not in geometry or "y" not in geometry:
        return None
    return Point(float(geometry["x"]), float(geometry["y"]))


def esri_polyline_to_geom(geometry):
    if not geometry or "paths" not in geometry:
        return None
    lines = []
    for path in geometry.get("paths", []):
        coords = [(float(x), float(y)) for x, y, *rest in path]
        if len(coords) >= 2:
            lines.append(LineString(coords))
    if not lines:
        return None
    if len(lines) == 1:
        return lines[0]
    return MultiLineString(lines)


def esri_geometry_to_shape(geometry):
    if not geometry:
        return None
    if "x" in geometry and "y" in geometry:
        return esri_point_to_geom(geometry)
    if "paths" in geometry:
        return esri_polyline_to_geom(geometry)
    try:
        return shape(geometry)
    except Exception:
        return None


def safe_cache_name(layer_name):
    return re.sub(r"[^A-Za-z0-9_]+", "_", layer_name.strip().lower()).strip("_")


def ensure_layer_cache_folder():
    if not os.path.isdir(LAYER_CACHE_FOLDER):
        os.makedirs(LAYER_CACHE_FOLDER)
        log(f"Created layer cache folder: {LAYER_CACHE_FOLDER}")


def layer_cache_paths(layer_name):
    ensure_layer_cache_folder()
    base = safe_cache_name(layer_name)
    data_path = os.path.join(LAYER_CACHE_FOLDER, base + ".pkl.gz")
    meta_path = os.path.join(LAYER_CACHE_FOLDER, base + ".meta.json")
    return data_path, meta_path


def metadata_field_names(meta):
    return [field.get("name") for field in meta.get("fields", []) if field.get("name")]


def resolve_from_names(field_names, candidates):
    return resolve_field_name(field_names, candidates)


def modified_field_from_meta(meta, layer_name):
    field_names = metadata_field_names(meta)
    field_name = resolve_from_names(field_names, MODIFIED_FIELD_CANDIDATES)
    if field_name:
        detail(f"{layer_name}: using delta modified field: {field_name}")
    else:
        warn(
            f"{layer_name}: no modified field found. Delta cache unavailable; full refresh will be used."
        )
    return field_name


def build_out_fields(meta, layer_name):
    field_names = metadata_field_names(meta)
    object_id_field = meta.get("object_id_field")
    modified_field = modified_field_from_meta(meta, layer_name)
    wanted = []
    if object_id_field:
        wanted.append(object_id_field)
    if modified_field:
        wanted.append(modified_field)
    if "historic" in layer_name.lower() or "leak" in layer_name.lower():
        candidate_groups = [
            LEAK_KEY_CANDIDATES,
            GLOBALID_CANDIDATES,
            ["jurisdiction", "Jurisdiction", "JURISDICTION"],
        ]
    else:
        candidate_groups = [
            PIPE_DIAMETER_CANDIDATES,
            PIPE_MATERIAL_CANDIDATES,
            PIPE_PRESSURE_CANDIDATES,
            GLOBALID_CANDIDATES,
            ["jurisdiction", "Jurisdiction", "JURISDICTION"],
        ]
    for group in candidate_groups:
        resolved = resolve_from_names(field_names, group)
        if resolved:
            wanted.append(resolved)
    deduped = []
    for field in wanted:
        if field and field not in deduped:
            deduped.append(field)
    if not deduped:
        warn(f"{layer_name}: could not narrow outFields; requesting all fields")
        return "*"
    out_fields = ",".join(deduped)
    detail(f"{layer_name}: using outFields={out_fields}")
    return out_fields


def query_count(session, layer_url, where_clause, layer_name):
    params = {"f": "json", "where": where_clause, "returnCountOnly": "true"}
    data = request_json(session, layer_url + "/query", params)
    count = data.get("count")
    if count is None:
        warn(f"{layer_name}: returnCountOnly did not return count.")
        return None
    log(f"{layer_name}: server-side count for WHERE [{where_clause}] = {int(count):,}")
    return int(count)


def date_value_to_epoch_ms(value):
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    parsed_number = parse_number(value)
    if parsed_number is not None:
        return int(parsed_number)
    try:
        parsed = pd.to_datetime(value, utc=True)
        if pd.isna(parsed):
            return None
        return int(parsed.timestamp() * 1000)
    except Exception:
        return None


def max_modified_epoch_ms(gdf, modified_field):
    if (
        gdf is None
        or len(gdf) == 0
        or not modified_field
        or modified_field not in gdf.columns
    ):
        return None
    values = []
    for value in gdf[modified_field].tolist():
        epoch_ms = date_value_to_epoch_ms(value)
        if epoch_ms is not None:
            values.append(epoch_ms)
    if not values:
        return None
    return max(values)


def epoch_ms_to_sql_timestamp(epoch_ms):
    safe_default = "timestamp '1970-01-01 00:00:00'"
    if epoch_ms is None:
        warn("Delta watermark was None. Falling back to full safe delta window.")
        return safe_default
    try:
        value = float(epoch_ms)
    except Exception:
        warn(
            f"Delta watermark was not numeric [{epoch_ms}]. Falling back to full safe delta window."
        )
        return safe_default
    try:
        if not math.isfinite(value):
            warn(
                f"Delta watermark was not finite [{epoch_ms}]. Falling back to full safe delta window."
            )
            return safe_default
    except Exception:
        pass
    if value <= 0:
        warn(
            f"Delta watermark was <= 0 [{epoch_ms}]. Falling back to full safe delta window."
        )
        return safe_default
    max_reasonable_epoch_ms = 32503680000000.0
    if value > max_reasonable_epoch_ms:
        warn(
            f"Delta watermark was outside valid millisecond range [{epoch_ms}]. Falling back to full safe delta window."
        )
        return safe_default
    try:
        when = dt.datetime.fromtimestamp(value / 1000.0, dt.UTC).replace(tzinfo=None)
    except Exception as ex:
        warn(
            f"Could not convert delta watermark [{epoch_ms}] to timestamp: {ex}. Falling back to full safe delta window."
        )
        return safe_default
    return f"timestamp '{when.strftime('%Y-%m-%d %H:%M:%S')}'"


def build_delta_where(base_where, modified_field, last_epoch_ms):
    return f"({base_where}) AND {modified_field} > {epoch_ms_to_sql_timestamp(last_epoch_ms)}"


def read_layer_cache(layer_name, layer_url, where_clause):
    if not USE_LAYER_CACHE or FORCE_LAYER_REFRESH:
        return None, None
    data_path, meta_path = layer_cache_paths(layer_name)
    if not os.path.isfile(data_path) or not os.path.isfile(meta_path):
        log(f"{layer_name}: no local cache found.")
        return None, None
    try:
        with open(meta_path, "r", encoding="utf-8") as handle:
            meta = json.load(handle)
        if meta.get("layer_url") != layer_url:
            log(f"{layer_name}: cache URL changed. Refreshing layer.")
            return None, None
        if meta.get("where_clause") != where_clause:
            log(f"{layer_name}: cache WHERE changed. Refreshing layer.")
            return None, None
        gdf = pd.read_pickle(data_path, compression="gzip")
        log(f"{layer_name}: loaded {len(gdf):,} records from local cache: {data_path}")
        return gdf, meta
    except Exception as ex:
        warn(
            f"{layer_name}: failed to read cache. Full refresh will be used. Error={ex}"
        )
        return None, None


def write_layer_cache(
    layer_name, layer_url, where_clause, server_count, modified_field, gdf
):
    if not USE_LAYER_CACHE:
        return
    data_path, meta_path = layer_cache_paths(layer_name)
    max_modified_ms = max_modified_epoch_ms(gdf, modified_field)
    try:
        gdf.to_pickle(data_path, compression="gzip")
        meta = {
            "layer_name": layer_name,
            "layer_url": layer_url,
            "where_clause": where_clause,
            "server_count": int(server_count) if server_count is not None else None,
            "modified_field": modified_field,
            "max_modified_epoch_ms": int(max_modified_ms)
            if max_modified_ms is not None
            else None,
            "cached_utc": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
            "record_count_written": len(gdf),
        }
        with open(meta_path, "w", encoding="utf-8") as handle:
            json.dump(meta, handle, indent=2)
        log(f"{layer_name}: wrote local cache: {data_path}")
        log(f"{layer_name}: max cached {modified_field} = {max_modified_ms}")
    except Exception as ex:
        warn(f"{layer_name}: failed to write cache. Error={ex}")


def chunk_list(values, chunk_size):
    for index in range(0, len(values), chunk_size):
        yield values[index : index + chunk_size]


def query_object_ids(session, layer_url, where_clause, layer_name):
    log(f"{layer_name}: requesting matching OBJECTIDs for WHERE [{where_clause}]")

    params = {"f": "json", "where": where_clause, "returnIdsOnly": "true"}

    data = request_json(session, layer_url + "/query", params)

    object_ids = data.get("objectIds") or data.get("objectids") or []
    object_id_field = data.get("objectIdFieldName") or data.get("objectIdField") or None

    object_ids = sorted([int(value) for value in object_ids])

    log(f"{layer_name}: returnIdsOnly returned {len(object_ids):,} OBJECTIDs")

    if object_id_field:
        log(f"{layer_name}: returnIdsOnly objectIdFieldName={object_id_field}")

    return object_ids


def fetch_objectid_batch(
    layer_url,
    object_id_batch,
    layer_name,
    meta,
    out_fields,
    batch_number,
    batch_total,
    token,
):
    local_session = requests.Session()
    local_session._arcgis_access_token = token

    params = {
        "f": "json",
        "objectIds": ",".join([str(value) for value in object_id_batch]),
        "outFields": out_fields,
        "returnGeometry": "true",
        "token": token,
    }

    if meta["wkid"]:
        params["outSR"] = meta["wkid"]

    data = request_json_post(local_session, layer_url + "/query", params)
    batch = data.get("features", [])
    log(
        f"{layer_name}: objectId batch {batch_number:,}/{batch_total:,} returned {len(batch):,} features"
    )
    return batch


def query_feature_set(session, layer_url, where_clause, layer_name, meta, out_fields):
    source_crs = f"EPSG:{meta['wkid']}" if meta["wkid"] else None

    if USE_OBJECTID_BATCH_DOWNLOAD:
        object_ids = query_object_ids(session, layer_url, where_clause, layer_name)

        if not object_ids:
            log(
                f"{layer_name}: no OBJECTIDs returned for WHERE. Returning empty GeoDataFrame."
            )
            return gpd.GeoDataFrame([], geometry=[], crs=source_crs)

        active_token = getattr(session, "_arcgis_access_token", None)

        if not active_token:
            active_token = get_arcgis_token(session)
            session._arcgis_access_token = active_token

        batches = list(chunk_list(object_ids, OBJECTID_BATCH_SIZE))
        total_batches = len(batches)
        log(
            f"{layer_name}: downloading {len(object_ids):,} OBJECTIDs in {total_batches:,} POST batches with {OBJECTID_DOWNLOAD_WORKERS} workers"
        )

        features = []

        with futures.ThreadPoolExecutor(
            max_workers=OBJECTID_DOWNLOAD_WORKERS
        ) as executor:
            future_lookup = {
                executor.submit(
                    fetch_objectid_batch,
                    layer_url,
                    object_id_batch,
                    layer_name,
                    meta,
                    out_fields,
                    batch_number,
                    total_batches,
                    active_token,
                ): batch_number
                for batch_number, object_id_batch in enumerate(batches, start=1)
            }

            completed_batches = 0

            for future in futures.as_completed(future_lookup):
                batch_number = future_lookup[future]

                try:
                    features.extend(future.result())
                except Exception as ex:
                    fail(
                        f"{layer_name}: objectId POST batch {batch_number:,}/{total_batches:,} failed: {ex}"
                    )

                completed_batches += 1

                if (
                    completed_batches == 1
                    or completed_batches % 10 == 0
                    or completed_batches == total_batches
                ):
                    log(
                        f"{layer_name}: completed {completed_batches:,}/{total_batches:,} batches; accumulated {len(features):,} features"
                    )

    else:
        page_size = meta["page_size"]
        offset = 0
        features = []

        while True:
            params = {
                "f": "json",
                "where": where_clause,
                "outFields": out_fields,
                "returnGeometry": "true",
                "resultOffset": offset,
                "resultRecordCount": page_size,
                "orderByFields": meta["object_id_field"],
            }

            if meta["wkid"]:
                params["outSR"] = meta["wkid"]

            data = request_json(session, layer_url + "/query", params)
            batch = data.get("features", [])

            if not batch:
                break

            features.extend(batch)
            log(f"{layer_name}: fetched {len(features):,} records")

            if len(batch) < page_size and not data.get("exceededTransferLimit"):
                break

            offset += page_size

    rows = []
    geometries = []

    for feature in features:
        attrs = feature.get("attributes", {}) or {}
        geom = esri_geometry_to_shape(feature.get("geometry"))
        rows.append(attrs)
        geometries.append(geom)

    if not rows:
        return gpd.GeoDataFrame([], geometry=[], crs=source_crs)

    gdf = gpd.GeoDataFrame(rows, geometry=geometries, crs=source_crs)
    gdf = gdf[gdf.geometry.notna()].copy()
    log(f"{layer_name}: usable geometries = {len(gdf):,}")
    return gdf


def upsert_cached_layer(cached_gdf, delta_gdf, object_id_field):
    if delta_gdf is None or len(delta_gdf) == 0:
        return cached_gdf
    if (
        object_id_field not in cached_gdf.columns
        or object_id_field not in delta_gdf.columns
    ):
        warn(
            "Cannot upsert delta because OBJECTID field is missing. Returning full delta only is unsafe, so keeping cached layer."
        )
        return cached_gdf
    delta_ids = set(delta_gdf[object_id_field].astype(str).tolist())
    keep_cached = cached_gdf[
        ~cached_gdf[object_id_field].astype(str).isin(delta_ids)
    ].copy()
    merged = pd.concat([keep_cached, delta_gdf], ignore_index=True)
    return gpd.GeoDataFrame(
        merged, geometry="geometry", crs=cached_gdf.crs or delta_gdf.crs
    )


def cache_age_seconds(layer_name, layer_url, where_clause):
    """Age of a usable cache in seconds, or None if there is not one."""
    if not USE_LAYER_CACHE or FORCE_LAYER_REFRESH:
        return None
    data_path, meta_path = layer_cache_paths(layer_name)
    if not os.path.isfile(data_path) or not os.path.isfile(meta_path):
        return None
    try:
        with open(meta_path, encoding="utf-8") as handle:
            meta = json.load(handle)
        if meta.get("layer_url") != layer_url or meta.get("where_clause") != where_clause:
            return None
        cached_utc = meta.get("cached_utc")
        if not cached_utc:
            return None
        stamp = dt.datetime.fromisoformat(cached_utc.replace("Z", "+00:00"))
        return max(0.0, (dt.datetime.now(dt.UTC) - stamp).total_seconds())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as ex:
        # A cache we cannot read the age of is simply treated as absent.
        detail(f"{layer_name}: could not read cache age ({ex})")
        return None


def query_arcgis_layer(session, layer_url, where_clause, layer_name):
    step(f"Querying ArcGIS REST layer: {layer_name}")

    # A recent cache is trusted without contacting the server. Checking costs a
    # metadata request plus two count queries per layer and needs a valid token,
    # which dominates start-up when the data has not changed.
    age = cache_age_seconds(layer_name, layer_url, where_clause)
    if age is not None and config.CACHE_FRESH_SECONDS > 0 and age < config.CACHE_FRESH_SECONDS:
        cached_gdf, _ = read_layer_cache(layer_name, layer_url, where_clause)
        if cached_gdf is not None:
            log(
                f"{layer_name}: cache is {age / 60:.0f} min old; skipping server check. "
                f"Set FORCE_LAYER_REFRESH=1 to refresh."
            )
            return cached_gdf

    meta = layer_metadata(session, layer_url)
    if not meta["object_id_field"]:
        fail(f"Could not determine object id field for {layer_name}")

    object_id_field = meta["object_id_field"]
    modified_field = modified_field_from_meta(meta, layer_name)
    out_fields = build_out_fields(meta, layer_name)
    server_count = query_count(session, layer_url, where_clause, layer_name)

    cached_gdf, cached_meta = read_layer_cache(layer_name, layer_url, where_clause)

    if cached_gdf is not None and modified_field and cached_meta:
        last_epoch_ms = cached_meta.get("max_modified_epoch_ms")
        if last_epoch_ms:
            delta_where = build_delta_where(where_clause, modified_field, last_epoch_ms)
            log(f"{layer_name}: attempting delta refresh WHERE [{delta_where}]")
            delta_count = query_count(
                session, layer_url, delta_where, layer_name + " delta"
            )
            if delta_count == 0:
                if server_count is None or len(cached_gdf) == int(server_count):
                    log(f"{layer_name}: no delta changes found. Using local cache.")
                    return cached_gdf
                warn(
                    f"{layer_name}: no modified deltas, but count differs cached={len(cached_gdf):,}, server={int(server_count):,}. Full refresh required."
                )
            else:
                delta_gdf = query_feature_set(
                    session,
                    layer_url,
                    delta_where,
                    layer_name + " delta",
                    meta,
                    out_fields,
                )
                merged_gdf = upsert_cached_layer(cached_gdf, delta_gdf, object_id_field)
                if server_count is not None and len(merged_gdf) != int(server_count):
                    warn(
                        f"{layer_name}: merged cache count {len(merged_gdf):,} does not equal server count {int(server_count):,}. This usually means deletes occurred; doing full refresh."
                    )
                else:
                    log(
                        f"{layer_name}: delta refresh merged {len(delta_gdf):,} changed records into cache."
                    )
                    write_layer_cache(
                        layer_name,
                        layer_url,
                        where_clause,
                        server_count,
                        modified_field,
                        merged_gdf,
                    )
                    return merged_gdf

    elif cached_gdf is not None and (
        server_count is None or len(cached_gdf) == int(server_count)
    ):
        log(
            f"{layer_name}: using local cache by count because modified field metadata is unavailable."
        )
        return cached_gdf

    log(f"{layer_name}: performing full layer download.")
    gdf = query_feature_set(
        session, layer_url, where_clause, layer_name, meta, out_fields
    )
    write_layer_cache(
        layer_name, layer_url, where_clause, server_count, modified_field, gdf
    )
    return gdf


def load_supplemental():
    step("Loading supplemental CSV")
    log(f"Supplemental CSV: {SUPPLEMENTAL_CSV}")
    if not os.path.isfile(SUPPLEMENTAL_CSV):
        fail(f"Missing supplemental CSV: {SUPPLEMENTAL_CSV}")
    df = pd.read_csv(
        SUPPLEMENTAL_CSV, dtype=str, encoding="utf-8-sig", keep_default_na=False
    )
    log(f"Supplemental rows: {len(df):,}")
    key_field = resolved_field(
        df.columns, SUPP_KEY_CANDIDATES, True, "supplemental leak key"
    )
    diameter_field = resolved_field(
        df.columns, SUPP_DIAMETER_CANDIDATES, False, "supplemental diameter"
    )
    material_field = resolved_field(
        df.columns, SUPP_MATERIAL_CANDIDATES, False, "supplemental material"
    )
    pressure_field = resolved_field(
        df.columns, SUPP_PRESSURE_CANDIDATES, False, "supplemental pressure"
    )
    facility_field = resolved_field(
        df.columns, SUPP_FACILITY_CANDIDATES, False, "supplemental facility type"
    )
    records = {}
    skipped_no_key = 0
    for _, row in df.iterrows():
        leak_key = normalize_key(row.get(key_field))
        if not leak_key:
            skipped_no_key += 1
            continue
        records[leak_key] = {
            "diameter": parse_number(row.get(diameter_field))
            if diameter_field
            else None,
            "material": clean(row.get(material_field)) if material_field else "",
            "pressure": clean(row.get(pressure_field)) if pressure_field else "",
            "facility": clean(row.get(facility_field)) if facility_field else "",
        }
    log(f"Supplemental records keyed: {len(records):,}")
    if skipped_no_key:
        warn(f"Supplemental rows skipped, missing key: {skipped_no_key:,}")
    if REQUIRE_PRESSURE_MATCH and not pressure_field:
        fail(
            "REQUIRE_PRESSURE_MATCH is True but no supplemental pressure field was resolved."
        )
    if not pressure_field:
        # Not actionable while pressure matching is off, which is the default.
        detail(
            "No supplemental pressure field was resolved. Pressure matching is not enforced."
        )
    return records


def to_target_crs(gdf, name):
    step(f"Projecting to analysis CRS: {name}")
    if gdf.crs is None:
        warn(
            f"{name} has no CRS from service metadata. Assuming EPSG:3857 before projecting."
        )
        gdf = gdf.set_crs("EPSG:3857")
    projected = gdf.to_crs(TARGET_CRS)
    log(f"{name}: CRS now {projected.crs}")
    return projected


def prepare_leaks(leaks_gdf, supplemental):
    step("Preparing leak records")
    leak_oid_field = resolved_field(
        leaks_gdf.columns, OBJECTID_CANDIDATES, True, "leak OBJECTID"
    )
    leak_key_field = resolved_field(
        leaks_gdf.columns, LEAK_KEY_CANDIDATES, True, "leak key"
    )
    leak_globalid_field = resolved_field(
        leaks_gdf.columns, GLOBALID_CANDIDATES, False, "leak GlobalID"
    )
    prepared = []
    counters = defaultdict(int)
    for _, row in leaks_gdf.iterrows():
        counters["leaks_read"] += 1
        leak_oid = int(row[leak_oid_field])
        leak_number = normalize_key(row.get(leak_key_field))
        leak_info = supplemental.get(leak_number)
        if not leak_info:
            counters["unmatched_missing_supplemental"] += 1
            continue
        if leak_info["diameter"] is None or not leak_info["material"]:
            counters["unmatched_missing_match_attributes"] += 1
            continue
        prepared.append(
            {
                "leak_oid": leak_oid,
                "leak_number": leak_number,
                "leak_globalid": clean(row.get(leak_globalid_field))
                if leak_globalid_field
                else "",
                "leak_info": leak_info,
                "allowed_layers": route_layers(leak_info["facility"]),
                "geometry": row.geometry,
            }
        )
    log(f"Prepared leak match tasks: {len(prepared):,}")
    for name in sorted(counters):
        log(f"{name}: {counters[name]:,}")
    return prepared, counters


def prepare_pipes(pipe_gdf, layer_name):
    step(f"Preparing pipe records: {layer_name}")
    pipe_oid_field = resolved_field(
        pipe_gdf.columns, OBJECTID_CANDIDATES, True, layer_name + " OBJECTID"
    )
    material_field = resolved_field(
        pipe_gdf.columns, PIPE_MATERIAL_CANDIDATES, True, layer_name + " material"
    )
    diameter_field = resolved_field(
        pipe_gdf.columns, PIPE_DIAMETER_CANDIDATES, True, layer_name + " diameter"
    )
    pressure_field = resolved_field(
        pipe_gdf.columns, PIPE_PRESSURE_CANDIDATES, False, layer_name + " pressure"
    )
    globalid_field = resolved_field(
        pipe_gdf.columns, GLOBALID_CANDIDATES, False, layer_name + " GlobalID"
    )
    if REQUIRE_PRESSURE_MATCH and not pressure_field:
        fail(
            f"REQUIRE_PRESSURE_MATCH is True but no pressure field was resolved on {layer_name}."
        )
    records = []
    missing_material = 0
    missing_diameter = 0
    for _, row in pipe_gdf.iterrows():
        pipe_oid = int(row[pipe_oid_field])
        mat = material_label(row.get(material_field))
        diam = parse_number(row.get(diameter_field))
        if not mat:
            missing_material += 1
        if diam is None:
            missing_diameter += 1
        records.append(
            {
                "layer": layer_name,
                "pipe_oid": pipe_oid,
                "globalid": clean(row.get(globalid_field)) if globalid_field else "",
                "material": mat,
                "diameter": diam,
                "pressure": clean(row.get(pressure_field)) if pressure_field else "",
                "geometry": row.geometry,
            }
        )
    log(f"Prepared {len(records):,} {layer_name} pipes")
    log(f"{layer_name} pipes missing material: {missing_material:,}")
    log(f"{layer_name} pipes missing diameter: {missing_diameter:,}")
    return records


def init_worker(pipe_sources):
    global WORKER_TREES
    WORKER_TREES = {}
    for layer_name, records in pipe_sources.items():
        geoms = [record["geometry"] for record in records]
        tree = STRtree(geoms)
        WORKER_TREES[layer_name] = {"tree": tree, "geoms": geoms, "records": records}


def get_tree_hits(tree_info, query_geometry):
    query_buffer = query_geometry.buffer(MAX_RADIUS_FT)
    raw_hits = tree_info["tree"].query(query_buffer)
    hits = []
    for hit in raw_hits:
        if isinstance(hit, numbers.Integral):
            hits.append(int(hit))
        else:
            try:
                hits.append(tree_info["geoms"].index(hit))
            except ValueError:
                continue
    return hits


def match_one_leak(task):
    leak_oid = task["leak_oid"]
    leak_number = task["leak_number"]
    leak_info = task["leak_info"]
    leak_geometry = task["geometry"]
    allowed_layers = task["allowed_layers"]
    candidates = []
    for layer_name in allowed_layers:
        tree_info = WORKER_TREES[layer_name]
        for idx in get_tree_hits(tree_info, leak_geometry):
            pipe = tree_info["records"][idx]
            distance_ft = leak_geometry.distance(pipe["geometry"])
            if distance_ft > MAX_RADIUS_FT:
                continue
            if not diameter_matches(leak_info["diameter"], pipe["diameter"]):
                continue
            if not material_matches(leak_info["material"], pipe["material"]):
                continue
            if not pressure_matches(leak_info["pressure"], pipe["pressure"]):
                continue
            candidates.append(
                {
                    "layer": layer_name,
                    "pipe_oid": pipe["pipe_oid"],
                    "distance_ft": distance_ft,
                }
            )
    if not candidates:
        return {
            "leak_oid": leak_oid,
            "matched": False,
            "reason": "No exact diameter/material/pressure match found within MAX_RADIUS_FT",
        }
    candidates.sort(key=lambda item: item["distance_ft"])
    best = candidates[0]
    verbose(
        f"Leak {leak_number}: matched {best['layer']} pipe OID {best['pipe_oid']} at {best['distance_ft']:.2f} ft"
    )
    return {
        "leak_oid": leak_oid,
        "matched": True,
        "layer": best["layer"],
        "pipe_oid": best["pipe_oid"],
        "distance_ft": best["distance_ft"],
        "matched_radius": matched_radius_from_distance(best["distance_ft"]),
        "reason": "",
    }


def run_matching(leak_tasks, pipe_sources):
    step("Running spatial + attribute matching")
    log(f"USE_MULTIPROCESSING: {USE_MULTIPROCESSING}")
    log(f"WORKERS: {WORKERS}")
    if not leak_tasks:
        return []
    if not USE_MULTIPROCESSING or WORKERS <= 1:
        init_worker(pipe_sources)
        results = []
        for index, task in enumerate(leak_tasks, start=1):
            if index == 1 or index % MESSAGE_EVERY_N_LEAKS == 0:
                log(f"Matched task progress: {index:,}/{len(leak_tasks):,}")
            results.append(match_one_leak(task))
        return results
    results = []
    with futures.ProcessPoolExecutor(
        max_workers=WORKERS, initializer=init_worker, initargs=(pipe_sources,)
    ) as executor:
        future_list = [executor.submit(match_one_leak, task) for task in leak_tasks]
        for index, future in enumerate(futures.as_completed(future_list), start=1):
            if index == 1 or index % MESSAGE_EVERY_N_LEAKS == 0:
                log(f"Matched task progress: {index:,}/{len(future_list):,}")
            results.append(future.result())
    log(f"Matching complete. Results: {len(results):,}")
    return results


def pipe_lookup(pipe_sources):
    lookup = {}
    for layer_name, records in pipe_sources.items():
        lookup[layer_name] = {record["pipe_oid"]: record for record in records}
    return lookup


def write_outputs(leak_tasks, match_results, pipe_sources, initial_counters):
    step("Writing GeoPackage outputs")
    task_lookup = {task["leak_oid"]: task for task in leak_tasks}
    pipes = pipe_lookup(pipe_sources)
    point_rows = []
    line_rows = []
    audit_rows = []
    counters = defaultdict(int)
    for name, value in initial_counters.items():
        counters[name] += value
    run_utc = dt.datetime.now(dt.UTC)
    for result in match_results:
        task = task_lookup[result["leak_oid"]]
        leak_oid = task["leak_oid"]
        leak_number = task["leak_number"]
        leak_globalid = task["leak_globalid"]
        leak_info = task["leak_info"]
        original_geometry = task["geometry"]
        original_point = (
            original_geometry
            if original_geometry.geom_type == "Point"
            else original_geometry.centroid
        )
        if not result["matched"]:
            counters["unmatched"] += 1
            audit_rows.append(
                {
                    "LeakOID": leak_oid,
                    "LeakKey": leak_number,
                    "LeakGlobalID": leak_globalid,
                    "LeakMaterial": leak_info["material"],
                    "LeakDiameter": leak_info["diameter"],
                    "LeakPressure": leak_info["pressure"],
                    "FacilityType": leak_info["facility"],
                    "LinkedLayer": ",".join(task["allowed_layers"]),
                    "MatchedPipeOID": None,
                    "MatchedPipeGID": "",
                    "PipeMaterial": "",
                    "PipeDiameter": None,
                    "PipePressure": "",
                    "SearchRadiusFt": None,
                    "DistanceFt": None,
                    "MatchStatus": "NoMatch",
                    "NoMatchReason": result["reason"],
                    "OrigX": original_point.x,
                    "OrigY": original_point.y,
                    "NewX": None,
                    "NewY": None,
                    "RunUTC": run_utc,
                }
            )
            continue
        pipe = pipes[result["layer"]][result["pipe_oid"]]
        snap_pair = nearest_points(original_point, pipe["geometry"])
        snapped_point = snap_pair[1]
        guide_line = LineString([original_point, snapped_point])
        point_rows.append(
            {
                "OrigLeakOID": leak_oid,
                "LeakKey": leak_number,
                "LinkedLayer": result["layer"],
                "MatchedPipeOID": result["pipe_oid"],
                "MatchedPipeGID": pipe["globalid"],
                "DistanceFt": result["distance_ft"],
                "SearchRadiusFt": result["matched_radius"],
                "MatchMaterial": pipe["material"],
                "MatchDiameter": pipe["diameter"],
                "MatchPressure": pipe["pressure"],
                "geometry": snapped_point,
            }
        )
        line_rows.append(
            {
                "LeakOID": leak_oid,
                "LeakKey": leak_number,
                "LinkedLayer": result["layer"],
                "DistanceFt": result["distance_ft"],
                "MatchedPipeOID": result["pipe_oid"],
                "geometry": guide_line,
            }
        )
        audit_rows.append(
            {
                "LeakOID": leak_oid,
                "LeakKey": leak_number,
                "LeakGlobalID": leak_globalid,
                "LeakMaterial": leak_info["material"],
                "LeakDiameter": leak_info["diameter"],
                "LeakPressure": leak_info["pressure"],
                "FacilityType": leak_info["facility"],
                "LinkedLayer": result["layer"],
                "MatchedPipeOID": result["pipe_oid"],
                "MatchedPipeGID": pipe["globalid"],
                "PipeMaterial": pipe["material"],
                "PipeDiameter": pipe["diameter"],
                "PipePressure": pipe["pressure"],
                "SearchRadiusFt": result["matched_radius"],
                "DistanceFt": result["distance_ft"],
                "MatchStatus": "Matched",
                "NoMatchReason": "",
                "OrigX": original_point.x,
                "OrigY": original_point.y,
                "NewX": snapped_point.x,
                "NewY": snapped_point.y,
                "RunUTC": run_utc,
            }
        )
        counters["matched"] += 1
    if os.path.exists(OUTPUT_GPKG):
        os.remove(OUTPUT_GPKG)
        log(f"Deleted old GeoPackage: {OUTPUT_GPKG}")
    relocated_gdf = gpd.GeoDataFrame(point_rows, geometry="geometry", crs=TARGET_CRS)
    lines_gdf = gpd.GeoDataFrame(line_rows, geometry="geometry", crs=TARGET_CRS)
    audit_df = pd.DataFrame(audit_rows)
    if len(audit_df):
        audit_gdf = gpd.GeoDataFrame(
            audit_df,
            geometry=gpd.points_from_xy(
                audit_df["OrigX"], audit_df["OrigY"], crs=TARGET_CRS
            ),
        )
    else:
        audit_gdf = gpd.GeoDataFrame(audit_df, geometry=[], crs=TARGET_CRS)
    relocated_gdf.to_file(OUTPUT_GPKG, layer="relocated_leaks", driver="GPKG")
    lines_gdf.to_file(OUTPUT_GPKG, layer="relocated_leak_offset_lines", driver="GPKG")
    audit_gdf.to_file(OUTPUT_GPKG, layer="leak_relocation_audit", driver="GPKG")
    log(f"Wrote relocated_leaks: {len(relocated_gdf):,}")
    log(f"Wrote relocated_leak_offset_lines: {len(lines_gdf):,}")
    log(f"Wrote leak_relocation_audit: {len(audit_gdf):,}")
    log(f"Output GeoPackage: {OUTPUT_GPKG}")
    return counters


def main():
    step("Starting non-ArcPy GeoPandas leak relocation")
    log(f"Supplemental CSV: {SUPPLEMENTAL_CSV}")
    log(f"Output GeoPackage: {OUTPUT_GPKG}")
    log("Analysis CRS: native per-layer (no reprojection before matching)")
    log(f"MA layer filter: {WHERE_MA}")
    log(f"Max radius ft: {MAX_RADIUS_FT}")
    log(f"Require pressure match: {REQUIRE_PRESSURE_MATCH}")
    log(f"Material family fallback: {ALLOW_MATERIAL_FAMILY_FALLBACK}")
    ensure_output_folder()

    with timed("supplemental CSV"):
        supplemental = load_supplemental()

    session = make_session()

    layer_specs = [
        ("historic leaks", HIST_LEAK_URL),
        ("distribution pipes", DISTRIBUTION_PIPE_URL),
        ("service pipes", SERVICE_PIPE_URL),
    ]

    with timed("load layers"):
        if config.PARALLEL_LAYER_LOAD:
            # Independent reads. Fetch the token once up front so the workers do
            # not race into three interactive sign-ins.
            get_arcgis_token(session)
            with futures.ThreadPoolExecutor(max_workers=len(layer_specs)) as pool:
                submitted = {
                    name: pool.submit(query_arcgis_layer, session, url, WHERE_MA, name)
                    for name, url in layer_specs
                }
                loaded = {name: future.result() for name, future in submitted.items()}
        else:
            loaded = {
                name: query_arcgis_layer(session, url, WHERE_MA, name)
                for name, url in layer_specs
            }

    leaks = loaded["historic leaks"]
    distribution = loaded["distribution pipes"]
    service = loaded["service pipes"]

    with timed("normalize CRS"):
        leaks, distribution, service, ANALYSIS_CRS = (
            normalize_loaded_layers_to_analysis_crs(session, leaks, distribution, service)
        )
    globals()["TARGET_CRS"] = ANALYSIS_CRS

    detail("CRS sanity check after normalization:")
    for label, gdf in [("historic leaks", leaks), ("distribution pipes", distribution),
                       ("service pipes", service)]:
        detail(f"  {label}: CRS={gdf.crs} bounds={list(gdf.total_bounds)}")

    with timed("prepare leaks"):
        leak_tasks, initial_counters = prepare_leaks(leaks, supplemental)
    with timed("build pipe indexes"):
        pipe_sources = {
            "distribution": prepare_pipes(distribution, "distribution"),
            "service": prepare_pipes(service, "service"),
        }
    with timed("match leaks to pipes"):
        match_results = run_matching(leak_tasks, pipe_sources)
    with timed("write outputs"):
        counters = write_outputs(leak_tasks, match_results, pipe_sources, initial_counters)

    step("Finished")
    for name in sorted(counters):
        log(f"{name}: {counters[name]:,}")
    log(f"GeoPackage: {OUTPUT_GPKG}")
    report_timings()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(traceback.format_exc(), flush=True)
        sys.exit(1)
