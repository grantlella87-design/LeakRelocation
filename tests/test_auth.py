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
    """Everything else should depend on the auth module, not carry the code."""

    def package_sources(self):
        folder = os.path.join(REPO_ROOT, "src", "leakrelocation")
        for name in sorted(os.listdir(folder)):
            if name.endswith(".py") and name != "auth.py":
                with open(os.path.join(folder, name), encoding="utf-8") as handle:
                    yield name, handle.read()

    def test_sign_in_is_defined_in_one_place(self):
        for module, source in self.package_sources():
            for name in ["def interactive_access_token",
                         "def capture_loopback_authorization_code",
                         "def keyring_get", "def build_authorize_url",
                         "def make_session", "def get_arcgis_token"]:
                assert name not in source, (
                    f"{name} is in {module}; it should live in leakrelocation.auth")

    def test_the_request_layer_gets_its_token_from_auth(self):
        sources = dict(self.package_sources())
        assert "auth.get_arcgis_token(" in sources["arcgis.py"]
        assert "auth.make_session(" in sources["workflow.py"]

    def test_auth_module_exposes_the_entry_points(self):
        for name in ["make_session", "get_arcgis_token", "interactive_access_token",
                     "cached_access_token", "clear_cached_access_token",
                     "build_authorize_url", "authorize_url_is_accepted",
                     "capture_loopback_authorization_code", "authenticated_count"]:
            assert hasattr(auth, name), name

    def test_logging_helpers_are_not_duplicated(self):
        """log/warn/detail live in leakrelocation.output, used by both."""
        for module, source in self.package_sources():
            if module == "output.py":
                continue
            assert "\ndef log(text):" not in source, module
            assert "\ndef detail(text):" not in source, module
            assert "\ndef warn(text):" not in source, module


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


# --- Reusing a sign-in -------------------------------------------------------

ACCESS_1 = "ACCESS-ONE-" + "a" * 40
ACCESS_2 = "ACCESS-TWO-" + "b" * 40
REFRESH = "REFRESH-" + "r" * 60
AUTH_CODE = "AUTHCODE-" + "c" * 20
SECRETS = (ACCESS_1, ACCESS_2, REFRESH, AUTH_CODE)


def stored(backend, name):
    return backend.get_password(config.KEYRING_SERVICE, name)


def save_refresh_token(backend, token=REFRESH, valid_for_days=10):
    expires = int((dt.datetime.now(dt.UTC)
                   + dt.timedelta(days=valid_for_days)).timestamp())
    backend.set_password(config.KEYRING_SERVICE,
                         config.KEYRING_REFRESH_TOKEN_USER, token)
    backend.set_password(config.KEYRING_SERVICE,
                         config.KEYRING_REFRESH_TOKEN_EXPIRES_USER, str(expires))


class Reply:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self.payload is None:
            raise ValueError("not JSON")
        return self.payload


