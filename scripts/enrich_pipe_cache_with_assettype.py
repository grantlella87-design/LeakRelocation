from pathlib import Path
import importlib.util
import json
import math
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

from _bootstrap import config

ROOT = config.WORK_ROOT
CACHE = config.LAYER_CACHE_DIR
OUT = config.ENRICHMENT_DIR
OUT.mkdir(parents=True, exist_ok=True)

MAIN_SCRIPT = config.NETWORK_MAIN_SCRIPT

BATCH_SIZE = 750

LAYERS = [
    {
        "layer_key": "distribution_pipes",
        "url_attr": "DISTRIBUTION_PIPE_URL",
        "cache_name": "distribution_pipes.pkl.gz"
    },
    {
        "layer_key": "service_pipes",
        "url_attr": "SERVICE_PIPE_URL",
        "cache_name": "service_pipes.pkl.gz"
    }
]

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

def material_family(label):
    text = "" if label is None else str(label).upper()
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

def get_layer_json(mod, session, layer_url):
    return mod.request_json(session, layer_url, {"f": "json"})

def build_assettype_decoder(layer_json):
    decoder = {}
    type_id_field = layer_json.get("typeIdField")
    for subtype in layer_json.get("types", []) or []:
        subtype_id = norm_code(subtype.get("id"))
        subtype_name = subtype.get("name")
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
            decoded = coded_value.get("name")
            decoder[(subtype_id, assettype_code)] = {
                "ASSETGROUP_DECODED": subtype_name,
                "ASSETTYPE_DECODED": decoded,
                "ASSETTYPE_DOMAIN": assettype_domain.get("name"),
                "PipeMaterialFamily": material_family(decoded)
            }
    return type_id_field, decoder

def query_asset_attrs(mod, session, layer_url, object_ids):
    query_url = layer_url.rstrip("/") + "/query"
    page_size = 2000
    max_workers = 6

    local_objectids = pd.Series(object_ids).dropna().astype("int64").drop_duplicates()
    wanted = pd.DataFrame({"OBJECTID": local_objectids})
    wanted_set = set(local_objectids.tolist())

    count_params = {
        "f": "json",
        "where": "1=1",
        "returnCountOnly": "true"
    }

    count_data = mod.request_json(session, query_url, count_params)

    if "error" in count_data:
        raise RuntimeError("ArcGIS count query error for {0}: {1}".format(layer_url, count_data["error"]))

    service_count = int(count_data.get("count") or 0)
    offsets = list(range(0, service_count, page_size))

    log("  service count: {0:,}".format(service_count))
    log("  local OBJECTIDs to keep: {0:,}".format(len(wanted_set)))
    log("  page size: {0:,}".format(page_size))
    log("  page count: {0:,}".format(len(offsets)))
    log("  parallel workers using one existing authenticated session: {0}".format(max_workers))

    def fetch_page(offset):
        params = {
            "f": "json",
            "where": "1=1",
            "outFields": "OBJECTID,ASSETGROUP,ASSETTYPE",
            "returnGeometry": "false",
            "orderByFields": "OBJECTID",
            "resultOffset": offset,
            "resultRecordCount": page_size
        }

        data = mod.request_json(session, query_url, params)

        if "error" in data:
            raise RuntimeError("ArcGIS page query error offset={0} for {1}: {2}".format(offset, layer_url, data["error"]))

        rows = []
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
        return offset, rows

    all_rows = []
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fetch_page, offset) for offset in offsets]

        for future in as_completed(futures):
            offset, rows = future.result()
            all_rows.extend(rows)
            completed += 1

            if completed == 1 or completed % 20 == 0 or completed == len(offsets):
                log("  completed pages: {0:,} / {1:,}; matched local rows so far: {2:,}".format(
                    completed,
                    len(offsets),
                    len(all_rows)
                ))

    attr_df = pd.DataFrame(all_rows)

    if len(attr_df) == 0:
        log("  WARNING: no ASSETGROUP/ASSETTYPE rows returned for local OBJECTIDs.")
        return wanted.assign(ASSETGROUP=None, ASSETTYPE=None)

    attr_df = attr_df.drop_duplicates("OBJECTID", keep="first")
    attr_df["OBJECTID"] = attr_df["OBJECTID"].astype("int64")

    attr_df = wanted.merge(attr_df, how="left", on="OBJECTID")

    log("  final joined attribute rows: {0:,}".format(len(attr_df)))
    log("  ASSETGROUP non-null: {0:,}".format(attr_df["ASSETGROUP"].notna().sum() if "ASSETGROUP" in attr_df.columns else 0))
    log("  ASSETTYPE non-null: {0:,}".format(attr_df["ASSETTYPE"].notna().sum() if "ASSETTYPE" in attr_df.columns else 0))

    return attr_df
