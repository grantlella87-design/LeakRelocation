"""One command: sign in, download, decode, match, write, and open the map.

    python run.py                    everything, then serve the map
    python run.py --no-view          stop after the GeoPackage
    python run.py --view-only        just rebuild and serve the map
    python run.py --refresh          ignore the layer caches
    python run.py --port 8800        serve on another port

Each stage used to be its own command in a particular order, and getting the
order wrong produced a failure that named the next command to run:

    RuntimeError: distribution: material is missing, so this cache is not
    ASSETTYPE-enriched. Run: python scripts/enrich_assettype_cache.py.

The material decode now happens inside the workflow, so there is no order left
to get wrong. The individual scripts still work on their own - this drives them.
"""
import argparse
import os
import sys
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
for folder in ("src", "scripts"):
    path = str(REPO_ROOT / folder)
    if path not in sys.path:
        sys.path.insert(0, path)

from leakrelocation import config
from leakrelocation.output import fail, log, step, warn

DEFAULT_PORT = 8777


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-view", action="store_true",
                        help="Stop after writing the GeoPackage.")
    parser.add_argument("--view-only", action="store_true",
                        help="Skip the workflow; rebuild and serve the map.")
    parser.add_argument("--refresh", action="store_true",
                        help="Ignore the layer caches and re-download.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port for the map. Default: {DEFAULT_PORT}.")
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


def build_view():
    step("Building the map")
    import build_local_relocation_viewer as builder

    return builder.build()


def serve(index_path, port, open_browser):
    """Serve the viewer folder until interrupted."""
    import functools
    import http.server

    directory = str(Path(index_path).parent)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=directory)
    step("Serving the map")
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        fail(f"Could not serve on port {port}: {exc}. "
             f"Use --port to pick another one.")

    url = f"http://127.0.0.1:{port}/{Path(index_path).name}"
    with server:
        log(f"Serving {directory} at {url}")
        log("Press Ctrl+C to stop.")
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            log("Stopped.")


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
             "The map will be built from whatever the builder can find.")

    if args.no_view:
        log("Done. Build the map later with: python run.py --view-only")
        return 0

    index = build_view()
    serve(index, args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
