
$ErrorActionPreference = "Stop"
$Root = Join-Path $env:USERPROFILE "Downloads\LeakRelocation-GeoPandas"
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (!(Test-Path $Py)) { throw "Python venv not found: $Py" }
$AuditPy = Join-Path $Root "audit_production_relocation_match_material.py"
$AuditLog = Join-Path $Root ("AUDIT_production_match_material_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".log")
@'
from pathlib import Path
import pandas as pd
import geopandas as gpd
ROOT = Path.home() / "Downloads" / "LeakRelocation-GeoPandas"
CACHE = ROOT / "layer_cache"
OUT = ROOT / "production_moved_leak_outputs"
OUT.mkdir(parents=True, exist_ok=True)
GPKG = Path(r"\\ngusnasnwh001\gasne\GasNE Shared\Shared\ENG\Complex Team\GIS AutoPrint\Distribution Leak Relocation\HistoricLeakRelocation.gpkg")
print("=== Audit relocated MatchMaterial against corrected pipe caches ===", flush=True)
print("GeoPackage:", GPKG, flush=True)
relocated = gpd.read_file(GPKG, layer="relocated_leaks")
relocated_df = pd.DataFrame(relocated.drop(columns="geometry"))
print("relocated rows:", len(relocated_df), flush=True)
print("relocated columns:", list(relocated_df.columns), flush=True)
needed_cols = [
    "OBJECTID",
    "material",
    "GradeMaterial",
    "ASSETGROUP",
    "ASSETTYPE",
    "ASSETGROUP_DECODED",
    "ASSETTYPE_DECODED",
    "PipeMaterialRaw",
    "PipeMaterialFamily",
    "nominaldiameter",
    "operatingpressure",
    "GLOBALID"
]
def load_pipe_cache(name):
    path = CACHE / (name + ".pkl.gz")
    df = pd.read_pickle(path, compression="gzip")
    keep = [c for c in needed_cols if c in df.columns]
    out = df[keep].copy()
    out["OBJECTID"] = pd.to_numeric(out["OBJECTID"], errors="coerce").astype("Int64")
    return out
pipe_by_layer = {
    "distribution": load_pipe_cache("distribution_pipes"),
    "service": load_pipe_cache("service_pipes")
}
rows = []
for layer_name, subset in relocated_df.groupby("LinkedLayer", dropna=False):
    layer_key = str(layer_name).lower()
    pipe_df = pipe_by_layer.get(layer_key)
    if pipe_df is None:
        temp = subset.copy()
        temp["AuditStatus"] = "No pipe cache for LinkedLayer"
        rows.append(temp)
        continue
    left = subset.copy()
    left["MatchedPipeOID_INT"] = pd.to_numeric(left["MatchedPipeOID"], errors="coerce").astype("Int64")
    merged = left.merge(pipe_df.add_prefix("PipeCache_"), how="left", left_on="MatchedPipeOID_INT", right_on="PipeCache_OBJECTID")
    rows.append(merged)
audit = pd.concat(rows, ignore_index=True)
for col in ["MatchMaterial", "PipeCache_material", "PipeCache_ASSETTYPE_DECODED", "PipeCache_PipeMaterialRaw"]:
    if col in audit.columns:
        audit[col + "_NORM"] = audit[col].astype(str).str.strip().str.upper().replace({"NAN": "", "NONE": ""})
if "PipeCache_ASSETTYPE_DECODED_NORM" in audit.columns and "MatchMaterial_NORM" in audit.columns:
    audit["MatchMaterial_Equals_PipeCache_ASSETTYPE_DECODED"] = audit["MatchMaterial_NORM"] == audit["PipeCache_ASSETTYPE_DECODED_NORM"]
else:
    audit["MatchMaterial_Equals_PipeCache_ASSETTYPE_DECODED"] = False
if "PipeCache_material_NORM" in audit.columns and "MatchMaterial_NORM" in audit.columns:
    audit["MatchMaterial_Equals_PipeCache_material"] = audit["MatchMaterial_NORM"] == audit["PipeCache_material_NORM"]
else:
    audit["MatchMaterial_Equals_PipeCache_material"] = False
short_code_mask = audit["MatchMaterial"].astype(str).str.fullmatch(r"[A-Za-z0-9]{1,3}", na=False)
audit["MatchMaterial_LooksLike_OldGradeCode"] = short_code_mask
out_csv = OUT / "relocated_leaks_pipe_material_audit.csv"
audit.to_csv(out_csv, index=False)
print("audit CSV:", out_csv, flush=True)
print("", flush=True)
print("Rows by LinkedLayer:", flush=True)
print(audit["LinkedLayer"].value_counts(dropna=False).to_string(), flush=True)
print("", flush=True)
print("MatchMaterial top values:", flush=True)
print(audit["MatchMaterial"].value_counts(dropna=False).head(40).to_string(), flush=True)
print("", flush=True)
if "PipeCache_ASSETTYPE_DECODED" in audit.columns:
    print("PipeCache_ASSETTYPE_DECODED top values:", flush=True)
    print(audit["PipeCache_ASSETTYPE_DECODED"].value_counts(dropna=False).head(40).to_string(), flush=True)
    print("", flush=True)
print("MatchMaterial looks like old short grade/code:", int(audit["MatchMaterial_LooksLike_OldGradeCode"].sum()), "of", len(audit), flush=True)
print("MatchMaterial equals PipeCache ASSETTYPE decoded:", int(audit["MatchMaterial_Equals_PipeCache_ASSETTYPE_DECODED"].sum()), "of", len(audit), flush=True)
print("MatchMaterial equals PipeCache material:", int(audit["MatchMaterial_Equals_PipeCache_material"].sum()), "of", len(audit), flush=True)
print("", flush=True)
show_cols = [c for c in [
    "OrigLeakOID", "LeakKey", "LinkedLayer", "MatchedPipeOID", "DistanceFt", "MatchMaterial",
    "PipeCache_material", "PipeCache_ASSETTYPE_DECODED", "PipeCache_PipeMaterialFamily", "PipeCache_GradeMaterial",
    "MatchDiameter", "PipeCache_nominaldiameter", "MatchPressure", "PipeCache_operatingpressure"
] if c in audit.columns]
print("Sample rows where MatchMaterial differs from corrected ASSETTYPE decoded:", flush=True)
if "MatchMaterial_Equals_PipeCache_ASSETTYPE_DECODED" in audit.columns:
    sample = audit[~audit["MatchMaterial_Equals_PipeCache_ASSETTYPE_DECODED"]][show_cols].head(50)
else:
    sample = audit[show_cols].head(50)
print(sample.to_string(index=False), flush=True)
print("DONE", flush=True)
'@ | Set-Content -Path $AuditPy -Encoding UTF8
cmd /c ""$Py" "$AuditPy" 2>&1" | Tee-Object -FilePath $AuditLog
$ExitCode = $LASTEXITCODE
Write-Host ""
Write-Host "Audit ExitCode: $ExitCode" -ForegroundColor Cyan
Write-Host "Audit log: $AuditLog" -ForegroundColor Cyan
$Text = Get-Content $AuditLog -Raw
$Text | Set-Clipboard
if ($ExitCode -ne 0) { throw "Audit failed. Log copied to clipboard." }
