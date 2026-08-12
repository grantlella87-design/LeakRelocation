"""One command: sign in, download, decode, match, write, and open the map.

    python run.py                    everything, then serve the map
    python run.py --no-view          stop after the GeoPackage
    python run.py --view-only        just serve the map
    python run.py --refresh          ignore the layer caches
    python run.py --port 8800        serve on another port

Each stage used to be its own command in a particular order, and getting the
order wrong produced a failure that named the next command to run:

    RuntimeError: distribution: material is missing, so this cache is not
    ASSETTYPE-enriched. Run: python scripts/enrich_assettype_cache.py.

The material decode now happens inside the workflow, so there is no order left
to get wrong. The individual scripts still work on their own - this drives them.

The map is src/leaflet_bbox_server.py, which shows the distribution and service
pipes coloured by material along with the leaks, the relocated points and the
trace lines. There used to be a second, static map and a --pipes flag to reach
the pipes at all; that map showed a subset of this one and has been removed.
"""
import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
for folder in ("src", "scripts"):
    path = str(REPO_ROOT / folder)
    if path not in sys.path:
        sys.path.insert(0, path)

from leakrelocation import config
from leakrelocation.output import fail, log, step, warn

PIPE_CACHES = ("distribution_pipes", "service_pipes")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-view", action="store_true",
                        help="Stop after writing the GeoPackage.")
    parser.add_argument("--view-only", action="store_true",
                        help="Skip the workflow and serve the map.")
    parser.add_argument("--refresh", action="store_true",
                        help="Ignore the layer caches and re-download.")
    parser.add_argument("--port", type=int, default=None,
                        help="Port for the map. Default: the map server's own.")
    parser.add_argument("--no-browser", action="store_true",
                        help="Serve the map without opening a browser.")
    parser.add_argument("--skip-signin-check", action="store_true",
                        help="Do not verify the token before the long stages.")
    return parser.parse_args(argv)


def check_signin():
    """Prove the token works before anything long starts.

    A sign-in problem used to surface part-way through a download, after minutes
    of waiting.
    """
    step("Checking the ArcGIS token")
    from leakrelocation import auth

    session = auth.make_session()
    token = auth.get_arcgis_token(session)
    if not token:
        fail("No ArcGIS token. Run: python scripts/arcgis_signin.py --check")

    count = auth.authenticated_count(session, config.DISTRIBUTION_PIPE_URL)
    if count is None:
        fail("The token was rejected by the service. "
             "Run: python scripts/arcgis_signin.py --force")
    log(f"Token accepted. Distribution pipe layer reports {count:,} features.")


def run_workflow(refresh):
    """Download, decode the material from ASSETTYPE, match, and write."""
    step("Running the relocation workflow")
    if refresh:
        os.environ["FORCE_LAYER_REFRESH"] = "1"
        log("FORCE_LAYER_REFRESH=1: caches will be ignored.")

    import leak_relocation_geopandas as workflow

    workflow.main()
    return Path(config.OUTPUT_GPKG)


def missing_pipe_caches():
    """Which pipe caches the map needs and does not have."""
    return [name for name in PIPE_CACHES
            if not (config.LAYER_CACHE_DIR / f"{name}.pkl.gz").exists()]


def serve_map(port, open_browser):
    """Serve the map with src/leaflet_bbox_server.py.

    That module is the map viewer, and this always runs it. It used to refuse when
    a pipe cache was missing, which on a fresh checkout - which has no caches -
    meant no map at all, and a traceback instead of a viewer. A layer with no
    source is empty now and says why, both here and on the page itself.

    The pipe layers are read from the downloaded caches and served by bounding
    box, a capped number of features at a time, because they are far too large to
    write into one GeoJSON and hand to a browser.
    """
    step("Serving the map")
    import leaflet_bbox_server as map_server

    missing = missing_pipe_caches()
    if missing:
        warn(f"No downloaded cache for {missing} in {config.LAYER_CACHE_DIR}, so "
             f"those pipe layers will be empty. Everything else still draws. "
             f"To fill them: python run.py --no-view")

    map_server.serve(port=port or map_server.PORT, open_browser=open_browser)


def main(argv=None):
    args = parse_args(argv)

    if args.no_view and args.view_only:
        fail("--no-view and --view-only ask for opposite things.")

    log("=== LeakRelocation ===")
    log(f"GeoPackage: {config.OUTPUT_GPKG}")
    log(f"Layer cache: {config.LAYER_CACHE_DIR}")

    if not args.view_only:
        if not args.skip_signin_check:
            check_signin()
        gpkg = run_workflow(args.refresh)
        if not gpkg.exists():
            fail(f"The workflow finished but {gpkg} is not there.")
    elif not Path(config.OUTPUT_GPKG).exists():
        warn(f"--view-only, but there is no GeoPackage at {config.OUTPUT_GPKG}. "
             "The pipes and leaks will still draw; the relocated points and "
             "trace lines will be empty until the workflow has run.")

    if args.no_view:
        log("Done. See the map later with: python run.py --view-only")
        return 0

    serve_map(args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
