from pathlib import Path
import shutil
import pandas as pd
import geopandas as gpd

from _bootstrap import config
from leakrelocation.viewer_html import render as render_viewer

ROOT = config.WORK_ROOT
PROJECT = config.PROJECT_DIR
GPKG = PROJECT / "HistoricLeakRelocation.gpkg"
OUT = config.VIEWER_DIR
OUT.mkdir(parents=True, exist_ok=True)
VENDOR_SRC = ROOT / "leaflet_context" / "vendor" / "leaflet"
VENDOR_DST = OUT / "leaflet"
POINT_LAYER = "relocated_leaks"
LINE_LAYER = "relocated_leak_offset_lines"

print("=== Build local LeakRelocation Leaflet viewer ===", flush=True)
print("GeoPackage:", GPKG, flush=True)
print("Output folder:", OUT, flush=True)
if not GPKG.exists():
    raise FileNotFoundError(f"GeoPackage not found: {GPKG}")

def clean_value(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    if hasattr(v, "item"):
        try:
            v = v.item()
        except Exception:
            pass
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if isinstance(v, (str, int, float, bool)):
        return v
    return str(v)

def reduce_columns(gdf):
    preferred = ["OrigLeakOID","LeakKey","LinkedLayer","MatchedPipeOID","MatchedPipeGID","DistanceFt","SearchRadiusFt","MatchMaterial","MatchDiameter","MatchPressure"]
    keep = [c for c in preferred if c in gdf.columns]
    for col in gdf.columns:
        if col == "geometry" or col in keep:
            continue
        low = str(col).lower()
        if any(k in low for k in ["leak", "pipe", "material", "diam", "distance", "match", "linked", "pressure"]):
            keep.append(col)
    keep = list(dict.fromkeys(keep))[:18]
    keep.append("geometry")
    out = gdf[keep].copy()
    for col in out.columns:
        if col != "geometry":
            out[col] = out[col].map(clean_value)
    return out

def load_layer(layer_name):
    gdf = gpd.read_file(GPKG, layer=layer_name)
    gdf = gdf[gdf.geometry.notna()].copy()
    print(f"{layer_name}: {len(gdf):,} features, CRS={gdf.crs}", flush=True)
    if gdf.crs is None:
        raise RuntimeError(f"Layer {layer_name} has no CRS")
    gdf = gdf.to_crs(4326)
    return reduce_columns(gdf)

points = load_layer(POINT_LAYER)
lines = load_layer(LINE_LAYER)
if len(lines):
    lines["geometry"] = lines.geometry.simplify(0.000001, preserve_topology=True)

points_geojson = OUT / "relocated_leaks.geojson"
lines_geojson = OUT / "relocated_leak_trace_lines.geojson"
points.to_file(points_geojson, driver="GeoJSON")
lines.to_file(lines_geojson, driver="GeoJSON")
print("Wrote:", points_geojson, flush=True)
print("Wrote:", lines_geojson, flush=True)

bounds = points.total_bounds if len(points) else lines.total_bounds
west, south, east, north = [float(x) for x in bounds]

leaflet_local = False
if VENDOR_SRC.exists():
    VENDOR_DST.mkdir(parents=True, exist_ok=True)
    for name in ["leaflet.css", "leaflet.js"]:
        src = VENDOR_SRC / name
        if src.exists():
            shutil.copy2(src, VENDOR_DST / name)
    leaflet_local = (VENDOR_DST / "leaflet.css").exists() and (VENDOR_DST / "leaflet.js").exists()
css_ref = "leaflet/leaflet.css" if leaflet_local else "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
js_ref = "leaflet/leaflet.js" if leaflet_local else "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"

html = render_viewer(css_ref, js_ref, south, west, north, east)
html_path = OUT / "index.html"
html_path.write_text(html, encoding="utf-8")
server_py = OUT / "serve_viewer.py"
server_py.write_text('''"""Serve the generated viewer on http://127.0.0.1:8777 and open a browser."""
import functools
import http.server
import pathlib
import webbrowser

PORT = 8777
HERE = pathlib.Path(__file__).resolve().parent

handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(HERE))
with http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler) as server:
    url = "http://127.0.0.1:{0}/index.html".format(PORT)
    print("Serving {0} at {1}".format(HERE, url), flush=True)
    webbrowser.open(url)
    server.serve_forever()
''', encoding="utf-8")
print("HTML viewer:", html_path, flush=True)
print("Viewer server:", server_py, flush=True)
print("Run:", f'python "{server_py}"', flush=True)
