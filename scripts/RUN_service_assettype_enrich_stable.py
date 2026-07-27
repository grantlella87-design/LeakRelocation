from pathlib import Path
import importlib.util
import os
import time
import traceback
import threading
import pandas as pd
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
ROOT = Path.home() / "Downloads" / "LeakRelocation-GeoPandas"
CACHE = ROOT / "layer_cache"
OUT = ROOT / "assettype_cache_enrichment"
OUT.mkdir(parents=True, exist_ok=True)
MAIN_SCRIPT = Path(r"\\ngusnasnwh001\gasne\GasNE Shared\Shared\ENG\Complex Team\GIS AutoPrint\Distribution Leak Relocation\Arcpy Code\leak reolcation - geopandas.py")
PAGE_SIZE = int(os.environ.get("LR_ASSETTYPE_PAGE_SIZE", "2000"))
MAX_RETRIES = int(os.environ.get("LR_ASSETTYPE_RETRIES", "5"))
DEFAULT_WORKERS = max(1, int((os.cpu_count() or 1) * 0.75))
MAX_WORKERS = max(1, int(os.environ.get("LR_ASSETTYPE_WORKERS", str(DEFAULT_WORKERS))))
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
def family_from_assettype(decoded_assettype):
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
                "PipeMaterialFamily": family_from_assettype(assettype_name)
            }
    return decoder
def retry_request(mod, session, query_url, params, label):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            data = mod.request_json(session, query_url, params)
            if "error" in data:
                raise RuntimeError("ArcGIS error {0}: {1}".format(label, data["error"]))
            return data
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as ex:
            sleep_seconds = min(30, 2 ** attempt)
            log("  transient network failure {0}, attempt {1}/{2}: {3}".format(label, attempt, MAX_RETRIES, ex))
            log("  retrying {0} after {1} seconds".format(label, sleep_seconds))
            time.sleep(sleep_seconds)
    raise RuntimeError("Request {0} failed after {1} attempts".format(label, MAX_RETRIES))
def query_range_pages(mod, base_session, layer_url, objectids):
    query_url = layer_url.rstrip("/") + "/query"
    wanted = pd.Series(objectids).dropna().astype("int64").drop_duplicates()
    wanted_set = set(wanted.tolist())
    min_oid = int(wanted.min())
    max_oid = int(wanted.max())
    where = "OBJECTID >= {0} AND OBJECTID <= {1}".format(min_oid, max_oid)
    count_params = {"f": "json", "where": where, "returnCountOnly": "true"}
    count_data = retry_request(mod, base_session, query_url, count_params, "count")
    service_range_count = int(count_data.get("count") or 0)
    offsets = list(range(0, service_range_count, PAGE_SIZE))
    log("  local OBJECTID min: {0:,}".format(min_oid))
    log("  local OBJECTID max: {0:,}".format(max_oid))
    log("  local OBJECTIDs to keep: {0:,}".format(len(wanted_set)))
    log("  service rows in OBJECTID range: {0:,}".format(service_range_count))
    log("  page size: {0:,}".format(PAGE_SIZE))
    log("  page count: {0:,}".format(len(offsets)))
    log("  logical processors: {0}".format(os.cpu_count() or 1))
    log("  default workers 75pct: {0}".format(DEFAULT_WORKERS))
    log("  active workers: {0}".format(MAX_WORKERS))
    log("  paging mode: OBJECTID range; no long objectIds URL")
    thread_local = threading.local()
    rows = []
    rows_lock = threading.Lock()
    def get_session():
        if not hasattr(thread_local, "session"):
            thread_local.session = mod.make_session()
        return thread_local.session
    def fetch_page(offset):
        label = "page_offset_{0}".format(offset)
        params = {
            "f": "json",
            "where": where,
            "outFields": "OBJECTID,ASSETGROUP,ASSETTYPE",
            "returnGeometry": "false",
            "orderByFields": "OBJECTID",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE
        }
        data = retry_request(mod, get_session(), query_url, params, label)
        local_rows = []
        for feature in data.get("features") or []:
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
            local_rows.append({"OBJECTID": oid_int, "ASSETGROUP": attrs.get("ASSETGROUP"), "ASSETTYPE": attrs.get("ASSETTYPE")})
        return offset, local_rows
    completed = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(fetch_page, offset) for offset in offsets]
        for future in as_completed(futures):
            offset, local_rows = future.result()
            with rows_lock:
                rows.extend(local_rows)
                completed += 1
                current_count = len(rows)
            if completed == 1 or completed % 10 == 0 or completed == len(offsets):
                log("  completed pages: {0:,} / {1:,}; matched local rows so far: {2:,}".format(completed, len(offsets), current_count))
    attr_df = pd.DataFrame(rows)
    if len(attr_df) == 0:
        log("  WARNING: no ASSETGROUP/ASSETTYPE rows matched local OBJECTIDs")
        return pd.DataFrame({"OBJECTID": wanted, "ASSETGROUP": None, "ASSETTYPE": None})
    attr_df = attr_df.drop_duplicates("OBJECTID", keep="first")
    attr_df["OBJECTID"] = attr_df["OBJECTID"].astype("int64")
    wanted_df = pd.DataFrame({"OBJECTID": wanted})
    attr_df = wanted_df.merge(attr_df, how="left", on="OBJECTID")
    log("  final joined attribute rows: {0:,}".format(len(attr_df)))
    log("  ASSETGROUP non-null: {0:,}".format(attr_df["ASSETGROUP"].notna().sum()))
    log("  ASSETTYPE non-null: {0:,}".format(attr_df["ASSETTYPE"].notna().sum()))
    return attr_df
def main():
    log("Service ASSETTYPE cache enrichment - dynamic 75pct workers")
    log("LeakRelocation only. No full relocation. ASSETTYPE only; Grade/material preserved separately.")
    log("Main script: {0}".format(MAIN_SCRIPT))
    mod = load_main()
    base_session = mod.make_session()
    layer_url = getattr(mod, URL_ATTR)
    cache_path = CACHE / CACHE_NAME
    log("cache: {0}".format(cache_path))
    log("url: {0}".format(layer_url))
    df = pd.read_pickle(cache_path, compression="gzip")
    log("cache rows before: {0:,}".format(len(df)))
    log("columns before: {0}".format(list(df.columns)))
    if "OBJECTID" not in df.columns:
        raise RuntimeError("OBJECTID missing from service cache")
    layer_json = mod.request_json(base_session, layer_url, {"f": "json"})
    decoder = build_assettype_decoder(layer_json)
    log("ASSETGROUP+ASSETTYPE decoder entries: {0:,}".format(len(decoder)))
    objectids = df["OBJECTID"].dropna().astype("int64").drop_duplicates().tolist()
    attr_df = query_range_pages(mod, base_session, layer_url, objectids)
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
    backup = cache_path.with_name(cache_path.name + ".before_service_assettype_dynamic75.bak")
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
    out_csv = OUT / "service_pipes_assettype_decode_counts_dynamic75.csv"
    distinct.to_csv(out_csv, index=False)
    log("distinct decode CSV: {0}".format(out_csv))
    log("DONE")
if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
