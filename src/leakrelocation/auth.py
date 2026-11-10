# ruff: noqa: BLE001, S110, S112
# Broad excepts are deliberate here: keyring, browser-history and clipboard
# access all fail in environment-specific ways, and none of them should stop a
# sign-in that can still fall back to another route. Moved verbatim from the
# workflow module.
"""ArcGIS Portal authentication for the leak relocation workflow.

Extracted from the workflow module so signing in can be exercised, debugged and
fixed on its own. `scripts/arcgis_signin.py` drives it from the command line.

A token is obtained by the cheapest route that works, in this order:

1. the access token saved in the OS credential store, while it is still good;
2. an access token given in the environment;
3. the saved refresh token, traded for a new access token with no browser;
4. signing in through the browser.

The access token lasts minutes and the refresh token two weeks, so the browser
is needed about once a fortnight, not once a run - and never in the middle of
one. Only one of these happens at a time: the download workers all find out the
token has lapsed at the same moment, and without that they would each start a
sign-in of their own.

Two ways to capture the authorization code when the browser is needed:

* a loopback redirect (default) - the browser lands on a page this process
  serves, so the code is never shown, and the window is closed once "Allow" has
  been clicked. Needs the redirect URI registered on the portal app.
* the out-of-band page - the fallback, which displays "SUCCESS code=...". The
  code is recovered from browser history or the clipboard.

leakrelocation.signin_window opens that window and closes it again.

No token is ever printed - not the access token, not the refresh token, not the
authorization code. Only lengths and expiry times.
"""
import glob
import html
import json
import os

# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member. spec_from_file_location gives a module no parent
# package, and a relative import then fails with "attempted relative import with
# no known parent package".
import os as _os
import re
import secrets
import shutil as _shutil
import sqlite3
import sys as _sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import keyring
import requests

_PACKAGE_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PACKAGE_PARENT not in _sys.path:
    _sys.path.insert(0, _PACKAGE_PARENT)

from leakrelocation import config, signin_window
from leakrelocation.matching import clean
from leakrelocation.output import detail, fail, log, warn

# One sign-in at a time, across every thread of the process. Re-entrant because
# obtaining a token is made of steps that each take it.
_TOKEN_LOCK = threading.RLock()


def make_session():
    try:
        import truststore

        truststore.inject_into_ssl()
        detail("Injected Windows certificate store through truststore.")
    except Exception as ex:
        warn(f"truststore injection failed: {ex}")
    for proxy_var in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
        if proxy_var in os.environ:
            os.environ.pop(proxy_var, None)
            detail(f"Removed runtime proxy environment variable: {proxy_var}")
    os.environ["NO_PROXY"] = (
        "gis.nationalgrid.com,.nationalgrid.com,localhost,127.0.0.1"
    )
    os.environ["no_proxy"] = os.environ["NO_PROXY"]
    session = requests.Session()
    session.trust_env = True
    session.headers.update({"User-Agent": "HistoricLeakRelocationGeoPandas/1.0"})
    return session


def keyring_get(name):
    try:
        return keyring.get_password(config.KEYRING_SERVICE, name)
    except Exception as ex:
        warn(f"Keyring read failed for {name}: {ex}")
        return None


def keyring_set(name, value):
    try:
        keyring.set_password(config.KEYRING_SERVICE, name, str(value))
    except Exception as ex:
        warn(f"Keyring write failed for {name}: {ex}")


def read_cached_access_token():
    """(token, what to say about it). The token is "" unless it is still good."""
    token = keyring_get(config.KEYRING_ACCESS_TOKEN_USER)
    expires_raw = keyring_get(config.KEYRING_ACCESS_TOKEN_EXPIRES_USER)

    if not token or not expires_raw:
        return "", ("No cached ArcGIS Portal access token found in Windows "
                    "Credential Manager.")

    try:
        expires_epoch = float(expires_raw)
    except Exception:
        return "", "Cached ArcGIS Portal access token expiration value is invalid."

    if time.time() + config.TOKEN_EXPIRY_SAFETY_SECONDS < expires_epoch:
        return token, ("Using cached ArcGIS Portal access token from Windows "
                       "Credential Manager.")

    return "", "Cached ArcGIS Portal access token is expired or inside safety window."


