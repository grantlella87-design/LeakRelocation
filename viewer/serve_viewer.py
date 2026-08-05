"""Serve the local relocation viewer and open it in a browser.

    python viewer/serve_viewer.py                 # serves index_basic.html
    python viewer/serve_viewer.py --port 8800
    python viewer/serve_viewer.py --page index_with_pipes.html
"""
import argparse
import functools
import http.server
import pathlib
import webbrowser

HERE = pathlib.Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--page", default="index_basic.html")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if not (HERE / args.page).exists():
        available = sorted(p.name for p in HERE.glob("*.html"))
        listing = ", ".join(available) or "none"
        parser.error(f"{args.page} not found in {HERE}. Available: {listing}")

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(HERE))
    with http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
        url = f"http://127.0.0.1:{args.port}/{args.page}"
        print(f"Serving {HERE} at {url}", flush=True)
        print("Press Ctrl+C to stop.", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.", flush=True)


if __name__ == "__main__":
    main()
