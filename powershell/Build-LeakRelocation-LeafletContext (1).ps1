
$ErrorActionPreference = "Stop"
$Root = Join-Path $env:USERPROFILE "Downloads\LeakRelocation-GeoPandas"
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$OutFolder = Join-Path $Root "leaflet_context"
$BuildPy = Join-Path $OutFolder "build_leaflet_context.py"
$RunLog = Join-Path $OutFolder "build_leaflet_context.log"
if (!(Test-Path $Py)) { throw "Python venv not found: $Py" }
if (!(Test-Path $OutFolder)) { New-Item -ItemType Directory -Path $OutFolder | Out-Null }
$PythonCode = @'
from pathlib import Path
import json
import math
import shutil
import warnings
warnings.filterwarnings("ignore")
import pandas as pd
import geopandas as gpd
from pyproj import CRS
try:
    import requests
except Exception:
    requests = None
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass
ROOT = Path.home() / "Downloads" / "LeakRelocation-GeoPandas"
CACHE = ROOT / "layer_cache"
OUT = ROOT / "leaflet_context"
OUT.mkdir(parents=True, exist_ok=True)
VENDOR = OUT / "vendor" / "leaflet"
VENDOR.mkdir(parents=True, exist_ok=True)
HTML = OUT / "LeakRelocation_DNV_Leaflet_Context.html"
SUMMARY = OUT / "LeakRelocation_DNV_Leaflet_Context_summary.txt"
NATIVE_WKT = 'PROJCS["NG_Equidistant_Conic_USft",GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],PROJECTION["Equidistant_Conic"],PARAMETER["False_Easting",0.0],PARAMETER["False_Northing",0.0],PARAMETER["Central_Meridian",-74.0],PARAMETER["Standard_Parallel_1",42.0],PARAMETER["Standard_Parallel_2",44.0],PARAMETER["Latitude_Of_Origin",43.0],UNIT["Foot_US",0.3048006096012192]]'
NATIVE_CRS = CRS.from_wkt(NATIVE_WKT)
WGS84 = CRS.from_epsg(4326)
LAYERS = [
    {"key":"historic_leaks", "label":"Historic leaks", "kind":"point", "sample":5000, "color":"#0066cc", "radius":4, "weight":1},
    {"key":"distribution_pipes", "label":"Distribution pipes", "kind":"line", "sample":3500, "color":"#202020", "radius":0, "weight":2},
    {"key":"service_pipes", "label":"Service pipes", "kind":"line", "sample":3500, "color":"#ff8c00", "radius":0, "weight":2}
]
LEAFLET_FILES = {
    "leaflet.css": "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
    "leaflet.js": "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
}
def log(msg):
    print(str(msg), flush=True)
def download_file(url, path):
    if path.exists() and path.stat().st_size > 1000:
        return "existing"
    if requests is None:
        raise RuntimeError("requests package not available; cannot download Leaflet asset: {0}".format(url))
    headers = {"User-Agent":"NationalGrid-LeakRelocation-Leaflet-Builder/1.0"}
    errors = []
    for verify in (True, False):
        try:
            r = requests.get(url, headers=headers, timeout=60, verify=verify)
            r.raise_for_status()
            path.write_bytes(r.content)
            return "downloaded_verify_{0}".format(verify)
        except Exception as ex:
            errors.append("verify={0}: {1}".format(verify, ex))
    raise RuntimeError("Could not download {0}. Errors: {1}".format(url, " | ".join(errors)))
def ensure_leaflet_assets():
    modes = {}
    for name, url in LEAFLET_FILES.items():
        modes[name] = download_file(url, VENDOR / name)
    return modes