class Portal:
    """A session that answers the token endpoint and records what it was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.posts = []
        self.lock = __import__("threading").Lock()

    def post(self, url, data=None, **kwargs):
        with self.lock:
            self.posts.append(dict(data or {}))
            reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def no_browser(monkeypatch):
    """Fail the test if anything tries to sign in through the browser."""
    def refuse(*args, **kwargs):
        raise AssertionError("the browser was opened")
    monkeypatch.setattr(auth, "sign_in_with_browser", refuse)


@pytest.fixture(autouse=True)
def no_environment_token(monkeypatch):
    for name in ("GIS_AccessToken", "GIS_ACCESS_TOKEN", "ARCGIS_ACCESS_TOKEN",
                 "ARCGIS_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def quiet(call, *args, **kwargs):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        result = call(*args, **kwargs)
    return result, buffer.getvalue()


class TestWhatASignInSaves:
    """The portal answers a sign-in with two tokens. Only the short-lived one
    used to be kept, so the browser came back as soon as it lapsed."""

    GRANT = {"access_token": ACCESS_1, "expires_in": 1800,
             "refresh_token": REFRESH, "refresh_token_expires_in": 1_209_600}

    def test_both_tokens_are_kept(self, credentials):
        assert auth.save_tokens(self.GRANT) == ACCESS_1
        assert stored(credentials, config.KEYRING_ACCESS_TOKEN_USER) == ACCESS_1
        assert stored(credentials, config.KEYRING_REFRESH_TOKEN_USER) == REFRESH

    def test_each_has_its_own_expiry(self, credentials):
        now = dt.datetime.now(dt.UTC).timestamp()
        auth.save_tokens(self.GRANT)
        access = float(stored(credentials, config.KEYRING_ACCESS_TOKEN_EXPIRES_USER))
        refresh = float(stored(credentials, config.KEYRING_REFRESH_TOKEN_EXPIRES_USER))
        assert access == pytest.approx(now + 1800, abs=5)
        assert refresh == pytest.approx(now + 1_209_600, abs=5)

    def test_a_renewal_does_not_wipe_the_refresh_token(self, credentials):
        """The reply to a refresh carries a new access token and nothing else."""
        auth.save_tokens(self.GRANT)
        auth.save_tokens({"access_token": ACCESS_2, "expires_in": 1800})
        assert stored(credentials, config.KEYRING_ACCESS_TOKEN_USER) == ACCESS_2
        assert stored(credentials, config.KEYRING_REFRESH_TOKEN_USER) == REFRESH

    def test_a_refresh_token_with_no_stated_expiry_is_still_usable(self, credentials):
        auth.save_tokens({"access_token": ACCESS_1, "refresh_token": REFRESH})
        assert auth.cached_refresh_token() == REFRESH

    def test_a_lapsed_refresh_token_is_not_offered(self, credentials):
        save_refresh_token(credentials, valid_for_days=-1)
        assert auth.cached_refresh_token() == ""

    def test_the_sign_in_asks_for_the_full_two_weeks(self):
        url = auth.build_authorize_url("http://localhost:8080/")
        assert "expiration=20160" in url


class TestRenewingWithoutTheBrowser:
    def test_a_lapsed_access_token_is_renewed_from_the_refresh_token(
            self, credentials, no_browser):
        cache_token(credentials, token=ACCESS_1, valid_for_hours=-1)
        save_refresh_token(credentials)
        portal = Portal(Reply({"access_token": ACCESS_2, "expires_in": 1800}))
        token, _ = quiet(auth.get_arcgis_token, portal)
        assert token == ACCESS_2
        assert portal.posts == [{
            "f": "json", "client_id": config.ARCGIS_CLIENT_ID,
            "grant_type": "refresh_token", "refresh_token": REFRESH}]

    def test_the_new_token_is_saved_for_the_next_run(self, credentials, no_browser):
        save_refresh_token(credentials)
        portal = Portal(Reply({"access_token": ACCESS_2, "expires_in": 1800}))
        quiet(auth.get_arcgis_token, portal)
        assert stored(credentials, config.KEYRING_ACCESS_TOKEN_USER) == ACCESS_2
        # And so the run after that asks nobody anything.
        token, _ = quiet(auth.get_arcgis_token, portal)
        assert token == ACCESS_2
        assert len(portal.posts) == 1

    def test_a_valid_access_token_asks_the_portal_nothing(
            self, credentials, no_browser):
        cache_token(credentials, token=ACCESS_1)
        save_refresh_token(credentials)
        portal = Portal(Reply({"access_token": ACCESS_2}))
        token, _ = quiet(auth.get_arcgis_token, portal)
        assert token == ACCESS_1
        assert portal.posts == []

    def test_it_says_so(self, credentials, no_browser):
        save_refresh_token(credentials)
        portal = Portal(Reply({"access_token": ACCESS_2, "expires_in": 1800}))
        _, printed = quiet(auth.get_arcgis_token, portal)
        assert "No browser needed" in printed

    def test_a_dropped_connection_is_tried_again(self, credentials, no_browser,
                                                 monkeypatch):
        """A renewal usually happens mid-download. One reset connection there
        should not be what ends a run that the data requests would survive."""
        import requests
        monkeypatch.setattr(auth.time, "sleep", lambda seconds: None)
        save_refresh_token(credentials)
        portal = Portal(requests.ConnectionError("reset"),
                        Reply({"access_token": ACCESS_2, "expires_in": 1800}))
        token, printed = quiet(auth.get_arcgis_token, portal)
        assert token == ACCESS_2
        assert len(portal.posts) == 2
        assert "retrying" in printed

    def test_a_portal_in_trouble_does_not_cost_the_sign_in(
            self, credentials, no_browser):
        """HTTP 503 says the portal is unwell, not that the refresh token is
        bad. Forgetting it there would turn an outage into a sign-in."""
        import requests
        save_refresh_token(credentials)
        portal = Portal(Reply(None, status=503))
        with redirect_stdout(io.StringIO()), pytest.raises(requests.HTTPError):
            auth.get_arcgis_token(portal)
        assert stored(credentials, config.KEYRING_REFRESH_TOKEN_USER) == REFRESH


class TestWhenTheRefreshTokenIsNoGood:
    REFUSED = Reply({"error": {"code": 498, "error": "invalid_grant",
                               "message": "Invalid refresh_token"}})

    @pytest.fixture
    def browser(self, monkeypatch):
        calls = []

        def sign_in(session):
            calls.append(session)
            return auth.save_tokens({"access_token": ACCESS_2, "expires_in": 1800,
                                     "refresh_token": "NEW-" + REFRESH,
                                     "refresh_token_expires_in": 1_209_600})
        monkeypatch.setattr(auth, "sign_in_with_browser", sign_in)
        return calls

    def test_the_browser_is_used_instead(self, credentials, browser):
        save_refresh_token(credentials)
        token, printed = quiet(auth.get_arcgis_token, Portal(self.REFUSED))
        assert token == ACCESS_2
        assert len(browser) == 1
        assert "no longer accepted" in printed

    def test_the_bad_one_is_forgotten(self, credentials, monkeypatch):
        """Otherwise every request would try it, be refused, and open a
        browser - one sign-in per request instead of one."""
        monkeypatch.setattr(auth, "sign_in_with_browser", lambda session: ACCESS_2)
        save_refresh_token(credentials)
        quiet(auth.get_arcgis_token, Portal(self.REFUSED))
        assert not stored(credentials, config.KEYRING_REFRESH_TOKEN_USER)

    def test_the_new_sign_in_replaces_it(self, credentials, browser):
        save_refresh_token(credentials)
        quiet(auth.get_arcgis_token, Portal(self.REFUSED))
        assert stored(credentials, config.KEYRING_REFRESH_TOKEN_USER) == "NEW-" + REFRESH

    def test_a_reply_that_is_not_json_counts_as_a_refusal(self, credentials, browser):
        save_refresh_token(credentials)
        token, _ = quiet(auth.get_arcgis_token, Portal(Reply(None, status=400)))
        assert token == ACCESS_2
        assert len(browser) == 1

    def test_with_nothing_saved_the_portal_is_not_asked(self, credentials, browser):
        portal = Portal(self.REFUSED)
        quiet(auth.get_arcgis_token, portal)
        assert portal.posts == []
        assert len(browser) == 1


class TestATokenTheServiceRefused:
    """Mid-run, the service says the access token is no good. It lasts
    minutes and a download lasts longer, so this is the ordinary case."""

    def test_it_is_renewed_without_the_browser(self, credentials, no_browser):
        cache_token(credentials, token=ACCESS_1)   # the store thinks it is fine
        save_refresh_token(credentials)
        portal = Portal(Reply({"access_token": ACCESS_2, "expires_in": 1800}))
        token, _ = quiet(auth.renew_access_token, portal, ACCESS_1)
        assert token == ACCESS_2

    def test_it_does_not_claim_to_be_using_the_refused_token(
            self, credentials, no_browser):
        cache_token(credentials, token=ACCESS_1)
        save_refresh_token(credentials)
        portal = Portal(Reply({"access_token": ACCESS_2, "expires_in": 1800}))
        _, printed = quiet(auth.renew_access_token, portal, ACCESS_1)
        assert "Using cached" not in printed
        assert "Renewed" in printed

    def test_the_refused_token_is_never_handed_back(self, credentials, monkeypatch):
        cache_token(credentials, token=ACCESS_1)
        monkeypatch.setattr(auth, "sign_in_with_browser", lambda session: ACCESS_2)
        token, _ = quiet(auth.renew_access_token, Portal(Reply({})), ACCESS_1)
        assert token != ACCESS_1

    def test_a_refused_environment_token_is_not_handed_back_either(
            self, credentials, monkeypatch):
        monkeypatch.setenv("ARCGIS_TOKEN", ACCESS_1)
        monkeypatch.setattr(auth, "sign_in_with_browser", lambda session: ACCESS_2)
        token, _ = quiet(auth.renew_access_token, Portal(Reply({})), ACCESS_1)
        assert token == ACCESS_2

    def test_eight_workers_refused_at_once_renew_it_once(
            self, credentials, no_browser):
        """Every download worker holds the same token and is refused in the
        same second. Each used to clear the cache and sign in for itself: eight
        browser windows, all wanting port 8080."""
        import threading
        cache_token(credentials, token=ACCESS_1)
        save_refresh_token(credentials)
        portal = Portal(Reply({"access_token": ACCESS_2, "expires_in": 1800}))
        results = []
        start = threading.Barrier(8)

        def worker():
            start.wait()
            with redirect_stdout(io.StringIO()):
                results.append(auth.renew_access_token(portal, ACCESS_1))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert results == [ACCESS_2] * 8
        assert len(portal.posts) == 1

    def test_the_request_layer_renews_and_retries(self, credentials, no_browser):
        """End to end through arcgis.request_json: refused, renewed, answered."""
        pytest.importorskip("geopandas")
        from leakrelocation import arcgis

        cache_token(credentials, token=ACCESS_1)
        save_refresh_token(credentials)

        class Service(Portal):
            def __init__(self):
                super().__init__(Reply({"access_token": ACCESS_2, "expires_in": 1800}))
                self.gets = []

            def get(self, url, params=None, **kwargs):
                self.gets.append(params["token"])
                if params["token"] == ACCESS_1:
                    return Reply({"error": {"code": 498, "message": "Invalid token."}})
                return Reply({"count": 7})

        service = Service()
        data, printed = quiet(arcgis.request_json, service,
                              "https://host/MapServer/6/query", {"f": "json"})
        assert data == {"count": 7}
        assert service.gets == [ACCESS_1, ACCESS_2]
        assert len(service.posts) == 1
        assert service._arcgis_access_token == ACCESS_2
        assert "Renewing" in printed


class TestSigningOut:
    def test_a_refused_access_token_keeps_the_sign_in(self, credentials):
        cache_token(credentials)
        save_refresh_token(credentials)
        auth.clear_cached_access_token()
        assert not stored(credentials, config.KEYRING_ACCESS_TOKEN_USER)
        assert stored(credentials, config.KEYRING_REFRESH_TOKEN_USER) == REFRESH

    def test_signing_out_forgets_both(self, credentials):
        cache_token(credentials)
        save_refresh_token(credentials)
        auth.clear_cached_tokens()
        assert not stored(credentials, config.KEYRING_ACCESS_TOKEN_USER)
        assert not stored(credentials, config.KEYRING_REFRESH_TOKEN_USER)
        assert auth.cached_refresh_token() == ""


class TestNothingSecretIsPrinted:
    """Not the access token, not the refresh token, not the code - in any of
    the messages a renewal or a sign-in prints, at any verbosity."""

    @pytest.fixture(autouse=True)
    def verbose(self, monkeypatch):
        monkeypatch.setattr(config, "VERBOSE", True)

    def assert_clean(self, printed):
        for secret in SECRETS:
            assert secret not in printed

    def test_a_renewal(self, credentials, no_browser):
        cache_token(credentials, token=ACCESS_1, valid_for_hours=-1)
        save_refresh_token(credentials)
        _, printed = quiet(auth.get_arcgis_token, Portal(
            Reply({"access_token": ACCESS_2, "expires_in": 1800})))
        assert printed
        self.assert_clean(printed)

    def test_a_refused_renewal(self, credentials, monkeypatch):
        monkeypatch.setattr(auth, "sign_in_with_browser", lambda session: ACCESS_2)
        save_refresh_token(credentials)
        _, printed = quiet(auth.get_arcgis_token, Portal(Reply(
            {"error": {"code": 498, "message": "Invalid refresh_token"}})))
        assert "Invalid refresh_token" in printed    # the reason is shown...
        self.assert_clean(printed)                   # ...the token is not

    def test_a_browser_sign_in(self, credentials, monkeypatch):
        monkeypatch.setattr(config, "USE_LOOPBACK_OAUTH", True)
        monkeypatch.setattr(auth, "authorize_url_is_accepted", lambda *a: True)
        monkeypatch.setattr(auth, "capture_loopback_authorization_code",
                            lambda url: AUTH_CODE)
        portal = Portal(Reply({"access_token": ACCESS_1, "expires_in": 1800,
                               "refresh_token": REFRESH,
                               "refresh_token_expires_in": 1_209_600}))
        token, printed = quiet(auth.get_arcgis_token, portal)
        assert token == ACCESS_1
        assert portal.posts[0]["code"] == AUTH_CODE
        assert stored(credentials, config.KEYRING_REFRESH_TOKEN_USER) == REFRESH
        assert "will not need the browser" in printed
        self.assert_clean(printed)

    def test_the_status_report(self, credentials):
        import arcgis_signin
        cache_token(credentials, token=ACCESS_1)
        save_refresh_token(credentials)
        code, printed = quiet(arcgis_signin.main, ["--status"])
        assert code == 0
        assert "refresh token: present" in printed
        self.assert_clean(printed)


class TestTheSignInCommandAndTheSavedSignIn:
    @pytest.fixture
    def cli(self):
        import arcgis_signin
        return arcgis_signin

    def test_a_lapsed_access_token_with_a_refresh_token_needs_no_browser(
            self, cli, credentials):
        cache_token(credentials, valid_for_hours=-2)
        save_refresh_token(credentials)
        code, printed = quiet(cli.main, ["--status"])
        assert code == 0
        assert "renews the access token by itself" in printed

    def test_nothing_saved_means_the_browser(self, cli, credentials):
        code, printed = quiet(cli.main, ["--status"])
        assert code == 1
        assert "will open the browser" in printed

    def test_clear_signs_out_completely(self, cli, credentials):
        cache_token(credentials)
        save_refresh_token(credentials)
        quiet(cli.main, ["--clear"])
        assert not stored(credentials, config.KEYRING_ACCESS_TOKEN_USER)
        assert not stored(credentials, config.KEYRING_REFRESH_TOKEN_USER)

    def test_renew_fetches_a_new_token_without_the_browser(
            self, cli, credentials, no_browser, monkeypatch):
        cache_token(credentials, token=ACCESS_1)
        save_refresh_token(credentials)
        portal = Portal(Reply({"access_token": ACCESS_2, "expires_in": 1800}))
        monkeypatch.setattr(auth, "make_session", lambda: portal)
        code, printed = quiet(cli.main, ["--renew"])
        assert code == 0
        assert stored(credentials, config.KEYRING_ACCESS_TOKEN_USER) == ACCESS_2
        assert len(portal.posts) == 1
        assert "No browser needed" in printed
