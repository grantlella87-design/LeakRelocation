from pathlib import Path
import importlib.util
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

from _bootstrap import config

ROOT = config.WORK_ROOT
CACHE = config.LAYER_CACHE_DIR
OUT = config.ENRICHMENT_DIR
OUT.mkdir(parents=True, exist_ok=True)

MAIN_SCRIPT = config.NETWORK_MAIN_SCRIPT

PAGE_SIZE = 2000
MAX_WORKERS = 8

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

REQUEST_JSON_LOCK = threading.Lock()

def safe_request_json(mod, session, url, params):
    # Prevent multiple worker threads from all detecting an expired token
    # and launching concurrent auth/browser refresh flows.
    with REQUEST_JSON_LOCK:
        return safe_request_json(mod, session, url, params)

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
    type_id_field = layer_json.get("typeIdField")

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

    return type_id_field, decoder

def fetch_assettype_pages(mod, session, layer_url, wanted_objectids):
    query_url = layer_url.rstrip("/") + "/query"
    wanted = pd.Series(wanted_objectids).dropna().astype("int64").drop_duplicates()
    wanted_set = set(wanted.tolist())

    count_data = safe_request_json(mod, session, query_url, {
        "f": "json",
        "where": "1=1",
        "returnCountOnly": "true"
    })

    if "error" in count_data:
        raise RuntimeError("ArcGIS count query error: {0}".format(count_data["error"]))

    service_count = int(count_data.get("count") or 0)
    offsets = list(range(0, service_count, PAGE_SIZE))

    log("  service count: {0:,}".format(service_count))
    log("  local cache OBJECTIDs to retain: {0:,}".format(len(wanted_set)))
    log("  page size: {0:,}".format(PAGE_SIZE))
    log("  page count: {0:,}".format(len(offsets)))
    log("  workers: {0}".format(MAX_WORKERS))
    log("  NOTE: using one existing authenticated session. No worker make_session calls.")

    def fetch_page(offset):
        params = {
            "f": "json",
            "where": "1=1",
            "outFields": "OBJECTID,ASSETGROUP,ASSETTYPE",
            "returnGeometry": "false",
            "orderByFields": "OBJECTID",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE
        }

        data = safe_request_json(mod, session, query_url, params)

        if "error" in data:
            raise RuntimeError("ArcGIS page query error offset={0}: {1}".format(offset, data["error"]))

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

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(fetch_page, offset) for offset in offsets]

        for future in as_completed(futures):
            offset, rows = future.result()
            all_rows.extend(rows)
            completed += 1

            if completed == 1 or completed % 10 == 0 or completed == len(offsets):
                log("  completed pages: {0:,} / {1:,}; matched local rows so far: {2:,}".format(
                    completed,
                    len(offsets),
                    len(all_rows)
                ))

    attr_df = pd.DataFrame(all_rows)

    if len(attr_df) == 0:
        log("  WARNING: no ASSETGROUP/ASSETTYPE rows returned.")
        return pd.DataFrame({"OBJECTID": wanted, "ASSETGROUP": None, "ASSETTYPE": None})

    attr_df = attr_df.drop_duplicates("OBJECTID", keep="first")
    attr_df["OBJECTID"] = attr_df["OBJECTID"].astype("int64")

    wanted_df = pd.DataFrame({"OBJECTID": wanted})
    attr_df = wanted_df.merge(attr_df, how="left", on="OBJECTID")

    log("  joined attribute rows: {0:,}".format(len(attr_df)))
    log("  ASSETGROUP non-null: {0:,}".format(attr_df["ASSETGROUP"].notna().sum()))
    log("  ASSETTYPE non-null: {0:,}".format(attr_df["ASSETTYPE"].notna().sum()))

    return attr_df

