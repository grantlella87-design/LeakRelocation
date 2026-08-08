"""Build and serve a Leaflet map of the MA main lines export.

    python GISportal/main_lines_viewer.py                  # build, then serve
    python GISportal/main_lines_viewer.py --build-only
    python GISportal/main_lines_viewer.py --port 8790
    python GISportal/main_lines_viewer.py --geojson some/other/export.geojson

Reads the GeoJSON that query_ma_main_lines_2022_plus.py writes and produces a
self-contained viewer folder: index.html, the GeoJSON, and a local copy of
Leaflet. It then serves that folder, because a page opened as file:// cannot
fetch its own GeoJSON - the browser blocks it and the map comes up empty.

Material colours are decided here, in Python, using the shared
leakrelocation.matching classifier. The relocation viewers each carry their own
JavaScript copy of that logic, and those copies drifting from the Python is what
made every copper pipe show as plastic and left the map showing only "plastic"
and "unknown". This viewer ships the family in the GeoJSON instead, so there is
one classifier and the map cannot disagree with the data.
"""
import argparse
import functools
import http.server
import json
import shutil
import sys
import urllib.request
import webbrowser
from pathlib import Path

from _bootstrap import auth, config  # noqa: F401 - auth re-export used by callers

from leakrelocation import matching
from leakrelocation.output import fail, log, warn
from leakrelocation.viewer_pane import PANE_CSS, PANE_HTML, PANE_JS

HERE = Path(__file__).resolve().parent
DEFAULT_GEOJSON = HERE.parent / "outputs" / "ma_main_lines_2022_plus.geojson"
DEFAULT_VIEWER_DIR = HERE.parent / "outputs" / "ma_main_lines_viewer"
DEFAULT_PORT = 8788

LEAFLET_VERSION = "1.9.4"
LEAFLET_CDN = f"https://unpkg.com/leaflet@{LEAFLET_VERSION}/dist"
LEAFLET_FILES = ("leaflet.css", "leaflet.js")

# Where scripts/build_leaflet_context.py leaves its download, so a machine that
# has already vendored Leaflet does not need to reach the internet again.
VENDORED_LEAFLET = config.WORK_ROOT / "leaflet_context" / "vendor" / "leaflet"

# The field holding the material, and the label column that
# query_ma_main_lines_2022_plus.py adds when the layer declares a coded-value
# domain for it. The label is preferred: a bare code carries no meaning.
MATERIAL_FIELD = "material"
LABEL_SUFFIX = "_LABEL"

# The family added to every feature's properties, and the one the map styles on.
FAMILY_PROPERTY = "MaterialFamily"

# Families the shared classifier can return, plus two this viewer adds:
# CODED for a numeric value the layer gave no domain label for, and OTHER for a
# label that matched no family. Anything not listed here is bucketed into OTHER
# rather than becoming its own colour, so the legend stays readable.
FAMILY_COLORS = {
    "PLASTIC": "#00a651",
    "STEEL": "#4d4d4d",
    "IRON": "#8b4513",
    "COPPER": "#b87333",
    "UNKNOWN": "#999999",
    "CODED": "#c86400",
    "OTHER": "#7b68ee",
}
CLASSIFIER_FAMILIES = ("PLASTIC", "STEEL", "IRON", "COPPER", "UNKNOWN")
CODED_FAMILY = "CODED"
OTHER_FAMILY = "OTHER"

# Attributes worth seeing first in a popup. Anything else the export carries
# still appears, after these.
POPUP_FIELD_ORDER = [
    "OBJECTID", MATERIAL_FIELD, MATERIAL_FIELD + LABEL_SUFFIX, FAMILY_PROPERTY,
    "assetid", "facilityid", "nominaldiameter", "outsidediameter",
    "measuredlength", "operatingpressure", "maopdesign", "installationdate",
    "CREATIONDATE", "lifecyclestatus", "jurisdiction", "ng_formattedaddress",
]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--geojson", default=str(DEFAULT_GEOJSON),
                        help="GeoJSON written by query_ma_main_lines_2022_plus.py.")
    parser.add_argument("--out-dir", default=str(DEFAULT_VIEWER_DIR),
                        help="Folder to build the viewer into.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port to serve on. Default: {DEFAULT_PORT}.")
    parser.add_argument("--build-only", action="store_true",
                        help="Write the viewer files and exit without serving.")
    parser.add_argument("--no-browser", action="store_true",
                        help="Serve without opening a browser.")
    return parser.parse_args(argv)


# --- Material families ------------------------------------------------------


