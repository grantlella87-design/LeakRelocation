
$ErrorActionPreference = "Stop"
$Root = Join-Path $env:USERPROFILE "Downloads\LeakRelocation-GeoPandas"
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$WorkPy = Join-Path $Root "RUN_service_assettype_enrich_stable.py"
$Log = Join-Path $Root ("LIVE_service_assettype_enrich_stable_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".log")
if (!(Test-Path $Py)) { throw "Python venv not found: $Py" }
@'
from pathlib import Path
import importlib.util
import time
import traceback
import pandas as pd
import requests
ROOT = Path.home() / "Downloads" / "LeakRelocation-GeoPandas"
CACHE = ROOT / "layer_cache"
OUT = ROOT / "assettype_cache_enrichment"
OUT.mkdir(parents=True, exist_ok=True)
MAIN_SCRIPT = Path(r"\\ngusnasnwh001\gasne\GasNE Shared\Shared\ENG\Complex Team\GIS AutoPrint\Distribution Leak Relocation\Arcpy Code\leak reolcation - geopandas.py")
CHUNK_SIZE = 250
MAX_RETRIES = 5
LAYER_KEY = "service_pipes"
CACHE_NAME = "service_pipes.pkl.gz"
URL_ATTR = "SERVICE_PIPE_URL"
def log(message):
    print(str(message), flush=True)
def load_main():
    spec = importlib.util.spec_from_file_location("leak_relocation_geopandas", str(MAIN_SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
def norm_code(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text
def material_family_from_assettype(decoded_assettype):
    text = "" if decoded_assettype is None else str(decoded_assettype).upper()
    if any(token in text for token in ["PLASTIC", "POLY", "PE", "PVC", "ABS", "HDPE", "MDPE"]):
        return "PLASTIC"
    if any(token in text for token in ["CAST IRON", "DUCTILE", "WROUGHT", "IRON"]):
        return "IRON"
    if any(token in text for token in ["STEEL", "GALVANIZED", "BARE", "COATED"]):
        return "STEEL"
    if "COPPER" in text:
        return "COPPER"
    if any(token in text for token in ["UNKNOWN", "UNK", "COMPOSITE"]) or not text.strip():
        return "UNKNOWN"
    return "OTHER"
def build_assettype_decoder(layer_json):
    decoder = {}
    for subtype in layer_json.get("types", []) or []:
        assetgroup_code = norm_code(subtype.get("id"))
        assetgroup_name = subtype.get("name")
        domains = subtype.get("domains") or {}
        assettype_domain = None
        for field_name, domain in domains.items():
            if str(field_name).lower() == "assettype":
                assettype_domain = domain
                break
        if not assettype_domain:
            continue
        for coded_value in assettype_domain.get("codedValues") or []:
            assettype_code = norm_code(coded_value.get("code"))
            assettype_name = coded_value.get("name")
            decoder[(assetgroup_code, assettype_code)] = {
                "ASSETGROUP_DECODED": assetgroup_name,
                "ASSETTYPE_DECODED": assettype_name,
                "ASSETTYPE_DOMAIN": assettype_domain.get("name"),
                "PipeMaterialFamily": material_family_from_assettype(assettype_name)
            }
    return decoder
def request_with_retries(mod, session, query_url, params, chunk_index):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            data = mod.request_json(session, query_url, params)
            if "error" in data:
                raise RuntimeError("ArcGIS error chunk={0}: {1}".format(chunk_index, data["error"]))
            return data
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as ex:
            sleep_seconds = min(30, 2 ** attempt)
            log("  transient network failure chunk {0}, attempt {1}/{2}: {3}".format(chunk_index, attempt, MAX_RETRIES, ex))
            log("  retrying after {0} seconds".format(sleep_seconds))
            time.sleep(sleep_seconds)
        except Exception:
            raise
    raise RuntimeError("Chunk {0} failed after {1} attempts".format(chunk_index, MAX_RETRIES))
def query_by_objectid_chunks(mod, session, layer_url, objectids):
    query_url = layer_url.rstrip("/") + "/query"
    rows = []
    chunks = [objectids[i:i + CHUNK_SIZE] for i in range(0, len(objectids), CHUNK_SIZE)]
    log("  objectId chunk size: {0}".format(CHUNK_SIZE))
    log("  objectId chunk count: {0:,}".format(len(chunks)))
    log("  workers: 1; reason: avoid concurrent token/auth prompt storm and connection resets")
    for idx, chunk in enumerate(chunks, start=1):
        params = {
            "f": "json",
            "objectIds": ",".join(str(int(x)) for x in chunk),
            "outFields": "OBJECTID,ASSETGROUP,ASSETTYPE",
            "returnGeometry": "false"
        }
        data = request_with_retries(mod, session, query_url, params, idx)
        for feature in data.get("features") or []:
            attrs = feature.get("attributes") or {}
            rows.append({
                "OBJECTID": attrs.get("OBJECTID"),
                "ASSETGROUP": attrs.get("ASSETGROUP"),
                "ASSETTYPE": attrs.get("ASSETTYPE")
            })
        if idx == 1 or idx % 25 == 0 or idx == len(chunks):
            log("  completed chunks: {0:,} / {1:,}; returned rows so far: {2:,}".format(idx, len(chunks), len(rows)))
    return pd.DataFrame(rows)
def main():
    log("Stable service ASSETTYPE cache enrichment")
    log("No parallel pools. No full relocation. No material/Grade usage.")
    log("Main script: {0}".format(MAIN_SCRIPT))
    mod = load_main()
    session = mod.make_session()
    layer_url = getattr(mod, URL_ATTR)
    cache_path = CACHE / CACHE_NAME
    log("cache: {0}".format(cache_path))
    log("url: {0}".format(layer_url))
    df = pd.read_pickle(cache_path, compression="gzip")
    log("cache rows before: {0:,}".format(len(df)))
    if "OBJECTID" not in df.columns:
        raise RuntimeError("OBJECTID missing from service cache")
    layer_json = mod.request_json(session, layer_url, {"f": "json"})
    decoder = build_assettype_decoder(layer_json)
    log("ASSETGROUP+ASSETTYPE decoder entries: {0:,}".format(len(decoder)))
    objectids = df["OBJECTID"].dropna().astype("int64").drop_duplicates().tolist()
    attr_df = query_by_objectid_chunks(mod, session, layer_url, objectids)
    log("returned attribute rows: {0:,}".format(len(attr_df)))
    attr_df = attr_df.dropna(subset=["OBJECTID"]).copy()
    attr_df["OBJECTID"] = attr_df["OBJECTID"].astype("int64")
    attr_df = attr_df.drop_duplicates("OBJECTID", keep="first")
    for col in ["ASSETGROUP", "ASSETTYPE", "ASSETGROUP_DECODED", "ASSETTYPE_DECODED", "ASSETTYPE_DOMAIN", "PipeMaterialFamily", "PipeMaterialRaw", "GradeMaterial"]:
        if col in df.columns:
            df = df.drop(columns=[col])
    if "material" in df.columns:
        df["GradeMaterial"] = df["material"]
    df = df.merge(attr_df, how="left", on="OBJECTID")
    decoded_rows = []
    for _, row in df[["ASSETGROUP", "ASSETTYPE"]].iterrows():
        key = (norm_code(row.get("ASSETGROUP")), norm_code(row.get("ASSETTYPE")))
        decoded_rows.append(decoder.get(key, {}))
    decoded_df = pd.DataFrame(decoded_rows)
    for col in ["ASSETGROUP_DECODED", "ASSETTYPE_DECODED", "ASSETTYPE_DOMAIN", "PipeMaterialFamily"]:
        df[col] = decoded_df[col] if col in decoded_df.columns else None
    df["PipeMaterialRaw"] = df["ASSETTYPE_DECODED"]
    df["material"] = df["ASSETTYPE_DECODED"]
    backup = cache_path.with_name(cache_path.name + ".before_service_assettype_stable.bak")
    if not backup.exists():
        backup.write_bytes(cache_path.read_bytes())
    df.to_pickle(cache_path, compression="gzip")
    log("backup written: {0}".format(backup))
    log("cache updated: {0}".format(cache_path))
    log("ASSETTYPE non-null: {0:,}".format(df["ASSETTYPE"].notna().sum()))
    log("ASSETTYPE_DECODED counts:")
    log(df["ASSETTYPE_DECODED"].value_counts(dropna=False).head(50))
    log("PipeMaterialFamily counts:")
    log(df["PipeMaterialFamily"].value_counts(dropna=False).head(20))
    distinct = df.groupby(["ASSETGROUP", "ASSETGROUP_DECODED", "ASSETTYPE", "ASSETTYPE_DECODED", "PipeMaterialFamily"], dropna=False).size().reset_index(name="count").sort_values("count", ascending=False)
    out_csv = OUT / "service_pipes_assettype_decode_counts_stable.csv"
    distinct.to_csv(out_csv, index=False)
    log("distinct decode CSV: {0}".format(out_csv))
    log("DONE")
if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
'@ | Set-Content -Path $WorkPy -Encoding UTF8
& $Py -m py_compile $WorkPy
if ($LASTEXITCODE -ne 0) { throw "Compile failed: $WorkPy" }
Write-Host "Running stable service ASSETTYPE enrichment with live output..." -ForegroundColor Cyan
Write-Host "Log: $Log" -ForegroundColor Cyan
& $Py -u $WorkPy 2>&1 | Tee-Object -FilePath $Log
$ExitCode = $LASTEXITCODE
Write-Host ""
Write-Host "ExitCode: $ExitCode" -ForegroundColor Cyan
Write-Host "Log: $Log" -ForegroundColor Cyan
if ($ExitCode -ne 0) {
    Get-Content $Log -Raw | Set-Clipboard
    throw "Stable service ASSETTYPE enrichment failed. Log copied to clipboard."
}
