"""The on-disk cache of each downloaded layer, and the download that fills it.

A layer is downloaded in full once and written to a GeoPackage beside a small
metadata file. Later runs ask the service only for rows modified since, and
merge them in. `query_arcgis_layer` is the one entry point: it decides between
the cache, a delta and a full download.
"""
import datetime as dt
import json
import os
import re
import sys

import geopandas as gpd
import pandas as pd

# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member. See leakrelocation/output.py for the full note.
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PACKAGE_PARENT not in sys.path:
    sys.path.insert(0, _PACKAGE_PARENT)

from leakrelocation import arcgis, config, dates, layer_fields
from leakrelocation.output import detail, fail, log, step, warn

USE_LAYER_CACHE = config.USE_LAYER_CACHE
LAYER_CACHE_FOLDER = str(config.LAYER_CACHE_DIR)
FORCE_LAYER_REFRESH = config.FORCE_LAYER_REFRESH


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


def missing_requested_columns(gdf, required_fields):
    """Which requested fields this frame does not have.

    Returns None when the answer is unknown - no field list, or a request for
    every field - so the caller refreshes rather than assuming.
    """
    if not required_fields or required_fields == "*":
        return None
    if isinstance(required_fields, str):
        wanted = [name.strip() for name in required_fields.split(",") if name.strip()]
    else:
        wanted = list(required_fields)
    present = {str(column).lower() for column in gdf.columns}
    return [name for name in wanted if name.lower() not in present]


def read_layer_cache(layer_name, layer_url, where_clause, required_fields=None):
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
        signature = layer_fields.out_field_request_signature(layer_name)
        stale_fields = meta.get("out_field_signature") != signature
        gdf = pd.read_pickle(data_path, compression="gzip")
        if stale_fields:
            # The signature says the request has changed, but the question that
            # actually matters is whether this cache already holds what is now
            # asked for. Checking the columns answers it exactly, and saves
            # re-downloading 1.27 million service pipes because the signature was
            # reorganised or a leak field was added.
            missing = missing_requested_columns(gdf, required_fields)
            if missing is None or missing:
                log(f"{layer_name}: the requested fields have changed since this "
                    f"cache was written"
                    + (f" and it has no {', '.join(missing)}" if missing else "")
                    + ". Refreshing the layer in full so the new fields are "
                    "populated for every record.")
                return None, None
            log(f"{layer_name}: the requested fields have changed, but this cache "
                f"already carries every one of them. Keeping it.")
        log(f"{layer_name}: loaded {len(gdf):,} records from local cache: {data_path}")
        return gdf, meta
    except Exception as ex:  # noqa: BLE001 - see below
        # Deliberately broad. Unpickling a cache written by a different pandas,
        # geopandas or Python raises whatever that payload's classes raise,
        # including ModuleNotFoundError and AttributeError, and a truncated file
        # raises EOFError or a gzip error. No cache is worth ending a run for:
        # the error is reported and the layer is refreshed from the service.
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
    max_modified_ms = dates.max_modified_epoch_ms(gdf, modified_field)
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
            "out_field_signature": layer_fields.out_field_request_signature(layer_name),
        }
        with open(meta_path, "w", encoding="utf-8") as handle:
            json.dump(meta, handle, indent=2)
        log(f"{layer_name}: wrote local cache: {data_path}")
        log(f"{layer_name}: max cached {modified_field} = {max_modified_ms}")
    except Exception as ex:  # noqa: BLE001 - the cache is an optimisation
        # Pickling a frame can fail on an unpicklable column, and the write
        # itself on a full disk or a permission problem. A run that has already
        # done the work should finish, just without leaving a cache behind.
        warn(f"{layer_name}: failed to write cache. Error={ex}")


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

    meta = arcgis.layer_metadata(session, layer_url)
    if not meta["object_id_field"]:
        fail(f"Could not determine object id field for {layer_name}")

    object_id_field = meta["object_id_field"]
    modified_field = layer_fields.modified_field_from_meta(meta, layer_name)
    out_fields = layer_fields.build_out_fields(meta, layer_name)
    server_count = arcgis.query_count(session, layer_url, where_clause, layer_name)

    cached_gdf, cached_meta = read_layer_cache(
        layer_name, layer_url, where_clause, out_fields)

    if cached_gdf is not None and modified_field and cached_meta:
        last_epoch_ms = cached_meta.get("max_modified_epoch_ms")
        if last_epoch_ms:
            delta_where = dates.build_delta_where(where_clause, modified_field, last_epoch_ms)
            log(f"{layer_name}: attempting delta refresh WHERE [{delta_where}]")
            delta_count = arcgis.query_count(
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
                delta_gdf = arcgis.query_feature_set(
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
    gdf = arcgis.query_feature_set(
        session, layer_url, where_clause, layer_name, meta, out_fields
    )
    write_layer_cache(
        layer_name, layer_url, where_clause, server_count, modified_field, gdf
    )
    return gdf
