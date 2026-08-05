"""Report what the material-family fix changes on your own data.

Runs entirely against the local pipe caches and the production GeoPackage. No
network access, no ArcGIS token and no writes to either input.

    python scripts/report_copper_impact.py
    python scripts/report_copper_impact.py --detail   # also write per-row CSVs

Substring matching classified copper as PLASTIC, because "COPPER" contains "PE"
and PLASTIC was tested first. This reports how many cached pipe rows change
family under the fix, and how many relocated leaks were matched on a material
whose classification changed - those are the relocations to re-check.

The console summary is counts only, so it is safe to paste into a ticket or
share for review. --detail writes CSVs containing asset values; those stay in
the local output folder and should be treated like any other extract.
"""
import argparse
import sys

import pandas as pd

from _bootstrap import config
from leakrelocation import matching
from leakrelocation.assettype import ASSETTYPE_FAMILY_TERMS, UNCLASSIFIED_FAMILY

CACHES = {
    "distribution_pipes": "distribution_pipes.pkl.gz",
    "service_pipes": "service_pipes.pkl.gz",
}

MATERIAL_COLUMN_CANDIDATES = ["ASSETTYPE_DECODED", "PipeMaterialRaw", "material"]
LEAK_MATERIAL_CANDIDATES = ["MatchMaterial", "SuppLeakMaterialType", "material"]


def log(message=""):
    print(message, flush=True)


def old_family(value):
    """The previous substring-matching implementation, kept for comparison."""
    text = "" if value is None else str(value).upper()
    for family, terms in ASSETTYPE_FAMILY_TERMS.items():
        if any(term in text for term in terms):
            return family
    if not text.strip():
        return "UNKNOWN"
    return UNCLASSIFIED_FAMILY


def new_family(value):
    from leakrelocation.assettype import family_from_assettype
    return family_from_assettype(value)


def first_present(df, candidates):
    for name in candidates:
        if name in df.columns:
            return name
    return None


def compare_series(values):
    """Return a frame of distinct values whose family classification changes."""
    distinct = pd.Series(values).dropna().astype(str).value_counts()
    rows = []
    for value, count in distinct.items():
        before, after = old_family(value), new_family(value)
        if before != after:
            rows.append({"value": value, "rows": int(count),
                         "family_before": before, "family_after": after})
    return pd.DataFrame(rows).sort_values("rows", ascending=False) if rows else pd.DataFrame()


def report_caches(detail):
    log("=" * 68)
    log("PIPE CACHES")
    log("=" * 68)

    any_found = False
    total_changed = 0

    for layer, filename in CACHES.items():
        path = config.LAYER_CACHE_DIR / filename
        log("")
        log("{0}  ({1})".format(layer, path))
        if not path.exists():
            log("  cache not found - skipped")
            continue
        any_found = True

        df = pd.read_pickle(path, compression="gzip")
        column = first_present(df, MATERIAL_COLUMN_CANDIDATES)
        if column is None:
            log("  no material column found; looked for {0}".format(MATERIAL_COLUMN_CANDIDATES))
            continue

        log("  rows: {0:,}   material column: {1}".format(len(df), column))
        changed = compare_series(df[column])
        if changed.empty:
            log("  no rows change family")
            continue

        affected = int(changed["rows"].sum())
        total_changed += affected
        log("  rows changing family: {0:,} ({1:.2f}% of layer)".format(
            affected, 100.0 * affected / max(len(df), 1)))
        for _, row in changed.iterrows():
            log("    {0:<28} {1:>9,}  {2} -> {3}".format(
                row["value"][:28], row["rows"], row["family_before"], row["family_after"]))

        if detail:
            config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            out = config.OUTPUT_DIR / (layer + "_family_changes.csv")
            changed.to_csv(out, index=False)
            log("  detail: {0}".format(out))

    if not any_found:
        log("")
        log("No pipe caches found under {0}".format(config.LAYER_CACHE_DIR))
    return total_changed


def report_relocations(detail):
    log("")
    log("=" * 68)
    log("RELOCATED LEAKS")
    log("=" * 68)

    gpkg = config.OUTPUT_GPKG
    log("")
    log("GeoPackage: {0}".format(gpkg))
    if not gpkg.exists():
        log("  not found - skipped (is the share mounted?)")
        return 0

    try:
        import geopandas as gpd
        relocated = gpd.read_file(gpkg, layer="relocated_leaks")
    except Exception as exc:  # noqa: BLE001 - reported, not fatal
        log("  could not read layer 'relocated_leaks': {0}".format(exc))
        return 0

    df = pd.DataFrame(relocated.drop(columns="geometry", errors="ignore"))
    column = first_present(df, LEAK_MATERIAL_CANDIDATES)
    if column is None:
        log("  no match-material column found; looked for {0}".format(LEAK_MATERIAL_CANDIDATES))
        return 0

    log("  relocated leaks: {0:,}   material column: {1}".format(len(df), column))

    changed = compare_series(df[column])
    if changed.empty:
        log("  no relocated leak was matched on a material whose family changes")
        return 0

    affected = int(changed["rows"].sum())
    log("")
    log("  RELOCATIONS TO RE-CHECK: {0:,} ({1:.2f}% of output)".format(
        affected, 100.0 * affected / max(len(df), 1)))
    for _, row in changed.iterrows():
        log("    {0:<28} {1:>9,}  {2} -> {3}".format(
            row["value"][:28], row["rows"], row["family_before"], row["family_after"]))

    if detail:
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        mask = df[column].astype(str).isin(set(changed["value"]))
        out = config.OUTPUT_DIR / "relocations_affected_by_family_fix.csv"
        df.loc[mask].to_csv(out, index=False)
        log("  detail: {0}".format(out))

    return affected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--detail", action="store_true",
                        help="Also write per-row CSVs to the local output folder.")
    args = parser.parse_args(argv)

    log("Material family fix - impact on local data")
    log("Reads only. No network access and no ArcGIS token required.")
    log("cache dir: {0}".format(config.LAYER_CACHE_DIR))

    pipes = report_caches(args.detail)
    leaks = report_relocations(args.detail)

    log("")
    log("=" * 68)
    log("SUMMARY")
    log("=" * 68)
    log("  cached pipe rows changing family: {0:,}".format(pipes))
    log("  relocated leaks to re-check:      {0:,}".format(leaks))
    log("")
    if leaks:
        log("  Those relocations were matched using a material whose family")
        log("  classification was wrong. Re-run the workflow and compare.")
    elif pipes:
        log("  Pipe classifications change, but no existing relocation used an")
        log("  affected material.")
    else:
        log("  Nothing on this machine is affected by the fix.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
