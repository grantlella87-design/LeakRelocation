# -*- coding: utf-8 -*-
"""
Fast non-ArcPy historic leak relocation workflow.
Uses ArcGIS REST + GeoPandas/Shapely instead of ArcPy geoprocessing.
Reads MA historic leaks, MA distribution pipes, MA service pipes, supplements leak attributes from the shared CSV,
matches each leak to the nearest pipe with exact diameter and material/family match, snaps the leak to the matched pipe,
and writes a GeoPackage with relocated points, offset guide lines, and an audit table.
"""
import concurrent.futures as futures
import datetime as dt
import json
import math
import numbers
import os
import re
import sys
import traceback
import datetime as _datetime
import glob
import shutil as _shutil
import tempfile
import sqlite3
import keyring
import webbrowser
import urllib.parse
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
import geopandas as gpd
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

from leakrelocation import config
from leakrelocation.matching import (
    MATERIAL_FAMILY_TERMS,
    SERVICE_ASSETTYPE_LABELS,
    clean,
    diameter_matches,
    material_family,
    material_label,
    material_matches,
    matched_radius_from_distance,
    normalize_key,
    parse_number,
    pressure_matches,
    resolve_field_name,
    route_layers,
    upper,
)

# === Native CRS patch: per-layer MapServer spatial reference ===
# Each source layer must get its own native CRS from the actual MapServer layer being queried.
# Do not force all layers to EPSG:2249 before matching.
from pyproj import CRS

LAYER_NATIVE_CRS_CONFIG = {
    "historic_leaks": {
        "url_global": "HIST_LEAK_URL"
    },
    "distribution_pipes": {
        "url_global": "DISTRIBUTION_PIPE_URL"
    },
    "service_pipes": {
        "url_global": "SERVICE_PIPE_URL"
    }
}

ANALYSIS_CRS_LAYER_KEY = "distribution_pipes"
_NATIVE_CRS_CACHE = {}

def _spatial_reference_from_layer_json(layer_json):
    sr = layer_json.get("extent", {}).get("spatialReference") or layer_json.get("spatialReference") or {}
    if sr.get("wkt"):
        return CRS.from_wkt(sr["wkt"]), sr
    wkid = sr.get("latestWkid") or sr.get("wkid")
    if wkid:
        return CRS.from_epsg(int(wkid)), sr
    raise RuntimeError("Layer metadata did not include a usable spatialReference: {0}".format(sr))

def get_layer_native_crs(session, layer_key):
    if layer_key in _NATIVE_CRS_CACHE:
        return _NATIVE_CRS_CACHE[layer_key]
    if layer_key not in LAYER_NATIVE_CRS_CONFIG:
        raise KeyError("Unknown layer_key for native CRS lookup: {0}".format(layer_key))
    cfg = LAYER_NATIVE_CRS_CONFIG[layer_key]
    url_global = cfg["url_global"]
    layer_url = globals().get(url_global)
    if not layer_url:
        raise RuntimeError("Missing layer URL global {0} for {1}".format(url_global, layer_key))
    layer_json = request_json(session, layer_url, {"f": "json"})
    crs, sr = _spatial_reference_from_layer_json(layer_json)
    _NATIVE_CRS_CACHE[layer_key] = crs
    print("{0}: native CRS assigned from MapServer [{1}]: {2}".format(layer_key, layer_url, crs.name), flush=True)
    return crs

def assign_native_crs(session, layer_key, gdf):
    crs = get_layer_native_crs(session, layer_key)
    if gdf is None or len(gdf) == 0:
        return gdf
    existing = getattr(gdf, "crs", None)
    if existing is None:
        print("{0}: dataframe CRS was empty; assigning native CRS {1}".format(layer_key, crs.name), flush=True)
    else:
        print("{0}: overriding dataframe CRS [{1}] with native CRS [{2}]".format(layer_key, existing, crs.name), flush=True)
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
        print("{0}: projecting from [{1}] to analysis CRS [{2}]".format(layer_key, gdf.crs, analysis.name), flush=True)
        return gdf.to_crs(analysis)
    print("{0}: already in analysis CRS [{1}]".format(layer_key, analysis.name), flush=True)
    return gdf