def material_value(properties):
    """The material to classify: the domain label when the export has one.

    A coded-value domain lives in the layer metadata, so the label is only
    present if the export decoded it. Falling back to the raw field is right,
    but a raw *code* is not a material - see family_of.
    """
    label = matching.clean((properties or {}).get(MATERIAL_FIELD + LABEL_SUFFIX))
    return label or matching.clean((properties or {}).get(MATERIAL_FIELD))


def looks_like_a_bare_code(text):
    """True for a value that is only digits and punctuation, e.g. "9".

    Such a value cannot be classified, and must not be guessed at: a code here
    belongs to this layer's own domain, while the shared classifier's numeric
    decoding is the DNV *service pipe* subtype domain. A "9" would come back
    "Plastic PE" on no evidence at all.
    """
    return bool(text) and not any(character.isalpha() for character in text)


def family_of(properties):
    """Material family for one feature, for both colour and legend.

    Uses matching.family_from_label rather than matching.material_family, so the
    text is classified as text. material_family decodes a leading number as a
    DNV subtype code, which would read "2 IN PLASTIC" as "Cast Iron".
    """
    text = material_value(properties)
    if not text:
        return "UNKNOWN"
    if looks_like_a_bare_code(text):
        return CODED_FAMILY
    family = matching.family_from_label(text)
    return family if family in CLASSIFIER_FAMILIES else OTHER_FAMILY


def enrich_features(features):
    """Add the material family to every feature, and count the families."""
    counts = {}
    for feature in features:
        properties = feature.setdefault("properties", {}) or {}
        family = family_of(properties)
        properties[FAMILY_PROPERTY] = family
        feature["properties"] = properties
        counts[family] = counts.get(family, 0) + 1
    return counts


# --- Geometry ---------------------------------------------------------------


def coordinates_of(geometry):
    """Every [lon, lat] pair in a LineString or MultiLineString."""
    kind = (geometry or {}).get("type")
    coordinates = (geometry or {}).get("coordinates") or []
    if kind == "LineString":
        return coordinates
    if kind == "MultiLineString":
        return [point for part in coordinates for point in part]
    return []


def bounds_of(features):
    """(south, west, north, east) over all line geometry, or None if empty."""
    south = west = north = east = None
    for feature in features:
        for point in coordinates_of(feature.get("geometry")):
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            longitude, latitude = float(point[0]), float(point[1])
            south = latitude if south is None else min(south, latitude)
            north = latitude if north is None else max(north, latitude)
            west = longitude if west is None else min(west, longitude)
            east = longitude if east is None else max(east, longitude)
    if south is None:
        return None
    # A single vertical or horizontal line gives a zero-width box, which
    # fitBounds cannot zoom to. Pad it into a small square.
    if south == north:
        south, north = south - 0.001, north + 0.001
    if west == east:
        west, east = west - 0.001, east + 0.001
    return south, west, north, east


# --- Leaflet assets ---------------------------------------------------------


def download_leaflet(target_dir):
    """Fetch Leaflet into target_dir. True if both files landed."""
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in LEAFLET_FILES:
        try:
            with urllib.request.urlopen(f"{LEAFLET_CDN}/{name}", timeout=30) as response:
                (target_dir / name).write_bytes(response.read())
        except Exception as exc:  # noqa: BLE001 - any failure means fall back to the CDN
            warn(f"Could not download {name}: {exc}")
            return False
    return True


def ensure_leaflet(out_dir):
    """Return (css_ref, js_ref) for the page, preferring local files.

    Local files matter here: this runs inside a corporate network where unpkg
    may be unreachable, and a page whose only Leaflet reference is a blocked
    CDN renders as a blank white rectangle with no clue why.
    """
    local = out_dir / "leaflet"
    have_local = all((local / name).exists() for name in LEAFLET_FILES)

    if not have_local and all((VENDORED_LEAFLET / name).exists() for name in LEAFLET_FILES):
        local.mkdir(parents=True, exist_ok=True)
        for name in LEAFLET_FILES:
            shutil.copy2(VENDORED_LEAFLET / name, local / name)
        log(f"Leaflet copied from {VENDORED_LEAFLET}")
        have_local = True

    if not have_local:
        have_local = download_leaflet(local)
        if have_local:
            log(f"Leaflet {LEAFLET_VERSION} downloaded into {local}")

    if have_local:
        return "leaflet/leaflet.css", "leaflet/leaflet.js"

    warn("Using the Leaflet CDN. If this network blocks unpkg.com the map will "
         "be blank; vendor the files into " + str(local) + " to fix that.")
    return f"{LEAFLET_CDN}/leaflet.css", f"{LEAFLET_CDN}/leaflet.js"


