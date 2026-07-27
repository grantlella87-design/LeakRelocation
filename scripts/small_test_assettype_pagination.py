from pathlib import Path
import importlib.util
import json

ROOT = Path.home() / "Downloads" / "LeakRelocation-GeoPandas"
MAIN_SCRIPT = Path(r"\\ngusnasnwh001\gasne\GasNE Shared\Shared\ENG\Complex Team\GIS AutoPrint\Distribution Leak Relocation\Arcpy Code\leak reolcation - geopandas.py")

def load_main():
    spec = importlib.util.spec_from_file_location("leak_relocation_geopandas", str(MAIN_SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def test_layer(mod, session, name, layer_url):
    print("")
    print("===", name, "===")
    query_url = layer_url.rstrip("/") + "/query"
    params = {
        "f": "json",
        "where": "1=1",
        "outFields": "OBJECTID,ASSETGROUP,ASSETTYPE,material,nominaldiameter",
        "returnGeometry": "false",
        "orderByFields": "OBJECTID",
        "resultOffset": 0,
        "resultRecordCount": 5
    }
    data = mod.request_json(session, query_url, params)
    print(json.dumps(data, indent=2, ensure_ascii=False)[:6000])

def main():
    mod = load_main()
    session = mod.make_session()
    test_layer(mod, session, "distribution_pipes", mod.DISTRIBUTION_PIPE_URL)
    test_layer(mod, session, "service_pipes", mod.SERVICE_PIPE_URL)

if __name__ == "__main__":
    main()
