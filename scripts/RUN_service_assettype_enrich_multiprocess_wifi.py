from pathlib import Path
import concurrent.futures as cf
import importlib.util
import math
import os
import time
import traceback
import pandas as pd
import requests

from _bootstrap import config

ROOT = config.WORK_ROOT
CACHE = config.LAYER_CACHE_DIR
OUT = config.ENRICHMENT_DIR
SHARD_DIR = OUT / "service_assettype_shards"
OUT.mkdir(parents=True, exist_ok=True)
SHARD_DIR.mkdir(parents=True, exist_ok=True)

MAIN_SCRIPT = config.NETWORK_MAIN_SCRIPT

CACHE_NAME = "service_pipes.pkl.gz"
URL_ATTR = "SERVICE_PIPE_URL"

PAGE_SIZE = int(os.environ.get("LR_ASSETTYPE_PAGE_SIZE", "2000"))
MAX_RETRIES = int(os.environ.get("LR_ASSETTYPE_RETRIES", "5"))
DEFAULT_WORKERS = max(1, int((os.cpu_count() or 1) * 0.75))
MAX_WORKERS = max(1, int(os.environ.get("LR_ASSETTYPE_WORKERS", str(DEFAULT_WORKERS))))
SHARD_MULTIPLIER = max(1, int(os.environ.get("LR_ASSETTYPE_SHARD_MULTIPLIER", "8")))
SHARD_COUNT = max(MAX_WORKERS, MAX_WORKERS * SHARD_MULTIPLIER)

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

def safe_request(mod, session, query_url, params, label):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            data = mod.request_json(session, query_url, params)
            if "error" in data:
                raise RuntimeError("ArcGIS JSON error {0}: {1}".format(label, data["error"]))
            return data

        except requests.exceptions.HTTPError as ex:
            status = None
            try:
                status = ex.response.status_code
            except Exception:
                pass

            if status in [429, 500, 502, 503, 504]:
                sleep_seconds = min(30, 2 ** attempt)
                print("  HTTP {0} on {1}, attempt {2}/{3}; retrying after {4} seconds".format(status, label, attempt, MAX_RETRIES, sleep_seconds), flush=True)
                time.sleep(sleep_seconds)
                continue

            raise RuntimeError("HTTP error on {0}: status={1}".format(label, status)) from None

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as ex:
            sleep_seconds = min(30, 2 ** attempt)
            print("  transient network failure {0}, attempt {1}/{2}: {3}".format(label, attempt, MAX_RETRIES, ex), flush=True)
            print("  retrying {0} after {1} seconds".format(label, sleep_seconds), flush=True)
            time.sleep(sleep_seconds)

    raise RuntimeError("Request {0} failed after {1} attempts".format(label, MAX_RETRIES))

def make_shards(min_oid, max_oid, shard_count):
    total_span = max_oid - min_oid + 1
    shard_size = max(1, math.ceil(total_span / shard_count))
    shards = []
    start = min_oid
    index = 0

    while start <= max_oid:
        end = min(max_oid, start + shard_size - 1)
        shards.append((index, start, end))
        start = end + 1
        index += 1

    return shards

def fetch_shard(args):
    shard_index, shard_min, shard_max, local_objectids = args

    mod = load_main()
    session = mod.make_session()
    layer_url = getattr(mod, URL_ATTR)
    query_url = layer_url.rstrip("/") + "/query"

    wanted_set = set(int(x) for x in local_objectids)
    where = "OBJECTID >= {0} AND OBJECTID <= {1}".format(shard_min, shard_max)

    count_data = safe_request(
        mod,
        session,
        query_url,
        {
            "f": "json",
            "where": where,
            "returnCountOnly": "true"
        },
        "shard_{0}_count".format(shard_index)
    )

    service_count = int(count_data.get("count") or 0)
    offsets = list(range(0, service_count, PAGE_SIZE))
    rows = []
    completed = 0

    for offset in offsets:
        params = {
            "f": "json",
            "where": where,
            "outFields": "OBJECTID,ASSETGROUP,ASSETTYPE",
            "returnGeometry": "false",
            "orderByFields": "OBJECTID",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE
        }

        data = safe_request(mod, session, query_url, params, "shard_{0}_offset_{1}".format(shard_index, offset))

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

            rows.append({
                "OBJECTID": oid_int,
                "ASSETGROUP": attrs.get("ASSETGROUP"),
                "ASSETTYPE": attrs.get("ASSETTYPE")
            })

        completed += 1
        if completed == 1 or completed % 10 == 0 or completed == len(offsets):
            print("  shard {0}: pages {1:,}/{2:,}; matched rows {3:,}".format(shard_index, completed, len(offsets), len(rows)), flush=True)

    shard_csv = SHARD_DIR / "service_assettype_shard_{0:04d}.csv".format(shard_index)
    pd.DataFrame(rows).to_csv(shard_csv, index=False)

    return {
        "shard_index": shard_index,
        "shard_min": shard_min,
        "shard_max": shard_max,
        "service_count": service_count,
        "matched_rows": len(rows),
        "csv": str(shard_csv)
    }

