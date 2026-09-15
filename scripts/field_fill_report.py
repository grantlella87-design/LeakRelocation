"""Which fields are actually populated, in the caches and in the service.

    python scripts/field_fill_report.py             the local caches and GeoPackage
    python scripts/field_fill_report.py --service   also ask the service (needs a token)

An empty column on the map has two quite different causes, and they need opposite
fixes:

  * the service holds no value for it - nothing here can fix that, and the field
    should probably stop being asked for or displayed;
  * this project asked for it and then lost it - a stale cache written before the
    field was requested, or a column dropped between the download and the page.

Reading the cache answers the second. --service answers the first, by asking the
layer how many features have a non-null value, which costs one count query per
field and returns no data.
"""
import argparse
import sys

import pandas as pd
from _bootstrap import config

from leakrelocation.output import log, warn

# The caches the map and the workflow read, and the layer each came from.
CACHES = {
    "historic_leaks": ("historic leaks", config.HIST_LEAK_URL),
    "distribution_pipes": ("distribution pipes", config.DISTRIBUTION_PIPE_URL),
    "service_pipes": ("service pipes", config.SERVICE_PIPE_URL),
    "retired_pipes": ("retired / abandoned pipes", config.RETIRED_PIPE_URL),
}

# What each source is expected to carry, so "missing" means something. Asking a
# pipe cache about ADDRESS would otherwise report it as a stale download, when
# that field belongs to the leak layer and to nothing else.
PIPE_FIELDS = ("ASSETGROUP", "ASSETTYPE", "nominaldiameter", "operatingpressure",
               "GLOBALID", "jurisdiction", "CREATIONDATE", "dateretired")
LEAK_FIELDS = ("LMSLEAKNUMBER", "GlobalID", "jurisdiction", "REVISEDLEAKDATE",
               "ADDRESS")

EXPECTED = {
    "historic_leaks": LEAK_FIELDS,
    "distribution_pipes": PIPE_FIELDS,
    "service_pipes": PIPE_FIELDS,
    "retired_pipes": PIPE_FIELDS,
    # Written by the workflow, so these are ours to get right.
    "relocated_leaks": ("LeakKey", "LeakAddress", "LeakDate", "LinkedLayer",
                        "MatchMaterial", "MatchDiameter", "DistanceFt"),
    "relocated_leak_offset_lines": ("LeakKey", "LinkedLayer", "DistanceFt"),
    "leak_relocation_audit": ("LeakKey", "LeakAddress", "LeakDate", "LeakMaterial",
                              "LeakDiameter", "PipeMaterial", "MatchStatus",
                              "DateCheck"),
}

# Added by the map rather than by the download, so they are only reported when
# they are there.
DERIVED = ("PipeMaterialDomain", "PipeMaterialFamily", "PipeMaterialRaw")

# Fields this project does not request, asked about only with --service.
#
# REVISEDLEAKDATE and ADDRESS came back present but empty on every one of the
# 98,501 MA leaks, which makes the relocation date rule a no-op - the audit table
# reads "no_leak_date" for 98% of rows - and leaves the address blank everywhere
# it is shown. Layer 206 carries six other date fields and two other location
# fields, so the question is which of them MA populates. One count query each
# answers it, and the answer is data rather than a guess.
CANDIDATES = {
    "historic_leaks": (
        # Dates, in the order they would be preferred for "when was this leak".
        "DISCOVEREDDATE", "REPAIREDDATE", "COMPLETEDDATE", "DUEDATE",
        "CREATIONDATE", "LASTUPDATE",
        # Where the leak is.
        "NEARESTXSTREET", "CITY", "STATE", "STATEROAD",
        # Useful context for both questions.
        "LEAKSTATUS", "ORIGINALLEAKCLASS", "REVISEDLEAKCLASS",
    ),
}

# Layers written by the workflow into the output GeoPackage.
GPKG_LAYERS = ("relocated_leaks", "relocated_leak_offset_lines",
               "leak_relocation_audit")


def filled(series):
    """How many values are neither null nor blank.

    The blank check is applied to every non-numeric column, not only to dtype
    "object". pandas 3 gives a text column dtype "str", so an object-only check
    silently counted empty strings as values - which reported LeakAddress and
    LeakDate as 100% filled when every one of them was "".
    """
    if series is None or len(series) == 0:
        return 0
    cleaned = series.dropna()
    if len(cleaned) == 0:
        return 0
    import pandas as pandas_module

    if not pandas_module.api.types.is_numeric_dtype(cleaned):
        text = cleaned.astype(str).str.strip()
        cleaned = cleaned[(text != "") & (text.str.lower() != "nat")]
    return len(cleaned)


def sample(series):
    for value in series.dropna().tolist():
        text = str(value).strip()
        if text:
            return text[:40]
    return ""