def normalize_loaded_layers_to_analysis_crs(session, historic_leaks, distribution_pipes, service_pipes):
    historic_leaks = assign_native_crs(session, "historic_leaks", historic_leaks)
    distribution_pipes = assign_native_crs(session, "distribution_pipes", distribution_pipes)
    service_pipes = assign_native_crs(session, "service_pipes", service_pipes)
    historic_leaks = to_analysis_crs(session, "historic_leaks", historic_leaks)
    distribution_pipes = to_analysis_crs(session, "distribution_pipes", distribution_pipes)
    service_pipes = to_analysis_crs(session, "service_pipes", service_pipes)
    analysis_crs = get_analysis_crs(session)
    print("Analysis CRS for all spatial matching: {0}".format(analysis_crs.name), flush=True)
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
MODIFIED_FIELD_CANDIDATES = ["LASTUPDATE", "lastupdate", "LastUpdate", "last_edited_date", "EditDate", "UPDATEDATE", "MODIFIEDDATE"]
LEAK_KEY_CANDIDATES = ["LMSLEAKNUMBER", "LMSLEAKSUMBER", "LEAKNUMBER", "LeakNumber", "Name", "name"]
PIPE_DIAMETER_CANDIDATES = ["nominaldiameter", "NOMINALDIAMETER", "NominalDiameter", "diameter", "DIAMETER", "outsidediameter", "OUTSIDEDIAMETER"]
PIPE_MATERIAL_CANDIDATES = ["assettype_material", "ASSETTYPE_MATERIAL", "AssetType_Material", "material", "MATERIAL", "ASSETTYPE", "assettype"]
PIPE_PRESSURE_CANDIDATES = ["operatingpressure", "OPERATINGPRESSURE", "OperatingPressure", "maopdesign", "MAOPDESIGN", "MAOPDesign", "pressure", "PRESSURE", "op_pressure", "OP_PRESSURE"]
SUPP_KEY_CANDIDATES = ["LeakNumber", "Name", "LMSLEAKNUMBER", "LEAKNUMBER"]
SUPP_DIAMETER_CANDIDATES = ["Diameter", "Abdn_Diameter_Main", "Abdn_Diameter_Service"]
SUPP_MATERIAL_CANDIDATES = ["LeakMaterialType", "Abdn_Material", "Adbn_Material_Main", "Abdn_Material_Service"]
SUPP_PRESSURE_CANDIDATES = ["Pressure", "OperatingPressure", "MAOP", "MAOPDesign", "op_pressure"]
SUPP_FACILITY_CANDIDATES = ["FacilityType", "PipeType"]
GLOBALID_CANDIDATES = ["GlobalID", "GLOBALID", "globalid"]
OBJECTID_CANDIDATES = ["OBJECTID", "ObjectID", "objectid", "FID", "OID"]
WORKER_TREES = None

def log(text):
    print(str(text), flush=True)

def step(text):
    log("\n--- {0} ---".format(text))

def warn(text):
    log("WARNING: {0}".format(text))

def fail(text):
    raise RuntimeError(str(text))

def verbose(text):
    if VERBOSE_PER_LEAK:
        log(text)

def resolved_field(columns, candidates, required=False, label="field"):
    available = list(columns)
    resolved = resolve_field_name(available, candidates)
    if resolved is not None:
        log("Resolved {0}: {1}".format(label, resolved))
        return resolved
    if required:
        fail("Could not resolve required {0}. Candidates={1}. Available={2}".format(label, candidates, available))
    warn("Could not resolve optional {0}. Candidates={1}".format(label, candidates))
    return None

def ensure_output_folder():
    step("Checking output folder")
    if not os.path.isdir(OUTPUT_FOLDER):
        os.makedirs(OUTPUT_FOLDER)
        log("Created output folder: {0}".format(OUTPUT_FOLDER))
    else:
        log("Output folder exists: {0}".format(OUTPUT_FOLDER))

def make_session():
    try:
        import truststore
        truststore.inject_into_ssl()
        log("Injected Windows certificate store through truststore.")
    except Exception as ex:
        warn("truststore injection failed: {0}".format(ex))
    for proxy_var in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
        if proxy_var in os.environ:
            os.environ.pop(proxy_var, None)
            log("Removed runtime proxy environment variable: {0}".format(proxy_var))
    os.environ["NO_PROXY"] = "gis.nationalgrid.com,.nationalgrid.com,localhost,127.0.0.1"
    os.environ["no_proxy"] = os.environ["NO_PROXY"]
    session = requests.Session()
    session.trust_env = True
    session.headers.update({"User-Agent": "HistoricLeakRelocationGeoPandas/1.0"})
    return session







def keyring_get(name):
    try:
        return keyring.get_password(KEYRING_SERVICE, name)
    except Exception as ex:
        warn("Keyring read failed for {0}: {1}".format(name, ex))
        return None

def keyring_set(name, value):
    try:
        keyring.set_password(KEYRING_SERVICE, name, str(value))
    except Exception as ex:
        warn("Keyring write failed for {0}: {1}".format(name, ex))

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
        candidates.extend([
            os.path.join(local, "Microsoft", "Edge", "User Data", "Default", "History"),
            os.path.join(local, "Microsoft", "Edge", "User Data", "Profile 1", "History"),
            os.path.join(local, "Google", "Chrome", "User Data", "Default", "History"),
            os.path.join(local, "Google", "Chrome", "User Data", "Profile 1", "History")
        ])

        candidates.extend(glob.glob(os.path.join(local, "Microsoft", "Edge", "User Data", "Profile *", "History")))
        candidates.extend(glob.glob(os.path.join(local, "Google", "Chrome", "User Data", "Profile *", "History")))

    deduped = []

    for path in candidates:
        if path and path not in deduped and os.path.isfile(path):
            deduped.append(path)

    return deduped


def try_extract_oob_code_from_browser_history_since(start_epoch_seconds):
    min_chrome_time = chrome_time_from_epoch(start_epoch_seconds)

    for history_path in browser_history_paths():
        temp_path = os.path.join(tempfile.gettempdir(), "arcgis_oauth_history_{0}.sqlite".format(abs(hash(history_path))))

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
                    ("%gis.nationalgrid.com%oauth2/approval%code=%", min_chrome_time)
                ).fetchall()
            finally:
                conn.close()

            for url, _last_visit_time in rows:
                code = extract_oauth_code(url)

                if code:
                    log("Captured fresh ArcGIS OAuth approval code from browser history.")
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
    log("Could not automatically capture a fresh ArcGIS OAuth code from browser history.")
    log("Fallback: copy the approval URL or code from the browser, then press Enter here.")

    input("After copying the ArcGIS approval code or URL, press Enter: ")

    code = extract_oauth_code(get_clipboard_text())

    if code:
        log("Read ArcGIS authorization code from clipboard fallback.")
        return code

    return extract_oauth_code(input("Paste ArcGIS authorization code or approval URL: ").strip())


