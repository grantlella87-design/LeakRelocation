$ErrorActionPreference = "Stop"
$ScriptPath = "\\ngusnasnwh001\gasne\GasNE Shared\Shared\ENG\Complex Team\GIS AutoPrint\Distribution Leak Relocation\Arcpy Code\leak reolcation - geopandas.py"
$BackupPath = "$ScriptPath.$(Get-Date -Format yyyyMMdd_HHmmss).before_native_crs_patch.bak"
Copy-Item -Path $ScriptPath -Destination $BackupPath -Force
Write-Host "Backup created:" -ForegroundColor Green
Write-Host $BackupPath -ForegroundColor Green
$Text = Get-Content -Path $ScriptPath -Raw
$Helper = @'

# === Native CRS patch: per-layer MapServer spatial reference ===
# Historic leaks, distribution pipes, and service pipes do not share one CRS.
# Do not force these layers to EPSG:2249 before matching.
from pyproj import CRS

LAYER_NATIVE_CRS_CONFIG = {
    "historic_leaks": {
        "url": "https://gis.nationalgrid.com/un/rest/services/Leaks/MapServer/0",
        "expected_name_contains": "NG_Equidistant_Conic"
    },
    "distribution_pipes": {
        "url": "https://gis.nationalgrid.com/arcgis/rest/services/MA/Material_View_MA/MapServer/341",
        "expected_name_contains": "NG_Equidistant_Conic_USft"
    },
    "service_pipes": {
        "url": "https://gis.nationalgrid.com/arcgis/rest/services/MA/Material_View_MA/MapServer/54",
        "expected_name_contains": "NG_Equidistant_Conic_USft"
    }
}

ANALYSIS_CRS_LAYER_KEY = "distribution_pipes"
_NATIVE_CRS_CACHE = {}


def _spatial_reference_from_layer_json(layer_json):
    sr = layer_json.get("extent", {}).get("spatialReference") or layer_json.get("spatialReference") or {}
    if sr.get("wkt"):
        return CRS.from_wkt(sr["wkt"]), sr
    wkid = sr.get("latestWkid") or sr.get("wkid")
    if wkid:
        return CRS.from_epsg(int(wkid)), sr
    raise RuntimeError("Layer metadata did not include a usable spatialReference: {0}".format(sr))


def get_layer_native_crs(session, layer_key):
    if layer_key in _NATIVE_CRS_CACHE:
        return _NATIVE_CRS_CACHE[layer_key]
    if layer_key not in LAYER_NATIVE_CRS_CONFIG:
        raise KeyError("Unknown layer_key for native CRS lookup: {0}".format(layer_key))
    cfg = LAYER_NATIVE_CRS_CONFIG[layer_key]
    layer_json = request_json(session, cfg["url"], {"f": "json"})
    crs, sr = _spatial_reference_from_layer_json(layer_json)
    expected = cfg.get("expected_name_contains", "")
    crs_text = " ".join([str(crs.name), crs.to_wkt()[:500]])
    if expected and expected not in crs_text:
        print("WARNING: {0} CRS did not contain expected text [{1}]. CRS name=[{2}]".format(layer_key, expected, crs.name), flush=True)
    _NATIVE_CRS_CACHE[layer_key] = crs
    print("{0}: native CRS assigned from MapServer: {1}".format(layer_key, crs.name), flush=True)
    return crs


def assign_native_crs(session, layer_key, gdf):
    crs = get_layer_native_crs(session, layer_key)
    if gdf is None or len(gdf) == 0:
        return gdf
    existing = getattr(gdf, "crs", None)
    if existing is None:
        print("{0}: cache/dataframe CRS was empty; assigning native CRS {1}".format(layer_key, crs.name), flush=True)
    else:
        print("{0}: overriding dataframe CRS [{1}] with native CRS [{2}]".format(layer_key, existing, crs.name), flush=True)
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
        print("{0}: projecting from [{1}] to analysis CRS [{2}]".format(layer_key, gdf.crs, analysis.name), flush=True)
        return gdf.to_crs(analysis)
    print("{0}: already in analysis CRS [{1}]".format(layer_key, analysis.name), flush=True)
    return gdf


def normalize_loaded_layers_to_analysis_crs(session, historic_leaks, distribution_pipes, service_pipes):
    historic_leaks = assign_native_crs(session, "historic_leaks", historic_leaks)
    distribution_pipes = assign_native_crs(session, "distribution_pipes", distribution_pipes)
    service_pipes = assign_native_crs(session, "service_pipes", service_pipes)
    historic_leaks = to_analysis_crs(session, "historic_leaks", historic_leaks)
    distribution_pipes = to_analysis_crs(session, "distribution_pipes", distribution_pipes)
    service_pipes = to_analysis_crs(session, "service_pipes", service_pipes)
    analysis_crs = get_analysis_crs(session)
    print("Analysis CRS for all spatial matching: {0}".format(analysis_crs.name), flush=True)
    return historic_leaks, distribution_pipes, service_pipes, analysis_crs
# === End native CRS patch ===
'@
if ($Text -notmatch "Native CRS patch: per-layer MapServer spatial reference") {
    $InsertAfter = [regex]::Match($Text, "(?ms)^(import .+?\r?\n(?:import .+?\r?\n|from .+? import .+?\r?\n)*)")
    if ($InsertAfter.Success) {
        $Text = $Text.Insert($InsertAfter.Index + $InsertAfter.Length, $Helper)
    } else {
        $Text = $Helper + "`r`n" + $Text
    }
}
$Text = $Text -replace 'TARGET_CRS\s*=\s*["'']EPSG:2249["'']', 'TARGET_CRS = None  # Native CRS patch: do not force EPSG:2249 before matching'
Set-Content -Path $ScriptPath -Value $Text -Encoding UTF8
Write-Host "Inserted native CRS helper block and disabled EPSG:2249 target assignment." -ForegroundColor Green
Write-Host "Now add this call immediately after the three source layers are loaded and before any spatial index/distance/nearest matching:" -ForegroundColor Yellow
Write-Host 'historic_leaks, distribution_pipes, service_pipes, ANALYSIS_CRS = normalize_loaded_layers_to_analysis_crs(session, historic_leaks, distribution_pipes, service_pipes)' -ForegroundColor Cyan
Write-Host "Script patched:" -ForegroundColor Green
Write-Host $ScriptPath -ForegroundColor Green
