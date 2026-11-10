"""Requests to the ArcGIS REST layers, and their replies as GeoDataFrames.

Everything that talks to a MapServer layer goes through `request_json` or
`request_json_post`: they attach the token, retry what is worth retrying and
turn an ArcGIS error body into an exception. `query_feature_set` downloads the
rows matching a where clause, in ObjectID batches across worker threads.
"""
import json
import os
import random
import sys
import threading
import time
from concurrent import futures

import geopandas as gpd
import requests
from shapely.errors import ShapelyError
from shapely.geometry import LineString, MultiLineString, Point, shape

# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member. See leakrelocation/output.py for the full note.
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PACKAGE_PARENT not in sys.path:
    sys.path.insert(0, _PACKAGE_PARENT)

from leakrelocation import auth, config
from leakrelocation.output import fail, log, warn

REQUEST_PAGE_SIZE = config.REQUEST_PAGE_SIZE

USE_OBJECTID_BATCH_DOWNLOAD = True
OBJECTID_BATCH_SIZE = config.OBJECTID_BATCH_SIZE
OBJECTID_DOWNLOAD_WORKERS = config.OBJECTID_DOWNLOAD_WORKERS
REQUEST_TIMEOUT_SECONDS = config.REQUEST_TIMEOUT_SECONDS
REQUEST_RETRY_ATTEMPTS = config.REQUEST_RETRY_ATTEMPTS
REQUEST_RETRY_BACKOFF_SECONDS = config.REQUEST_RETRY_BACKOFF_SECONDS
VERIFY_SSL = config.VERIFY_SSL


def is_pipe_layer_url(url):
    """True when this URL addresses one of the DNV pipe layers.

    The layer id is the path segment straight after MapServer, so it is read
    from there rather than searched for. Testing for "/mapserver/6" as a
    substring also matched layers 600, 601, 602 and 603, which exist on this
    service (SOP_polygon, LeakRateArea and SOP_Polygon_Reference) - a query
    against one of those would have had ASSETGROUP and ASSETTYPE appended to its
    outFields, and the service rejects a query naming fields the layer does not
    have. The URL may address the layer itself or an operation on it
    (".../MapServer/6" or ".../MapServer/6/query"), so the tail varies.
    """
    segments = [segment for segment in str(url).split("?")[0].split("/") if segment]
    lowered = [segment.lower() for segment in segments]
    if "mapserver" not in lowered:
        return False
    index = lowered.index("mapserver") + 1
    if index >= len(segments) or not segments[index].isdigit():
        return False
    return int(segments[index]) in config.PIPE_LAYER_IDS


def apply_pipe_domain_out_fields(url, params):
    """Ensure DNV pipe-layer queries also return ASSETGROUP and ASSETTYPE.

    ASSETTYPE is the pipe material and ASSETGROUP selects the subtype domain
    that names it, so no pipe query that asks for attributes is of any use
    without them. build_out_fields now requests both outright; this stays as the
    backstop for any other code path that builds an outFields list.

    Returns `params` unchanged unless this is a pipe-layer query whose
    outFields requests attributes but omits ASSETTYPE.
    """
    if not isinstance(params, dict):
        return params

    if not is_pipe_layer_url(url):
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
    # Deliberately does not look for "material": that field is not requested
    # any more, and a request built without it still needs the domain fields.
    wants_attributes = (
        "nominaldiameter" in lowered
        or "operatingpressure" in lowered
    )
    if not wants_attributes or "assettype" in lowered:
        return params

    updated = dict(params)
    updated[out_field_key] = out_fields + ",ASSETGROUP,ASSETTYPE"
    log("Appended DNV pipe domain fields to outFields: ASSETGROUP,ASSETTYPE")
    return updated


# One requests.Session per worker thread, reused across batches.
#
# A fresh Session per batch meant a fresh TLS handshake per batch: 588 of them for
# one layer, eight at a time, through a proxy that re-signs every one. Reusing the
# connection is faster and far gentler on whatever was resetting them.
_WORKER_STATE = threading.local()


def worker_session(token):
    """The calling thread's session, created on first use."""
    session = getattr(_WORKER_STATE, "session", None)
    if session is None:
        session = requests.Session()
        _WORKER_STATE.session = session
    session._arcgis_access_token = token
    return session


# --- Transient network failures ----------------------------------------------
#
# A download of the pipe layers is hundreds of requests over many minutes, through
# a corporate proxy. One of them being dropped is normal; it used to be fatal. A
# run died on "objectId POST batch 20/322 failed: ConnectionResetError(10054, 'An
# existing connection was forcibly closed by the remote host')" with another layer
# 584 batches of 588 through, and every one of those batches was thrown away.
#
# These are the failures worth trying again. requests wraps the socket-level ones
# - urllib3's ProtocolError, Python's ConnectionResetError, WinError 10054 - in
# ConnectionError, so catching that covers a reset, a dropped connection and a
# refused one.
TRANSIENT_EXCEPTIONS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)

# Statuses that mean "not now" rather than "not ever". 500 is deliberately absent:
# ArcGIS returns a genuine query error that way, and repeating a bad query just
# makes it fail more slowly. 429 and the gateway codes are what a proxy or a busy
# service returns under load.
TRANSIENT_STATUSES = (429, 502, 503, 504)