def interactive_access_token(session):
    cached = cached_access_token()

    if cached:
        return cached

    env_token = os.environ.get("GIS_AccessToken") or os.environ.get("GIS_ACCESS_TOKEN") or os.environ.get("ARCGIS_ACCESS_TOKEN") or os.environ.get("ARCGIS_TOKEN")

    if env_token and env_token.strip():
        log("Using ArcGIS access token from environment variable.")
        return env_token.strip()

    if not ARCGIS_CLIENT_ID:
        fail("ARCGIS_CLIENT_ID is not set.")

    authorize_params = {
        "client_id": ARCGIS_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": ARCGIS_REDIRECT_URI,
        "expiration": "20160"
    }

    authorize_url = PORTAL_AUTHORIZE_URL + "?" + urllib.parse.urlencode(authorize_params)

    log("Opening browser for ArcGIS user authentication.")
    log("Using OOB redirect. The script will silently capture the fresh approval code from browser history when possible.")
    log("Redirect URI: {0}".format(ARCGIS_REDIRECT_URI))

    auth_start_epoch = time.time()

    webbrowser.open(authorize_url, new=1, autoraise=True)

    auth_code = get_oob_authorization_code(auth_start_epoch)

    if not auth_code:
        fail("No authorization code was captured.")

    token_payload = {
        "f": "json",
        "client_id": ARCGIS_CLIENT_ID,
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": ARCGIS_REDIRECT_URI
    }

    response = session.post(
        PORTAL_TOKEN_URL,
        data=token_payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
        verify=VERIFY_SSL
    )

    response.raise_for_status()
    data = response.json()

    if "error" in data:
        fail("ArcGIS OAuth token request failed: {0}".format(json.dumps(data["error"], indent=2)))

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
        url,
        params=request_params,
        timeout=REQUEST_TIMEOUT_SECONDS,
        verify=VERIFY_SSL
    )

    response.raise_for_status()
    data = response.json()

    if "error" in data:
        error = data.get("error", {})
        code = str(error.get("code"))

        if code in ("498", "499"):
            warn("ArcGIS access token was rejected. Clearing cached token and forcing one fresh login.")
            clear_cached_access_token()
            if hasattr(session, "_arcgis_access_token"):
                delattr(session, "_arcgis_access_token")

            request_params["token"] = get_arcgis_token(session)
            session._arcgis_access_token = request_params["token"]

            response = session.get(
                url,
                params=request_params,
                timeout=REQUEST_TIMEOUT_SECONDS,
                verify=VERIFY_SSL
            )

            response.raise_for_status()
            data = response.json()

            if "error" in data:
                fail("ArcGIS REST error after refreshed access token from {0}: {1}".format(url, json.dumps(data.get("error", {}), indent=2)))
        else:
            fail("ArcGIS REST error from {0}: {1}".format(url, json.dumps(error, indent=2)))

    return data



def request_json_post(session, url, params):
    request_params = dict(params or {})

    token = request_params.get("token") or getattr(session, "_arcgis_access_token", None)

    if not token:
        token = get_arcgis_token(session)
        session._arcgis_access_token = token

    if token and "token" not in request_params:
        request_params["token"] = token

    response = session.post(
        url,
        data=request_params,
        timeout=REQUEST_TIMEOUT_SECONDS,
        verify=VERIFY_SSL
    )

    if response.status_code >= 400:
        snippet = response.text[:1000] if response.text else ""
        fail("ArcGIS POST failed for {0}. HTTP {1}. Response snippet: {2}".format(url, response.status_code, snippet))

    data = response.json()

    if "error" in data:
        error = data.get("error", {})
        code = str(error.get("code"))

        if code in ("498", "499"):
            warn("ArcGIS access token was rejected during POST. Clearing cached token and forcing one fresh login.")
            clear_cached_access_token()
            if hasattr(session, "_arcgis_access_token"):
                delattr(session, "_arcgis_access_token")
            request_params["token"] = get_arcgis_token(session)
            session._arcgis_access_token = request_params["token"]

            response = session.post(
                url,
                data=request_params,
                timeout=REQUEST_TIMEOUT_SECONDS,
                verify=VERIFY_SSL
            )

            if response.status_code >= 400:
                snippet = response.text[:1000] if response.text else ""
                fail("ArcGIS POST failed after refreshed token for {0}. HTTP {1}. Response snippet: {2}".format(url, response.status_code, snippet))

            data = response.json()

            if "error" in data:
                fail("ArcGIS REST POST error after refreshed access token from {0}: {1}".format(url, json.dumps(data.get("error", {}), indent=2)))
        else:
            fail("ArcGIS REST POST error from {0}: {1}".format(url, json.dumps(error, indent=2)))

    return data


