import json
import os
import sys
import threading
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

warnings.filterwarnings("ignore")
import geopandas as gpd
import pandas as pd
from pyproj import CRS

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# Shared with the relocation workflow. A local copy of this classification used
# substring matching, where "PE" matched inside "PIPE" and "TYPE" and sent every
# such label to PLASTIC.
from leakrelocation import assettype, config, schema
from leakrelocation.assettype import decoder_for_layer, norm_code
from leakrelocation.assettype import family_from_assettype as material_family
from leakrelocation.viewer_pane import PANE_CSS, PANE_HTML, PANE_JS

HOST = "127.0.0.1"
PORT = 8765
ROOT = config.WORK_ROOT
CACHE = config.LAYER_CACHE_DIR
PROJECT = config.PROJECT_DIR
SUPPLEMENTAL_CSV = config.SUPPLEMENTAL_CSV
OUTPUT_GPKG = config.OUTPUT_GPKG
VENDOR = ROOT / "leaflet_context" / "vendor" / "leaflet"
NATIVE_WKT = 'PROJCS["NG_Equidistant_Conic_USft",GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],PROJECTION["Equidistant_Conic"],PARAMETER["False_Easting",0.0],PARAMETER["False_Northing",0.0],PARAMETER["Central_Meridian",-74.0],PARAMETER["Standard_Parallel_1",42.0],PARAMETER["Standard_Parallel_2",44.0],PARAMETER["Latitude_Of_Origin",43.0],UNIT["Foot_US",0.3048006096012192]]'
NATIVE_CRS = CRS.from_wkt(NATIVE_WKT)
WGS84 = CRS.from_epsg(4326)
MAX_FEATURES_DEFAULT = 25000
SIMPLIFY_DEFAULT = 0.000002
KEEP_TOKENS = [
    "objectid",
    "globalid",
    "lms",
    "leak",
    "diam",
    "material",
    "facility",
    "status",
    "distance",
    "linked",
    "nearest",
    "match",
    "reason",
    "confidence",
    "orig",
    "new",
    "pipe",
    "asset",
    "operatingpressure",
    "pressure",
]
MATERIAL_COLORS = {
    "PLASTIC": "#f7e708f0",
    "STEEL": "#061FFAF6",
    "IRON": "#8b4513",
    "COPPER": "#b87333",
    "UNKNOWN": "#D30FFAEB",
    "OTHER": "#7b68ee",
}
LAYERS = {
    "historic_leaks": {
        "label": "Historic leaks original",
        "source": "cache",
        "kind": "leak_point",
        "color": "#0066cc",
        "radius": 3,
        "weight": 1,
        "default": True,
    },
    "distribution_pipes": {
        "label": "Distribution pipes full - material coded",
        "source": "cache",
        "kind": "pipe_line",
        "color": "#202020",
        "radius": 0,
        "weight": 2,
        "default": False,
    },
    "service_pipes": {
        "label": "Service pipes full - material coded",
        "source": "cache",
        "kind": "pipe_line",
        "color": "#ff8c00",
        "radius": 0,
        "weight": 2,
        "default": False,
    },
    "relocated_leaks": {
        "label": "Relocated leak points",
        "source": "gpkg",
        "layer": "relocated_leaks",
        "kind": "relocated_point",
        "color": "#00a651",
        "radius": 4,
        "weight": 1,
        "default": True,
    },
    "relocated_trace_lines": {
        "label": "Original to relocated trace lines",
        "source": "gpkg",
        "layer": "relocated_leak_offset_lines",
        "kind": "trace_line",
        "color": "#d40000",
        "radius": 0,
        "weight": 2,
        "default": True,
    },
}
DATA = {}
BOUNDS = None
LOCK = threading.Lock()
SUPPLEMENTAL = None


def log(value):
    print(str(value), flush=True)


def clean_value(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (int, float, str, bool)):
        return value
    return str(value)


