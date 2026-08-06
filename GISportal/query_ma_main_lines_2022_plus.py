# ruff: noqa: BLE001, S110
"""Query MA main lines installed or created on/after 2022-01-01.

Uses GISportal.auth for the ArcGIS Portal token, then queries:
https://gis.nationalgrid.com/arcgis/rest/services/MA/Material_View_MA/MapServer/341

Default outputs:
- outputs/ma_main_lines_2022_plus.csv
- outputs/ma_main_lines_2022_plus.geojson

By default the script requests all layer attributes. Use --curated-fields to limit
the export to a smaller practical attribute set.
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


_PACKAGE_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PACKAGE_PARENT not in sys.path:
    sys.path.insert(0, _PACKAGE_PARENT)

from GISportal import auth
from GISportal.output import fail, log, warn

LAYER_URL = "https://gis.nationalgrid.com/arcgis/rest/services/MA/Material_View_MA/MapServer/341"
DEFAULT_SINCE_DATE = "2022-01-01"
DEFAULT_OUT_FIELDS = [
    "OBJECTID",
    "GLOBALID",
    "assetid",
    "facilityid",
    "workorderid",
    "projectnumber",
    "nominaldiameter",
    "outsidediameter",
    "material",
    "operatingpressure",
    "maopdesign",
    "measuredlength",
    "installationdate",
    "CREATIONDATE",
    "LASTUPDATE",
    "UPDATEDBY",
    "CREATOR",
    "lifecyclestatus",
    "jurisdiction",
    "ng_formattedaddress",
    "citycode",
    "PressureSubnetworkName",
    "SystemSubnetworkName",
]
DATE_FIELDS = {"installationdate", "CREATIONDATE", "LASTUPDATE", "inservicedate", "manufacturedate"}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Export MA main lines installed or created since a date.")
    parser.add_argument("--since", default=DEFAULT_SINCE_DATE, help="Inclusive start date, YYYY-MM-DD. Default: 2022-01-01.")
    parser.add_argument("--layer-url", default=LAYER_URL, help="ArcGIS REST layer URL. Default is MA Material View main lines layer 341.")
    parser.add_argument("--out-dir", default="outputs", help="Output folder. Default: outputs under current working directory.")
    parser.add_argument("--basename", default="ma_main_lines_2022_plus", help="Output file basename without extension.")
    parser.add_argument("--curated-fields", action="store_true", help="Request only the curated practical field list instead of all attributes.")
    parser.add_argument("--no-geojson", action="store_true", help="Skip GeoJSON output and only write CSV attributes.")
    parser.add_argument("--chunk-size", type=int, default=500, help="ObjectID query chunk size. Default: 500.")
    return parser.parse_args(argv)


def request_json(session, url, params, timeout=120):
    response = session.get(url, params=params, timeout=timeout, verify=True)
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise RuntimeError(json.dumps(data["error"], indent=2))
    return data


def build_where_candidates(since_date):
    stamp = f"{since_date} 00:00:00"
    return [
        f"(installationdate >= TIMESTAMP '{stamp}' OR CREATIONDATE >= TIMESTAMP '{stamp}')",
        f"(installationdate >= DATE '{since_date}' OR CREATIONDATE >= DATE '{since_date}')",
        f"(installationdate >= timestamp '{stamp}' OR CREATIONDATE >= timestamp '{stamp}')",
        f"(installationdate >= date '{since_date}' OR CREATIONDATE >= date '{since_date}')",
    ]


def choose_working_where(session, layer_url, token, since_date):
    for where in build_where_candidates(since_date):
        params = {
            "f": "json",
            "where": where,
            "returnCountOnly": "true",
            "token": token,
        }
        try:
            data = request_json(session, f"{layer_url.rstrip('/')}/query", params)
            count = int(data.get("count") or 0)
            log(f"Using WHERE clause: {where}")
            log(f"Matching feature count: {count:,}")
            return where, count
        except Exception as ex:
            warn(f"WHERE clause failed, trying next date syntax: {where} -> {ex}")
    fail("No supported date WHERE syntax worked against the ArcGIS layer.")


def get_layer_metadata(session, layer_url, token):
    params = {"f": "json", "token": token}
    return request_json(session, layer_url.rstrip("/"), params)


def get_object_ids(session, layer_url, token, where):
    params = {
        "f": "json",
        "where": where,
        "returnIdsOnly": "true",
        "token": token,
    }
    data = request_json(session, f"{layer_url.rstrip('/')}/query", params)
    object_ids = sorted(int(v) for v in data.get("objectIds") or [])
    log(f"Returned ObjectIDs: {len(object_ids):,}")
    return object_ids


def chunked(values, size):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def query_features_by_ids(session, layer_url, token, object_ids, out_fields, chunk_size, include_geometry):
    rows = []
    total = len(object_ids)
    query_url = f"{layer_url.rstrip('/')}/query"
    for batch_number, batch_ids in enumerate(chunked(object_ids, chunk_size), start=1):
        params = {
            "f": "json",
            "objectIds": ",".join(str(v) for v in batch_ids),
            "outFields": out_fields,
            "returnGeometry": "true" if include_geometry else "false",
            "outSR": "4326",
            "returnTrueCurves": "false",
            "token": token,
        }
        data = request_json(session, query_url, params)
        features = data.get("features") or []
        rows.extend(features)
        log(f"Fetched batch {batch_number:,}: {len(rows):,}/{total:,}")
    return rows


def arcgis_date_to_iso(value):
    if value in (None, ""):
        return ""
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return value


def get_date_field_names(metadata):
    date_fields = set(DATE_FIELDS)
    for field in metadata.get("fields") or []:
        if str(field.get("type") or "").lower() == "esrifieldtypedate":
            name = field.get("name")
            if name:
                date_fields.add(name)
    return date_fields

def normalize_attributes(attributes, date_field_names):
    lowered_date_fields = {str(v).lower() for v in date_field_names}
    normalized = {}
    for key, value in (attributes or {}).items():
        if str(key).lower() in lowered_date_fields:
            normalized[key] = arcgis_date_to_iso(value)
        else:
            normalized[key] = value
    return normalized


def collect_fieldnames(features):
    fieldnames = []
    for feature in features:
        for key in (feature.get("attributes") or {}).keys():
            if key not in fieldnames:
                fieldnames.append(key)
    return fieldnames


def write_csv(features, path, date_field_names):
    fieldnames = collect_fieldnames(features)
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for feature in features:
            writer.writerow(normalize_attributes(feature.get("attributes") or {}, date_field_names))
    log(f"Wrote CSV: {path}")


def arcgis_line_geometry_to_geojson(geometry):
    if not geometry:
        return None
    paths = geometry.get("paths") or []
    if not paths:
        return None
    if len(paths) == 1:
        return {"type": "LineString", "coordinates": paths[0]}
    return {"type": "MultiLineString", "coordinates": paths}


def write_geojson(features, path, date_field_names):
    output_features = []
    for feature in features:
        geometry = arcgis_line_geometry_to_geojson(feature.get("geometry"))
        if not geometry:
            continue
        output_features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": normalize_attributes(feature.get("attributes") or {}, date_field_names),
            }
        )
    payload = {"type": "FeatureCollection", "features": output_features}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    log(f"Wrote GeoJSON: {path}")


def main(argv=None):
    args = parse_args(argv)
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{args.basename}.csv"
    geojson_path = out_dir / f"{args.basename}.geojson"
    session = auth.make_session()
    token = auth.get_arcgis_token(session)
    metadata = get_layer_metadata(session, args.layer_url, token)
    log(f"Layer: {metadata.get('name') or metadata.get('id') or args.layer_url}")
    where, expected_count = choose_working_where(session, args.layer_url, token, args.since)
    object_ids = get_object_ids(session, args.layer_url, token, where)
    if expected_count != len(object_ids):
        warn(f"Count query returned {expected_count:,}, but returnIdsOnly returned {len(object_ids):,} IDs.")
    if not object_ids:
        warn("No matching main lines found. No files written.")
        return 0
    date_field_names = get_date_field_names(metadata)
    out_fields = ",".join(DEFAULT_OUT_FIELDS) if args.curated_fields else "*"
    features = query_features_by_ids(
        session=session,
        layer_url=args.layer_url,
        token=token,
        object_ids=object_ids,
        out_fields=out_fields,
        chunk_size=max(1, args.chunk_size),
        include_geometry=not args.no_geojson,
    )
    write_csv(features, csv_path, date_field_names)
    if not args.no_geojson:
        write_geojson(features, geojson_path, date_field_names)
    log(f"Done. Exported {len(features):,} features.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
