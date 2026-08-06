"""HTML template for the local relocation viewer.

Kept out of the generator script so the committed snapshot in
viewer/index_basic.html can be rendered and checked by a test instead of
drifting from the code that produces it.
"""
# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member. spec_from_file_location gives a module no parent
# package, and a relative import then fails with "attempted relative import with
# no known parent package".
import os as _os
import sys as _sys

_PACKAGE_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PACKAGE_PARENT not in _sys.path:
    _sys.path.insert(0, _PACKAGE_PARENT)

from leakrelocation.viewer_pane import PANE_CSS, PANE_HTML, PANE_JS

TEMPLATE = """\
<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<title>LeakRelocation Production Output Viewer</title>
<link rel="stylesheet" href="__CSS_REF__"/>
<style>
.info{background:white;padding:10px 12px;border:1px solid #777;border-radius:4px;font-size:13px;box-shadow:0 1px 5px rgba(0,0,0,.35);max-width:620px}.legend-row{margin:3px 0}.swatch{display:inline-block;width:18px;height:10px;margin-right:6px;border:1px solid #555}.leaflet-popup-content table{border-collapse:collapse;font-size:12px}.leaflet-popup-content th,.leaflet-popup-content td{border:1px solid #ccc;padding:3px 5px;vertical-align:top}.leaflet-popup-content th{background:#f4f4f4}
__PANE_CSS__
</style>
</head>
<body>
<div id="map"></div>
__PANE_HTML__
<script src="__JS_REF__"></script>
<script>
const map=L.map('map',{preferCanvas:true});
const osm=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:20,attribution:'&copy; OpenStreetMap contributors'}).addTo(map);
const imagery=L.tileLayer('https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{maxZoom:20,attribution:'Tiles &copy; Esri'});
const topo=L.tileLayer('https://services.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}',{maxZoom:20,attribution:'Tiles &copy; Esri'});
map.fitBounds([[__SOUTH__,__WEST__],[__NORTH__,__EAST__]],{padding:[24,24]});
function esc(v){if(v===null||v===undefined)return '';return String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')}
function materialFamily(mat){const toks=(mat||'').toString().toUpperCase().split(/[^A-Z0-9]+/).filter(Boolean);if(!toks.length)return 'UNKNOWN';const hit=(terms)=>terms.some(t=>{const p=t.split(' ');if(p.length>1){for(let i=0;i+p.length<=toks.length;i++){if(p.every((x,j)=>toks[i+j]===x))return true}return false}return toks.includes(t)||(t.length>=4&&toks.some(k=>k.startsWith(t)))});if(hit(['PLASTIC','POLY','PE','PVC','ABS','HDPE','MDPE','POLYBUTYLENE']))return 'PLASTIC';if(hit(['CAST IRON','DUCTILE','WROUGHT','IRON','CI','WI']))return 'IRON';if(hit(['STEEL','GALVANIZED','BARE','COATED','ST']))return 'STEEL';if(hit(['COPPER','CU']))return 'COPPER';if(hit(['UNKNOWN','UNK','COMPOSITE','UN']))return 'UNKNOWN';return 'OTHER'}
function materialColor(mat){const fam=materialFamily(mat);return {PLASTIC:'#00a651',STEEL:'#4d4d4d',IRON:'#8b4513',COPPER:'#b87333',UNKNOWN:'#999999',OTHER:'#7b68ee'}[fam]||'#7b68ee'}
function layerColor(linkedLayer){const text=(linkedLayer||'').toString().toLowerCase();if(text.includes('distribution'))return '#0066cc';if(text.includes('service'))return '#00a651';return '#d40000'}
function popupHtml(props){let rows='';const order=['LeakKey','OrigLeakOID','LinkedLayer','MatchedPipeOID','MatchedPipeGID','DistanceFt','SearchRadiusFt','MatchMaterial','MatchDiameter','MatchPressure'];const keys=[];for(const k of order)if(Object.prototype.hasOwnProperty.call(props,k))keys.push(k);for(const k of Object.keys(props))if(!keys.includes(k)&&keys.length<18)keys.push(k);for(const k of keys)rows+='<tr><th>'+esc(k)+'</th><td>'+esc(props[k])+'</td></tr>';return '<table>'+rows+'</table>'}
function bindFeature(f,l){l.bindPopup(popupHtml(f.properties||{}));l.on('click',()=>AttributePane.selectFromMap(l))}
const traceLayer=L.geoJSON(null,{style:f=>{const p=f.properties||{};return {color:layerColor(p.LinkedLayer),weight:2,opacity:.65}},onEachFeature:bindFeature});
const leakLayer=L.geoJSON(null,{pointToLayer:(f,latlng)=>{const p=f.properties||{};const c=materialColor(p.MatchMaterial);return L.circleMarker(latlng,{radius:4,color:'#222',weight:1,fillColor:c,fillOpacity:.82})},onEachFeature:bindFeature});
Promise.all([fetch('relocated_leaks.geojson').then(r=>r.json()),fetch('relocated_leak_trace_lines.geojson').then(r=>r.json())]).then(([leaks,traces])=>{traceLayer.addData(traces).addTo(map);leakLayer.addData(leaks).addTo(map);updateInfo(leaks.features.length,traces.features.length);AttributePane.register('leaks','Relocated leaks',leakLayer);AttributePane.register('traces','Trace lines',traceLayer);AttributePane.build()}).catch(err=>{alert('Failed to load local GeoJSON. Serve this folder with serve_viewer.py. '+err)});
L.control.layers({'OpenStreetMap':osm,'Esri imagery':imagery,'Esri topo':topo},{'Relocated leaks':leakLayer,'Trace lines':traceLayer},{collapsed:false}).addTo(map);L.control.scale({imperial:true,metric:true}).addTo(map);
__PANE_JS__
const info=L.control({position:'bottomleft'});info.onAdd=function(){const div=L.DomUtil.create('div','info');div.id='info';return div};info.addTo(map);
function updateInfo(pointCount,lineCount){document.getElementById('info').innerHTML='<b>LeakRelocation production output</b><br/>relocated_leaks: '+pointCount.toLocaleString()+'<br/>relocated_leak_offset_lines: '+lineCount.toLocaleString()+'<br/><div class="legend-row"><span class="swatch" style="background:#00a651"></span>Plastic / Service</div><div class="legend-row"><span class="swatch" style="background:#4d4d4d"></span>Steel</div><div class="legend-row"><span class="swatch" style="background:#8b4513"></span>Iron</div><div class="legend-row"><span class="swatch" style="background:#0066cc"></span>Distribution trace</div><div class="legend-row"><span class="swatch" style="background:#999999"></span>Unknown</div>'}
</script>
</body>
</html>"""


def render(css_ref, js_ref, south, west, north, east):
    """Render the viewer HTML with the attribute pane embedded."""
    return (TEMPLATE
            .replace("__PANE_CSS__", PANE_CSS)
            .replace("__PANE_HTML__", PANE_HTML)
            .replace("__PANE_JS__", PANE_JS)
            .replace("__CSS_REF__", css_ref)
            .replace("__JS_REF__", js_ref)
            .replace("__SOUTH__", str(south))
            .replace("__WEST__", str(west))
            .replace("__NORTH__", str(north))
            .replace("__EAST__", str(east)))
