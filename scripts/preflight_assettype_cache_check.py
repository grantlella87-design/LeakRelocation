"""Check the local pipe caches are ASSETTYPE-enriched and actually populated.

    python scripts/preflight_assettype_cache_check.py

Reads only. No network access and no ArcGIS token required.

Presence of a column is not enough: an enrichment run that matched no OBJECTIDs
leaves the columns in place and every value null, which looks identical to a
working cache until you notice a blank PipeMaterialRaw in the viewer. This
reports the fill rate for each column and the values actually present.

Exits non-zero when a cache is missing, unenriched, or enriched but empty.
"""
import sys

import pandas as pd
from _bootstrap import config

CACHES = ["distribution_pipes", "service_pipes"]

REQUIRED = [
    "ASSETGROUP",
    "ASSETTYPE",
    "ASSETGROUP_DECODED",
    "ASSETTYPE_DECODED",
    "ASSETTYPE_DOMAIN",
    "PipeMaterialFamily",
    "PipeMaterialRaw",
    "GradeMaterial",
    "material",
]

# Blank values in these make the workflow and the viewers wrong rather than
# merely incomplete.
MUST_BE_POPULATED = ["ASSETTYPE", "ASSETTYPE_DECODED", "PipeMaterialRaw", "material"]


def log(message=""):
    print(message, flush=True)


def check_cache(name):
    """Report on one cache. Returns True when it is usable."""
    path = config.LAYER_CACHE_DIR / (name + ".pkl.gz")
    log("")
    log("=" * 68)
    log(f"{name}")
    log("=" * 68)
    log(f"path: {path}")

    if not path.exists():
        log("MISSING CACHE - run the workflow once to populate it")
        return False

    df = pd.read_pickle(path, compression="gzip")
    log(f"rows: {len(df):,}")

    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        log("")
        log(f"NOT ENRICHED - missing columns: {missing}")
        log("  material still holds the DNV Grade field, so material families and")
        log("  viewer colours would be derived from grade data. Fix with:")
        log("    python scripts/enrich_assettype_cache.py")
        return False

    log("")
    log(f"  {'column':<22} {'filled':>12} {'of rows':>10}   sample")
    log(f"  {'-' * 22} {'-' * 12:>12} {'-' * 10:>10}   {'-' * 22}")
    empty = []
    for column in REQUIRED:
        filled = int(df[column].notna().sum())
        if filled == 0 and column in MUST_BE_POPULATED:
            empty.append(column)
        present = df[column].dropna()
        sample = ", ".join(str(v) for v in present.unique()[:3]) if len(present) else "-"
        log(f"  {column:<22} {filled:>12,} {len(df):>10,}   {sample[:40]}")

    if empty:
        log("")
        log(f"ENRICHED BUT EMPTY - no values in: {empty}")
        log("  The columns exist but every row is null, so the last enrichment")
        log("  matched no OBJECTIDs. Fix with:")
        log("    python scripts/enrich_assettype_cache.py --force")
        return False

    log("")
    log("ASSETTYPE_DECODED values:")
    log(df["ASSETTYPE_DECODED"].value_counts(dropna=False).head(20).to_string())
    log("")
    log("PipeMaterialFamily values:")
    log(df["PipeMaterialFamily"].value_counts(dropna=False).head(20).to_string())

    # A single family across the whole layer is the signature of a
    # classification that is collapsing rather than discriminating.
    families = df["PipeMaterialFamily"].dropna().unique()
    if len(families) == 1 and len(df) > 1:
        log("")
        log(f"WARNING every row classifies as {families[0]!r}. That is what a")
        log("  collapsing classification looks like - check the decoded values above.")

    log("")
    log("PASS")
    return True


def main():
    log("ASSETTYPE cache preflight")
    log(f"cache dir: {config.LAYER_CACHE_DIR}")
    results = [check_cache(name) for name in CACHES]

    log("")
    log("=" * 68)
    if all(results):
        log("All caches are enriched and populated.")
        return 0
    log("One or more caches need attention - see above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