def read_cache(cache_name):
    p = CACHE / (cache_name + ".pkl.gz")
    if not p.exists():
        raise RuntimeError("Missing cache file: {0}".format(p))
    df = pd.read_pickle(p, compression="gzip")
    if isinstance(df, gpd.GeoDataFrame):
        gdf = df.copy()
    else:
        if "geometry" not in df.columns:
            raise RuntimeError("Cache has no geometry column: {0}".format(p))
        gdf = gpd.GeoDataFrame(df.copy(), geometry="geometry")
    gdf = gdf[gdf.geometry.notna()].copy()
    gdf = gdf.set_crs(NATIVE_CRS, allow_override=True)
    return gdf
def sample_gdf(gdf, count, seed):
    if len(gdf) <= count:
        return gdf.copy()
    return gdf.sample(n=count, random_state=seed).copy()
def iter_xy_geojson_geom(geom):
    if not geom:
        return
    t = geom.get("type")
    c = geom.get("coordinates")
    if not c:
        return
    if t == "Point":
        yield float(c[0]), float(c[1])
    elif t in ("LineString", "MultiPoint"):
        for xy in c:
            yield float(xy[0]), float(xy[1])
    elif t in ("Polygon", "MultiLineString"):
        for part in c:
            for xy in part:
                yield float(xy[0]), float(xy[1])
    elif t == "MultiPolygon":
        for poly in c:
            for ring in poly:
                for xy in ring:
                    yield float(xy[0]), float(xy[1])
def bounds_for(feature_collections):
    xs, ys = [], []
    for fc in feature_collections:
        for feat in fc.get("features", []):
            for x, y in iter_xy_geojson_geom(feat.get("geometry")):
                if math.isfinite(x) and math.isfinite(y):
                    xs.append(x); ys.append(y)
    if not xs:
        raise RuntimeError("No finite WGS84 coordinates found in generated GeoJSON.")
    return {"west":min(xs),"south":min(ys),"east":max(xs),"north":max(ys),"center_lon":(min(xs)+max(xs))/2.0,"center_lat":(min(ys)+max(ys))/2.0}
def geojson_for_layer(cache_name, sample_count, seed):
    gdf = read_cache(cache_name)
    sample = sample_gdf(gdf, sample_count, seed).to_crs(WGS84)
    geojson = json.loads(sample.to_json(drop_id=True))
    return gdf, sample, geojson
def js_string(value):
    return json.dumps(value, ensure_ascii=False)
