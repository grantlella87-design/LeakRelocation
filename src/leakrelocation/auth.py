# ruff: noqa: BLE001, S110, S112
# Broad excepts are deliberate here: keyring, browser-history and clipboard
# access all fail in environment-specific ways, and none of them should stop a
# sign-in that can still fall back to another route. Moved verbatim from the
# workflow module.
"""ArcGIS Portal authentication for the leak relocation workflow.

Extracted from the workflow module so signing in can be exercised, debugged and
fixed on its own. `scripts/arcgis_signin.py` drives it from the command line.

Two ways to capture the OAuth authorization code:

* a loopback redirect (default) - the browser lands on a page this process
  serves, which reports success and closes itself, so the code is never shown
  and no tab is left behind. Needs the redirect URI registered on the portal app.
* the out-of-band page - the fallback, which displays "SUCCESS code=..." and
  cannot be closed programmatically because the page belongs to the portal. The
  code is recovered from browser history or the clipboard.

Tokens are cached in the OS credential store through keyring, so most runs need
no browser at all.
"""
import glob
import json
import os

# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member. spec_from_file_location gives a module no parent
# package, and a relative import then fails with "attempted relative import with
# no known parent package".
import os as _os
import re
import shutil as _shutil
import sqlite3
import subprocess
import sys as _sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import keyring
import requests

_PACKAGE_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PACKAGE_PARENT not in _sys.path:
    _sys.path.insert(0, _PACKAGE_PARENT)

from leakrelocation import config
from leakrelocation.matching import clean
from leakrelocation.output import detail, fail, log, warn


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


def cached_access_token():
    token = keyring_get(config.KEYRING_ACCESS_TOKEN_USER)
    expires_raw = keyring_get(config.KEYRING_ACCESS_TOKEN_EXPIRES_USER)

    if not token or not expires_raw:
        log("No cached ArcGIS Portal access token found in Windows Credential Manager.")
        return ""

    try:
        expires_epoch = float(expires_raw)
    except Exception:
        log("Cached ArcGIS Portal access token expiration value is invalid.")
        return ""

    if time.time() + config.TOKEN_EXPIRY_SAFETY_SECONDS < expires_epoch:
        log("Using cached ArcGIS Portal access token from Windows Credential Manager.")
        return token

    log("Cached ArcGIS Portal access token is expired or inside safety window.")
    return ""


def clear_cached_access_token():
    keyring_set(config.KEYRING_ACCESS_TOKEN_USER, "")
    keyring_set(config.KEYRING_ACCESS_TOKEN_EXPIRES_USER, "0")


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


LOOPBACK_SUCCESS_PAGE = b"""<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<title>ArcGIS Loopback Sign-in Complete</title>
</head>
<body style="font-family:Arial,sans-serif;padding:2em">
<p id="msg">Signed in. Closing this tab...</p>
<script>
document.title = "ArcGIS Loopback Sign-in Complete";
window.open("", "_self");
window.close();
setTimeout(function(){
  document.getElementById("msg").textContent = "Signed in. This tab can be closed.";
}, 700);
</script>
</body>
</html>"""


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
            "expiration": "20160",
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


def close_loopback_callback_tab():
    """Best-effort close of the browser tab that displayed the loopback success page."""
    ps_script = r'''
$title = 'ArcGIS Loopback Sign-in Complete'
$shell = New-Object -ComObject WScript.Shell
Start-Sleep -Milliseconds 500
$activated = $false
for ($i = 0; $i -lt 20; $i++) {
    if ($shell.AppActivate($title)) {
        $activated = $true
        break
    }
    Start-Sleep -Milliseconds 200
}
if ($activated) {
    Start-Sleep -Milliseconds 200
    $shell.SendKeys('^w')
}
'''
    try:
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps_script,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as ex:
        detail(f"Could not auto-close loopback browser tab: {ex}")