def layer_metadata(session, layer_url):
    data = request_json(session, layer_url, {"f": "json"})
    fields = data.get("fields", [])
    object_id_field = data.get("objectIdField") or next((f.get("name") for f in fields if f.get("type") == "esriFieldTypeOID"), None)
    spatial_reference = data.get("extent", {}).get("spatialReference") or data.get("spatialReference") or {}
    wkid = spatial_reference.get("latestWkid") or spatial_reference.get("wkid")
    max_record_count = int(data.get("maxRecordCount") or REQUEST_PAGE_SIZE)
    page_size = min(REQUEST_PAGE_SIZE, max_record_count) if max_record_count > 0 else REQUEST_PAGE_SIZE
    return {"object_id_field": object_id_field, "wkid": wkid, "fields": fields, "page_size": page_size}

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
        log("Created layer cache folder: {0}".format(LAYER_CACHE_FOLDER))

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
        log("{0}: using delta modified field: {1}".format(layer_name, field_name))
    else:
        warn("{0}: no modified field found. Delta cache unavailable; full refresh will be used.".format(layer_name))
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
            ["jurisdiction", "Jurisdiction", "JURISDICTION"]
        ]
    else:
        candidate_groups = [
            PIPE_DIAMETER_CANDIDATES,
            PIPE_MATERIAL_CANDIDATES,
            PIPE_PRESSURE_CANDIDATES,
            GLOBALID_CANDIDATES,
            ["jurisdiction", "Jurisdiction", "JURISDICTION"]
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
        log("{0}: could not narrow outFields; using *".format(layer_name))
        return "*"
    out_fields = ",".join(deduped)
    # LR_FORCE_ASSETGROUP_ASSETTYPE_OUTFIELDS
    _lr_is_pipe_layer = False
    for _lr_local_name, _lr_local_value in list(locals().items()):
        try:
            _lr_text = str(_lr_local_value).lower()
        except Exception:
            _lr_text = ''
        if (
            'distribution pipes' in _lr_text
            or 'service pipes' in _lr_text
            or 'distribution pipe' in _lr_text
            or 'service pipe' in _lr_text
            or '/mapserver/6' in _lr_text
            or '/mapserver/7' in _lr_text
        ):
            _lr_is_pipe_layer = True
            break
    if _lr_is_pipe_layer:
        for _lr_local_name, _lr_local_value in list(locals().items()):
            if isinstance(_lr_local_value, list):
                _lr_lower_values = [str(_v).lower() for _v in _lr_local_value]
                if (
                    'nominaldiameter' in _lr_lower_values
                    or 'material' in _lr_lower_values
                    or 'operatingpressure' in _lr_lower_values
                ):
                    for _lr_required_field in ['ASSETGROUP', 'ASSETTYPE']:
                        if _lr_required_field not in _lr_local_value:
                            _lr_local_value.append(_lr_required_field)
                            print('Added required DNV pipe domain field to outFields: {0}'.format(_lr_required_field), flush=True)
            elif isinstance(_lr_local_value, str):
                _lr_lower_text = _lr_local_value.lower()
                if (
                    'nominaldiameter' in _lr_lower_text
                    and 'material' in _lr_lower_text
                    and 'assettype' not in _lr_lower_text
                ):
                    # Best effort for string-based outFields variables. In CPython, locals() assignment
                    # is not always reliable for fast locals, so list-based modification above is preferred.
                    locals()[_lr_local_name] = _lr_local_value + ',ASSETGROUP,ASSETTYPE'
    log("{0}: using outFields={1}".format(layer_name, out_fields))
    return out_fields

def query_count(session, layer_url, where_clause, layer_name):
    params = {
        "f": "json",
        "where": where_clause,
        "returnCountOnly": "true"
    }
    data = request_json(session, layer_url + "/query", params)
    count = data.get("count")
    if count is None:
        warn("{0}: returnCountOnly did not return count.".format(layer_name))
        return None
    log("{0}: server-side count for WHERE [{1}] = {2:,}".format(layer_name, where_clause, int(count)))
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
    if gdf is None or len(gdf) == 0 or not modified_field or modified_field not in gdf.columns:
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
        warn("Delta watermark was not numeric [{0}]. Falling back to full safe delta window.".format(epoch_ms))
        return safe_default
    try:
        if not math.isfinite(value):
            warn("Delta watermark was not finite [{0}]. Falling back to full safe delta window.".format(epoch_ms))
            return safe_default
    except Exception:
        pass
    if value <= 0:
        warn("Delta watermark was <= 0 [{0}]. Falling back to full safe delta window.".format(epoch_ms))
        return safe_default
    max_reasonable_epoch_ms = 32503680000000.0
    if value > max_reasonable_epoch_ms:
        warn("Delta watermark was outside valid millisecond range [{0}]. Falling back to full safe delta window.".format(epoch_ms))
        return safe_default
    try:
        when = dt.datetime.fromtimestamp(value / 1000.0, dt.UTC).replace(tzinfo=None)
    except Exception as ex:
        warn("Could not convert delta watermark [{0}] to timestamp: {1}. Falling back to full safe delta window.".format(epoch_ms, ex))
        return safe_default
    return "timestamp '{0}'".format(when.strftime("%Y-%m-%d %H:%M:%S"))



def build_delta_where(base_where, modified_field, last_epoch_ms):
    return "({0}) AND {1} > {2}".format(base_where, modified_field, epoch_ms_to_sql_timestamp(last_epoch_ms))

def read_layer_cache(layer_name, layer_url, where_clause):
    if not USE_LAYER_CACHE or FORCE_LAYER_REFRESH:
        return None, None
    data_path, meta_path = layer_cache_paths(layer_name)
    if not os.path.isfile(data_path) or not os.path.isfile(meta_path):
        log("{0}: no local cache found.".format(layer_name))
        return None, None
    try:
        with open(meta_path, "r", encoding="utf-8") as handle:
            meta = json.load(handle)
        if meta.get("layer_url") != layer_url:
            log("{0}: cache URL changed. Refreshing layer.".format(layer_name))
            return None, None
        if meta.get("where_clause") != where_clause:
            log("{0}: cache WHERE changed. Refreshing layer.".format(layer_name))
            return None, None
        gdf = pd.read_pickle(data_path, compression="gzip")
        log("{0}: loaded {1:,} records from local cache: {2}".format(layer_name, len(gdf), data_path))
        return gdf, meta
    except Exception as ex:
        warn("{0}: failed to read cache. Full refresh will be used. Error={1}".format(layer_name, ex))
        return None, None

def write_layer_cache(layer_name, layer_url, where_clause, server_count, modified_field, gdf):
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
            "max_modified_epoch_ms": int(max_modified_ms) if max_modified_ms is not None else None,
            "cached_utc": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
            "record_count_written": int(len(gdf))
        }
        with open(meta_path, "w", encoding="utf-8") as handle:
            json.dump(meta, handle, indent=2)
        log("{0}: wrote local cache: {1}".format(layer_name, data_path))
        log("{0}: max cached {1} = {2}".format(layer_name, modified_field, max_modified_ms))
    except Exception as ex:
        warn("{0}: failed to write cache. Error={1}".format(layer_name, ex))