# --- The page ---------------------------------------------------------------

TEMPLATE = """\
<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<title>__TITLE__</title>
<link rel="stylesheet" href="__CSS_REF__"/>
<style>
.info{background:#fff;padding:10px 12px;border:1px solid #777;border-radius:4px;
  font-size:13px;box-shadow:0 1px 5px rgba(0,0,0,.35);max-width:420px}
.legend-row{margin:3px 0}
.swatch{display:inline-block;width:18px;height:6px;margin-right:6px;
  border:1px solid #555;vertical-align:middle}
.leaflet-popup-content table{border-collapse:collapse;font-size:12px}
.leaflet-popup-content th,.leaflet-popup-content td{border:1px solid #ccc;
  padding:3px 5px;vertical-align:top}
.leaflet-popup-content th{background:#f4f4f4;text-align:left}
#loadError{padding:12px;color:#a00;font-size:13px}
__PANE_CSS__
</style>
</head>
<body>
<div id="map"></div>
__PANE_HTML__
<script src="__JS_REF__"></script>
<script>
__PANE_JS__
</script>
<script>
/* Colours and families come from Python - see main_lines_viewer.py. */
const FAMILY_COLORS = __FAMILY_COLORS__;
const FAMILY_PROPERTY = "__FAMILY_PROPERTY__";
const POPUP_FIELD_ORDER = __POPUP_FIELD_ORDER__;
const GEOJSON_FILE = "__GEOJSON_FILE__";

const map = L.map('map', {preferCanvas: true});
const osm = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom: 20, attribution: '&copy; OpenStreetMap contributors'}).addTo(map);
const imagery = L.tileLayer('https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
  {maxZoom: 20, attribution: 'Tiles &copy; Esri'});
const topo = L.tileLayer('https://services.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}',
  {maxZoom: 20, attribution: 'Tiles &copy; Esri'});
map.fitBounds(__BOUNDS__, {padding: [24, 24]});
L.control.scale({imperial: true, metric: true}).addTo(map);

const info = L.control({position: 'bottomleft'});
info.onAdd = function () {
  const div = L.DomUtil.create('div', 'info');
  div.id = 'info';
  return div;
};
info.addTo(map);

function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
}

function popupHtml(props) {
  const keys = POPUP_FIELD_ORDER.filter(
    k => Object.prototype.hasOwnProperty.call(props, k));
  for (const key of Object.keys(props)) {
    if (!keys.includes(key) && keys.length < 24) keys.push(key);
  }
  let rows = '';
  for (const key of keys) {
    rows += '<tr><th>' + esc(key) + '</th><td>' + esc(props[key]) + '</td></tr>';
  }
  return '<table>' + rows + '</table>';
}

function styleFor(family) {
  return {
    color: FAMILY_COLORS[family] || FAMILY_COLORS.OTHER,
    weight: 3,
    opacity: 0.85,
  };
}

function bindFeature(feature, layer) {
  layer.bindPopup(popupHtml(feature.properties || {}));
  layer.on('click', () => AttributePane.selectFromMap(layer));
}

function showLegend(total, counts) {
  const families = Object.keys(counts).sort();
  let html = '<b>__TITLE__</b><br/>main lines: ' + total.toLocaleString();
  for (const family of families) {
    html += '<div class="legend-row"><span class="swatch" style="background:'
      + (FAMILY_COLORS[family] || FAMILY_COLORS.OTHER) + '"></span>'
      + esc(family) + ' ' + counts[family].toLocaleString() + '</div>';
  }
  document.getElementById('info').innerHTML = html;
}

/* One layer per material family, so a family can be isolated from the layer
   control, and each gets its own tab in the attribute table. */
function buildLayers(features) {
  const grouped = {};
  for (const feature of features) {
    const family = (feature.properties || {})[FAMILY_PROPERTY] || 'UNKNOWN';
    (grouped[family] = grouped[family] || []).push(feature);
  }
  const overlays = {};
  const counts = {};
  for (const family of Object.keys(grouped).sort()) {
    const group = grouped[family];
    counts[family] = group.length;
    const layer = L.geoJSON({type: 'FeatureCollection', features: group}, {
      style: () => styleFor(family),
      onEachFeature: bindFeature,
    }).addTo(map);
    overlays[family + ' (' + group.length.toLocaleString() + ')'] = layer;
    AttributePane.register(family, family, layer);
  }
  return {overlays: overlays, counts: counts};
}

fetch(GEOJSON_FILE).then(response => {
  if (!response.ok) throw new Error(response.status + ' ' + response.statusText);
  return response.json();
}).then(data => {
  const features = (data && data.features) || [];
  const built = buildLayers(features);
  L.control.layers({'OpenStreetMap': osm, 'Esri imagery': imagery, 'Esri topo': topo},
                   built.overlays, {collapsed: false}).addTo(map);
  showLegend(features.length, built.counts);
  AttributePane.build();
  document.body.dataset.mainLinesLoaded = String(features.length);
}).catch(error => {
  /* Reported in the page, not only the console: a silent blank map is what
     sent people looking for a problem in the data. */
  const note = L.DomUtil.create('div', '', document.getElementById('attrPaneBody'));
  note.id = 'loadError';
  note.textContent = 'Could not load ' + GEOJSON_FILE + ': ' + error
    + '. Serve this folder with main_lines_viewer.py rather than opening '
    + 'index.html directly - a file:// page is not allowed to fetch it.';
  document.body.dataset.mainLinesError = String(error);
});
</script>
</body>
</html>"""


