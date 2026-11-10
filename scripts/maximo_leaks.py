"""Relocate the enterprise (Maximo) leaks, read from SQL.

    python scripts/maximo_leaks.py --check     connection and query only
    python scripts/maximo_leaks.py --preview   read the leaks, relocate nothing
    python scripts/maximo_leaks.py             the whole run
    python scripts/maximo_leaks.py --diameter fuzzy

The second leak source. `python run.py` relocates the historic (LMS) leaks from
ArcGIS layer 206, supplemented from a committed CSV because that layer carries
neither material nor diameter. These leaks come from one SQL query that returns
their material and diameter with them, so there is no supplemental join here -
and none of the wrong-attribute risk that join carried.

Only the reading differs. Once the query has run, a Maximo leak is a point with
a material, a diameter and a facility type, exactly as a historic leak is, and
the same code matches it to the same pipes, applies the same date rule and the
same diameter rule, and writes the same three layers. That is why this is a
separate entry point rather than a separate workflow: a second copy of the
matching would drift from the first, and the two leak sets would stop being
comparable.

It writes its own GeoPackage, so a Maximo run never overwrites a historic one.

Before the first run:

    set LEAKRELOCATION_MAXIMO_DSN=Driver={ODBC Driver 18 for SQL Server};Server=...
    set LEAKRELOCATION_MAXIMO_QUERY=C:\\path\\to\\your_query.sql
    set LEAKRELOCATION_MAXIMO_CRS=EPSG:4326

The DSN is read from the environment and never written to a file or a log.
"""
import argparse
import os
import sys

from _bootstrap import config

from leakrelocation import maximo_sql
from leakrelocation.output import fail, log, step


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="Connect, run the query, report the columns and "
                             "the row count, then stop. Nothing is downloaded "
                             "and nothing is written.")
    parser.add_argument("--preview", action="store_true",
                        help="Read the leaks and report what came back, then "
                             "stop before the pipes are downloaded.")
    parser.add_argument("--limit", type=int, default=0,
                        help="Read at most this many leaks. For a first look "
                             "at a query against a large table.")
    parser.add_argument("--diameter", choices=("exact", "fuzzy"), default=None,
                        help="How strictly a leak's diameter must match the "
                             "pipe's. Same rule as run.py; each writes its own "
                             "GeoPackage.")
    parser.add_argument("--refresh", action="store_true",
                        help="Ignore the pipe layer caches and re-download.")
    parser.add_argument("--out", default="",
                        help=f"Where to write the GeoPackage. "
                             f"Default: {config.MAXIMO_OUTPUT_GPKG}")
    return parser.parse_args(argv)


def apply_diameter_mode(mode):
    """Set the diameter rule before the workflow module reads it.

    It has to happen before the import: that module binds OUTPUT_GPKG and
    DIAMETER_MATCH_MODE at import time, so setting the variable afterwards
    would write the widened output over the strict one.
    """
    if mode:
        os.environ["LEAKRELOCATION_DIAMETER_MODE"] = mode
    import importlib

    from leakrelocation import config as live
    importlib.reload(live)
    return live.DIAMETER_MATCH_MODE


def output_path(mode, override=""):
    """Where this run writes, with the diameter rule in the name when widened."""
    from pathlib import Path
    base = Path(override) if override else config.MAXIMO_OUTPUT_GPKG
    return config.output_gpkg_for(mode, base=base)


def check_only(limit):
    """Prove the connection and the query before anything long starts.

    The same idea as run.py's sign-in check: a query that is going to be
    rejected should say so now, not after the pipe layers have been downloaded.
    """
    step("Checking the Maximo connection and query")
    log(f"DSN from {config.MAXIMO_DSN_ENV}: "
        f"{maximo_sql.safe_dsn(os.environ.get(config.MAXIMO_DSN_ENV))}")
    log(f"Query file: {config.MAXIMO_QUERY_FILE}")
    leaks, attributes, counters = maximo_sql.read_leaks(limit=limit or 200)
    log("")
    log("The query runs and returns the columns this project needs.")
    log(f"Leaks usable from the rows read: {len(attributes):,}")
    if len(leaks):
        log(f"Sample leak: {dict(list(attributes.items())[0:1])}")
    log("")
    log("Next: python scripts/maximo_leaks.py --preview   (read them all)")
    log("      python scripts/maximo_leaks.py             (the whole run)")
    return 0


def main(argv=None):
    args = parse_args(argv)
    mode = apply_diameter_mode(args.diameter)
    destination = output_path(mode, args.out)

    if args.check:
        return check_only(args.limit)

    leaks, attributes, counters = maximo_sql.read_leaks(limit=args.limit or None)

    if not len(leaks):
        fail("The query returned no usable leaks, so there is nothing to "
             "relocate.\n"
             "  Check the WHERE clause, and that X/Y, Material and Diameter "
             "are populated for the rows it returns.\n"
             "  python scripts/maximo_leaks.py --check")

    if args.preview:
        step("Preview only")
        log(f"{len(leaks):,} leaks are ready to relocate.")
        log(f"They would be written to: {destination}")
        log("Run without --preview to do it.")
        return 0

    if args.refresh:
        os.environ["FORCE_LAYER_REFRESH"] = "1"
        log("FORCE_LAYER_REFRESH=1: pipe caches will be ignored.")

    # Imported here, not at the top: the workflow reads the diameter rule and
    # the refresh flag when it is imported, and both were only just set.
    from leakrelocation import auth, output, workflow

    workflow.banner(
        f"enterprise (Maximo) leaks, SQL - {config.MAXIMO_QUERY_FILE}",
        destination)
    log(f"Leak CRS: {config.MAXIMO_LEAK_CRS}")
    workflow.ensure_output_folder()

    session = auth.make_session()
    with output.timed("load pipe layers"):
        loaded = workflow.load_pipe_layers(session)

    workflow.relocate(
        session,
        leaks,
        attributes,
        loaded["distribution pipes"],
        loaded["service pipes"],
        output_gpkg=destination,
        leak_label="Maximo leaks",
        leak_source="maximo",
    )

    log("")
    log("Distance dashboard for this output:")
    log(f"  python scripts/relocation_dashboard.py --gpkg {destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
