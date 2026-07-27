from pathlib import Path
import importlib.util
import json
import pandas as pd

ROOT = Path.home() / "Downloads" / "LeakRelocation-GeoPandas"
MAIN_SCRIPT = Path(r"\\ngusnasnwh001\gasne\GasNE Shared\Shared\ENG\Complex Team\GIS AutoPrint\Distribution Leak Relocation\Arcpy Code\leak reolcation - geopandas.py")

def log(x):
    print(str(x), flush=True)

def load_main():
    spec = importlib.util.spec_from_file_location("leak_relocation_geopandas", str(MAIN_SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def norm(v):
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except Exception:
        pass
    s = str(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s

def get_field(layer_json, name):
    for f in layer_json.get("fields", []) or []:
        if str(f.get("name")).lower() == name.lower():
            return f
    return None

def decode_assettype(layer_json, assetgroup, assettype):
    type_id_field = layer_json.get("typeIdField")
    if not type_id_field:
        return None

    for subtype in layer_json.get("types", []) or []:
        if norm(subtype.get("id")) != norm(assetgroup):
            continue

        domains = subtype.get("domains") or {}
        domain = None
        for fname, dom in domains.items():
            if str(fname).lower() == "assettype":
                domain = dom
                break

        if not domain:
            return None

        for cv in domain.get("codedValues") or []:
            if norm(cv.get("code")) == norm(assettype):
                return {
                    "subtype_id": subtype.get("id"),
                    "subtype_name": subtype.get("name"),
                    "domain_name": domain.get("name"),
                    "assettype_code": cv.get("code"),
                    "assettype_decoded": cv.get("name")
                }

    return None

def test_layer(mod, session, layer_key, layer_url):
    log("")
    log("============================================================")
    log(layer_key)
    log("Layer URL: " + layer_url)

    layer_json = mod.request_json(session, layer_url, {"f": "json"})

    log("")
    log("Layer name: " + str(layer_json.get("name")))
    log("typeIdField: " + str(layer_json.get("typeIdField")))

    for field_name in ["ASSETGROUP", "ASSETTYPE", "material", "nominaldiameter"]:
        f = get_field(layer_json, field_name)
        log("")
        log("FIELD " + field_name + ":")
        if not f:
            log("  NOT FOUND IN METADATA")
        else:
            log("  name: " + str(f.get("name")))
            log("  alias: " + str(f.get("alias")))
            log("  type: " + str(f.get("type")))
            log("  domain: " + str((f.get("domain") or {}).get("name")))

    log("")
    log("Subtype count: " + str(len(layer_json.get("types") or [])))
    for subtype in (layer_json.get("types") or [])[:10]:
        domains = subtype.get("domains") or {}
        assettype_domain = None
        for fname, dom in domains.items():
            if str(fname).lower() == "assettype":
                assettype_domain = dom
                break
        log("Subtype id={0}, name={1}, has ASSETTYPE domain={2}, domain={3}".format(
            subtype.get("id"),
            subtype.get("name"),
            assettype_domain is not None,
            None if not assettype_domain else assettype_domain.get("name")
        ))
        if assettype_domain:
            preview = assettype_domain.get("codedValues", [])[:12]
            log("  ASSETTYPE coded values preview: " + json.dumps(preview, ensure_ascii=False))

    query_url = layer_url.rstrip("/") + "/query"

    # Critical direct test: request the actual fields explicitly.
    params = {
        "f": "json",
        "where": "1=1",
        "outFields": "OBJECTID,ASSETGROUP,ASSETTYPE,material,nominaldiameter",
        "returnGeometry": "false",
        "resultRecordCount": 10
    }

    log("")
    log("Direct query params:")
    log(json.dumps(params, indent=2))

    data = mod.request_json(session, query_url, params)

    if "error" in data:
        log("")
        log("QUERY ERROR:")
        log(json.dumps(data["error"], indent=2))
        return

    features = data.get("features") or []
    log("")
    log("Returned feature count: " + str(len(features)))

    for i, feat in enumerate(features):
        attrs = feat.get("attributes") or {}
        decoded = decode_assettype(layer_json, attrs.get("ASSETGROUP"), attrs.get("ASSETTYPE"))
        log("")
        log("Feature " + str(i + 1))
        log("Attributes returned:")
        log(json.dumps(attrs, indent=2, ensure_ascii=False))
        log("Decoded ASSETGROUP/ASSETTYPE:")
        log(json.dumps(decoded, indent=2, ensure_ascii=False))

def main():
    log("Main script: " + str(MAIN_SCRIPT))
    mod = load_main()
    session = mod.make_session()

    test_layer(mod, session, "distribution_pipes", mod.DISTRIBUTION_PIPE_URL)
    test_layer(mod, session, "service_pipes", mod.SERVICE_PIPE_URL)

if __name__ == "__main__":
    main()
