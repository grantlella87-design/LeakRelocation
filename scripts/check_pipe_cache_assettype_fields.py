from pathlib import Path
import pandas as pd

from _bootstrap import config

ROOT = config.WORK_ROOT
CACHE = config.LAYER_CACHE_DIR

for name in ["distribution_pipes", "service_pipes"]:
    path = CACHE / (name + ".pkl.gz")
    print("")
    print("===", name, "===")
    print("path:", path)
    df = pd.read_pickle(path, compression="gzip")
    print("rows:", len(df))
    print("columns:", list(df.columns))
    for field in ["ASSETGROUP", "ASSETTYPE", "material", "nominaldiameter"]:
        print("")
        print(field, "present:", field in df.columns)
        if field in df.columns:
            print(df[field].value_counts(dropna=False).head(30))
