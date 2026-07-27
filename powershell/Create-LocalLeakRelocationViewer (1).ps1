
$ErrorActionPreference = "Stop"

$Root = Join-Path $env:USERPROFILE "Downloads\LeakRelocation-GeoPandas"
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$ViewDir = Join-Path $Root "production_moved_leak_outputs\local_leaflet_view"
$BuildPy = Join-Path $Root "build_local_relocation_viewer.py"
$BuildLog = Join-Path $Root ("BUILD_local_relocation_viewer_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".log")

if (!(Test-Path $Root)) { throw "Root folder not found: $Root" }
if (!(Test-Path $Py)) { throw "Python venv not found: $Py" }
if (!(Test-Path $ViewDir)) { New-Item -ItemType Directory -Path $ViewDir | Out-Null }

@'
from pathlib import Path
import shutil
import pandas as pd
import geopandas as gpd

ROOT = Path.home() / "Downloads" / "LeakRelocation-GeoPandas"
PROJECT = Path(r"\\ngusnasnwh001\gasne\GasNE Shared\Shared\ENG\Complex Team\GIS AutoPrint\Distribution Leak Relocation")
GPKG = PROJECT / "HistoricLeakRelocation.gpkg"
OUT = ROOT / "production_moved_leak_outputs" / "local_leaflet_view"
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

html = '''<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<title>LeakRelocation Production Output Viewer</title>
<link rel="stylesheet" href="__CSS_REF__"/>
<style>
html,body,#map{height:100%;width:100%;margin:0;padding:0;font-family:Arial,sans-serif}.info{background:white;padding:10px 12px;border:1px solid #777;border-radius:4px;font-size:13px;box-shadow:0 1px 5px rgba(0,0,0,.35);max-width:620px}.legend-row{margin:3px 0}.swatch{display:inline-block;width:18px;height:10px;margin-right:6px;border:1px solid #555}table{border-collapse:collapse;font-size:12px}th,td{border:1px solid #ccc;padding:3px 5px;vertical-align:top}th{background:#f4f4f4}
</style>
</head>
<body>
<div id="map"></div>
<script src="__JS_REF__"></script>
<script>
const map=L.map('map',{preferCanvas:true});
const osm=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:20,attribution:'&copy; OpenStreetMap contributors'}).addTo(map);
const imagery=L.tileLayer('https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxZoom:20,attribution:'Tiles &copy; Esri'});
const topo=L.tileLayer('https://services.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}',{maxZoom:20,attribution:'Tiles &copy; Esri'});
map.fitBounds([[__SOUTH__,__WEST__],[__NORTH__,__EAST__]],{padding:[24,24]});
function esc(v){if(v===null||v===undefined)return '';return String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')}
function materialFamily(mat){const text=(mat||'').toString().toUpperCase();if(text.includes('PLASTIC')||text.includes(' PE')||text==='PE'||text.includes('PVC')||text.includes('ABS')||text.includes('POLY'))return 'PLASTIC';if(text.includes('STEEL')||text==='ST'||text.includes('COATED')||text.includes('BARE')||text.includes('GALVANIZED'))return 'STEEL';if(text.includes('IRON')||text==='CI'||text==='WI')return 'IRON';if(text.includes('COPPER')||text==='CU')return 'COPPER';if(text.includes('UNK')||text.includes('UNKNOWN')||text==='UN'||text==='')return 'UNKNOWN';return 'OTHER'}
function materialColor(mat){const fam=materialFamily(mat);return {PLASTIC:'#00a651',STEEL:'#4d4d4d',IRON:'#8b4513',COPPER:'#b87333',UNKNOWN:'#999999',OTHER:'#7b68ee'}[fam]||'#7b68ee'}
function layerColor(linkedLayer){const text=(linkedLayer||'').toString().toLowerCase();if(text.includes('distribution'))return '#0066cc';if(text.includes('service'))return '#00a651';return '#d40000'}
function popupHtml(props){let rows='';const order=['LeakKey','OrigLeakOID','LinkedLayer','MatchedPipeOID','MatchedPipeGID','DistanceFt','SearchRadiusFt','MatchMaterial','MatchDiameter','MatchPressure'];const keys=[];for(const k of order)if(Object.prototype.hasOwnProperty.call(props,k))keys.push(k);for(const k of Object.keys(props))if(!keys.includes(k)&&keys.length<18)keys.push(k);for(const k of keys)rows+='<tr><th>'+esc(k)+'</th><td>'+esc(props[k])+'</td></tr>';return '<table>'+rows+'</table>'}
const traceLayer=L.geoJSON(null,{style:f=>{const p=f.properties||{};return {color:layerColor(p.LinkedLayer),weight:2,opacity:.65}},onEachFeature:(f,l)=>l.bindPopup(popupHtml(f.properties||{}))});
const leakLayer=L.geoJSON(null,{pointToLayer:(f,latlng)=>{const p=f.properties||{};const c=materialColor(p.MatchMaterial);return L.circleMarker(latlng,{radius:4,color:'#222',weight:1,fillColor:c,fillOpacity:.82})},onEachFeature:(f,l)=>l.bindPopup(popupHtml(f.properties||{}))});
Promise.all([fetch('relocated_leaks.geojson').then(r=>r.json()),fetch('relocated_leak_trace_lines.geojson').then(r=>r.json())]).then(([leaks,traces])=>{traceLayer.addData(traces).addTo(map);leakLayer.addData(leaks).addTo(map);updateInfo(leaks.features.length,traces.features.length)}).catch(err=>{alert('Failed to load local GeoJSON. Use the included local HTTP server PS1. '+err)});
L.control.layers({'OpenStreetMap':osm,'Esri imagery':imagery,'Esri topo':topo},{'Relocated leaks':leakLayer,'Trace lines':traceLayer},{collapsed:false}).addTo(map);L.control.scale({imperial:true,metric:true}).addTo(map);
const info=L.control({position:'bottomleft'});info.onAdd=function(){const div=L.DomUtil.create('div','info');div.id='info';return div};info.addTo(map);
function updateInfo(pointCount,lineCount){document.getElementById('info').innerHTML='<b>LeakRelocation production output</b><br/>relocated_leaks: '+pointCount.toLocaleString()+'<br/>relocated_leak_offset_lines: '+lineCount.toLocaleString()+'<br/><div class="legend-row"><span class="swatch" style="background:#00a651"></span>Plastic / Service</div><div class="legend-row"><span class="swatch" style="background:#4d4d4d"></span>Steel</div><div class="legend-row"><span class="swatch" style="background:#8b4513"></span>Iron</div><div class="legend-row"><span class="swatch" style="background:#0066cc"></span>Distribution trace</div><div class="legend-row"><span class="swatch" style="background:#999999"></span>Unknown</div>'}
</script>
</body>
</html>'''
html = html.replace('__CSS_REF__', css_ref).replace('__JS_REF__', js_ref).replace('__SOUTH__', str(south)).replace('__WEST__', str(west)).replace('__NORTH__', str(north)).replace('__EAST__', str(east))
html_path = OUT / "index.html"
html_path.write_text(html, encoding="utf-8")
server_ps1 = OUT / "Start-LocalRelocationViewer.ps1"
server_ps1.write_text(r'''$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path $env:USERPROFILE "Downloads\LeakRelocation-GeoPandas\.venv\Scripts\python.exe"
if (!(Test-Path $Py)) { throw "Python venv not found: $Py" }
Set-Location $Here
Start-Process "http://127.0.0.1:8777/index.html"
& $Py -m http.server 8777 --bind 127.0.0.1
''', encoding="utf-8")
print("HTML viewer:", html_path, flush=True)
print("Start server PS1:", server_ps1, flush=True)
print("Run:", f'powershell -NoProfile -ExecutionPolicy Bypass -File "{server_ps1}"', flush=True)
'@ | Set-Content -Path $BuildPy -Encoding UTF8

Write-Host "Building local LeakRelocation viewer..." -ForegroundColor Cyan
cmd /c "`"$Py`" `"$BuildPy`" 2>&1" | Tee-Object -FilePath $BuildLog
$ExitCode = $LASTEXITCODE
Write-Host ""
Write-Host "Build ExitCode: $ExitCode" -ForegroundColor Cyan
Write-Host "Build log: $BuildLog" -ForegroundColor Cyan
if ($ExitCode -ne 0) {
    Get-Content $BuildLog -Raw | Set-Clipboard
    throw "Local viewer build failed. Log copied to clipboard."
}

$Index = Join-Path $ViewDir "index.html"
$ServerPs1 = Join-Path $ViewDir "Start-LocalRelocationViewer.ps1"
Write-Host ""
Write-Host "Viewer files created:" -ForegroundColor Green
Write-Host $ViewDir -ForegroundColor Green
Write-Host "Opening local viewer..." -ForegroundColor Cyan
Start-Process powershell.exe -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$ServerPs1`"")
Start-Sleep -Seconds 1
Start-Process "http://127.0.0.1:8777/index.html"
$Clip = @(
    "=== Local LeakRelocation Viewer Created ===",
    "ViewerFolder: $ViewDir",
    "IndexHtml: $Index",
    "StartServer: $ServerPs1",
    "URL: http://127.0.0.1:8777/index.html",
    "BuildLog: $BuildLog"
) -join "`r`n"
$Clip | Set-Clipboard
$Clip
