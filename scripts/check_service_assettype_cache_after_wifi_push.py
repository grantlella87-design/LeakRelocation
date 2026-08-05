from pathlib import Path
import pandas as pd

from _bootstrap import config

ROOT = config.WORK_ROOT
CACHE = config.LAYER_CACHE_DIR
path = CACHE / "service_pipes.pkl.gz"

df = pd.read_pickle(path, compression="gzip")

print("=== service_pipes cache verification ===")
print("path:", path)
print("rows:", len(df))
print("columns:", list(df.columns))

for col in [
    "ASSETGROUP",
    "ASSETTYPE",
    "ASSETGROUP_DECODED",
    "ASSETTYPE_DECODED",
    "ASSETTYPE_DOMAIN",
    "PipeMaterialRaw",
    "PipeMaterialFamily",
    "GradeMaterial",
    "material",
    "nominaldiameter"
]:
    print("")
    print(col, "present:", col in df.columns)
    if col in df.columns:
        print(df[col].value_counts(dropna=False).head(50))
