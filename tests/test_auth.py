"""Tests for the extracted authentication module and its command line entry.

Authentication used to be reachable only by running the whole relocation job.
These exercise it directly, with an in-memory credential store so nothing
touches a real keyring and no network call is made.
"""
import datetime as dt
import io
import os
import sys
from contextlib import redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

keyring = pytest.importorskip("keyring")

from leakrelocation import auth, config


class MemoryKeyring:
    """Stands in for the keyring module: an in-memory credential store.

    The auth module's keyring calls are redirected here rather than swapping the
    global backend, so no real credential store is touched and no platform
    backend is loaded - resolving one needs native crypto bindings that are not
    always present.
    """

    def __init__(self):
        self.store = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, password):
        self.store[(service, user)] = password

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    backend = MemoryKeyring()
    monkeypatch.setattr(auth, "keyring", backend)
    return backend


def cache_token(backend, token="T" * 64, valid_for_hours=5):
    expires = int((dt.datetime.now(dt.UTC) + dt.timedelta(hours=valid_for_hours)).timestamp())
    backend.set_password(config.KEYRING_SERVICE, config.KEYRING_ACCESS_TOKEN_USER, token)
    backend.set_password(config.KEYRING_SERVICE, config.KEYRING_ACCESS_TOKEN_EXPIRES_USER, str(expires))
    return expires


class TestModuleBoundary:
    """The workflow should depend on the auth module, not carry the code."""

    def test_workflow_imports_auth_rather_than_defining_it(self):
        path = os.path.join(REPO_ROOT, "src", "leak_relocation_geopandas.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        assert "from leakrelocation.auth import" in source
        for name in ["def interactive_access_token", "def capture_loopback_authorization_code",
                     "def keyring_get", "def build_authorize_url"]:
            assert name not in source, f"{name} should live in leakrelocation.auth"

    def test_auth_module_exposes_the_entry_points(self):
        for name in ["make_session", "get_arcgis_token", "interactive_access_token",
                     "cached_access_token", "clear_cached_access_token",
                     "build_authorize_url", "authorize_url_is_accepted",
                     "capture_loopback_authorization_code", "authenticated_count"]:
            assert hasattr(auth, name), name

    def test_logging_helpers_are_not_duplicated(self):
        """log/warn/detail live in leakrelocation.output, used by both."""
        path = os.path.join(REPO_ROOT, "src", "leak_relocation_geopandas.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        assert "from leakrelocation.output import" in source
        assert "\ndef log(text):" not in source
        assert "\ndef detail(text):" not in source


class TestCachedToken:
    """cached_access_token returns an empty string, not None, when there is no
    usable token, and clear writes empty values rather than deleting entries."""

    def test_no_token_cached(self, credentials):
        with redirect_stdout(io.StringIO()):
            assert auth.cached_access_token() == ""

    def test_valid_token_is_returned(self, credentials):
        cache_token(credentials)
        with redirect_stdout(io.StringIO()):
            assert auth.cached_access_token() == "T" * 64

    def test_expired_token_is_ignored(self, credentials):
        cache_token(credentials, valid_for_hours=-1)
        with redirect_stdout(io.StringIO()):
            assert not auth.cached_access_token()

    def test_token_inside_the_safety_window_is_ignored(self, credentials):
        expires = int((dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)).timestamp())
        credentials.set_password(config.KEYRING_SERVICE, config.KEYRING_ACCESS_TOKEN_USER, "X")
        credentials.set_password(config.KEYRING_SERVICE,
                                 config.KEYRING_ACCESS_TOKEN_EXPIRES_USER, str(expires))
        with redirect_stdout(io.StringIO()):
            assert not auth.cached_access_token()

    def test_clear_removes_it(self, credentials):
        cache_token(credentials)
        with redirect_stdout(io.StringIO()):
            auth.clear_cached_access_token()
        assert not credentials.get_password(config.KEYRING_SERVICE,
                                           config.KEYRING_ACCESS_TOKEN_USER)
        with redirect_stdout(io.StringIO()):
            assert not auth.cached_access_token()


class TestAuthenticatedCount:
    """A token can look valid in the credential store and still be rejected."""

    class Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class Session:
        def __init__(self, payload):
            self._payload = payload
            self._arcgis_access_token = "TOKEN"
            self.params = None

        def get(self, url, params=None, **kwargs):
            self.params = params
            return TestAuthenticatedCount.Response(self._payload)

    def test_returns_the_count(self, credentials):
        cache_token(credentials)
        session = self.Session({"count": 1234})
        with redirect_stdout(io.StringIO()):
            assert auth.authenticated_count(session, "https://host/MapServer/6") == 1234

    def test_sends_the_token(self, credentials):
        cache_token(credentials)
        session = self.Session({"count": 1})
        with redirect_stdout(io.StringIO()):
            auth.authenticated_count(session, "https://host/MapServer/6")
        # The token comes from the credential store via get_arcgis_token,
        # not from whatever the session happens to carry.
        assert session.params["token"] == "T" * 64
        assert session.params["returnCountOnly"] == "true"

    def test_portal_error_is_raised(self, credentials):
        cache_token(credentials)
        session = self.Session({"error": {"code": 498, "message": "Invalid token"}})
        with redirect_stdout(io.StringIO()), pytest.raises(RuntimeError, match="Invalid token"):
            auth.authenticated_count(session, "https://host/MapServer/6")


class TestSignInCommand:
    @pytest.fixture
    def cli(self):
        import arcgis_signin
        return arcgis_signin

    def run(self, cli, *argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.main(list(argv))
        return code, buffer.getvalue()

    def test_status_reports_missing_token(self, cli, credentials):
        code, output = self.run(cli, "--status")
        assert code == 1
        assert "none cached" in output

    def test_status_reports_a_valid_token(self, cli, credentials):
        cache_token(credentials)
        code, output = self.run(cli, "--status")
        assert code == 0
        assert "valid for" in output

    def test_status_never_prints_the_token(self, cli, credentials):
        cache_token(credentials, token="SUPERSECRETTOKENVALUE")
        _, output = self.run(cli, "--status")
        assert "SUPERSECRETTOKENVALUE" not in output

    def test_status_reports_an_expired_token(self, cli, credentials):
        cache_token(credentials, valid_for_hours=-2)
        code, output = self.run(cli, "--status")
        assert code == 1
        assert "expired" in output

    def test_clear_forgets_the_token(self, cli, credentials):
        cache_token(credentials)
        code, _ = self.run(cli, "--clear")
        assert code == 0
        assert not credentials.get_password(config.KEYRING_SERVICE,
                                           config.KEYRING_ACCESS_TOKEN_USER)

    def test_configuration_is_reported_without_secrets(self, cli, credentials):
        _, output = self.run(cli, "--status")
        assert config.PORTAL_ROOT in output
        assert config.LOOPBACK_REDIRECT_URI in output
        # The client id is a public app identifier; no token or secret here.
        assert "token is" not in output