def report_frame(label, frame, expected=None, every_column=False):
    """Print the fill rate of a frame's columns.

    `expected` is what this particular source should carry, so a name that is not
    there is reported as missing. Without it, every source would be asked about
    every field and a pipe cache would be blamed for having no ADDRESS.
    """
    log(f"\n=== {label}: {len(frame):,} rows ===")
    if len(frame) == 0:
        log("   (empty)")
        return
    present = [c for c in frame.columns if c != "geometry"]
    lowered = {str(c).lower(): c for c in present}
    if every_column or not expected:
        chosen, missing = present, []
    else:
        chosen = [lowered[name.lower()] for name in expected
                  if name.lower() in lowered]
        chosen += [lowered[name.lower()] for name in DERIVED
                   if name.lower() in lowered]
        missing = [name for name in expected if name.lower() not in lowered]
    log(f"   {'column':<24} {'filled':>9}  {'of rows':>8}  sample")
    for column in chosen:
        count = filled(frame[column])
        percent = count / len(frame) * 100
        mark = "  " if count else "<-"
        log(f" {mark}{column:<24} {count:>9,}  {percent:>7.1f}%  {sample(frame[column])}")
    for name in missing:
        log(f" <-{name:<24}   not in this source - it predates the field; "
            f"re-run: python run.py --refresh")


def read_cache(name):
    path = config.LAYER_CACHE_DIR / f"{name}.pkl.gz"
    if not path.exists():
        return None
    return pd.read_pickle(path, compression="gzip")


def service_counts(url, layer_label, fields, where, candidates=()):
    """Ask the layer how many features have a value for each field.

    One count query per field. Nothing is downloaded, so this is cheap even
    against a layer of a million features.
    """
    import leak_relocation_geopandas as workflow

    session = workflow.make_session()
    log(f"\n=== {layer_label} on the service ===")
    log(f"   {url}")
    total = workflow.query_count(session, url, where, layer_label)
    log(f"   {'field':<24} {'with a value':>13}  of {total:,}")
    ask(session, workflow, url, layer_label, fields, where, total)
    if candidates:
        log("   -- not requested by this project, for comparison --")
        ask(session, workflow, url, layer_label, candidates, where, total)


def ask(session, workflow, url, layer_label, fields, where, total):
    for field in fields:
        clause = f"{where} AND {field} IS NOT NULL"
        try:
            count = workflow.query_count(session, url, clause, f"{layer_label}.{field}")
        except Exception as ex:  # noqa: BLE001 - one field must not stop the report
            log(f"   {field:<24} {'could not ask':>13}  ({ex})")
            continue
        mark = "  " if count else "<-"
        share = (count / total * 100) if total else 0.0
        log(f" {mark}{field:<24} {count:>13,}  {share:>6.1f}%")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--service", action="store_true",
                        help="Also ask the service which fields hold values.")
    parser.add_argument("--all-columns", action="store_true",
                        help="Report every column, not only the ones of interest.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    log("=== Local caches ===")
    log(f"{config.LAYER_CACHE_DIR}")
    found_any = False
    for name, (label, _) in CACHES.items():
        frame = read_cache(name)
        if frame is None:
            warn(f"{name}: no cache at {config.LAYER_CACHE_DIR / (name + '.pkl.gz')}")
            continue
        found_any = True
        report_frame(f"cache {name} ({label})", frame, EXPECTED.get(name),
                     args.all_columns)

    if config.OUTPUT_GPKG.exists():
        import geopandas as gpd
        log("\n=== Output GeoPackage ===")
        log(f"{config.OUTPUT_GPKG}")
        for layer in GPKG_LAYERS:
            try:
                frame = gpd.read_file(config.OUTPUT_GPKG, layer=layer)
            except Exception as ex:  # noqa: BLE001 - a missing layer is reportable
                warn(f"{layer}: could not read ({ex})")
                continue
            report_frame(f"gpkg {layer}", frame, EXPECTED.get(layer),
                         args.all_columns)
    else:
        warn(f"No output GeoPackage at {config.OUTPUT_GPKG}")

    if not found_any:
        warn("No caches to read. Download them first: python run.py --no-view")

    if args.service:
        import leak_relocation_geopandas as workflow
        for name, (label, url) in CACHES.items():
            fields = list(EXPECTED.get(name, ()))
            try:
                service_counts(url, label, fields, workflow.WHERE_MA,
                               CANDIDATES.get(name, ()))
            except Exception as ex:  # noqa: BLE001 - keep going to the next layer
                warn(f"{label}: could not be asked ({ex})")

    log("\nReading a column marked <-:")
    log("  not in this source   the download predates the field. Re-run:")
    log("                       python run.py --refresh")
    log("  0 here, filled on the service (--service): it is being lost after the")
    log("                       download - that is a bug in this project.")
    log("  0 in both            the service does not populate it. Nothing here")
    log("                       can fill it in.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