def chunk_list(values, chunk_size):
    for index in range(0, len(values), chunk_size):
        yield values[index:index + chunk_size]

def query_object_ids(session, layer_url, where_clause, layer_name):
    log("{0}: requesting matching OBJECTIDs for WHERE [{1}]".format(layer_name, where_clause))

    params = {
        "f": "json",
        "where": where_clause,
        "returnIdsOnly": "true"
    }

    data = request_json(session, layer_url + "/query", params)

    object_ids = data.get("objectIds") or data.get("objectids") or []
    object_id_field = data.get("objectIdFieldName") or data.get("objectIdField") or None

    object_ids = sorted([int(value) for value in object_ids])

    log("{0}: returnIdsOnly returned {1:,} OBJECTIDs".format(layer_name, len(object_ids)))

    if object_id_field:
        log("{0}: returnIdsOnly objectIdFieldName={1}".format(layer_name, object_id_field))

    return object_ids

def fetch_objectid_batch(layer_url, object_id_batch, layer_name, meta, out_fields, batch_number, batch_total, token):
    local_session = requests.Session()
    local_session._arcgis_access_token = token

    params = {
        "f": "json",
        "objectIds": ",".join([str(value) for value in object_id_batch]),
        "outFields": out_fields,
        "returnGeometry": "true",
        "token": token
    }

    if meta["wkid"]:
        params["outSR"] = meta["wkid"]

    data = request_json_post(local_session, layer_url + "/query", params)
    batch = data.get("features", [])
    log("{0}: objectId batch {1:,}/{2:,} returned {3:,} features".format(layer_name, batch_number, batch_total, len(batch)))
    return batch



def query_feature_set(session, layer_url, where_clause, layer_name, meta, out_fields):
    source_crs = "EPSG:{0}".format(meta["wkid"]) if meta["wkid"] else None

    if USE_OBJECTID_BATCH_DOWNLOAD:
        object_ids = query_object_ids(session, layer_url, where_clause, layer_name)

        if not object_ids:
            log("{0}: no OBJECTIDs returned for WHERE. Returning empty GeoDataFrame.".format(layer_name))
            return gpd.GeoDataFrame([], geometry=[], crs=source_crs)

        active_token = getattr(session, "_arcgis_access_token", None)

        if not active_token:
            active_token = get_arcgis_token(session)
            session._arcgis_access_token = active_token

        batches = list(chunk_list(object_ids, OBJECTID_BATCH_SIZE))
        total_batches = len(batches)
        log("{0}: downloading {1:,} OBJECTIDs in {2:,} POST batches with {3} workers".format(layer_name, len(object_ids), total_batches, OBJECTID_DOWNLOAD_WORKERS))

        features = []

        with futures.ThreadPoolExecutor(max_workers=OBJECTID_DOWNLOAD_WORKERS) as executor:
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
                    active_token
                ): batch_number
                for batch_number, object_id_batch in enumerate(batches, start=1)
            }

            completed_batches = 0

            for future in futures.as_completed(future_lookup):
                batch_number = future_lookup[future]

                try:
                    features.extend(future.result())
                except Exception as ex:
                    fail("{0}: objectId POST batch {1:,}/{2:,} failed: {3}".format(layer_name, batch_number, total_batches, ex))

                completed_batches += 1

                if completed_batches == 1 or completed_batches % 10 == 0 or completed_batches == total_batches:
                    log("{0}: completed {1:,}/{2:,} batches; accumulated {3:,} features".format(layer_name, completed_batches, total_batches, len(features)))

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
                "orderByFields": meta["object_id_field"]
            }

            if meta["wkid"]:
                params["outSR"] = meta["wkid"]

            data = request_json(session, layer_url + "/query", params)
            batch = data.get("features", [])

            if not batch:
                break

            features.extend(batch)
            log("{0}: fetched {1:,} records".format(layer_name, len(features)))

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
    log("{0}: usable geometries = {1:,}".format(layer_name, len(gdf)))
    return gdf