def cached_access_token():
    token, message = read_cached_access_token()
    log(message)
    return token


def cached_refresh_token():
    """The saved refresh token, or "" when there is none or it has lapsed."""
    token = keyring_get(config.KEYRING_REFRESH_TOKEN_USER)
    if not token:
        return ""
    try:
        expires_epoch = float(
            keyring_get(config.KEYRING_REFRESH_TOKEN_EXPIRES_USER) or 0)
    except (TypeError, ValueError):
        expires_epoch = 0.0
    # 0 is "the portal did not say when it lapses". It is tried anyway, and the
    # portal's answer decides.
    if expires_epoch and (
            time.time() + config.TOKEN_EXPIRY_SAFETY_SECONDS >= expires_epoch):
        return ""
    return token


def clear_cached_access_token():
    """Forget the access token, keeping the sign-in it came from.

    For a token the service has refused. The refresh token is left in place, so
    the next one is fetched without a browser.
    """
    keyring_set(config.KEYRING_ACCESS_TOKEN_USER, "")
    keyring_set(config.KEYRING_ACCESS_TOKEN_EXPIRES_USER, "0")


def clear_cached_refresh_token():
    keyring_set(config.KEYRING_REFRESH_TOKEN_USER, "")
    keyring_set(config.KEYRING_REFRESH_TOKEN_EXPIRES_USER, "0")


def clear_cached_tokens():
    """Sign out: forget everything, so the next token needs the browser."""
    clear_cached_access_token()
    clear_cached_refresh_token()


def save_tokens(data):
    """Store what a token response granted, and return the access token.

    A response to a refresh carries no new refresh token, and the one already
    stored is left as it is.
    """
    token = data.get("access_token") or data.get("token")
    if not token:
        fail("ArcGIS OAuth response did not include access_token/token.")
    now = time.time()
    keyring_set(config.KEYRING_ACCESS_TOKEN_USER, token)
    keyring_set(config.KEYRING_ACCESS_TOKEN_EXPIRES_USER,
                now + float(data.get("expires_in") or 3600))
    refresh = data.get("refresh_token")
    if refresh:
        lifetime = data.get("refresh_token_expires_in")
        keyring_set(config.KEYRING_REFRESH_TOKEN_USER, refresh)
        keyring_set(config.KEYRING_REFRESH_TOKEN_EXPIRES_USER,
                    now + float(lifetime) if lifetime else 0)
    return token


def extract_oauth_code(value):
    value = clean(value)

    if not value:
        return ""

    if "code=" in value:
        try:
            parsed = urllib.parse.urlparse(value)
            query = urllib.parse.parse_qs(parsed.query)
            code = query.get("code", [""])[0]

            if code:
                return clean(code)
        except Exception:
            pass

    match = re.search(r"[?&]code=([^&\s]+)", value)

    if match:
        return clean(urllib.parse.unquote(match.group(1)))

    return value


def chrome_time_from_epoch(epoch_seconds):
    return int((float(epoch_seconds) + 11644473600) * 1000000)


def browser_history_paths():
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = []

    if local:
        candidates.extend(
            [
                os.path.join(
                    local, "Microsoft", "Edge", "User Data", "Default", "History"
                ),
                os.path.join(
                    local, "Microsoft", "Edge", "User Data", "Profile 1", "History"
                ),
                os.path.join(
                    local, "Google", "Chrome", "User Data", "Default", "History"
                ),
                os.path.join(
                    local, "Google", "Chrome", "User Data", "Profile 1", "History"
                ),
            ]
        )

        candidates.extend(
            glob.glob(
                os.path.join(
                    local, "Microsoft", "Edge", "User Data", "Profile *", "History"
                )
            )
        )
        candidates.extend(
            glob.glob(
                os.path.join(
                    local, "Google", "Chrome", "User Data", "Profile *", "History"
                )
            )
        )

    deduped = []

    for path in candidates:
        if path and path not in deduped and os.path.isfile(path):
            deduped.append(path)

    return deduped


