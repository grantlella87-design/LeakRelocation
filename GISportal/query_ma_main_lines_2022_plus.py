"""Export MA main lines installed or created on/after a date.

    python GISportal/query_ma_main_lines_2022_plus.py
    python GISportal/query_ma_main_lines_2022_plus.py --since 2023-01-01
    python GISportal/query_ma_main_lines_2022_plus.py --count-only
    python GISportal/query_ma_main_lines_2022_plus.py --curated-fields

Writes <out-dir>/<basename>.csv and .geojson.

Authentication, configuration and console output come from the shared
`leakrelocation` package, so signing in once serves both projects.
"""
import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from _bootstrap import auth, config

from leakrelocation.output import fail, log, warn

# Built from the shared GIS host so the hostname is not spelled out twice.
DEFAULT_LAYER_URL = (
    f"{config.GIS_ROOT}/arcgis/rest/services/MA/Material_View_MA/MapServer/341"
)
DEFAULT_SINCE_DATE = "2022-01-01"

# "Installed or created" - the field actually used is whichever of these the
# layer has, confirmed against its metadata rather than assumed.
INSTALL_DATE_FIELDS = ("installationdate", "inservicedate")
CREATION_DATE_FIELDS = ("CREATIONDATE", "created_date")

CURATED_FIELDS = [
    "OBJECTID", "GLOBALID", "assetid", "facilityid", "workorderid",
    "projectnumber", "nominaldiameter", "outsidediameter", "material",
    "operatingpressure", "maopdesign", "measuredlength", "installationdate",
    "CREATIONDATE", "LASTUPDATE", "UPDATEDBY", "CREATOR", "lifecyclestatus",
    "jurisdiction", "ng_formattedaddress", "citycode",
    "PressureSubnetworkName", "SystemSubnetworkName",
]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", default=DEFAULT_SINCE_DATE,
                        help="Inclusive start date, YYYY-MM-DD. Default: 2022-01-01.")
    parser.add_argument("--layer-url", default=DEFAULT_LAYER_URL,
                        help="ArcGIS REST layer URL.")
    parser.add_argument("--out-dir", default="outputs", help="Output folder.")
    parser.add_argument("--basename", default="ma_main_lines_2022_plus",
                        help="Output file basename, without extension.")
    parser.add_argument("--curated-fields", action="store_true",
                        help="Request a curated attribute set instead of all fields.")
    parser.add_argument("--no-geojson", action="store_true",
                        help="Write only the CSV.")
    parser.add_argument("--count-only", action="store_true",
                        help="Report the matching count and exit, writing nothing.")
    parser.add_argument("--chunk-size", type=int, default=500,
                        help="ObjectIDs per query. Default: 500.")
    return parser.parse_args(argv)


def validate_since(value):
    try:
        datetime.strptime(value, "%Y-%m-%d")  # noqa: DTZ007 - a date, not an instant
    except ValueError:
        fail(f"--since must be YYYY-MM-DD, got {value!r}")
    return value


# --- Layer metadata ---------------------------------------------------------


def layer_metadata(session, layer_url):
    return auth.request_json(session, layer_url.rstrip("/"), {"f": "json"})


def field_names(metadata):
    return [f.get("name") for f in metadata.get("fields") or [] if f.get("name")]


def date_field_names(metadata):
    """Field names the layer reports as dates, so their epoch values get
    converted on export."""
    return {
        f["name"] for f in metadata.get("fields") or []
        if f.get("name") and str(f.get("type") or "").lower() == "esrifieldtypedate"
    }


def first_present(available, preferred):
    """Return the first preferred name the layer actually has, case-insensitively."""
    lookup = {str(name).lower(): name for name in available}
    for name in preferred:
        if name.lower() in lookup:
            return lookup[name.lower()]
    return None