# ArcGIS returns these codes when a token is rejected. 498 is invalid, 499 is
# missing or expired. Either way the cached token is dropped and one fresh
# sign-in is tried before the request is given up on.
TOKEN_REJECTED_CODES = ("498", "499")


def send_with_retry(send, describe):
    """Run a request, retrying transient failures with backoff.

    `send` is called again from scratch each attempt, so it must build its own
    request - these are all read-only queries, so repeating one is safe.
    """
    last_error = None
    for attempt in range(1, REQUEST_RETRY_ATTEMPTS + 1):
        try:
            response = send()
            if response.status_code not in TRANSIENT_STATUSES:
                return response
            last_error = f"HTTP {response.status_code}"
        except TRANSIENT_EXCEPTIONS as ex:
            last_error = f"{type(ex).__name__}: {ex}"
            if attempt == REQUEST_RETRY_ATTEMPTS:
                raise
        if attempt == REQUEST_RETRY_ATTEMPTS:
            return response
        delay = REQUEST_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
        # Jitter, because the batches run eight at a time: without it a proxy
        # that drops several at once gets them all back in the same instant.
        delay += random.uniform(0, delay / 2)
        warn(f"{describe}: {last_error}. Attempt {attempt} of "
             f"{REQUEST_RETRY_ATTEMPTS}; retrying in {delay:.1f}s")
        time.sleep(delay)
    return response


def request_json(session, url, params=None):
    params = apply_pipe_domain_out_fields(url, params)
    request_params = dict(params or {})

    token = getattr(session, "_arcgis_access_token", None)
    if not token:
        token = auth.get_arcgis_token(session)
        session._arcgis_access_token = token

    if token and "token" not in request_params:
        request_params["token"] = token

    response = send_with_retry(
        lambda: session.get(
            url, params=request_params, timeout=REQUEST_TIMEOUT_SECONDS,
            verify=VERIFY_SSL,
        ),
        f"GET {url}",
    )

    response.raise_for_status()
    data = response.json()

    if "error" in data:
        error = data.get("error", {})
        code = str(error.get("code"))

        if code in TOKEN_REJECTED_CODES:
            warn(
                "ArcGIS access token was rejected. Clearing cached token and forcing one fresh login."
            )
            auth.clear_cached_access_token()
            if hasattr(session, "_arcgis_access_token"):
                delattr(session, "_arcgis_access_token")

            request_params["token"] = auth.get_arcgis_token(session)
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
        token = auth.get_arcgis_token(session)
        session._arcgis_access_token = token

    if token and "token" not in request_params:
        request_params["token"] = token

    response = send_with_retry(
        lambda: session.post(
            url, data=request_params, timeout=REQUEST_TIMEOUT_SECONDS,
            verify=VERIFY_SSL,
        ),
        f"POST {url}",
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

        if code in TOKEN_REJECTED_CODES:
            warn(
                "ArcGIS access token was rejected during POST. Clearing cached token and forcing one fresh login."
            )
            auth.clear_cached_access_token()
            if hasattr(session, "_arcgis_access_token"):
                delattr(session, "_arcgis_access_token")
            request_params["token"] = auth.get_arcgis_token(session)
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
        coords = [(float(x), float(y)) for x, y, *_ in path]
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
    except (AttributeError, KeyError, TypeError, ValueError, ShapelyError):
        # Everything a malformed geometry dict raises: a missing "coordinates"
        # is a KeyError, a non-dict an AttributeError, bad coordinate values a
        # TypeError or ValueError, and an unknown "type" a GeometryTypeError -
        # which descends from ShapelyError, not from TypeError, so it has to be
        # named.
        return None


def query_count(session, layer_url, where_clause, layer_name):
    params = {"f": "json", "where": where_clause, "returnCountOnly": "true"}
    data = request_json(session, layer_url + "/query", params)
    count = data.get("count")
    if count is None:
        warn(f"{layer_name}: returnCountOnly did not return count.")
        return None
    log(f"{layer_name}: server-side count for WHERE [{where_clause}] = {int(count):,}")
    return int(count)


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
    local_session = worker_session(token)

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
            active_token = auth.get_arcgis_token(session)
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

            for completed_batches, future in enumerate(
                futures.as_completed(future_lookup), start=1
            ):
                batch_number = future_lookup[future]

                try:
                    features.extend(future.result())
                except Exception as ex:  # noqa: BLE001 - re-raised from a worker
                    # future.result() re-raises whatever the download raised, so
                    # narrowing here would mean listing every failure mode of
                    # the request path. It is turned into one fatal message that
                    # names the batch.
                    #
                    # Reaching here means the batch already failed
                    # REQUEST_RETRY_ATTEMPTS times with backoff, so this is a
                    # network that is not coming back on its own.
                    fail(
                        f"{layer_name}: objectId POST batch {batch_number:,}/{total_batches:,} "
                        f"failed after {REQUEST_RETRY_ATTEMPTS} attempts: {ex}\n"
                        f"  If this keeps happening, fewer parallel requests often "
                        f"helps: set LEAKRELOCATION_DOWNLOAD_WORKERS=2 (currently "
                        f"{OBJECTID_DOWNLOAD_WORKERS}), or raise "
                        f"LEAKRELOCATION_RETRY_ATTEMPTS."
                    )

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