def upsert_cached_layer(cached_gdf, delta_gdf, object_id_field):
    if delta_gdf is None or len(delta_gdf) == 0:
        return cached_gdf
    if object_id_field not in cached_gdf.columns or object_id_field not in delta_gdf.columns:
        warn("Cannot upsert delta because OBJECTID field is missing. Returning full delta only is unsafe, so keeping cached layer.")
        return cached_gdf
    delta_ids = set(delta_gdf[object_id_field].astype(str).tolist())
    keep_cached = cached_gdf[~cached_gdf[object_id_field].astype(str).isin(delta_ids)].copy()
    merged = pd.concat([keep_cached, delta_gdf], ignore_index=True)
    return gpd.GeoDataFrame(merged, geometry="geometry", crs=cached_gdf.crs or delta_gdf.crs)


def query_arcgis_layer(session, layer_url, where_clause, layer_name):
    step("Querying ArcGIS REST layer: {0}".format(layer_name))
    meta = layer_metadata(session, layer_url)
    if not meta["object_id_field"]:
        fail("Could not determine object id field for {0}".format(layer_name))

    object_id_field = meta["object_id_field"]
    modified_field = modified_field_from_meta(meta, layer_name)
    out_fields = build_out_fields(meta, layer_name)
    server_count = query_count(session, layer_url, where_clause, layer_name)

    cached_gdf, cached_meta = read_layer_cache(layer_name, layer_url, where_clause)

    if cached_gdf is not None and modified_field and cached_meta:
        last_epoch_ms = cached_meta.get("max_modified_epoch_ms")
        if last_epoch_ms:
            delta_where = build_delta_where(where_clause, modified_field, last_epoch_ms)
            log("{0}: attempting delta refresh WHERE [{1}]".format(layer_name, delta_where))
            delta_count = query_count(session, layer_url, delta_where, layer_name + " delta")
            if delta_count == 0:
                if server_count is None or len(cached_gdf) == int(server_count):
                    log("{0}: no delta changes found. Using local cache.".format(layer_name))
                    return cached_gdf
                warn("{0}: no modified deltas, but count differs cached={1:,}, server={2:,}. Full refresh required.".format(layer_name, len(cached_gdf), int(server_count)))
            else:
                delta_gdf = query_feature_set(session, layer_url, delta_where, layer_name + " delta", meta, out_fields)
                merged_gdf = upsert_cached_layer(cached_gdf, delta_gdf, object_id_field)
                if server_count is not None and len(merged_gdf) != int(server_count):
                    warn("{0}: merged cache count {1:,} does not equal server count {2:,}. This usually means deletes occurred; doing full refresh.".format(layer_name, len(merged_gdf), int(server_count)))
                else:
                    log("{0}: delta refresh merged {1:,} changed records into cache.".format(layer_name, len(delta_gdf)))
                    write_layer_cache(layer_name, layer_url, where_clause, server_count, modified_field, merged_gdf)
                    return merged_gdf

    elif cached_gdf is not None and (server_count is None or len(cached_gdf) == int(server_count)):
        log("{0}: using local cache by count because modified field metadata is unavailable.".format(layer_name))
        return cached_gdf

    log("{0}: performing full layer download.".format(layer_name))
    gdf = query_feature_set(session, layer_url, where_clause, layer_name, meta, out_fields)
    write_layer_cache(layer_name, layer_url, where_clause, server_count, modified_field, gdf)
    return gdf



