"""Build the relocation-distance dashboard from the output GeoPackage.

    python scripts/relocation_dashboard.py
    python scripts/relocation_dashboard.py --mode fuzzy
    python scripts/relocation_dashboard.py --both
    python scripts/relocation_dashboard.py --open
    python scripts/relocation_dashboard.py --out C:\\temp\\distance.html

Reads the `leak_relocation_audit` layer, writes one self-contained HTML file
next to the GeoPackage, and prints the headline numbers so a run in a terminal
is useful on its own.

Each diameter rule has its own output, so `--mode` picks which one to report on
and `--both` writes a page for each. When the other rule's GeoPackage is on disk
the page also carries the difference between the two - which leaks the widened
rule added, how much diameter slack each took, and whether anything was lost.

The same pages are served live by the map server at /dashboard?mode=exact and
?mode=fuzzy, which is what `python run.py` starts. This script is for the copies
you keep or send on.
"""
import argparse
import sys

from _bootstrap import config

from leakrelocation import distance_dashboard, distance_report
from leakrelocation.output import fail, log, step

AUDIT_LAYER = "leak_relocation_audit"
MODES = ("exact", "fuzzy")


def default_name(mode):
    """The strict run keeps the plain filename; the widened one is suffixed, so
    the two pages sit beside each other exactly as their GeoPackages do."""
    if mode == "exact":
        return "relocation_distance_dashboard.html"
    return f"relocation_distance_dashboard_{mode}_diameter.html"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=MODES, default=None,
                        help="Which diameter rule's output to report on. "
                             "Default: LEAKRELOCATION_DIAMETER_MODE, or exact.")
    parser.add_argument("--both", action="store_true",
                        help="Write a page for every rule whose output exists.")
    parser.add_argument("--gpkg", default="",
                        help="Report on this GeoPackage instead of the one the "
                             "mode implies.")
    parser.add_argument("--out", default="",
                        help="Where to write the HTML. Default: beside the "
                             "GeoPackage. Ignored with --both.")
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
    step(f"Relocation distance - {report.get('diameter_mode') or 'unknown'} "
         f"diameter rule")
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

    for row in report.get("diameter_match") or []:
        log(f"   diameter {row['name']:<14} {row['count']:>10,}")

    print_comparison(report.get("comparison"))

    for text in report["warnings"]:
        log(f"\nWARNING {text}")


def print_comparison(comparison):
    """The difference between the two diameter rules, in the terminal.

    The whole reason both outputs exist, so it is printed rather than left to
    whoever opens the HTML.
    """
    if not comparison:
        return
    log("")
    if not comparison.get("usable"):
        log(f"Comparison unavailable: {comparison.get('why', '')}")
        return
    base, other = comparison["base_label"], comparison["other_label"]
    step(f"{base} against {other}")
    log(f"Relocated under {base:<12}: {comparison['base_relocated']:,}")
    log(f"Relocated under {other:<12}: {comparison['other_relocated']:,}")
    log(f"Gained by {other:<18}: +{comparison['gained']:,}")
    log(f"Lost{'':<24}: {comparison['lost']:,}")
    log(f"Moved to another pipe{'':<7}: {comparison['moved_to_another_pipe']:,}")
    gained = comparison["gained_distance"]
    if gained["count"]:
        log(f"The gained relocations moved a median of {gained['median']:,.1f} ft "
            f"(p90 {gained['p90']:,.1f}, max {gained['max']:,.1f})")
    for row in comparison["gained_by_tier"]:
        log(f"   {row['name']:<16} {row['count']:>10,}")
    if comparison["lost"] or comparison["moved_to_another_pipe"]:
        log("")
        log("WARNING Widening the diameter rule should only add relocations. A "
            "non-zero Lost or Moved means a leak the strict run placed was "
            "changed, which is worth finding before either output is used.")


def read_audit_or_none(path):
    """The audit layer, or None when that GeoPackage is not there or not readable."""
    from pathlib import Path
    if not Path(path).is_file():
        return None
    import geopandas as gpd
    try:
        return gpd.read_file(str(path), layer=AUDIT_LAYER)
    except Exception as ex:  # noqa: BLE001 - as in read_audit
        log(f"WARNING could not read {path} for comparison: {ex}")
        return None


def build_one(mode, gpkg_override="", out_override=""):
    """Write one mode's page, and return its report and where it landed."""
    from pathlib import Path

    mine = Path(gpkg_override) if gpkg_override else config.output_gpkg_for(mode)
    other_mode = next(name for name in MODES if name != mode)
    theirs = config.output_gpkg_for(other_mode)

    audit = read_audit(str(mine))
    compare = read_audit_or_none(str(theirs)) if not gpkg_override else None

    report = distance_report.build_report(
        audit, source=str(mine), max_radius_ft=config.MAX_RADIUS_FT,
        compare_audit=compare, compare_source=str(theirs))

    destination = (Path(out_override) if out_override
                   else mine.parent / default_name(mode))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        distance_dashboard.dashboard_html(report), encoding="utf-8")
    return report, destination


def main(argv=None):
    args = parse_args(argv)

    if args.both:
        wanted = [mode for mode in MODES if config.output_gpkg_for(mode).is_file()]
        if not wanted:
            fail("Neither diameter rule has written an output yet.\n"
                 "  python run.py                    the exact rule\n"
                 "  python run.py --diameter fuzzy   one nominal size up or down")
    else:
        wanted = [args.mode or config.DIAMETER_MATCH_MODE or "exact"]
        if wanted[0] not in MODES:
            fail(f"Unknown diameter rule {wanted[0]!r}. Choose from: "
                 f"{', '.join(MODES)}")

    written = []
    for mode in wanted:
        report, destination = build_one(
            mode, args.gpkg, "" if args.both else args.out)
        written.append(destination)
        if not args.quiet:
            print_summary(report)

    log("")
    for destination in written:
        log(f"Dashboard: {destination}")

    if args.open_browser:
        import webbrowser
        for destination in written:
            webbrowser.open(destination.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
