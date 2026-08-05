from pathlib import Path
import importlib.util
import json
import pandas as pd

from _bootstrap import config

ROOT = config.WORK_ROOT
CACHE = config.LAYER_CACHE_DIR
OUT = ROOT / "assettype_only_small_test"
OUT.mkdir(parents=True, exist_ok=True)

MAIN_SCRIPT = config.NETWORK_MAIN_SCRIPT

SAMPLE_SIZE = 10

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
                "PipeMaterialFamily_FROM_ASSETTYPE": material_family_from_assettype(assettype_name)
            }

    return decoder

def query_assettype_only(mod, session, layer_url, object_ids):
    query_url = layer_url.rstrip("/") + "/query"

    params = {
        "f": "json",
        "objectIds": ",".join(str(int(x)) for x in object_ids),
        "outFields": "OBJECTID,ASSETGROUP,ASSETTYPE,nominaldiameter",
        "returnGeometry": "false"
    }

    data = mod.request_json(session, query_url, params)

    if "error" in data:
        raise RuntimeError("ArcGIS query error: {0}".format(data["error"]))

    rows = []
    for feature in data.get("features") or []:
        attrs = feature.get("attributes") or {}
        rows.append({
            "OBJECTID": attrs.get("OBJECTID"),
            "ASSETGROUP": attrs.get("ASSETGROUP"),
            "ASSETTYPE": attrs.get("ASSETTYPE"),
            "nominaldiameter": attrs.get("nominaldiameter")
        })

    return pd.DataFrame(rows)

def run_layer(mod, session, cfg):
    layer_key = cfg["layer_key"]
    cache_path = CACHE / cfg["cache_name"]
    layer_url = getattr(mod, cfg["url_attr"])

    print("")
    print("============================================================")
    print(layer_key)
    print("cache:", cache_path)
    print("url:", layer_url)

    cache_df = pd.read_pickle(cache_path, compression="gzip")
    sample_objectids = cache_df["OBJECTID"].dropna().astype(int).drop_duplicates().head(SAMPLE_SIZE).tolist()

    print("sample size:", len(sample_objectids))
    print("sample OBJECTIDs:", sample_objectids)

    layer_json = mod.request_json(session, layer_url, {"f": "json"})
    print("layer name:", layer_json.get("name"))
    print("typeIdField:", layer_json.get("typeIdField"))

    decoder = build_assettype_decoder(layer_json)
    print("ASSETGROUP+ASSETTYPE decoder entries:", len(decoder))

    query_df = query_assettype_only(mod, session, layer_url, sample_objectids)

    decoded_rows = []
    for _, row in query_df.iterrows():
        key = (norm_code(row.get("ASSETGROUP")), norm_code(row.get("ASSETTYPE")))
        decoded = decoder.get(key, {})
        out = row.to_dict()
        out.update(decoded)
        out["decode_key"] = str(key)
        decoded_rows.append(out)

    result = pd.DataFrame(decoded_rows)
    result.insert(0, "layer_key", layer_key)

    print("")
    print("ASSETTYPE-ONLY RESULT")
    print(result.to_string(index=False))

    out_csv = OUT / (layer_key + "_assettype_only_sample.csv")
    result.to_csv(out_csv, index=False)
    print("")
    print("wrote:", out_csv)

    return result

def main():
    print("TEST: ASSETTYPE ONLY")
    print("No material field requested.")
    print("No cache modification.")
    print("No relocation.")
    print("sample size:", SAMPLE_SIZE)

    mod = load_main()
    session = mod.make_session()

    results = []
    for cfg in LAYERS:
        results.append(run_layer(mod, session, cfg))

    combined = pd.concat(results, ignore_index=True)
    combined_csv = OUT / "combined_assettype_only_sample.csv"
    combined.to_csv(combined_csv, index=False)

    print("")
    print("============================================================")
    print("COMBINED CSV")
    print(combined_csv)
    print("")
    print(combined.to_string(index=False))

if __name__ == "__main__":
    main()
