"""Build the relocation-distance dashboard from the output GeoPackage.

    python scripts/relocation_dashboard.py
    python scripts/relocation_dashboard.py --open
    python scripts/relocation_dashboard.py --out C:\\temp\\distance.html

Reads the `leak_relocation_audit` layer, writes one self-contained HTML file
next to the GeoPackage, and prints the headline numbers so a run in a terminal
is useful on its own.

The same page is served live by the map server at /dashboard, which is what
`python run.py` starts. This script is for the copy you keep or send on.
"""
import argparse
import sys

from _bootstrap import config

from leakrelocation import distance_dashboard, distance_report
from leakrelocation.output import fail, log, step

AUDIT_LAYER = "leak_relocation_audit"
DEFAULT_NAME = "relocation_distance_dashboard.html"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gpkg", default=str(config.OUTPUT_GPKG),
                        help="Default: %(default)s")
    parser.add_argument("--out", default="",
                        help=f"Where to write the HTML. Default: {DEFAULT_NAME} "
                             f"beside the GeoPackage.")
    parser.add_argument("--open", action="store_true", dest="open_browser",
                        help="Open the page when it is written.")
    parser.add_argument("--quiet", action="store_true",
                        help="Write the file without printing the summary.")
    return parser.parse_args(argv)


def read_audit(path):
    """The audit layer as a frame, or a fatal message saying why not."""
    import geopandas as gpd

    from pathlib import Path
    if not Path(path).is_file():
        fail(f"No GeoPackage at {path}\n"
             f"  Run the workflow first: python run.py")
    try:
        return gpd.read_file(str(path), layer=AUDIT_LAYER)
    except Exception as ex:  # noqa: BLE001 - see below
        # Which exception an unreadable layer raises depends on the engine
        # geopandas chose: pyogrio raises DataLayerError for a missing layer and
        # DataSourceError for an unrecognised file, fiona raises its own
        # DriverError, and a half-written GeoPackage can fail inside GDAL in
        # ways neither documents. All of them mean the same thing here.
        fail(f"Could not read the {AUDIT_LAYER} layer from {path}: {ex}\n"
             f"  If the file is from an older run, rebuild it: python run.py")


def print_summary(report):
    totals, distance = report["totals"], report["distance"]
    step("Relocation distance")
    log(f"Audited leaks      : {totals['audited']:,}")
    log(f"Relocated          : {totals['relocated']:,}")
    log(f"No match           : {totals['unmatched']:,}")
    if not distance["count"]:
        log("No relocation distances to report.")
        return
    log("")
    log(f"Median move        : {distance['median']:,.1f} ft")
    log(f"Mean move          : {distance['mean']:,.1f} ft")
    for name, value in report["distance"]["percentiles"].items():
        log(f"{name:<19}: {value:,.1f} ft")
    log(f"Furthest move      : {distance['max']:,.1f} ft")
    log(f"Already on the pipe: {distance['on_pipe']:,} "
        f"(moved {distance['on_pipe_ft']:,.1f} ft or less)")
    log("")
    log("   within      relocations    % within      beyond    % beyond")
    for row in report["thresholds"]:
        whole = row["at_or_under"] + row["over"]
        beyond = (100.0 * row["over"] / whole) if whole else 0.0
        log(f"   {row['ft']:>7,.0f} ft {row['at_or_under']:>14,}"
            f"{row['pct_at_or_under']:>12.1f}%{row['over']:>12,}"
            f"{beyond:>11.1f}%")
    for text in report["warnings"]:
        log(f"\nWARNING {text}")


def main(argv=None):
    args = parse_args(argv)
    from pathlib import Path

    audit = read_audit(args.gpkg)
    report = distance_report.build_report(
        audit, source=args.gpkg, max_radius_ft=config.MAX_RADIUS_FT)

    destination = Path(args.out) if args.out else Path(args.gpkg).parent / DEFAULT_NAME
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        distance_dashboard.dashboard_html(report), encoding="utf-8")

    if not args.quiet:
        print_summary(report)
    log(f"\nDashboard: {destination}")

    if args.open_browser:
        import webbrowser
        webbrowser.open(destination.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
