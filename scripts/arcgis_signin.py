"""Sign in to ArcGIS Portal, or inspect and clear the saved sign-in.

Authentication is the part of this workflow most likely to need attention, and
it used to be reachable only by running the whole relocation job. This drives it
on its own.

    python scripts/arcgis_signin.py                # sign in if needed
    python scripts/arcgis_signin.py --status       # report the saved sign-in
    python scripts/arcgis_signin.py --check        # test the redirect only
    python scripts/arcgis_signin.py --renew        # get a new access token
                                                   #   without the browser
    python scripts/arcgis_signin.py --force        # sign out, then sign in
    python scripts/arcgis_signin.py --clear        # sign out
    python scripts/arcgis_signin.py --test-query   # prove the token works

Two things are saved by a sign-in. The access token is what requests carry, and
lasts minutes. The refresh token lasts two weeks and is traded for a new access
token whenever one is needed, which is why the browser is rarely seen.

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


def _remaining(expires):
    """(seconds left, when) from a stored expiry, or (None, None) if unreadable."""
    import datetime as dt
    try:
        epoch = int(float(expires))
    except (TypeError, ValueError):
        return None, None
    now = int(dt.datetime.now(dt.UTC).timestamp())
    return epoch - now, dt.datetime.fromtimestamp(epoch, dt.UTC)


def _duration(seconds):
    days, rest = divmod(int(seconds), 86400)
    hours, minutes = rest // 3600, rest % 3600 // 60
    return f"{days}d {hours}h" if days else f"{hours}h {minutes}m"


def access_token_status():
    """Report the saved access token, without revealing it. True if usable."""
    token = auth.keyring_get(config.KEYRING_ACCESS_TOKEN_USER)
    expires = auth.keyring_get(config.KEYRING_ACCESS_TOKEN_EXPIRES_USER)

    if not token:
        log("  access token:  none cached")
        return False

    log(f"  access token:  present, {len(token)} characters")
    if not expires:
        log("                 no expiry recorded, so it will be treated as expired")
        return False

    remaining, when = _remaining(expires)
    if remaining is None:
        log(f"                 expiry unreadable: {expires!r}")
        return False
    if remaining <= 0:
        log(f"                 expired {when:%Y-%m-%d %H:%M} UTC")
        return False
    if remaining <= config.TOKEN_EXPIRY_SAFETY_SECONDS:
        log(f"                 expires {when:%Y-%m-%d %H:%M} UTC, inside the safety window")
        return False

    log(f"                 valid for {_duration(remaining)} "
        f"(until {when:%Y-%m-%d %H:%M} UTC)")
    return True


def refresh_token_status():
    """Report the saved refresh token, without revealing it. True if usable."""
    token = auth.keyring_get(config.KEYRING_REFRESH_TOKEN_USER)
    if not token:
        log("  refresh token: none saved")
        return False

    log(f"  refresh token: present, {len(token)} characters")
    expires = auth.keyring_get(config.KEYRING_REFRESH_TOKEN_EXPIRES_USER)
    remaining, when = _remaining(expires)
    if remaining is None or not float(expires or 0):
        log("                 the portal did not say when it lapses")
        return True
    if remaining <= config.TOKEN_EXPIRY_SAFETY_SECONDS:
        log(f"                 lapsed {when:%Y-%m-%d %H:%M} UTC")
        return False
    log(f"                 good for {_duration(remaining)} "
        f"(until {when:%Y-%m-%d %H:%M} UTC)")
    return True


def report_status():
    """Say whether the next run needs the browser. True when it does not."""
    step("Saved sign-in")
    access = access_token_status()
    refresh = refresh_token_status()
    if access:
        log("  The next run uses the access token as it is.")
    elif refresh:
        log("  The next run renews the access token by itself. No browser.")
    else:
        log("  The next run will open the browser to sign in.")
    return access or refresh


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
                        help="Report the saved sign-in and exit.")
    parser.add_argument("--check", action="store_true",
                        help="Test whether the portal accepts the redirect, then exit.")
    parser.add_argument("--clear", action="store_true",
                        help="Sign out: forget the saved tokens and exit.")
    parser.add_argument("--force", action="store_true",
                        help="Sign out, then sign in again through the browser.")
    parser.add_argument("--renew", action="store_true",
                        help="Drop the access token and fetch a new one from "
                             "the saved sign-in. Shows whether a run can carry "
                             "on without the browser.")
    parser.add_argument("--test-query", action="store_true",
                        help="After signing in, query a layer to prove the token works.")
    args = parser.parse_args(argv)

    describe_configuration()

    if args.clear:
        step("Signing out")
        auth.clear_cached_tokens()
        log("  cleared - the next run will open the browser")
        return 0

    if args.status:
        return 0 if report_status() else 1

    session = auth.make_session()

    if args.check:
        return 0 if check_redirect(session) else 1

    if args.force:
        step("Signing out first (--force)")
        auth.clear_cached_tokens()
    elif args.renew:
        step("Dropping the access token (--renew)")
        auth.clear_cached_access_token()
        log("  The refresh token is kept. If the next step opens the browser, "
            "the portal did not accept it.")
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