def resolve_date_fields(metadata):
    """Find the install and creation date fields this layer really has.

    The layer metadata answers this, so it is looked up rather than guessed. A
    WHERE clause naming a field the layer does not have fails the whole query
    with a syntax error that says nothing useful about the cause.
    """
    available = field_names(metadata)
    install = first_present(available, INSTALL_DATE_FIELDS)
    creation = first_present(available, CREATION_DATE_FIELDS)

    if not install and not creation:
        fail(
            "The layer has neither an installation nor a creation date field. "
            f"Looked for {list(INSTALL_DATE_FIELDS + CREATION_DATE_FIELDS)}. "
            f"Date fields present: {sorted(date_field_names(metadata))}"
        )
    if not install:
        warn(f"No installation date field; filtering on {creation} alone.")
    if not creation:
        warn(f"No creation date field; filtering on {install} alone.")
    return install, creation


# --- Selecting rows ---------------------------------------------------------


def where_variants(install_field, creation_field, since_date):
    """WHERE clauses to try, most portable first.

    Only the date literal syntax varies - TIMESTAMP versus DATE is genuinely
    server-dependent. SQL keywords are case-insensitive, so upper and lower case
    spellings of the same keyword are the same clause and only one is tried.
    """
    fields = [f for f in (install_field, creation_field) if f]
    for literal in (f"TIMESTAMP '{since_date} 00:00:00'", f"DATE '{since_date}'"):
        yield " OR ".join(f"{field} >= {literal}" for field in fields)


def count_matching(session, layer_url, where):
    data = auth.request_json(session, f"{layer_url.rstrip('/')}/query", {
        "where": where, "returnCountOnly": "true",
    })
    return int(data.get("count") or 0)


def choose_where(session, layer_url, install_field, creation_field, since_date):
    """Pick a WHERE clause the server accepts, preferring one that matches rows.

    A clause the server accepts but which matches nothing used to be taken as
    success, so the run reported zero features and stopped without trying the
    other date syntax.
    """
    accepted_but_empty = None
    for where in where_variants(install_field, creation_field, since_date):
        try:
            count = count_matching(session, layer_url, where)
        except Exception as exc:  # noqa: BLE001 - try the next syntax
            warn(f"Server rejected this date syntax, trying the next: {exc}")
            continue
        if count > 0:
            log(f"WHERE: {where}")
            log(f"Matching features: {count:,}")
            return where, count
        log(f"Accepted but matched nothing: {where}")
        accepted_but_empty = accepted_but_empty or where

    if accepted_but_empty:
        log("No date syntax matched any rows; the layer may have nothing since "
            f"{since_date}.")
        return accepted_but_empty, 0

    fail("No supported date WHERE syntax worked against the layer.")
    return None, 0  # unreachable; fail() raises


def object_ids_matching(session, layer_url, where):
    data = auth.request_json(session, f"{layer_url.rstrip('/')}/query", {
        "where": where, "returnIdsOnly": "true",
    })
    ids = sorted({int(value) for value in data.get("objectIds") or []})
    log(f"ObjectIDs returned: {len(ids):,}")
    return ids


# --- Fetching ---------------------------------------------------------------


def resolve_out_fields(metadata, curated):
    """Which fields to request. Curated names are checked against the layer,
    because one missing name fails the entire query."""
    if not curated:
        return "*"
    available = field_names(metadata)
    lookup = {str(name).lower(): name for name in available}
    resolved = [lookup[name.lower()] for name in CURATED_FIELDS if name.lower() in lookup]
    missing = [name for name in CURATED_FIELDS if name.lower() not in lookup]
    if missing:
        warn(f"Curated fields not on this layer, skipped: {missing}")
    if not resolved:
        fail(f"No curated field exists on this layer. Available: {sorted(available)}")
    log(f"Requesting {len(resolved)} curated fields.")
    return ",".join(resolved)


