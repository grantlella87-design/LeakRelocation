
$ErrorActionPreference = "Stop"
$Root = Join-Path $env:USERPROFILE "Downloads\LeakRelocation-GeoPandas"
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$WorkPy = Join-Path $Root "RUN_service_assettype_enrich_stable.py"
if (!(Test-Path $Py)) { throw "Python venv not found: $Py" }
if (!(Test-Path $WorkPy)) { throw "Stable enrichment python not found: $WorkPy" }
$Text = Get-Content $WorkPy -Raw
$Start = $Text.IndexOf("def query_by_objectid_chunks(")
$End = $Text.IndexOf("def main():", $Start)
if ($Start -lt 0 -or $End -lt 0) { throw "Could not find query_by_objectid_chunks through def main in $WorkPy" }
$NewFunction = @'
def query_by_objectid_chunks(mod, session, layer_url, objectids):
    # Do not use objectIds=... for this layer; long URLs with token become unstable/404.
    # Use OBJECTID range pagination instead, then filter back to local cache OBJECTIDs.
    query_url = layer_url.rstrip("/") + "/query"
    wanted = pd.Series(objectids).dropna().astype("int64").drop_duplicates()
    wanted_set = set(wanted.tolist())
    min_oid = int(wanted.min())
    max_oid = int(wanted.max())
    where = "OBJECTID >= {0} AND OBJECTID <= {1}".format(min_oid, max_oid)
    page_size = 2000
    offset = 0
    page_index = 0
    rows = []
    count_params = {
        "f": "json",
        "where": where,
        "returnCountOnly": "true"
    }
    count_data = request_with_retries(mod, session, query_url, count_params, "count")
    service_range_count = int(count_data.get("count") or 0)
    log("  local OBJECTID min: {0:,}".format(min_oid))
    log("  local OBJECTID max: {0:,}".format(max_oid))
    log("  local OBJECTIDs to keep: {0:,}".format(len(wanted_set)))
    log("  service rows in OBJECTID range: {0:,}".format(service_range_count))
    log("  page size: {0:,}".format(page_size))
    log("  paging mode: OBJECTID range, not objectIds URL")
    log("  workers: 1; reason: avoid auth prompt storm and connection resets")
    while True:
        params = {
            "f": "json",
            "where": where,
            "outFields": "OBJECTID,ASSETGROUP,ASSETTYPE",
            "returnGeometry": "false",
            "orderByFields": "OBJECTID",
            "resultOffset": offset,
            "resultRecordCount": page_size
        }
        data = request_with_retries(mod, session, query_url, params, page_index)
        features = data.get("features") or []
        if not features:
            break
        for feature in features:
            attrs = feature.get("attributes") or {}
            oid = attrs.get("OBJECTID")
            if oid is None:
                continue
            try:
                oid_int = int(oid)
            except Exception:
                continue
            if oid_int not in wanted_set:
                continue
            rows.append({
                "OBJECTID": oid_int,
                "ASSETGROUP": attrs.get("ASSETGROUP"),
                "ASSETTYPE": attrs.get("ASSETTYPE")
            })
        page_index += 1
        offset += page_size
        if page_index == 1 or page_index % 10 == 0 or len(features) < page_size:
            log("  completed pages: {0:,}; scanned service rows: {1:,} / {2:,}; matched local rows so far: {3:,}".format(
                page_index,
                min(offset, service_range_count),
                service_range_count,
                len(rows)
            ))
        if len(features) < page_size:
            break
    attr_df = pd.DataFrame(rows)
    if len(attr_df) == 0:
        log("  WARNING: no ASSETGROUP/ASSETTYPE rows matched local OBJECTIDs.")
        return pd.DataFrame({"OBJECTID": wanted, "ASSETGROUP": None, "ASSETTYPE": None})
    attr_df = attr_df.drop_duplicates("OBJECTID", keep="first")
    attr_df["OBJECTID"] = attr_df["OBJECTID"].astype("int64")
    wanted_df = pd.DataFrame({"OBJECTID": wanted})
    attr_df = wanted_df.merge(attr_df, how="left", on="OBJECTID")
    log("  final joined attribute rows: {0:,}".format(len(attr_df)))
    log("  ASSETGROUP non-null: {0:,}".format(attr_df["ASSETGROUP"].notna().sum()))
    log("  ASSETTYPE non-null: {0:,}".format(attr_df["ASSETTYPE"].notna().sum()))
    return attr_df

'@
$Text = $Text.Substring(0, $Start) + $NewFunction + $Text.Substring($End)
Set-Content -Path $WorkPy -Value $Text -Encoding UTF8
& $Py -m py_compile $WorkPy
if ($LASTEXITCODE -ne 0) { throw "Compile failed after range pagination patch: $WorkPy" }
$Log = Join-Path $Root ("LIVE_service_assettype_enrich_RANGE_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".log")
Write-Host "Running service ASSETTYPE enrichment with OBJECTID range pagination..." -ForegroundColor Cyan
Write-Host "This is LeakRelocation only. No CAD/OAuth/other project work." -ForegroundColor Cyan
Write-Host "Log: $Log" -ForegroundColor Cyan
cmd /c ""$Py" -u "$WorkPy" 2>&1" | Tee-Object -FilePath $Log
$ExitCode = $LASTEXITCODE
Write-Host ""
Write-Host "ExitCode: $ExitCode" -ForegroundColor Cyan
Write-Host "Log: $Log" -ForegroundColor Cyan
$Tail = (Get-Content $Log -Raw) -split "`r?`n" | Select-Object -Last 220
$Clip = @(
    "=== Service ASSETTYPE Enrichment RANGE Run ===",
    "ExitCode: $ExitCode",
    "Log: $Log",
    "",
    "--- LAST 220 LOG LINES ---",
    ($Tail -join "`r`n")
) -join "`r`n"
$Clip | Set-Clipboard
if ($ExitCode -ne 0) { throw "Service ASSETTYPE range enrichment failed. Tail copied to clipboard." }