def capture_loopback_authorization_code(authorize_url, timeout_seconds=180):
    """Serve the OAuth redirect on loopback and return the authorization code.

    Replaces reading the code off the out-of-band page, which left a tab showing
    "SUCCESS code=..." that could not be closed programmatically because the
    page belonged to the portal. Here the redirect lands on a page this process
    serves, so it can close itself and never displays the code.

    Returns None if the redirect never arrives, so the caller can fall back.
    """
    captured = {}
    ready = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            detail(f"Loopback callback received: {self.path}")
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = (query.get("code") or [None])[0]
            error = (query.get("error_description") or query.get("error") or [None])[0]
            if code:
                captured["code"] = code
            elif error:
                captured["error"] = error
            else:
                self.send_response(204)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(LOOPBACK_SUCCESS_PAGE)))
            self.end_headers()
            self.wfile.write(LOOPBACK_SUCCESS_PAGE)
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
        log("Opening browser to sign in to ArcGIS.")
        detail(f"Redirect URI: {config.LOOPBACK_REDIRECT_URI}")
        detail(f"Authorize URL: {authorize_url}")
        webbrowser.open(authorize_url, new=1, autoraise=True)
        if not ready.wait(timeout_seconds):
            warn("Sign-in did not complete within the timeout.")
            return None
    finally:
        server.shutdown()
        server.server_close()
    if captured.get("error"):
        fail(f"ArcGIS sign-in failed: {captured['error']}")
    return captured.get("code")


def interactive_access_token(session):
    cached = cached_access_token()
    if cached:
        return cached
    env_token = (
        os.environ.get("GIS_AccessToken")
        or os.environ.get("GIS_ACCESS_TOKEN")
        or os.environ.get("ARCGIS_ACCESS_TOKEN")
        or os.environ.get("ARCGIS_TOKEN")
    )
    if env_token and env_token.strip():
        log("Using ArcGIS access token from environment variable.")
        return env_token.strip()
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
        log("Opening browser for ArcGIS user authentication (out-of-band).")
        detail(f"Redirect URI: {config.ARCGIS_REDIRECT_URI}")
        auth_start_epoch = time.time()
        webbrowser.open(build_authorize_url(config.ARCGIS_REDIRECT_URI), new=1, autoraise=True)
        auth_code = get_oob_authorization_code(auth_start_epoch)
        redirect_uri = config.ARCGIS_REDIRECT_URI
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
    token = data.get("access_token") or data.get("token")
    if not token:
        fail("ArcGIS OAuth response did not include access_token/token.")
    expires_epoch = time.time() + float(data.get("expires_in") or 3600)
    keyring_set(config.KEYRING_ACCESS_TOKEN_USER, token)
    keyring_set(config.KEYRING_ACCESS_TOKEN_EXPIRES_USER, expires_epoch)
    log("Saved ArcGIS Portal access token to Windows Credential Manager.")
    close_loopback_callback_tab()
    return token

def get_arcgis_token(session=None):
    active_session = session if session is not None else requests.Session()
    return interactive_access_token(active_session)


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
        log("\n--- Clearing cached token (--force) ---")
        clear_cached_access_token()

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


# ArcGIS returns these codes when a token is rejected. 498 is invalid, 499 is
# missing or expired.
TOKEN_REJECTED_CODES = ("498", "499")


def request_json(session, url, params=None):
    """GET an ArcGIS REST endpoint as JSON, re-authenticating once if the token
    is rejected.

    A token that was valid when the run started can be rejected part way
    through, which otherwise fails the whole export. On rejection the cached
    token is discarded, a fresh one is obtained, and the request is retried once.

    Raises RuntimeError carrying the portal's own error for anything else.
    """
    request_params = dict(params or {})

    token = getattr(session, "_arcgis_access_token", None) or get_arcgis_token(session)
    session._arcgis_access_token = token
    request_params.setdefault("token", token)
    request_params.setdefault("f", "json")

    def send():
        response = session.get(
            url,
            params=request_params,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
            verify=config.VERIFY_SSL,
        )
        response.raise_for_status()
        return response.json()

    data = send()
    error = data.get("error") or {}
    if not error:
        return data

    if str(error.get("code")) in TOKEN_REJECTED_CODES:
        warn("ArcGIS rejected the access token. Signing in again and retrying once.")
        clear_cached_access_token()
        if hasattr(session, "_arcgis_access_token"):
            del session._arcgis_access_token
        request_params["token"] = get_arcgis_token(session)
        session._arcgis_access_token = request_params["token"]
        data = send()
        error = data.get("error") or {}
        if not error:
            return data

    raise RuntimeError(f"ArcGIS REST error from {url}: {json.dumps(error, indent=2)}")
