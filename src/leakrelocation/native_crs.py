"""The coordinate system each layer is really in, and the one matching uses.

Each source layer gets its native CRS from the metadata of the MapServer layer
being queried - the DNV layers report a custom equidistant conic in US feet
that no EPSG code describes - and everything is then projected to the
distribution pipes' CRS for matching. Nothing is forced to a state plane first.
"""
import os
import re
import sqlite3
import sys

from pyproj import CRS

# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member. See leakrelocation/output.py for the full note.
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PACKAGE_PARENT not in sys.path:
    sys.path.insert(0, _PACKAGE_PARENT)

from leakrelocation import arcgis, config

# layer key -> the config setting that holds the layer's URL. Looked up when
# the CRS is asked for, so the URL is the one this run is configured with.
LAYER_URL_SETTINGS = {
    "historic_leaks": "HIST_LEAK_URL",
    "distribution_pipes": "DISTRIBUTION_PIPE_URL",
    "service_pipes": "SERVICE_PIPE_URL",
    # On the MA service, so its native spatial reference is read from its own
    # metadata like every other layer rather than assumed to match the DNV ones.
    "retired_pipes": "RETIRED_PIPE_URL",
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
    if layer_key not in LAYER_URL_SETTINGS:
        raise KeyError(f"Unknown layer_key for native CRS lookup: {layer_key}")
    setting = LAYER_URL_SETTINGS[layer_key]
    layer_url = getattr(config, setting, None)
    if not layer_url:
        raise RuntimeError(f"No layer URL is configured in {setting} for {layer_key}")
    layer_json = arcgis.request_json(session, layer_url, {"f": "json"})
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


def leaks_to_analysis_crs(session, leaks):
    """Leaks that carry their own CRS, projected into the analysis CRS.

    For a leak set that did not come from a MapServer layer. The Maximo leaks
    are read from SQL and already say what their coordinates are in; labelling
    them with layer 206's CRS instead would turn a longitude into an easting in
    feet and put every leak hundreds of miles from the nearest pipe.
    """
    if leaks is None or len(leaks) == 0:
        return leaks
    if leaks.crs is None:
        raise RuntimeError(
            "These leaks have no coordinate system, so their coordinates "
            "cannot be compared with the pipes'. Leaks that do not come from "
            "the leak layer have to say what CRS they are in."
        )
    analysis = get_analysis_crs(session)
    if leaks.crs != analysis:
        print(
            f"leaks: projecting from [{leaks.crs}] to analysis CRS [{analysis.name}]",
            flush=True,
        )
        return leaks.to_crs(analysis)
    print(f"leaks: already in analysis CRS [{analysis.name}]", flush=True)
    return leaks


def normalize_loaded_layers_to_analysis_crs(
    session, leaks, distribution_pipes, service_pipes, leak_layer_key="historic_leaks"
):
    """Every frame in the analysis CRS, ready to be compared.

    `leak_layer_key` names the layer the leaks were downloaded from, whose
    native CRS they are then in. Pass None for leaks from anywhere else: they
    are projected from the CRS they arrived with.
    """
    if leak_layer_key:
        leaks = assign_native_crs(session, leak_layer_key, leaks)
    distribution_pipes = assign_native_crs(
        session, "distribution_pipes", distribution_pipes
    )
    service_pipes = assign_native_crs(session, "service_pipes", service_pipes)
    if leak_layer_key:
        leaks = to_analysis_crs(session, leak_layer_key, leaks)
    else:
        leaks = leaks_to_analysis_crs(session, leaks)
    distribution_pipes = to_analysis_crs(
        session, "distribution_pipes", distribution_pipes
    )
    service_pipes = to_analysis_crs(session, "service_pipes", service_pipes)
    analysis_crs = get_analysis_crs(session)
    print(f"Analysis CRS for all spatial matching: {analysis_crs.name}", flush=True)
    return leaks, distribution_pipes, service_pipes, analysis_crs


# --- GeoPackages that ArcGIS can open ----------------------------------------
#
# The DNV layers publish their custom equidistant conic as Esri WKT. GDAL writes
# it into a GeoPackage as GDAL WKT1 - latitude_of_center, longitude_of_center,
# AXIS[...] - and ArcGIS Pro rejects that outright: "The coordinate system
# description is invalid", and the layer will not add. Rewriting the stored
# definition as Esri WKT fixes Pro, and GDAL and pyproj read Esri WKT back to the
# same CRS, so nothing in this project notices the difference.

# pyproj's WKT1_ESRI keeps PROJ's name for the US foot; Esri's is Foot_US.
_ESRI_UNIT_NAMES = {"US survey foot": ("Foot_US", "0.3048006096012192")}


def esri_wkt(crs):
    """`crs` as Esri WKT, in the form ArcGIS Pro accepts."""
    wkt = CRS(crs).to_wkt("WKT1_ESRI")
    for proj_name, (esri_name, factor) in _ESRI_UNIT_NAMES.items():
        wkt = re.sub(rf'UNIT\["{re.escape(proj_name)}",[^\]]*\]',
                     f'UNIT["{esri_name}",{factor}]', wkt)
    return wkt


def make_gpkg_crs_readable_by_arcgis(path):
    """Rewrite the GeoPackage's custom coordinate systems as Esri WKT.

    Only the records with no EPSG code are touched: ArcGIS reads those by code.
    Returns how many records were rewritten.
    """
    connection = sqlite3.connect(str(path))
    try:
        rows = connection.execute(
            "SELECT srs_id, definition FROM gpkg_spatial_ref_sys "
            "WHERE upper(organization) <> 'EPSG' AND srs_id > 0 "
            "AND lower(definition) <> 'undefined'").fetchall()
        rewritten = 0
        for srs_id, definition in rows:
            replacement = esri_wkt(CRS.from_wkt(definition))
            if replacement != definition:
                connection.execute(
                    "UPDATE gpkg_spatial_ref_sys SET definition = ? "
                    "WHERE srs_id = ?", (replacement, srs_id))
                rewritten += 1
        connection.commit()
    finally:
        connection.close()
    return rewritten