def try_extract_oob_code_from_browser_history_since(start_epoch_seconds):
    min_chrome_time = chrome_time_from_epoch(start_epoch_seconds)

    for history_path in browser_history_paths():
        temp_path = os.path.join(
            tempfile.gettempdir(),
            f"arcgis_oauth_history_{abs(hash(history_path))}.sqlite",
        )

        try:
            _shutil.copy2(history_path, temp_path)

            conn = sqlite3.connect(temp_path)

            try:
                rows = conn.execute(
                    """
                    SELECT url, last_visit_time
                    FROM urls
                    WHERE url LIKE ?
                      AND last_visit_time >= ?
                    ORDER BY last_visit_time DESC
                    LIMIT 25
                    """,
                    ("%gis.nationalgrid.com%oauth2/approval%code=%", min_chrome_time),
                ).fetchall()
            finally:
                conn.close()

            for url, _last_visit_time in rows:
                code = extract_oauth_code(url)

                if code:
                    log(
                        "Captured fresh ArcGIS OAuth approval code from browser history."
                    )
                    return code

        except Exception:
            continue

    return ""


def wait_for_fresh_oob_code_from_history(start_epoch_seconds, seconds_to_wait=180):
    for _ in range(seconds_to_wait):
        code = try_extract_oob_code_from_browser_history_since(start_epoch_seconds)

        if code:
            return code

        time.sleep(1)

    return ""


def get_clipboard_text():
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        value = root.clipboard_get()
        root.destroy()
        return clean(value)
    except Exception:
        return ""


def get_oob_authorization_code(start_epoch_seconds):
    # Primary path: silently capture a new OOB approval URL from Edge/Chrome history.
    code = wait_for_fresh_oob_code_from_history(start_epoch_seconds)

    if code:
        return code

        # Fallback only: if silent capture fails, then use whatever user gives us.
    log(
        "Could not automatically capture a fresh ArcGIS OAuth code from browser history."
    )
    log(
        "Fallback: copy the approval URL or code from the browser, then press Enter here."
    )

    input("After copying the ArcGIS approval code or URL, press Enter: ")

    code = extract_oauth_code(get_clipboard_text())

    if code:
        log("Read ArcGIS authorization code from clipboard fallback.")
        return code

    return extract_oauth_code(
        input("Paste ArcGIS authorization code or approval URL: ").strip()
    )


# The title of the page a sign-in ends on. Each sign-in adds a random marker to
# it, so the window showing it can be found and closed without any chance of
# closing a different one.
LOOPBACK_TITLE = "ArcGIS sign-in complete"