def chunked(values, size):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def fetch_features(session, layer_url, object_ids, out_fields, chunk_size, with_geometry):
    query_url = f"{layer_url.rstrip('/')}/query"
    features = []
    total = len(object_ids)
    for number, batch in enumerate(chunked(object_ids, chunk_size), start=1):
        data = auth.request_json(session, query_url, {
            "objectIds": ",".join(str(v) for v in batch),
            "outFields": out_fields,
            "returnGeometry": "true" if with_geometry else "false",
            "outSR": "4326",
            "returnTrueCurves": "false",
        })
        features.extend(data.get("features") or [])
        log(f"Batch {number:,}: {len(features):,}/{total:,}")

    if len(features) != total:
        warn(f"Requested {total:,} features but received {len(features):,}.")
    return features


# --- Writing ----------------------------------------------------------------


def epoch_ms_to_iso(value):
    """ArcGIS dates arrive as epoch milliseconds."""
    if value in (None, ""):
        return ""
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return value


def normalize_attributes(attributes, date_fields):
    lowered = {str(name).lower() for name in date_fields}
    return {
        key: (epoch_ms_to_iso(value) if str(key).lower() in lowered else value)
        for key, value in (attributes or {}).items()
    }


def column_order(features):
    """Union of attribute names, in the order first seen."""
    ordered = []
    for feature in features:
        for key in feature.get("attributes") or {}:
            if key not in ordered:
                ordered.append(key)
    return ordered


def write_csv(features, path, date_fields):
    columns = column_order(features)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for feature in features:
            writer.writerow(normalize_attributes(feature.get("attributes"), date_fields))
    log(f"Wrote {len(features):,} rows: {path}")


def paths_to_geojson_geometry(geometry):
    paths = (geometry or {}).get("paths") or []
    if not paths:
        return None
    if len(paths) == 1:
        return {"type": "LineString", "coordinates": paths[0]}
    return {"type": "MultiLineString", "coordinates": paths}


def write_geojson(features, path, date_fields):
    written = []
    skipped = 0
    for feature in features:
        geometry = paths_to_geojson_geometry(feature.get("geometry"))
        if not geometry:
            skipped += 1
            continue
        written.append({
            "type": "Feature",
            "geometry": geometry,
            "properties": normalize_attributes(feature.get("attributes"), date_fields),
        })
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"type": "FeatureCollection", "features": written}, handle,
                  ensure_ascii=False)
    if skipped:
        warn(f"{skipped:,} features had no line geometry and are not in the GeoJSON.")
    log(f"Wrote {len(written):,} features: {path}")


# --- Entry point ------------------------------------------------------------


def main(argv=None):
    args = parse_args(argv)
    since = validate_since(args.since)

    session = auth.make_session()
    metadata = layer_metadata(session, args.layer_url)
    log(f"Layer: {metadata.get('name') or args.layer_url}")

    install_field, creation_field = resolve_date_fields(metadata)
    where, count = choose_where(session, args.layer_url, install_field,
                                creation_field, since)

    if args.count_only:
        log(f"{count:,} main lines since {since}.")
        return 0
    if not count:
        warn("Nothing to export.")
        return 0

    object_ids = object_ids_matching(session, args.layer_url, where)
    if not object_ids:
        warn(f"Count reported {count:,} but no ObjectIDs came back. Nothing written.")
        return 1
    if len(object_ids) != count:
        warn(f"Count reported {count:,}, ObjectIDs returned {len(object_ids):,}.")

    out_fields = resolve_out_fields(metadata, args.curated_fields)
    features = fetch_features(
        session, args.layer_url, object_ids, out_fields,
        max(1, args.chunk_size), with_geometry=not args.no_geojson,
    )
    if not features:
        warn("No features returned. Nothing written.")
        return 1

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    dates = date_field_names(metadata)

    write_csv(features, out_dir / f"{args.basename}.csv", dates)
    if not args.no_geojson:
        write_geojson(features, out_dir / f"{args.basename}.geojson", dates)

    log(f"Done. {len(features):,} features exported.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