def build_html(loaded, bounds, asset_modes):
    data_js = []
    layer_js = []
    overlay_parts = []
    for cfg, full_gdf, sample_gdf_obj, geojson in loaded:
        data_var = cfg["key"] + "Data"
        layer_var = cfg["key"] + "Layer"
        data_js.append("const {0} = {1};".format(data_var, js_string(geojson)))
        if cfg["kind"] == "point":
            layer_js.append("""
const {layer_var} = L.geoJSON({data_var}, {{
  pointToLayer: function(feature, latlng) {{
    return L.circleMarker(latlng, {{
      radius: {radius},
      color: "{color}",
      weight: 1,
      fillColor: "{color}",
      fillOpacity: 0.75
    }});
  }},
  onEachFeature: bindPopup
}}).addTo(map);
""".format(layer_var=layer_var,data_var=data_var,radius=cfg["radius"],color=cfg["color"]))
        else:
            layer_js.append("""
const {layer_var} = L.geoJSON({data_var}, {{
  style: function(feature) {{
    return {{
      color: "{color}",
      weight: {weight},
      opacity: 0.75
    }};
  }},
  onEachFeature: bindPopup
}}).addTo(map);
""".format(layer_var=layer_var,data_var=data_var,color=cfg["color"],weight=cfg["weight"]))
        overlay_parts.append('"{0}": {1}'.format(cfg["label"], layer_var))
    center_url = "https://www.openstreetmap.org/?mlat={0:.8f}&mlon={1:.8f}#map=12/{0:.8f}/{1:.8f}".format(bounds["center_lat"], bounds["center_lon"])
    html = """<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>LeakRelocation DNV Leaflet Context</title>
  <link rel="stylesheet" href="vendor/leaflet/leaflet.css"/>
  <style>
    html, body { height: 100%; width: 100%; margin: 0; padding: 0; font-family: Arial, sans-serif; }
    #map { height: 100%; width: 100%; }
    .info { background: white; padding: 10px 12px; border: 1px solid #777; border-radius: 4px; font-size: 13px; box-shadow: 0 1px 5px rgba(0,0,0,0.35); max-width: 520px; }
    .leaflet-popup-content table { border-collapse: collapse; }
    .leaflet-popup-content th { text-align: left; padding-right: 8px; vertical-align: top; }
    .leaflet-popup-content td { vertical-align: top; }
    .swatch { display: inline-block; width: 22px; height: 4px; margin-right: 6px; vertical-align: middle; }
    .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
    #load-warning { display:none; position:absolute; z-index:9999; left:12px; top:12px; background:#fff3cd; border:1px solid #d6b656; padding:12px; max-width:620px; }
  </style>
</head>
<body>
<div id="load-warning"><b>Leaflet did not load.</b><br/>This HTML expects local files under <code>vendor/leaflet</code>. Re-run the builder if the vendor files are missing.</div>
<div id="map"></div>
<script src="vendor/leaflet/leaflet.js"></script>
<script>
if (typeof L === "undefined") {
  document.getElementById("load-warning").style.display = "block";
} else {
  const map = L.map("map", { preferCanvas: true });
  const osm = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "&copy; OpenStreetMap contributors" });
  const esriImagery = L.tileLayer("https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", { maxZoom: 19, attribution: "Tiles &copy; Esri" });
  const esriTopo = L.tileLayer("https://services.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}", { maxZoom: 19, attribution: "Tiles &copy; Esri" });
  osm.addTo(map);
  function htmlEscape(value) {
    if (value === null || value === undefined) return "";
    return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
  }
  function bindPopup(feature, layer) {
    const props = feature.properties || {};
    const keys = Object.keys(props).slice(0, 14);
    let rows = "";
    for (const k of keys) rows += "<tr><th>" + htmlEscape(k) + "</th><td>" + htmlEscape(props[k]) + "</td></tr>";
    if (!rows) rows = "<tr><td>No attributes in sample</td></tr>";
    layer.bindPopup("<table>" + rows + "</table>");
  }
  __DATA_JS__
  __LAYER_JS__
  const dataBounds = [[__SOUTH__, __WEST__], [__NORTH__, __EAST__]];
  map.fitBounds(dataBounds, { padding: [24, 24] });
  const extentBox = L.rectangle(dataBounds, { color: "#d40000", weight: 2, fill: false, dashArray: "8,6" }).addTo(map);
  const centerMarker = L.marker([__CENTER_LAT__, __CENTER_LON__]).addTo(map);
  centerMarker.bindPopup("Data extent center<br/>Lat: __CENTER_LAT__<br/>Lon: __CENTER_LON__<br/><a href='__CENTER_URL__' target='_blank'>Open center in OpenStreetMap</a>");
  const baseMaps = { "OpenStreetMap street map": osm, "Esri World Imagery": esriImagery, "Esri World Topographic": esriTopo };
  const overlays = { __OVERLAYS__, "Red data extent box": extentBox, "Extent center marker": centerMarker };
  L.control.layers(baseMaps, overlays, { collapsed: false }).addTo(map);
  L.control.scale({ imperial: true, metric: true }).addTo(map);
  const info = L.control({ position: "bottomleft" });
  info.onAdd = function() {
    const div = L.DomUtil.create("div", "info");
    div.innerHTML =
      "<b>LeakRelocation DNV Leaflet local sanity check</b><br/>" +
      "Local Leaflet assets; online map tiles; WGS84 GeoJSON samples embedded.<br/>" +
      "West: __WEST__<br/>South: __SOUTH__<br/>East: __EAST__<br/>North: __NORTH__<br/>" +
      "Center: __CENTER_LAT__, __CENTER_LON__<br/><br/>" +
      "<span class='dot' style='background:#0066cc'></span>Historic leaks<br/>" +
      "<span class='swatch' style='background:#202020'></span>Distribution pipes<br/>" +
      "<span class='swatch' style='background:#ff8c00'></span>Service pipes<br/>" +
      "<span class='swatch' style='background:#d40000'></span>Red data extent";
    return div;
  };
  info.addTo(map);
}
</script>
</body>
</html>
"""
    replaces = {
        "__DATA_JS__":"\n  ".join(data_js),
        "__LAYER_JS__":"\n  ".join(layer_js),
        "__OVERLAYS__":", ".join(overlay_parts),
        "__WEST__":"{0:.8f}".format(bounds["west"]),
        "__SOUTH__":"{0:.8f}".format(bounds["south"]),
        "__EAST__":"{0:.8f}".format(bounds["east"]),
        "__NORTH__":"{0:.8f}".format(bounds["north"]),
        "__CENTER_LAT__":"{0:.8f}".format(bounds["center_lat"]),
        "__CENTER_LON__":"{0:.8f}".format(bounds["center_lon"]),
        "__CENTER_URL__":center_url
    }
    for k, v in replaces.items():
        html = html.replace(k, v)
    HTML.write_text(html, encoding="utf-8")