def norm_key(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    text = str(value).strip()
    text = text.removesuffix(".0")
    return text.upper()


def find_col(columns, candidates):
    lookup = {str(c).lower(): c for c in columns}
    for cand in candidates:
        if cand.lower() in lookup:
            return lookup[cand.lower()]
    for col in columns:
        low = str(col).lower().replace("_", "").replace(" ", "")
        for cand in candidates:
            c = cand.lower().replace("_", "").replace(" ", "")
            if c and c in low:
                return col
    return None




def material_color(family):
    return MATERIAL_COLORS.get(str(family or "OTHER").upper(), MATERIAL_COLORS["OTHER"])


def load_supplemental():
    if not SUPPLEMENTAL_CSV.exists():
        log(f"WARNING supplemental CSV missing: {SUPPLEMENTAL_CSV}")
        return pd.DataFrame()
    df = pd.read_csv(SUPPLEMENTAL_CSV, dtype=str, encoding_errors="ignore")
    key_col = find_col(
        df.columns, ["LeakNumber", "LMSLEAKNUMBER", "LMSLeakNumber", "Leak_Number"]
    )
    mat_col = find_col(
        df.columns, ["LeakMaterialType", "Material", "Leak Material Type"]
    )
    dia_col = find_col(df.columns, ["Diameter", "LeakDiameter", "NominalDiameter"])
    fac_col = find_col(df.columns, ["FacilityType", "Facility Type"])
    cond_col = find_col(df.columns, ["PipeCondition", "Pipe Condition"])
    if not key_col:
        log(
            "WARNING supplemental key column not found; initial leak popup will not be enriched."
        )
        return pd.DataFrame()
    out = pd.DataFrame()
    out["JoinLeakKey"] = df[key_col].map(norm_key)
    out["SuppLeakMaterialType"] = df[mat_col] if mat_col else None
    out["SuppDiameter"] = df[dia_col] if dia_col else None
    out["SuppFacilityType"] = df[fac_col] if fac_col else None
    out["SuppPipeCondition"] = df[cond_col] if cond_col else None
    out["SuppMaterialFamily"] = out["SuppLeakMaterialType"].map(material_family)
    out = out[out["JoinLeakKey"] != ""].drop_duplicates("JoinLeakKey", keep="first")
    log(f"Supplemental loaded for popup enrichment: {len(out):,} keyed records")
    return out


def enrich_historic_leaks(gdf):
    global SUPPLEMENTAL
    if SUPPLEMENTAL is None:
        SUPPLEMENTAL = load_supplemental()
    if SUPPLEMENTAL is None or len(SUPPLEMENTAL) == 0 or len(gdf) == 0:
        return gdf
    key_col = find_col(
        gdf.columns, ["LMSLEAKNUMBER", "LMSLeakNumber", "LeakNumber", "Leak_Number"]
    )
    if not key_col:
        log(
            "WARNING historic leak key not found; cannot enrich initial leak popup attributes."
        )
        return gdf
    gdf = gdf.copy()
    gdf["JoinLeakKey"] = gdf[key_col].map(norm_key)
    merged = gdf.merge(SUPPLEMENTAL, how="left", on="JoinLeakKey")
    matched = (
        merged["SuppLeakMaterialType"].notna().sum()
        if "SuppLeakMaterialType" in merged.columns
        else 0
    )
    log(
        f"Historic leak popup enrichment matched supplemental rows: {int(matched):,} of {len(merged):,}"
    )
    return merged


def decode_from_subtypes(gdf, layer_id, layer_name):
    """Name each pipe's material from ASSETGROUP + ASSETTYPE.

    ASSETTYPE is the material type and its subtype domain value is the material
    name. A downloaded cache carries both codes, and the committed service
    metadata carries the domains, so the decode needs no token and no enrichment
    pass. Returns a list of labels, or None when it cannot be done.

    This exists because the map used to require a cache that
    scripts/enrich_assettype_cache.py had already written ASSETTYPE_DECODED into.
    Without that pass every pipe fell to one colour - the same trap the workflow
    hit, where the codes were present and only the decode was missing.
    """
    if schema.ASSETGROUP not in gdf.columns or schema.ASSETTYPE not in gdf.columns:
        return None

    type_id_field, decoder = decoder_for_layer(layer_id)
    if not decoder:
        log(f"WARNING {layer_name}: no committed metadata for layer {layer_id} in "
            f"{config.REFERENCE_DIR}, so ASSETTYPE cannot be decoded.")
        return None

    labels = [
        decoder.get((norm_code(group), norm_code(asset_type)), {}).get("ASSETTYPE_DECODED")
        for group, asset_type in zip(gdf[schema.ASSETGROUP], gdf[schema.ASSETTYPE])
    ]
    named = sum(1 for label in labels if label)
    log(f"{layer_name}: decoded {named:,}/{len(labels):,} pipe materials from "
        f"{type_id_field or schema.ASSETGROUP} + {schema.ASSETTYPE}")
    return labels


# Which DNV layer each cached pipe frame came from, so its domains can be found.
PIPE_LAYER_IDS_BY_KEY = {
    "distribution_pipes": config.DISTRIBUTION_PIPE_LAYER_ID,
    "service_pipes": config.SERVICE_PIPE_LAYER_ID,
}


def add_pipe_material_fields(gdf, layer_name="pipes"):
    if len(gdf) == 0:
        return gdf
    gdf = gdf.copy()
    # These columns are written by scripts/enrich_assettype_cache.py, so they are
    # read by name. Resolving them by candidate list meant a missed guess silently
    # classified the wrong column - which is how "material" (the DNV Grade field
    # on an unenriched cache) ended up driving material families.
    has_raw = schema.ASSETTYPE in gdf.columns
    has_decoded = schema.ASSETTYPE_DECODED in gdf.columns

    if not has_raw:
        log(
            f"WARNING {layer_name}: no {schema.ASSETTYPE} column in this cache, so "
            f"{schema.PIPE_MATERIAL_RAW} will be empty. "
            f"Run: python scripts/enrich_assettype_cache.py"
        )

    # nominaldiameter comes from the DNV service, not from this project, so it is
    # still resolved by candidate list.
    dia_col = find_col(
        gdf.columns,
        ["nominaldiameter", "diameter", "MatchedPipeDiameter", "outsidediameter"],
    )

    gdf[schema.PIPE_MATERIAL_RAW] = gdf[schema.ASSETTYPE].map(clean_value) if has_raw else None

    if has_decoded:
        gdf["PipeMaterialDomain"] = gdf[schema.ASSETTYPE_DECODED].map(clean_value)
    else:
        # No enrichment pass on this cache. Decode the codes it does carry.
        labels = decode_from_subtypes(
            gdf, PIPE_LAYER_IDS_BY_KEY.get(layer_name), layer_name)
        if labels is None:
            log(f"WARNING {layer_name}: material cannot be named, so every pipe "
                f"will draw in the {assettype.UNCLASSIFIED_FAMILY} colour. "
                f"Run: python scripts/enrich_assettype_cache.py")
            gdf["PipeMaterialDomain"] = None
        else:
            gdf["PipeMaterialDomain"] = [clean_value(label) for label in labels]

    gdf["PipeMaterialFamily"] = gdf["PipeMaterialDomain"].map(material_family)

    # The column can also exist and be entirely null, when an enrichment run
    # matched no OBJECTIDs. That looks identical in the viewer, so report it.
    if has_raw and gdf[schema.PIPE_MATERIAL_RAW].isna().all():
        log(
            f"WARNING {layer_name}: {schema.ASSETTYPE} is present but empty for every "
            f"row, so {schema.PIPE_MATERIAL_RAW} is blank. The last enrichment matched "
            f"no OBJECTIDs. Run: python scripts/enrich_assettype_cache.py --force"
        )
    gdf["PipeMaterialColor"] = gdf["PipeMaterialFamily"].map(material_color)
    if dia_col:
        gdf["PipeDiameter"] = gdf[dia_col].map(clean_value)
    else:
        gdf["PipeDiameter"] = None
    return gdf


def limit_columns(gdf):
    keep = []
    for col in gdf.columns:
        if col == "geometry":
            keep.append(col)
            continue
        low = str(col).lower()
        if (
            any(t in low for t in KEEP_TOKENS)
            or str(col).startswith("Supp")
            or str(col).startswith("Pipe")
            or col == "JoinLeakKey"
        ):
            keep.append(col)
    if "geometry" not in keep:
        keep.append("geometry")
    if len(keep) <= 1:
        for col in list(gdf.columns)[:10]:
            if col != "geometry":
                keep.append(col)
    keep = list(dict.fromkeys(keep))
    out = gdf[keep].copy()
    for col in out.columns:
        if col == "geometry":
            continue
        if str(out[col].dtype).startswith("datetime"):
            out[col] = out[col].astype(str)
        elif str(out[col].dtype) == "object":
            out[col] = out[col].map(clean_value)
    return out


def read_cache(key):
    path = CACHE / (key + ".pkl.gz")
    if not path.exists():
        raise RuntimeError(f"Missing cache file: {path}")
    df = pd.read_pickle(path, compression="gzip")
    gdf = (
        df.copy()
        if isinstance(df, gpd.GeoDataFrame)
        else gpd.GeoDataFrame(df.copy(), geometry="geometry")
    )
    gdf = gdf[gdf.geometry.notna()].copy()
    gdf = gdf.set_crs(NATIVE_CRS, allow_override=True).to_crs(WGS84)
    if key == "historic_leaks":
        gdf = enrich_historic_leaks(gdf)
    if key in ["distribution_pipes", "service_pipes"]:
        gdf = add_pipe_material_fields(gdf, key)
    return limit_columns(gdf)


def read_gpkg(layer):
    if not OUTPUT_GPKG.exists():
        log(f"WARNING missing output GeoPackage: {OUTPUT_GPKG}")
        return gpd.GeoDataFrame(geometry=[], crs=WGS84)
    try:
        gdf = gpd.read_file(str(OUTPUT_GPKG), layer=layer)
    except Exception as ex:
        log(f"WARNING could not read {layer}: {ex}")
        return gpd.GeoDataFrame(geometry=[], crs=WGS84)
    gdf = gdf[gdf.geometry.notna()].copy()
    if gdf.crs is None:
        gdf = gdf.set_crs(NATIVE_CRS, allow_override=True)
    gdf = gdf.to_crs(WGS84)
    return limit_columns(gdf)


def load_all():
    global BOUNDS
    all_bounds = []
    for key, cfg in LAYERS.items():
        log(f"Loading layer: {key}")
        gdf = read_cache(key) if cfg["source"] == "cache" else read_gpkg(cfg["layer"])
        if len(gdf):
            _ = gdf.sindex
            all_bounds.append(gdf.total_bounds)
        DATA[key] = gdf
        log(f"  {key}: {len(gdf):,} features")
    if all_bounds:
        west = min(b[0] for b in all_bounds)
        south = min(b[1] for b in all_bounds)
        east = max(b[2] for b in all_bounds)
        north = max(b[3] for b in all_bounds)
        BOUNDS = {
            "west": float(west),
            "south": float(south),
            "east": float(east),
            "north": float(north),
            "center_lat": float((south + north) / 2),
            "center_lon": float((west + east) / 2),
        }
    else:
        BOUNDS = {
            "west": -72.5,
            "south": 41.0,
            "east": -69.5,
            "north": 43.0,
            "center_lat": 42.0,
            "center_lon": -71.0,
        }
    log(f"Bounds: {BOUNDS}")


def gdf_to_geojson(gdf):
    if len(gdf) == 0:
        return {"type": "FeatureCollection", "features": []}
    return json.loads(gdf.to_json(drop_id=True))


def select_bbox(key, west, south, east, north, simplify, max_features):
    gdf = DATA[key]
    if len(gdf) == 0:
        return gdf, False, 0
    sub = gdf.cx[west:east, south:north].copy()
    total = len(sub)
    truncated = False
    if max_features and total > max_features:
        sub = sub.head(max_features).copy()
        truncated = True
    is_line = LAYERS[key]["kind"] in ["pipe_line", "trace_line"]
    if simplify and is_line and len(sub):
        sub["geometry"] = sub.geometry.simplify(simplify, preserve_topology=True)
        sub = sub[sub.geometry.notna() & ~sub.geometry.is_empty].copy()
    return sub, truncated, total


LEAFLET_CDN = "https://unpkg.com/leaflet@1.9.4/dist"
LEAFLET_FILES = ("leaflet.css", "leaflet.js")

CONTENT_TYPES = {".css": "text/css", ".js": "application/javascript",
                 ".png": "image/png", ".svg": "image/svg+xml"}


def guess_content_type(name):
    return CONTENT_TYPES.get(os.path.splitext(name)[1].lower(),
                             "application/octet-stream")


def leaflet_dir():
    """The folder to serve /leaflet/ from, or None.

    The copy committed in the repository is preferred, so the map works with no
    internet and nothing built first. The work-root copy is still honoured for a
    machine that already has one.
    """
    for candidate in (config.VENDORED_LEAFLET_DIR, VENDOR):
        if all((candidate / name).exists() for name in LEAFLET_FILES):
            return candidate
    return None


def leaflet_refs():
    """Where the page should load Leaflet from.

    When no local copy exists this used to emit /leaflet/leaflet.js anyway, the
    request 404'd, and the page rendered as an empty white rectangle - the only
    clue being "L is not defined" in the browser console.
    """
    if leaflet_dir():
        return "/leaflet/leaflet.css", "/leaflet/leaflet.js"
    log(f"WARNING no local Leaflet in {config.VENDORED_LEAFLET_DIR} or {VENDOR}, "
        f"so the page will load it from unpkg.com. If this network blocks that, "
        f"the map will be blank.")
    return f"{LEAFLET_CDN}/leaflet.css", f"{LEAFLET_CDN}/leaflet.js"


def html_page():
    cfg_json = json.dumps(LAYERS)
    bounds_json = json.dumps(BOUNDS)
    parts = []
    parts.append('<!doctype html><html><head><meta charset="utf-8"/>')
    parts.append("<title>LeakRelocation DNV Material Leaflet Context</title>")
    css_ref, js_ref = leaflet_refs()
    parts.append(f'<link rel="stylesheet" href="{css_ref}"/>')
    parts.append(
        "<style>html,body{height:100%;width:100%;margin:0;padding:0;font-family:Arial,sans-serif}.info{background:white;padding:10px 12px;border:1px solid #777;border-radius:4px;font-size:13px;box-shadow:0 1px 5px rgba(0,0,0,.35);max-width:790px}.warn{color:#a94442;font-weight:bold}.leaflet-control-layers{max-height:72vh;overflow:auto}.legend-line{display:inline-block;width:24px;height:4px;margin-right:6px;vertical-align:middle}" + PANE_CSS + "</style>"
    )
    parts.append(
        '</head><body><div id="map"></div>' + PANE_HTML
        + f'<script src="{js_ref}"></script><script>' + PANE_JS
    )
    parts.append("const LAYER_CONFIG = " + cfg_json + ";")
    parts.append("const DATA_BOUNDS = " + bounds_json + ";")
    parts.append("const MAX_FEATURES=25000; const SIMPLIFY=0.000002;")
    parts.append(
        # Generated from the Python MATERIAL_COLORS above, not written out a
        # second time. The two used to be separate literals, so editing the
        # Python one - as commit 87453fb did, moving PLASTIC to yellow and STEEL
        # to blue - changed nothing on the map: this line is what paints the
        # pipes, and it still held the old values.
        "const MATERIAL_COLORS=" + json.dumps(MATERIAL_COLORS) + ";"
    )
    parts.append("const map=L.map('map',{preferCanvas:true});")
    parts.append(
        "const osm=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'});"
    )
    parts.append(
        "const esriImagery=L.tileLayer('https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxZoom:19,attribution:'Tiles &copy; Esri'});"
    )
    parts.append(
        "const esriTopo=L.tileLayer('https://services.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}',{maxZoom:19,attribution:'Tiles &copy; Esri'});"
    )
    parts.append(
        "osm.addTo(map); const groups={}; const active=new Set(); const status={};"
    )
    parts.append(
        "function esc(v){if(v===null||v===undefined)return '';return String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')}"
    )
    parts.append(
        "function bindPopup(feature,layer){const props=feature.properties||{};const ordered=['LMSLEAKNUMBER','LeakNumber','SuppLeakMaterialType','SuppDiameter','SuppFacilityType','SuppPipeCondition','SuppMaterialFamily','PipeMaterialDomain','PipeMaterialRaw','PipeMaterialFamily','PipeDiameter','OBJECTID','GlobalID','GLOBALID','DistanceToPipe','ConfidenceLevel','LinkedLayer','NearestPipeID','NearestPipeGlobalID'];let keys=[];for(const k of ordered){if(Object.prototype.hasOwnProperty.call(props,k))keys.push(k)}for(const k of Object.keys(props)){if(!keys.includes(k)&&keys.length<18)keys.push(k)}let rows='';for(const k of keys)rows+='<tr><th>'+esc(k)+'</th><td>'+esc(props[k])+'</td></tr>';if(!rows)rows='<tr><td>No attributes</td></tr>';layer.bindPopup('<table>'+rows+'</table>');layer.on('click',function(){AttributePane.selectFromMap(layer)})}"
    )
    parts.append(
        "function styleFor(k,feature){const c=LAYER_CONFIG[k];const p=(feature&&feature.properties)||{};if(c.kind==='pipe_line'){const fam=p.PipeMaterialFamily||'OTHER';return {color:MATERIAL_COLORS[fam]||MATERIAL_COLORS.OTHER,weight:c.weight||2,opacity:.82}}return {color:c.color,weight:c.weight||1,opacity:.75}}"
    )
    parts.append(
        "function pointToLayerFor(k){const c=LAYER_CONFIG[k];return function(feature,latlng){return L.circleMarker(latlng,{radius:c.radius||3,color:c.color,fillColor:c.color,fillOpacity:.72,weight:1})}}"
    )
    parts.append(
        "for(const key of Object.keys(LAYER_CONFIG)){groups[key]=L.layerGroup();if(LAYER_CONFIG[key].default){groups[key].addTo(map);active.add(key)}AttributePane.register(key,LAYER_CONFIG[key].label,groups[key])}"
    )
    parts.append(
        "const extentBox=L.rectangle([[DATA_BOUNDS.south,DATA_BOUNDS.west],[DATA_BOUNDS.north,DATA_BOUNDS.east]],{color:'#d40000',weight:2,fill:false,dashArray:'8,6'}).addTo(map); const centerMarker=L.marker([DATA_BOUNDS.center_lat,DATA_BOUNDS.center_lon]).addTo(map);"
    )
    parts.append(
        "const baseMaps={'OpenStreetMap street map':osm,'Esri World Imagery':esriImagery,'Esri World Topographic':esriTopo}; const overlays={}; for(const key of Object.keys(LAYER_CONFIG))overlays[LAYER_CONFIG[key].label]=groups[key]; overlays['Red data extent box']=extentBox; overlays['Extent center marker']=centerMarker; L.control.layers(baseMaps,overlays,{collapsed:false}).addTo(map); L.control.scale({imperial:true,metric:true}).addTo(map);"
    )
    parts.append(
        "map.fitBounds([[DATA_BOUNDS.south,DATA_BOUNDS.west],[DATA_BOUNDS.north,DATA_BOUNDS.east]],{padding:[24,24]});"
    )
    parts.append(
        "async function refreshLayer(key){if(!active.has(key))return; const b=map.getBounds(); const url='/api/layer?name='+encodeURIComponent(key)+'&west='+b.getWest()+'&south='+b.getSouth()+'&east='+b.getEast()+'&north='+b.getNorth()+'&simplify='+SIMPLIFY+'&max='+MAX_FEATURES; const r=await fetch(url); const payload=await r.json(); groups[key].clearLayers(); L.geoJSON(payload.geojson,{pointToLayer:pointToLayerFor(key),style:function(f){return styleFor(key,f)},onEachFeature:bindPopup}).addTo(groups[key]); status[key]=payload; updateInfo(); AttributePane.build();}"
    )
    parts.append(
        "function refreshActive(){for(const key of Array.from(active))refreshLayer(key)} map.on('overlayadd',function(e){for(const key of Object.keys(groups)){if(groups[key]===e.layer){active.add(key);refreshLayer(key)}}}); map.on('overlayremove',function(e){for(const key of Object.keys(groups)){if(groups[key]===e.layer){active.delete(key);groups[key].clearLayers();delete status[key];updateInfo();AttributePane.build()}}}); map.on('moveend zoomend',refreshActive);"
    )
    parts.append(
        "const info=L.control({position:'bottomleft'}); info.onAdd=function(){const div=L.DomUtil.create('div','info');div.id='infoBox';return div}; info.addTo(map);"
    )
    parts.append(
        'function updateInfo(){const div=document.getElementById(\'infoBox\');let html=\'<b>LeakRelocation DNV material-coded Leaflet context</b><br/>Initial leaks are enriched from Supplemental CSV: material, diameter, facility type, pipe condition.<br/>Pipelines are colored by material family.<br/><span class="legend-line" style="background:#00b050"></span>Plastic <span class="legend-line" style="background:#404040;margin-left:8px"></span>Steel <span class="legend-line" style="background:#8b4513;margin-left:8px"></span>Iron <span class="legend-line" style="background:#b87333;margin-left:8px"></span>Copper <span class="legend-line" style="background:#999999;margin-left:8px"></span>Unknown <span class="legend-line" style="background:#7b68ee;margin-left:8px"></span>Other<br/><br/>\'; for(const key of active){const s=status[key]; if(s){html+=LAYER_CONFIG[key].label+\': shown \'+s.returned.toLocaleString()+\' of \'+s.total_in_view.toLocaleString()+\' in viewport\'; if(s.truncated)html+=\' <span class="warn">TRUNCATED - zoom in</span>\'; html+=\'<br/>\'}}div.innerHTML=html}'
    )
    parts.append("setTimeout(refreshActive,250);</script></body></html>")
    return "\n".join(parts)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path in ["/", "/index.html"]:
                self.send_text(html_page(), "text/html")
            elif parsed.path.startswith("/leaflet/"):
                folder = leaflet_dir()
                if folder is None:
                    self.send_error(404, "No local Leaflet")
                    return
                # Resolved and checked against the folder, so a crafted path
                # cannot walk out of it. Subpaths matter: leaflet.css asks for
                # images/layers.png and images/marker-icon.png.
                relative = parsed.path[len("/leaflet/"):]
                path = (folder / relative).resolve()
                if folder.resolve() not in path.parents or not path.is_file():
                    self.send_error(404, "Missing Leaflet asset")
                    return
                self.send_bytes(path.read_bytes(), guess_content_type(path.name))
            elif parsed.path == "/api/layer":
                q = parse_qs(parsed.query)
                name = q.get("name", [""])[0]
                if name not in DATA:
                    self.send_error(400, "Unknown layer")
                    return
                west = float(q.get("west", [BOUNDS["west"]])[0])
                south = float(q.get("south", [BOUNDS["south"]])[0])
                east = float(q.get("east", [BOUNDS["east"]])[0])
                north = float(q.get("north", [BOUNDS["north"]])[0])
                simplify = float(q.get("simplify", [SIMPLIFY_DEFAULT])[0])
                max_features = int(float(q.get("max", [MAX_FEATURES_DEFAULT])[0]))
                with LOCK:
                    sub, truncated, total = select_bbox(
                        name, west, south, east, north, simplify, max_features
                    )
                    gj = gdf_to_geojson(sub)
                self.send_text(
                    json.dumps(
                        {
                            "name": name,
                            "returned": len(sub),
                            "total_in_view": total,
                            "truncated": truncated,
                            "geojson": gj,
                        },
                        ensure_ascii=False,
                    ),
                    "application/json",
                )
            else:
                self.send_error(404, "Not found")
        except Exception as ex:
            self.send_text(json.dumps({"error": str(ex)}), "application/json", 500)

    def send_text(self, text, ctype, status=200):
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_bytes(self, data, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        return


def serve(port=PORT, open_browser=False):
    """Load the layers and serve the map until interrupted.

    A function rather than module-level code so run.py can start it; it used to
    run only under __main__.
    """
    load_all()
    url = f"http://{HOST}:{port}/"
    log(f"OPEN THIS URL: {url}")
    log("Press Ctrl+C in this window to stop the server.")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    with ThreadingHTTPServer((HOST, port), Handler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            log("Stopped.")


if __name__ == "__main__":
    serve()
