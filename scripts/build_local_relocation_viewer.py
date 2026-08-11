"""Build the local Leaflet map of the relocated leaks.

    python scripts/build_local_relocation_viewer.py
    python scripts/build_local_relocation_viewer.py --gpkg path/to/other.gpkg

Reads the GeoPackage the workflow writes and produces a viewer folder holding
index.html and the two GeoJSON layers.

Importable: run.py calls build() directly so one command can go from download to
map. Everything used to execute at import time, which made that impossible.
"""
import argparse
import shutil
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from _bootstrap import config

from leakrelocation.output import fail, log, warn
from leakrelocation.viewer_html import render as render_viewer

POINT_LAYER = "relocated_leaks"
LINE_LAYER = "relocated_leak_offset_lines"

LEAFLET_CDN = "https://unpkg.com/leaflet@1.9.4/dist"

PREFERRED_COLUMNS = [
    "OrigLeakOID", "LeakKey", "LinkedLayer", "MatchedPipeOID", "MatchedPipeGID",
    "DistanceFt", "SearchRadiusFt", "MatchMaterial", "MatchDiameter",
    "MatchPressure",
]
COLUMN_KEYWORDS = ["leak", "pipe", "material", "diam", "distance", "match",
                   "linked", "pressure"]
MAX_COLUMNS = 18


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gpkg", default=None,
                        help="GeoPackage to read. Default: the workflow's output.")
    parser.add_argument("--out-dir", default=None,
                        help="Viewer folder to write. Default: config.VIEWER_DIR.")
    return parser.parse_args(argv)


def resolve_gpkg(explicit=None):
    """Which GeoPackage to map.

    Defaults to the local output the workflow writes, falling back to the
    published copy on the share. It used to read the share unconditionally,
    which stopped being right when output moved local: a fresh run wrote locally
    and the map kept showing whatever had last been published.
    """
    if explicit:
        path = Path(explicit)
        if not path.exists():
            fail(f"GeoPackage not found: {path}")
        return path

    local = Path(config.OUTPUT_GPKG)
    if local.exists():
        return local

    published = Path(config.PUBLISHED_OUTPUT_GPKG)
    if published.exists():
        warn(f"No local GeoPackage at {local}; using the published copy at "
             f"{published}. Run the workflow to refresh the local one.")
        return published

    fail(f"No GeoPackage at {local} (nor at {published}). "
         f"Run: python src/leak_relocation_geopandas.py")
    return None  # unreachable; fail() raises


def clean_value(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        # pd.isna raises on array-likes; those are kept and stringified below.
        pass
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (AttributeError, ValueError):
            pass
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def reduce_columns(gdf):
    """Keep the columns worth showing, and make their values JSON-safe."""
    keep = [name for name in PREFERRED_COLUMNS if name in gdf.columns]
    for column in gdf.columns:
        if column == "geometry" or column in keep:
            continue
        lowered = str(column).lower()
        if any(keyword in lowered for keyword in COLUMN_KEYWORDS):
            keep.append(column)
    keep = list(dict.fromkeys(keep))[:MAX_COLUMNS]
    keep.append("geometry")

    out = gdf[keep].copy()
    for column in out.columns:
        if column != "geometry":
            out[column] = out[column].map(clean_value)
    return out


def load_layer(gpkg, layer_name):
    gdf = gpd.read_file(gpkg, layer=layer_name)
    gdf = gdf[gdf.geometry.notna()].copy()
    log(f"{layer_name}: {len(gdf):,} features, CRS={gdf.crs}")
    if gdf.crs is None:
        fail(f"Layer {layer_name} has no CRS")
    return reduce_columns(gdf.to_crs(4326))


def copy_vendored_leaflet(out_dir):
    """Return (css_ref, js_ref), preferring a local copy of Leaflet."""
    source = config.WORK_ROOT / "leaflet_context" / "vendor" / "leaflet"
    target = out_dir / "leaflet"
    names = ["leaflet.css", "leaflet.js"]

    if source.exists():
        target.mkdir(parents=True, exist_ok=True)
        for name in names:
            if (source / name).exists():
                shutil.copy2(source / name, target / name)

    if all((target / name).exists() for name in names):
        return "leaflet/leaflet.css", "leaflet/leaflet.js"

    warn("Leaflet is not vendored locally, so the page will load it from "
         "unpkg.com. If this network blocks that, the map will be blank. "
         "Run: python scripts/build_leaflet_context.py")
    return f"{LEAFLET_CDN}/leaflet.css", f"{LEAFLET_CDN}/leaflet.js"


def build(gpkg=None, out_dir=None):
    """Write the viewer folder. Returns the index.html path."""
    gpkg = resolve_gpkg(gpkg)
    out_dir = Path(out_dir) if out_dir else Path(config.VIEWER_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    log("=== Build local LeakRelocation Leaflet viewer ===")
    log(f"GeoPackage: {gpkg}")
    log(f"Output folder: {out_dir}")

    points = load_layer(gpkg, POINT_LAYER)
    lines = load_layer(gpkg, LINE_LAYER)
    if len(lines):
        lines["geometry"] = lines.geometry.simplify(0.000001, preserve_topology=True)

    if not len(points) and not len(lines):
        fail(f"{gpkg} holds no relocated features, so there is nothing to map.")

    points_geojson = out_dir / "relocated_leaks.geojson"
    lines_geojson = out_dir / "relocated_leak_trace_lines.geojson"
    points.to_file(points_geojson, driver="GeoJSON")
    lines.to_file(lines_geojson, driver="GeoJSON")
    log(f"Wrote: {points_geojson}")
    log(f"Wrote: {lines_geojson}")

    west, south, east, north = (
        float(value) for value in (points.total_bounds if len(points)
                                   else lines.total_bounds))

    css_ref, js_ref = copy_vendored_leaflet(out_dir)
    index = out_dir / "index.html"
    index.write_text(render_viewer(css_ref, js_ref, south, west, north, east),
                     encoding="utf-8")
    log(f"HTML viewer: {index}")
    log(f"Serve it with: python viewer/serve_viewer.py --page {index}")
    return index


def main(argv=None):
    args = parse_args(argv)
    build(args.gpkg, args.out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