def main():
    log("Service ASSETTYPE cache enrichment - MULTIPROCESS Wi-Fi push")
    log("LeakRelocation only. ASSETTYPE only. DNV material/Grade will be preserved as GradeMaterial.")
    log("Logical processors: {0}".format(os.cpu_count() or 1))
    log("Default workers 75pct: {0}".format(DEFAULT_WORKERS))
    log("Active workers: {0}".format(MAX_WORKERS))
    log("Shard multiplier: {0}".format(SHARD_MULTIPLIER))
    log("Shard count: {0}".format(SHARD_COUNT))
    log("Page size: {0}".format(PAGE_SIZE))
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

    local_oids = df["OBJECTID"].dropna().astype("int64").drop_duplicates().sort_values()
    min_oid = int(local_oids.min())
    max_oid = int(local_oids.max())

    shards = make_shards(min_oid, max_oid, SHARD_COUNT)

    log("local OBJECTID min: {0:,}".format(min_oid))
    log("local OBJECTID max: {0:,}".format(max_oid))
    log("local OBJECTIDs: {0:,}".format(len(local_oids)))
    log("OBJECTID shard count: {0:,}".format(len(shards)))

    shard_args = []
    for shard_index, shard_min, shard_max in shards:
        shard_oids = local_oids[(local_oids >= shard_min) & (local_oids <= shard_max)].tolist()
        if shard_oids:
            shard_args.append((shard_index, shard_min, shard_max, shard_oids))

    log("non-empty shard count: {0:,}".format(len(shard_args)))

    for old in SHARD_DIR.glob("service_assettype_shard_*.csv"):
        old.unlink()

    results = []
    completed = 0

    with cf.ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(fetch_shard, arg) for arg in shard_args]

        for future in cf.as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1

            if completed == 1 or completed % 5 == 0 or completed == len(futures):
                total_matched = sum(r["matched_rows"] for r in results)
                log("  completed shards: {0:,}/{1:,}; matched rows so far: {2:,}".format(completed, len(futures), total_matched))

    results_df = pd.DataFrame(results).sort_values("shard_index")
    result_csv = OUT / "service_assettype_shard_run_summary.csv"
    results_df.to_csv(result_csv, index=False)

    shard_frames = []
    for csv_path in results_df["csv"].tolist():
        shard_df = pd.read_csv(csv_path)
        if len(shard_df):
            shard_frames.append(shard_df)

    if not shard_frames:
        raise RuntimeError("No shard rows returned")

    attr_df = pd.concat(shard_frames, ignore_index=True)
    attr_df = attr_df.dropna(subset=["OBJECTID"]).copy()
    attr_df["OBJECTID"] = attr_df["OBJECTID"].astype("int64")
    attr_df = attr_df.drop_duplicates("OBJECTID", keep="first")

    wanted_df = pd.DataFrame({"OBJECTID": local_oids})
    attr_df = wanted_df.merge(attr_df, how="left", on="OBJECTID")

    log("joined attribute rows: {0:,}".format(len(attr_df)))
    log("ASSETGROUP non-null: {0:,}".format(attr_df["ASSETGROUP"].notna().sum()))
    log("ASSETTYPE non-null: {0:,}".format(attr_df["ASSETTYPE"].notna().sum()))

    for col in [
        "ASSETGROUP",
        "ASSETTYPE",
        "ASSETGROUP_DECODED",
        "ASSETTYPE_DECODED",
        "ASSETTYPE_DOMAIN",
        "PipeMaterialFamily",
        "PipeMaterialRaw",
        "GradeMaterial"
    ]:
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

    # Compatibility bridge for existing relocation logic:
    # material now means decoded ASSETTYPE; original Grade is preserved as GradeMaterial.
    df["material"] = df["ASSETTYPE_DECODED"]

    backup = cache_path.with_name(cache_path.name + ".before_service_assettype_multiprocess.bak")
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

    distinct = df.groupby(
        ["ASSETGROUP", "ASSETGROUP_DECODED", "ASSETTYPE", "ASSETTYPE_DECODED", "PipeMaterialFamily"],
        dropna=False
    ).size().reset_index(name="count").sort_values("count", ascending=False)

    distinct_csv = OUT / "service_pipes_assettype_decode_counts_multiprocess.csv"
    distinct.to_csv(distinct_csv, index=False)

    log("distinct decode CSV: {0}".format(distinct_csv))
    log("shard summary CSV: {0}".format(result_csv))
    log("DONE")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