def render_html(title, css_ref, js_ref, bounds, geojson_file):
    south, west, north, east = bounds
    return (TEMPLATE
            .replace("__PANE_CSS__", PANE_CSS)
            .replace("__PANE_HTML__", PANE_HTML)
            .replace("__PANE_JS__", PANE_JS)
            .replace("__FAMILY_COLORS__", json.dumps(FAMILY_COLORS))
            .replace("__FAMILY_PROPERTY__", FAMILY_PROPERTY)
            .replace("__POPUP_FIELD_ORDER__", json.dumps(POPUP_FIELD_ORDER))
            .replace("__GEOJSON_FILE__", geojson_file)
            .replace("__BOUNDS__", f"[[{south},{west}],[{north},{east}]]")
            .replace("__CSS_REF__", css_ref)
            .replace("__JS_REF__", js_ref)
            .replace("__TITLE__", title))


# --- Build and serve --------------------------------------------------------


def read_features(geojson_path):
    path = Path(geojson_path)
    if not path.exists():
        fail(f"No GeoJSON at {path}. Produce it first:\n"
             f"    python GISportal/query_ma_main_lines_2022_plus.py "
             f"--out-dir {path.parent} --basename {path.stem}")
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    features = data.get("features") or []
    if not features:
        fail(f"{path} has no features, so there is nothing to map.")
    return features


def build(geojson_path, out_dir, title=None):
    """Write the viewer folder. Returns the index.html path."""
    features = read_features(geojson_path)
    log(f"Loaded {len(features):,} features from {geojson_path}")

    counts = enrich_features(features)
    for family in sorted(counts):
        log(f"  {family}: {counts[family]:,}")
    if counts.get(CODED_FAMILY):
        warn(f"{counts[CODED_FAMILY]:,} features have a numeric {MATERIAL_FIELD} "
             f"value and no {MATERIAL_FIELD}{LABEL_SUFFIX} column, so their "
             "material cannot be named. The export adds that column when the "
             "layer declares a coded-value domain for the field.")

    bounds = bounds_of(features)
    if bounds is None:
        fail("No line geometry in the GeoJSON, so the map has nothing to show. "
             "Re-run the query without --no-geojson.")

    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    geojson_name = "main_lines.geojson"
    with open(out_dir / geojson_name, "w", encoding="utf-8") as handle:
        json.dump({"type": "FeatureCollection", "features": features}, handle,
                  ensure_ascii=False)

    css_ref, js_ref = ensure_leaflet(out_dir)
    html = render_html(title or Path(geojson_path).stem, css_ref, js_ref,
                       bounds, geojson_name)
    index = out_dir / "index.html"
    index.write_text(html, encoding="utf-8")

    log(f"Viewer: {index}")
    return index


def serve(out_dir, port=DEFAULT_PORT, open_browser=True):
    directory = str(Path(out_dir).resolve())
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=directory)
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as server:
        url = f"http://127.0.0.1:{port}/index.html"
        log(f"Serving {directory} at {url}")
        log("Press Ctrl+C to stop.")
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            log("Stopped.")


def main(argv=None):
    args = parse_args(argv)
    build(args.geojson, args.out_dir)
    if args.build_only:
        log(f"Serve it with: python {Path(__file__).name} --out-dir {args.out_dir}")
        return 0
    serve(args.out_dir, args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
