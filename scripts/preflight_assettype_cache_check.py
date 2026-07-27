from pathlib import Path
import pandas as pd
import sys

ROOT = Path.home() / "Downloads" / "LeakRelocation-GeoPandas"
CACHE = ROOT / "layer_cache"

required = [
    "ASSETGROUP",
    "ASSETTYPE",
    "ASSETGROUP_DECODED",
    "ASSETTYPE_DECODED",
    "ASSETTYPE_DOMAIN",
    "PipeMaterialFamily",
    "PipeMaterialRaw",
    "GradeMaterial",
    "material"
]

ok = True

for name in ["distribution_pipes", "service_pipes"]:
    path = CACHE / (name + ".pkl.gz")
    print("")
    print("===", name, "===")
    print("path:", path)

    if not path.exists():
        print("MISSING CACHE")
        ok = False
        continue

    df = pd.read_pickle(path, compression="gzip")
    print("rows:", len(df))
    print("columns:", list(df.columns))

    missing = [c for c in required if c not in df.columns]
    if missing:
        print("MISSING REQUIRED COLUMNS:", missing)
        ok = False
    else:
        print("ASSETTYPE cache columns present: PASS")
        print("ASSETTYPE_DECODED top values:")
        print(df["ASSETTYPE_DECODED"].value_counts(dropna=False).head(20))
        print("PipeMaterialFamily top values:")
        print(df["PipeMaterialFamily"].value_counts(dropna=False).head(20))

if not ok:
    sys.exit(1)
