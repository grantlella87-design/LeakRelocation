"""Sign in to ArcGIS Portal, or inspect and clear the cached token.

Authentication is the part of this workflow most likely to need attention, and
it used to be reachable only by running the whole relocation job. This drives it
on its own.

    python scripts/arcgis_signin.py                # sign in if needed
    python scripts/arcgis_signin.py --status       # report the cached token
    python scripts/arcgis_signin.py --check        # test the redirect only
    python scripts/arcgis_signin.py --force        # ignore the cache, sign in
    python scripts/arcgis_signin.py --clear        # forget the cached token
    python scripts/arcgis_signin.py --test-query   # prove the token works

No token value is ever printed - only whether one exists and when it expires.
"""
import argparse
import sys

from _bootstrap import config

from leakrelocation import auth
from leakrelocation.output import log, step


def describe_configuration():
    step("Configuration")
    log(f"  portal:        {config.PORTAL_ROOT}")
    log(f"  client id:     {config.ARCGIS_CLIENT_ID}")
    log(f"  loopback:      {'on' if config.USE_LOOPBACK_OAUTH else 'off'}")
    if config.USE_LOOPBACK_OAUTH:
        log(f"  redirect uri:  {config.LOOPBACK_REDIRECT_URI}")
        log("                 this exact string must be registered on the portal app")
    log(f"  fallback uri:  {config.ARCGIS_REDIRECT_URI}  (out-of-band)")
    log(f"  keyring:       {config.KEYRING_SERVICE}")


def report_status():
    """Report whether a usable token is cached, without revealing it."""
    step("Cached token")
    import datetime as dt

    token = auth.keyring_get(config.KEYRING_ACCESS_TOKEN_USER)
    expires = auth.keyring_get(config.KEYRING_ACCESS_TOKEN_EXPIRES_USER)

    if not token:
        log("  none cached - the next run will open a browser")
        return False

    log(f"  present, {len(token)} characters")
    if not expires:
        log("  no expiry recorded, so it will be treated as expired")
        return False

    try:
        remaining = int(float(expires)) - int(dt.datetime.now(dt.UTC).timestamp())
    except (TypeError, ValueError):
        log(f"  expiry unreadable: {expires!r}")
        return False

    when = dt.datetime.fromtimestamp(int(float(expires)), dt.UTC)
    if remaining <= 0:
        log(f"  expired {when:%Y-%m-%d %H:%M} UTC")
        return False
    if remaining <= config.TOKEN_EXPIRY_SAFETY_SECONDS:
        log(f"  expires {when:%Y-%m-%d %H:%M} UTC, inside the safety window")
        return False

    log(f"  valid for {remaining // 3600}h {remaining % 3600 // 60}m (until {when:%Y-%m-%d %H:%M} UTC)")
    return True


def check_redirect(session):
    """Ask the portal whether it accepts the redirect, without a browser."""
    step("Redirect check")
    redirect = (config.LOOPBACK_REDIRECT_URI if config.USE_LOOPBACK_OAUTH
                else config.ARCGIS_REDIRECT_URI)
    url = auth.build_authorize_url(redirect)
    log(f"  authorize url: {url}")
    if auth.authorize_url_is_accepted(session, url, redirect):
        log("  the portal accepted this sign-in request")
        return True
    log("  rejected - see the warnings above")
    return False


def test_query(session):
    """A token that cannot query a layer is not working, whatever its status."""
    step("Test query")
    log(f"  {config.DISTRIBUTION_PIPE_URL}")
    try:
        count = auth.authenticated_count(session, config.DISTRIBUTION_PIPE_URL)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        log(f"  FAILED: {exc}")
        return False
    log(f"  server returned a count of {count:,} - the token works")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", action="store_true",
                        help="Report the cached token and exit.")
    parser.add_argument("--check", action="store_true",
                        help="Test whether the portal accepts the redirect, then exit.")
    parser.add_argument("--clear", action="store_true",
                        help="Forget the cached token and exit.")
    parser.add_argument("--force", action="store_true",
                        help="Ignore the cached token and sign in again.")
    parser.add_argument("--test-query", action="store_true",
                        help="After signing in, query a layer to prove the token works.")
    args = parser.parse_args(argv)

    describe_configuration()

    if args.clear:
        step("Clearing cached token")
        auth.clear_cached_access_token()
        log("  cleared")
        return 0

    if args.status:
        return 0 if report_status() else 1

    session = auth.make_session()

    if args.check:
        return 0 if check_redirect(session) else 1

    if args.force:
        step("Clearing cached token first (--force)")
        auth.clear_cached_access_token()
    else:
        report_status()

    step("Signing in")
    try:
        token = auth.get_arcgis_token(session)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        log(f"  FAILED: {exc}")
        return 1
    if not token:
        log("  FAILED: no token returned")
        return 1
    log(f"  signed in, token is {len(token)} characters")

    report_status()

    if args.test_query and not test_query(session):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