def enrich_one_layer(mod, session, cfg):
    layer_key = cfg["layer_key"]
    cache_path = CACHE / cfg["cache_name"]
    layer_url = getattr(mod, cfg["url_attr"])
    log("")
    log("=== {0} ===".format(layer_key))
    log("cache: {0}".format(cache_path))
    log("url: {0}".format(layer_url))
    df = pd.read_pickle(cache_path, compression="gzip")
    log("cache rows: {0:,}".format(len(df)))
    if "OBJECTID" not in df.columns:
        raise RuntimeError("OBJECTID missing from cache: {0}".format(cache_path))
    layer_json = get_layer_json(mod, session, layer_url)
    type_id_field, decoder = build_assettype_decoder(layer_json)
    log("typeIdField from service metadata: {0}".format(type_id_field))
    log("assettype decoder entries: {0:,}".format(len(decoder)))
    object_ids = df["OBJECTID"].dropna().astype(int).drop_duplicates().tolist()
    attr_df = query_asset_attrs(mod, session, layer_url, object_ids)
    log("returned attribute rows: {0:,}".format(len(attr_df)))
    before_cols = list(df.columns)
    for col in ["ASSETGROUP", "ASSETTYPE", "ASSETGROUP_DECODED", "ASSETTYPE_DECODED", "ASSETTYPE_DOMAIN", "PipeMaterialRaw", "PipeMaterialFamily"]:
        if col in df.columns:
            df = df.drop(columns=[col])
    df = df.merge(attr_df, how="left", on="OBJECTID")
    decoded_rows = []
    for _, row in df[["ASSETGROUP", "ASSETTYPE"]].iterrows():
        key = (norm_code(row.get("ASSETGROUP")), norm_code(row.get("ASSETTYPE")))
        decoded = decoder.get(key, {})
        decoded_rows.append(decoded)
    decoded_df = pd.DataFrame(decoded_rows)
    if len(decoded_df) == 0:
        decoded_df = pd.DataFrame(index=df.index)
    for col in ["ASSETGROUP_DECODED", "ASSETTYPE_DECODED", "ASSETTYPE_DOMAIN", "PipeMaterialFamily"]:
        df[col] = decoded_df[col] if col in decoded_df.columns else None
    df["PipeMaterialRaw"] = df["ASSETTYPE_DECODED"]
    out_backup = cache_path.with_name(cache_path.name + ".before_assettype_enrichment.bak")
    if not out_backup.exists():
        out_backup.write_bytes(cache_path.read_bytes())
    df.to_pickle(cache_path, compression="gzip")
    log("backup written: {0}".format(out_backup))
    log("cache updated: {0}".format(cache_path))
    log("columns before: {0}".format(before_cols))
    log("columns after: {0}".format(list(df.columns)))
    log("")
    log("ASSETGROUP value counts:")
    log(df["ASSETGROUP"].value_counts(dropna=False).head(30))
    log("")
    log("ASSETTYPE decoded value counts:")
    log(df["ASSETTYPE_DECODED"].value_counts(dropna=False).head(40))
    log("")
    log("PipeMaterialFamily value counts:")
    log(df["PipeMaterialFamily"].value_counts(dropna=False).head(20))
    distinct_out = OUT / (layer_key + "_assettype_distinct.csv")
    distinct = df.groupby(["ASSETGROUP", "ASSETGROUP_DECODED", "ASSETTYPE", "ASSETTYPE_DECODED", "PipeMaterialFamily"], dropna=False).size().reset_index(name="count")
    distinct = distinct.sort_values("count", ascending=False)
    distinct.to_csv(distinct_out, index=False)
    log("distinct decode csv: {0}".format(distinct_out))

def main():
    log("Main script: {0}".format(MAIN_SCRIPT))
    mod = load_main()
    session = mod.make_session()
    for cfg in LAYERS:
        enrich_one_layer(mod, session, cfg)

if __name__ == "__main__":
    main()




