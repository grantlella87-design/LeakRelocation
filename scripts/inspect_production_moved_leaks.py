from pathlib import Path
import pandas as pd
import geopandas as gpd
import sys

ROOT = Path.home() / "Downloads" / "LeakRelocation-GeoPandas"
OUT = ROOT / "production_moved_leak_outputs"
OUT.mkdir(parents=True, exist_ok=True)

GPKG = Path(r"\\ngusnasnwh001\gasne\GasNE Shared\Shared\ENG\Complex Team\GIS AutoPrint\Distribution Leak Relocation\HistoricLeakRelocation.gpkg")

print("=== Production moved leak output inspection ===")
print("GeoPackage:", GPKG)

if not GPKG.exists():
    print("GeoPackage not found.")
    sys.exit(1)

try:
    import pyogrio
    layer_info = pyogrio.list_layers(GPKG)
    layers = [row[0] for row in layer_info]
except Exception:
    import fiona
    layers = list(fiona.listlayers(GPKG))

print("")
print("GeoPackage layers:")
for layer in layers:
    print(" -", layer)

preferred_point_layers = [
    "relocated_leaks",
    "relocated_leak_points",
    "moved_leaks",
    "HistoricLeakRelocation"
]

preferred_line_layers = [
    "relocated_leak_offset_lines",
    "relocated_trace_lines",
    "original_to_relocated_trace_lines"
]

point_layer = next((x for x in preferred_point_layers if x in layers), None)
line_layer = next((x for x in preferred_line_layers if x in layers), None)

if not point_layer:
    candidates = [x for x in layers if "relocat" in x.lower() or "moved" in x.lower() or "leak" in x.lower()]
    point_layer = candidates[0] if candidates else None

if not point_layer:
    print("Could not identify moved/relocated leak point layer.")
    sys.exit(1)

print("")
print("Using moved leak point layer:", point_layer)

moved = gpd.read_file(GPKG, layer=point_layer)
print("Moved leak feature count:", len(moved))
print("Moved leak CRS:", moved.crs)
print("Moved leak columns:")
print(list(moved.columns))

# Add WKT for CSV export.
moved_export = moved.copy()
moved_export["geometry_wkt"] = moved_export.geometry.to_wkt()
moved_export = pd.DataFrame(moved_export.drop(columns=["geometry"]))

full_csv = OUT / "relocated_leaks_ALL.csv"
sample_csv = OUT / "relocated_leaks_SAMPLE_250.csv"

moved_export.to_csv(full_csv, index=False)
moved_export.head(250).to_csv(sample_csv, index=False)

print("")
print("Moved leak full CSV:", full_csv)
print("Moved leak sample CSV:", sample_csv)

interesting_keywords = [
    "leak",
    "lms",
    "objectid",
    "oid",
    "globalid",
    "linked",
    "layer",
    "pipe",
    "asset",
    "material",
    "assettype",
    "diam",
    "distance",
    "confidence",
    "reason",
    "match"
]

interesting_cols = []
for col in moved_export.columns:
    low = str(col).lower()
    if any(k in low for k in interesting_keywords):
        interesting_cols.append(col)

# Keep output readable.
interesting_cols = list(dict.fromkeys(interesting_cols))
if "geometry_wkt" in moved_export.columns:
    interesting_cols.append("geometry_wkt")

if not interesting_cols:
    interesting_cols = list(moved_export.columns[:18])

print("")
print("Moved leak sample rows:")
print(moved_export[interesting_cols].head(25).to_string(index=False))

# Distance stats if available.
distance_cols = [c for c in moved_export.columns if "distance" in str(c).lower() or str(c).lower() in ["distanceft", "distance_ft"]]
for c in distance_cols:
    try:
        numeric = pd.to_numeric(moved_export[c], errors="coerce")
        print("")
        print("Distance stats for", c)
        print(numeric.describe(percentiles=[0.5, 0.75, 0.9, 0.95, 0.99]).to_string())
    except Exception as ex:
        print("Could not summarize distance column", c, ex)

# Group counts for useful categorical fields.
for c in moved_export.columns:
    low = str(c).lower()
    if any(k in low for k in ["linkedlayer", "match", "reason", "confidence", "material", "assettype", "pipematerialfamily"]):
        try:
            print("")
            print("Value counts for", c)
            print(moved_export[c].value_counts(dropna=False).head(30).to_string())
        except Exception:
            pass

if line_layer:
    print("")
    print("Using trace/offset line layer:", line_layer)
    lines = gpd.read_file(GPKG, layer=line_layer)
    print("Trace line feature count:", len(lines))
    print("Trace line CRS:", lines.crs)
    print("Trace line columns:")
    print(list(lines.columns))

    lines_export = lines.copy()
    lines_export["geometry_wkt"] = lines_export.geometry.to_wkt()
    lines_export = pd.DataFrame(lines_export.drop(columns=["geometry"]))

    line_csv = OUT / "relocated_leak_trace_lines_ALL.csv"
    lines_export.to_csv(line_csv, index=False)

    print("Trace line CSV:", line_csv)
    print("")
    print("Trace line sample rows:")
    print(lines_export.head(15).to_string(index=False))

print("")
print("DONE inspecting moved leak output.")