def load_supplemental():
    step("Loading supplemental CSV")
    log("Supplemental CSV: {0}".format(SUPPLEMENTAL_CSV))
    if not os.path.isfile(SUPPLEMENTAL_CSV):
        fail("Missing supplemental CSV: {0}".format(SUPPLEMENTAL_CSV))
    df = pd.read_csv(SUPPLEMENTAL_CSV, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    log("Supplemental rows: {0:,}".format(len(df)))
    key_field = resolved_field(df.columns, SUPP_KEY_CANDIDATES, True, "supplemental leak key")
    diameter_field = resolved_field(df.columns, SUPP_DIAMETER_CANDIDATES, False, "supplemental diameter")
    material_field = resolved_field(df.columns, SUPP_MATERIAL_CANDIDATES, False, "supplemental material")
    pressure_field = resolved_field(df.columns, SUPP_PRESSURE_CANDIDATES, False, "supplemental pressure")
    facility_field = resolved_field(df.columns, SUPP_FACILITY_CANDIDATES, False, "supplemental facility type")
    records = {}
    skipped_no_key = 0
    for _, row in df.iterrows():
        leak_key = normalize_key(row.get(key_field))
        if not leak_key:
            skipped_no_key += 1
            continue
        records[leak_key] = {
            "diameter": parse_number(row.get(diameter_field)) if diameter_field else None,
            "material": clean(row.get(material_field)) if material_field else "",
            "pressure": clean(row.get(pressure_field)) if pressure_field else "",
            "facility": clean(row.get(facility_field)) if facility_field else ""
        }
    log("Supplemental records keyed: {0:,}".format(len(records)))
    log("Supplemental rows skipped missing key: {0:,}".format(skipped_no_key))
    if REQUIRE_PRESSURE_MATCH and not pressure_field:
        fail("REQUIRE_PRESSURE_MATCH is True but no supplemental pressure field was resolved.")
    if not pressure_field:
        warn("No supplemental pressure field was resolved. Pressure matching is currently not enforced.")
    return records

def to_target_crs(gdf, name):
    step("Projecting to analysis CRS: {0}".format(name))
    if gdf.crs is None:
        warn("{0} has no CRS from service metadata. Assuming EPSG:3857 before projecting.".format(name))
        gdf = gdf.set_crs("EPSG:3857")
    projected = gdf.to_crs(TARGET_CRS)
    log("{0}: CRS now {1}".format(name, projected.crs))
    return projected

def prepare_leaks(leaks_gdf, supplemental):
    step("Preparing leak records")
    leak_oid_field = resolved_field(leaks_gdf.columns, OBJECTID_CANDIDATES, True, "leak OBJECTID")
    leak_key_field = resolved_field(leaks_gdf.columns, LEAK_KEY_CANDIDATES, True, "leak key")
    leak_globalid_field = resolved_field(leaks_gdf.columns, GLOBALID_CANDIDATES, False, "leak GlobalID")
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
        prepared.append({
            "leak_oid": leak_oid,
            "leak_number": leak_number,
            "leak_globalid": clean(row.get(leak_globalid_field)) if leak_globalid_field else "",
            "leak_info": leak_info,
            "allowed_layers": route_layers(leak_info["facility"]),
            "geometry": row.geometry
        })
    log("Prepared leak match tasks: {0:,}".format(len(prepared)))
    for name in sorted(counters):
        log("{0}: {1:,}".format(name, counters[name]))
    return prepared, counters

def prepare_pipes(pipe_gdf, layer_name):
    step("Preparing pipe records: {0}".format(layer_name))
    pipe_oid_field = resolved_field(pipe_gdf.columns, OBJECTID_CANDIDATES, True, layer_name + " OBJECTID")
    material_field = resolved_field(pipe_gdf.columns, PIPE_MATERIAL_CANDIDATES, True, layer_name + " material")
    diameter_field = resolved_field(pipe_gdf.columns, PIPE_DIAMETER_CANDIDATES, True, layer_name + " diameter")
    pressure_field = resolved_field(pipe_gdf.columns, PIPE_PRESSURE_CANDIDATES, False, layer_name + " pressure")
    globalid_field = resolved_field(pipe_gdf.columns, GLOBALID_CANDIDATES, False, layer_name + " GlobalID")
    if REQUIRE_PRESSURE_MATCH and not pressure_field:
        fail("REQUIRE_PRESSURE_MATCH is True but no pressure field was resolved on {0}.".format(layer_name))
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
        records.append({
            "layer": layer_name,
            "pipe_oid": pipe_oid,
            "globalid": clean(row.get(globalid_field)) if globalid_field else "",
            "material": mat,
            "diameter": diam,
            "pressure": clean(row.get(pressure_field)) if pressure_field else "",
            "geometry": row.geometry
        })
    log("Prepared {0:,} {1} pipes".format(len(records), layer_name))
    log("{0} pipes missing material: {1:,}".format(layer_name, missing_material))
    log("{0} pipes missing diameter: {1:,}".format(layer_name, missing_diameter))
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
            candidates.append({
                "layer": layer_name,
                "pipe_oid": pipe["pipe_oid"],
                "distance_ft": distance_ft
            })
    if not candidates:
        return {"leak_oid": leak_oid, "matched": False, "reason": "No exact diameter/material/pressure match found within MAX_RADIUS_FT"}
    candidates.sort(key=lambda item: item["distance_ft"])
    best = candidates[0]
    verbose("Leak {0}: matched {1} pipe OID {2} at {3:.2f} ft".format(leak_number, best["layer"], best["pipe_oid"], best["distance_ft"]))
    return {
        "leak_oid": leak_oid,
        "matched": True,
        "layer": best["layer"],
        "pipe_oid": best["pipe_oid"],
        "distance_ft": best["distance_ft"],
        "matched_radius": matched_radius_from_distance(best["distance_ft"]),
        "reason": ""
    }

def run_matching(leak_tasks, pipe_sources):
    step("Running spatial + attribute matching")
    log("USE_MULTIPROCESSING: {0}".format(USE_MULTIPROCESSING))
    log("WORKERS: {0}".format(WORKERS))
    if not leak_tasks:
        return []
    if not USE_MULTIPROCESSING or WORKERS <= 1:
        init_worker(pipe_sources)
        results = []
        for index, task in enumerate(leak_tasks, start=1):
            if index == 1 or index % MESSAGE_EVERY_N_LEAKS == 0:
                log("Matched task progress: {0:,}/{1:,}".format(index, len(leak_tasks)))
            results.append(match_one_leak(task))
        return results
    results = []
    with futures.ProcessPoolExecutor(max_workers=WORKERS, initializer=init_worker, initargs=(pipe_sources,)) as executor:
        future_list = [executor.submit(match_one_leak, task) for task in leak_tasks]
        for index, future in enumerate(futures.as_completed(future_list), start=1):
            if index == 1 or index % MESSAGE_EVERY_N_LEAKS == 0:
                log("Matched task progress: {0:,}/{1:,}".format(index, len(future_list)))
            results.append(future.result())
    log("Matching complete. Results: {0:,}".format(len(results)))
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
        original_point = original_geometry if original_geometry.geom_type == "Point" else original_geometry.centroid
        if not result["matched"]:
            counters["unmatched"] += 1
            audit_rows.append({
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
                "RunUTC": run_utc
            })
            continue
        pipe = pipes[result["layer"]][result["pipe_oid"]]
        snap_pair = nearest_points(original_point, pipe["geometry"])
        snapped_point = snap_pair[1]
        guide_line = LineString([original_point, snapped_point])
        point_rows.append({
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
            "geometry": snapped_point
        })
        line_rows.append({
            "LeakOID": leak_oid,
            "LeakKey": leak_number,
            "LinkedLayer": result["layer"],
            "DistanceFt": result["distance_ft"],
            "MatchedPipeOID": result["pipe_oid"],
            "geometry": guide_line
        })
        audit_rows.append({
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
            "RunUTC": run_utc
        })
        counters["matched"] += 1
    if os.path.exists(OUTPUT_GPKG):
        os.remove(OUTPUT_GPKG)
        log("Deleted old GeoPackage: {0}".format(OUTPUT_GPKG))
    relocated_gdf = gpd.GeoDataFrame(point_rows, geometry="geometry", crs=TARGET_CRS)
    lines_gdf = gpd.GeoDataFrame(line_rows, geometry="geometry", crs=TARGET_CRS)
    audit_df = pd.DataFrame(audit_rows)
    if len(audit_df):
        audit_gdf = gpd.GeoDataFrame(audit_df, geometry=gpd.points_from_xy(audit_df["OrigX"], audit_df["OrigY"], crs=TARGET_CRS))
    else:
        audit_gdf = gpd.GeoDataFrame(audit_df, geometry=[], crs=TARGET_CRS)
    relocated_gdf.to_file(OUTPUT_GPKG, layer="relocated_leaks", driver="GPKG")
    lines_gdf.to_file(OUTPUT_GPKG, layer="relocated_leak_offset_lines", driver="GPKG")
    audit_gdf.to_file(OUTPUT_GPKG, layer="leak_relocation_audit", driver="GPKG")
    log("Wrote relocated_leaks: {0:,}".format(len(relocated_gdf)))
    log("Wrote relocated_leak_offset_lines: {0:,}".format(len(lines_gdf)))
    log("Wrote leak_relocation_audit: {0:,}".format(len(audit_gdf)))
    log("Output GeoPackage: {0}".format(OUTPUT_GPKG))
    return counters

def main():
    step("Starting non-ArcPy GeoPandas leak relocation")
    log("Supplemental CSV: {0}".format(SUPPLEMENTAL_CSV))
    log("Output GeoPackage: {0}".format(OUTPUT_GPKG))
    log("Target CRS: {0}".format(TARGET_CRS))
    log("MA layer filter: {0}".format(WHERE_MA))
    log("Max radius ft: {0}".format(MAX_RADIUS_FT))
    log("Require pressure match: {0}".format(REQUIRE_PRESSURE_MATCH))
    log("Material family fallback: {0}".format(ALLOW_MATERIAL_FAMILY_FALLBACK))
    ensure_output_folder()
    supplemental = load_supplemental()
    session = make_session()
    leaks = query_arcgis_layer(session, HIST_LEAK_URL, WHERE_MA, "historic leaks")
    distribution = query_arcgis_layer(session, DISTRIBUTION_PIPE_URL, WHERE_MA, "distribution pipes")
    service = query_arcgis_layer(session, SERVICE_PIPE_URL, WHERE_MA, "service pipes")
    leaks, distribution, service, ANALYSIS_CRS = normalize_loaded_layers_to_analysis_crs(
        session,
        leaks,
        distribution,
        service
    )
    globals()["TARGET_CRS"] = ANALYSIS_CRS
    print("CRS sanity check after normalization:", flush=True)
    print("historic leaks CRS = {0}".format(leaks.crs), flush=True)
    print("distribution pipes CRS = {0}".format(distribution.crs), flush=True)
    print("service pipes CRS = {0}".format(service.crs), flush=True)
    print("historic leaks bounds = {0}".format(list(leaks.total_bounds)), flush=True)
    print("distribution pipes bounds = {0}".format(list(distribution.total_bounds)), flush=True)
    print("service pipes bounds = {0}".format(list(service.total_bounds)), flush=True)
    leak_tasks, initial_counters = prepare_leaks(leaks, supplemental)
    pipe_sources = {
        "distribution": prepare_pipes(distribution, "distribution"),
        "service": prepare_pipes(service, "service")
    }
    match_results = run_matching(leak_tasks, pipe_sources)
    counters = write_outputs(leak_tasks, match_results, pipe_sources, initial_counters)
    step("Finished")
    for name in sorted(counters):
        log("{0}: {1:,}".format(name, counters[name]))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(traceback.format_exc(), flush=True)
        sys.exit(1)