def main():
    asset_modes = ensure_leaflet_assets()
    loaded = []
    collections = []
    for i, cfg in enumerate(LAYERS, start=1):
        print("Building GeoJSON sample: {0}".format(cfg["label"]), flush=True)
        full_gdf, sample, geojson = geojson_for_layer(cfg["cache"], cfg["sample"], 500 + i)
        loaded.append((cfg, full_gdf, sample, geojson))
        collections.append(geojson)
    bounds = bounds_for(collections)
    build_html(loaded, bounds, asset_modes)
    summary = []
    summary.append("LeakRelocation DNV Leaflet Context")
    summary.append("HTML: {0}".format(HTML))
    summary.append("Leaflet asset modes: {0}".format(asset_modes))
    summary.append("Native CRS used for local cache assignment: {0}".format(NATIVE_CRS.name))
    for cfg, full_gdf, sample, geojson in loaded:
        summary.append("{0}: full_count={1:,}; embedded_sample={2:,}; sample_bounds={3}".format(cfg["label"], len(full_gdf), len(sample), list(sample.total_bounds)))
    summary.append("Bounds west: {0:.8f}".format(bounds["west"]))
    summary.append("Bounds south: {0:.8f}".format(bounds["south"]))
    summary.append("Bounds east: {0:.8f}".format(bounds["east"]))
    summary.append("Bounds north: {0:.8f}".format(bounds["north"]))
    summary.append("Center lat: {0:.8f}".format(bounds["center_lat"]))
    summary.append("Center lon: {0:.8f}".format(bounds["center_lon"]))
    SUMMARY.write_text("\n".join(summary), encoding="utf-8")
    print("\n".join(summary), flush=True)
if __name__ == "__main__":
    main()
'@
Set-Content -Path $BuildPy -Value $PythonCode -Encoding UTF8
cmd /c ""$Py" "$BuildPy" > "$RunLog" 2>&1"
$ExitCode = $LASTEXITCODE
$HtmlPath = Join-Path $OutFolder "LeakRelocation_DNV_Leaflet_Context.html"
$SummaryPath = Join-Path $OutFolder "LeakRelocation_DNV_Leaflet_Context_summary.txt"
$Text = Get-Content $RunLog -Raw
$Clip = @(
    "=== LeakRelocation Leaflet Context Builder ===",
    "ExitCode: $ExitCode",
    "RunLog: $RunLog",
    "HTML: $HtmlPath",
    "Summary: $SummaryPath",
    "",
    "--- OUTPUT ---",
    $Text
) -join "`r`n"
$Clip | Set-Clipboard
$Clip
if (Test-Path $HtmlPath) { Invoke-Item $HtmlPath }