_LOOPBACK_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<title>%(title)s</title>
</head>
<body style="font-family:Arial,sans-serif;padding:2em">
<p id="msg">%(message)s</p>
<script>
// This page was reached at an address carrying the one-time code. It has been
// used by now; take it out of the address bar and the history.
try { history.replaceState(null, "", "/"); } catch (e) {}
%(script)s
</script>
</body>
</html>"""

# The browser refuses window.close() on a tab that has been through a sign-in,
# so this only works when the portal sent the browser straight back. The window
# is closed from outside in every other case; the message is for when neither
# happened.
_CLOSE_SCRIPT = """window.close();
setTimeout(function(){
  document.getElementById("msg").textContent =
    "Signed in. You can close this window.";
}, 2500);"""


def loopback_page(title):
    """What the browser shows once the portal has sent it back signed in."""
    return (_LOOPBACK_PAGE % {
        "title": html.escape(title),
        "message": "Signed in. This window will close.",
        "script": _CLOSE_SCRIPT,
    }).encode("utf-8")


def loopback_error_page():
    """What it shows when the portal sent it back with an error instead.

    It used to show the success page here too, so someone who clicked Deny was
    told they had signed in.
    """
    return (_LOOPBACK_PAGE % {
        "title": "ArcGIS sign-in was not completed",
        "message": "ArcGIS sign-in was not completed. You can close this "
                   "window; the reason is in the window you started from.",
        "script": "",
    }).encode("utf-8")


def build_authorize_url(redirect_uri):
    """Build the portal authorize URL.

    ":" and "/" are left unescaped. They are legal in a query value under
    RFC 3986, the redirect_uri stays readable in the address bar, and it removes
    percent-encoding as a variable when the portal rejects a redirect.
    """
    query = urllib.parse.urlencode(
        {
            "client_id": config.ARCGIS_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "expiration": str(config.SIGNIN_VALID_MINUTES),
        },
        safe=":/",
    )
    return config.PORTAL_AUTHORIZE_URL + "?" + query


def authorize_url_is_accepted(session, authorize_url, redirect_uri):
    """Check whether the portal accepts this redirect_uri before opening a browser.

    A rejected redirect_uri otherwise surfaces only as a 400 page inside the
    browser, which the script cannot read, leaving no indication of the cause.
    Returns True when the request is worth handing to a browser.
    """
    probe_url = authorize_url + "&f=json"
    try:
        response = session.get(
            probe_url,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
            verify=config.VERIFY_SSL,
            allow_redirects=False,
        )
    except requests.RequestException as ex:
        detail(f"Could not pre-check the authorize URL ({ex}); continuing.")
        return True

    message = ""
    try:
        payload = response.json()
        error = payload.get("error") or {}
        message = "; ".join(
            str(part) for part in [error.get("message"), *(error.get("details") or [])] if part
        )
    except ValueError:
        # A sign-in page rather than JSON, which means the request was accepted.
        if response.status_code < 400:
            return True
        message = response.text.strip()[:300]

    if response.status_code < 400 and not message:
        return True

    warn(f"The portal rejected this sign-in request (HTTP {response.status_code}).")
    if message:
        warn(f"Portal said: {message}")
    warn(f"redirect_uri sent: {redirect_uri}")
    warn(
        "Add that exact value to the app registration's redirect URIs "
        f"(client id {config.ARCGIS_CLIENT_ID}). The string must match including the "
        "scheme, host spelling and trailing slash. Override with "
        "LEAKRELOCATION_LOOPBACK_REDIRECT_URI, or set "
        "LEAKRELOCATION_LOOPBACK_OAUTH=0 to skip the loopback flow."
    )
    return False


def capture_loopback_authorization_code(authorize_url, timeout_seconds=180):
    """Serve the OAuth redirect on loopback and return the authorization code.

    The redirect lands on a page this process serves, so the code is never
    displayed, and the window showing that page is closed as soon as the code
    has arrived - which is the moment after "Allow" is clicked.

    Returns None if the redirect never arrives, so the caller can fall back.
    """
    captured = {}
    ready = threading.Event()
    title = f"{LOOPBACK_TITLE} {secrets.token_hex(3)}"
    pages = {"code": loopback_page(title), "error": loopback_error_page()}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            # The names only. The path carries the one-time code, and that is
            # not printed at any verbosity.
            detail(f"Loopback callback received, carrying: "
                   f"{', '.join(sorted(query)) or 'nothing'}")
            code = (query.get("code") or [None])[0]
            error = (query.get("error_description") or query.get("error") or [None])[0]
            if code:
                captured["code"] = code
                page = pages["code"]
            elif error:
                captured["error"] = error
                page = pages["error"]
            else:
                self.send_response(204)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            # The address carried the code; nothing should keep a copy of it.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page)
            ready.set()

        def log_message(self, *args):
            """Silence the default per-request logging to stderr."""
            return

    try:
        server = HTTPServer(("127.0.0.1", config.LOOPBACK_OAUTH_PORT), Handler)
    except OSError as exc:
        warn(f"Could not listen on {config.LOOPBACK_REDIRECT_URI}: {exc}")
        return None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        log("Opening a window to sign in to ArcGIS.")
        detail(f"Redirect URI: {config.LOOPBACK_REDIRECT_URI}")
        detail(f"Authorize URL: {authorize_url}")
        signin_window.open_signin(authorize_url)
        if not ready.wait(timeout_seconds):
            warn("Sign-in did not complete within the timeout.")
            return None
    finally:
        server.shutdown()
        server.server_close()
    if captured.get("error"):
        fail(f"ArcGIS sign-in failed: {captured['error']}")
    signin_window.close_signin_window(title)
    return captured.get("code")


def environment_access_token():
    """An access token handed in through the environment, or ""."""
    value = (
        os.environ.get("GIS_AccessToken")
        or os.environ.get("GIS_ACCESS_TOKEN")
        or os.environ.get("ARCGIS_ACCESS_TOKEN")
        or os.environ.get("ARCGIS_TOKEN")
    )
    return (value or "").strip()


def refresh_access_token(session):
    """Trade the saved refresh token for a new access token. No browser.

    Returns "" when there is no usable refresh token, or the portal no longer
    accepts it - in which case it is forgotten, so the browser is asked for
    once rather than on every request.
    """
    refresh = cached_refresh_token()
    if not refresh:
        return ""
    payload = {
        "f": "json",
        "client_id": config.ARCGIS_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh,
    }
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            response = session.post(
                config.PORTAL_TOKEN_URL,
                data=payload,
                timeout=config.REQUEST_TIMEOUT_SECONDS,
                verify=config.VERIFY_SSL,
            )
            break
        except (requests.ConnectionError, requests.Timeout) as ex:
            # A renewal usually happens in the middle of a long download, where
            # one dropped connection should not be what ends the run.
            if attempt == attempts:
                raise
            warn(f"Could not reach the portal to renew the sign-in "
                 f"({type(ex).__name__}). Attempt {attempt} of {attempts}; "
                 f"retrying.")
            time.sleep(attempt)
    if response.status_code >= 500:
        # The portal is in trouble, which says nothing about the sign-in.
        response.raise_for_status()
    try:
        data = response.json()
    except ValueError:
        data = None
    granted = isinstance(data, dict) and "error" not in data and (
        data.get("access_token") or data.get("token"))
    if not granted:
        error = data.get("error") if isinstance(data, dict) else None
        reason = ""
        if isinstance(error, dict):
            reason = str(error.get("message") or error.get("error") or "")
        log("The saved ArcGIS sign-in is no longer accepted, so the browser is "
            "needed this once.")
        if reason:
            detail(f"Portal said: {reason}")
        clear_cached_refresh_token()
        return ""
    token = save_tokens(data)
    log("Renewed the ArcGIS access token from the saved sign-in. "
        "No browser needed.")
    return token


def sign_in_with_browser(session):
    """Sign in through the browser and store what the portal grants."""
    if not config.ARCGIS_CLIENT_ID:
        fail("config.ARCGIS_CLIENT_ID is not set.")
    auth_code = None
    redirect_uri = config.ARCGIS_REDIRECT_URI
    if config.USE_LOOPBACK_OAUTH:
        loopback_url = build_authorize_url(config.LOOPBACK_REDIRECT_URI)
        if authorize_url_is_accepted(session, loopback_url, config.LOOPBACK_REDIRECT_URI):
            auth_code = capture_loopback_authorization_code(loopback_url)
            if auth_code:
                redirect_uri = config.LOOPBACK_REDIRECT_URI
            else:
                warn("Loopback sign-in did not complete; falling back.")
    if not auth_code:
        log("Opening a window for ArcGIS user authentication (out-of-band).")
        detail(f"Redirect URI: {config.ARCGIS_REDIRECT_URI}")
        auth_start_epoch = time.time()
        signin_window.open_signin(build_authorize_url(config.ARCGIS_REDIRECT_URI))
        auth_code = get_oob_authorization_code(auth_start_epoch)
        redirect_uri = config.ARCGIS_REDIRECT_URI
        if auth_code:
            # The portal's own page, titled with the code it is showing. That
            # makes the title unique, so the same close works on it.
            signin_window.close_signin_window(f"SUCCESS code={auth_code}")
    if not auth_code:
        fail("No authorization code was captured.")
    token_payload = {
        "f": "json",
        "client_id": config.ARCGIS_CLIENT_ID,
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": redirect_uri,
    }
    response = session.post(
        config.PORTAL_TOKEN_URL,
        data=token_payload,
        timeout=config.REQUEST_TIMEOUT_SECONDS,
        verify=config.VERIFY_SSL,
    )
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        fail(
            f"ArcGIS OAuth token request failed: {json.dumps(data['error'], indent=2)}"
        )
    token = save_tokens(data)
    if data.get("refresh_token"):
        log("Signed in. Saved to Windows Credential Manager; later runs will "
            "not need the browser until the sign-in lapses.")
    else:
        log("Saved ArcGIS Portal access token to Windows Credential Manager.")
        warn("The portal did not issue a refresh token, so the browser will be "
             "needed again when this access token lapses.")
    return token


def interactive_access_token(session, rejected=""):
    """A usable access token, by the cheapest route that works.

    `rejected` is a token the service has just refused. Whatever is returned
    will not be that one, however valid the credential store believes it is.
    """
    cached, message = read_cached_access_token()
    if cached != rejected or not cached:
        # Not said of the token that was just refused: "using cached" would be
        # the opposite of what is about to happen.
        log(message)
    if cached and cached != rejected:
        return cached
    env_token = environment_access_token()
    if env_token and env_token != rejected:
        log("Using ArcGIS access token from environment variable.")
        return env_token
    return refresh_access_token(session) or sign_in_with_browser(session)


def get_arcgis_token(session=None):
    active_session = session if session is not None else requests.Session()
    with _TOKEN_LOCK:
        return interactive_access_token(active_session)


def renew_access_token(session, rejected):
    """Replace an access token the service has refused.

    Every download worker holds the same token and is refused at the same
    moment, so this is called by all of them at once. The first one in renews
    it; the rest find the new token already saved and take that, instead of
    each discarding it and renewing again.
    """
    active_session = session if session is not None else requests.Session()
    with _TOKEN_LOCK:
        return interactive_access_token(active_session, rejected=rejected or "")


def authenticated_count(session, layer_url):
    """Run a minimal authenticated count query against a layer.

    A token can look valid in the credential store and still be rejected by the
    service, so this is the only check that actually proves sign-in worked.
    Returns the feature count and raises with the portal's error otherwise.
    """
    token = get_arcgis_token(session)
    response = session.get(
        layer_url.rstrip("/") + "/query",
        params={"f": "json", "where": "1=1", "returnCountOnly": "true", "token": token},
        timeout=config.REQUEST_TIMEOUT_SECONDS,
        verify=config.VERIFY_SSL,
    )
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise RuntimeError(json.dumps(data["error"]))
    return int(data.get("count") or 0)


def main(argv=None):
    """Run a sign-in session directly: python src/leakrelocation/auth.py

    Reports the resolved configuration and the cached token, signs in when one
    is needed, then proves the token works with an authenticated count. Running
    this module is the quickest way to see why sign-in is failing without
    starting the relocation workflow.

    scripts/arcgis_signin.py wraps the same steps with --status, --check,
    --force, --clear and --test-query.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="auth.py",
        description="Sign in to ArcGIS Portal and verify the token.",
    )
    parser.add_argument("--force", action="store_true",
                        help="Ignore the cached token and sign in again.")
    parser.add_argument("--no-verify", action="store_true",
                        help="Skip the authenticated count that proves the token works.")
    args = parser.parse_args(argv)

    log("--- Configuration ---")
    log(f"  portal:       {config.PORTAL_ROOT}")
    log(f"  client id:    {config.ARCGIS_CLIENT_ID}")
    log(f"  loopback:     {'on' if config.USE_LOOPBACK_OAUTH else 'off'}")
    if config.USE_LOOPBACK_OAUTH:
        log(f"  redirect uri: {config.LOOPBACK_REDIRECT_URI}")
        log("                must be registered on the portal app, exactly")
    log(f"  fallback uri: {config.ARCGIS_REDIRECT_URI}  (out-of-band)")

    if args.force:
        log("\n--- Signing out first (--force) ---")
        clear_cached_tokens()

    session = make_session()

    log("\n--- Signing in ---")
    try:
        token = get_arcgis_token(session)
    except Exception as exc:
        log(f"  FAILED: {exc}")
        return 1
    if not token:
        log("  FAILED: no token returned")
        return 1
    # Length only. The token itself is never printed.
    log(f"  signed in, token is {len(token)} characters")

    if args.no_verify:
        return 0

    log("\n--- Verifying against a layer ---")
    log(f"  {config.DISTRIBUTION_PIPE_URL}")
    try:
        count = authenticated_count(session, config.DISTRIBUTION_PIPE_URL)
    except Exception as exc:
        log(f"  FAILED: {exc}")
        log("  A token can be cached and still be rejected by the service.")
        return 1
    log(f"  count returned: {count:,} - the token works")
    return 0


if __name__ == "__main__":
    _sys.exit(main())