def enrich_layer(mod, session, cfg):
    layer_key = cfg["layer_key"]
    cache_path = CACHE / cfg["cache_name"]
    layer_url = getattr(mod, cfg["url_attr"])

    log("")
    log("============================================================")
    log("ENRICHING: {0}".format(layer_key))
    log("cache: {0}".format(cache_path))
    log("url: {0}".format(layer_url))

    df = pd.read_pickle(cache_path, compression="gzip")
    log("cache rows before: {0:,}".format(len(df)))
    log("columns before: {0}".format(list(df.columns)))

    # LR_SKIP_ALREADY_ASSETTYPE_ENRICHED
    if (
        "ASSETGROUP" in df.columns
        and "ASSETTYPE" in df.columns
        and "ASSETTYPE_DECODED" in df.columns
        and "PipeMaterialFamily" in df.columns
        and df["ASSETTYPE"].notna().sum() > 0
    ):
        log("SKIP: {0} already has ASSETGROUP/ASSETTYPE decoded in cache.".format(layer_key))
        log("ASSETTYPE_DECODED counts:")
        log(df["ASSETTYPE_DECODED"].value_counts(dropna=False).head(40))
        log("PipeMaterialFamily counts:")
        log(df["PipeMaterialFamily"].value_counts(dropna=False).head(20))
        return

    if "OBJECTID" not in df.columns:
        raise RuntimeError("OBJECTID missing from cache: {0}".format(cache_path))

    layer_json = safe_request_json(mod, session, layer_url, {"f": "json"})
    type_id_field, decoder = build_assettype_decoder(layer_json)

    log("typeIdField: {0}".format(type_id_field))
    log("ASSETGROUP+ASSETTYPE decoder entries: {0:,}".format(len(decoder)))

    objectids = df["OBJECTID"].dropna().astype("int64").drop_duplicates().tolist()
    attr_df = fetch_assettype_pages(mod, session, layer_url, objectids)

    for col in [
        "ASSETGROUP",
        "ASSETTYPE",
        "ASSETGROUP_DECODED",
        "ASSETTYPE_DECODED",
        "ASSETTYPE_DOMAIN",
        "PipeMaterialRaw",
        "PipeMaterialFamily",
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

    # Critical compatibility bridge:
    # Existing relocation code expects pipe material in column named "material".
    # DNV "material" is Grade, so preserve it as GradeMaterial and make material mean decoded ASSETTYPE.
    df["material"] = df["ASSETTYPE_DECODED"]

    backup = cache_path.with_name(cache_path.name + ".before_assettype_as_material.bak")
    if not backup.exists():
        backup.write_bytes(cache_path.read_bytes())

    df.to_pickle(cache_path, compression="gzip")

    log("backup written: {0}".format(backup))
    log("cache updated: {0}".format(cache_path))
    log("columns after: {0}".format(list(df.columns)))

    log("")
    log("ASSETTYPE_DECODED counts:")
    log(df["ASSETTYPE_DECODED"].value_counts(dropna=False).head(50))

    log("")
    log("PipeMaterialFamily counts:")
    log(df["PipeMaterialFamily"].value_counts(dropna=False).head(20))

    distinct = df.groupby(
        ["ASSETGROUP", "ASSETGROUP_DECODED", "ASSETTYPE", "ASSETTYPE_DECODED", "PipeMaterialFamily"],
        dropna=False
    ).size().reset_index(name="count").sort_values("count", ascending=False)

    distinct_csv = OUT / (layer_key + "_assettype_decode_counts.csv")
    distinct.to_csv(distinct_csv, index=False)
    log("distinct decode CSV: {0}".format(distinct_csv))

def main():
    log("ASSETTYPE full cache enrichment")
    log("Main script: {0}".format(MAIN_SCRIPT))
    log("MAX_WORKERS: {0}".format(MAX_WORKERS))
    log("PAGE_SIZE: {0}".format(PAGE_SIZE))
    log("This modifies only local pipe caches. It does not run relocation.")

    mod = load_main()
    session = mod.make_session()

    for cfg in LAYERS:
        enrich_layer(mod, session, cfg)

    log("")
    log("DONE: local pipe caches now use decoded ASSETTYPE as material.")

if __name__ == "__main__":
    main()

