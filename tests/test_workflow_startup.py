"""Tests for output location, sign-in capture, cache freshness and verbosity."""
import importlib.util
import io
import json
import os
import sys
import threading
import time
import urllib.request
from contextlib import redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from leakrelocation import auth as lr_auth
from leakrelocation import config

pytest.importorskip("geopandas")
pytest.importorskip("keyring")


@pytest.fixture(scope="module")
def lr():
    path = os.path.join(REPO_ROOT, "src", "leak_relocation_geopandas.py")
    spec = importlib.util.spec_from_file_location("lr_startup", path)
    module = importlib.util.module_from_spec(spec)
    with redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)
    return module


class TestOutputIsLocal:
    def test_geopackage_defaults_under_the_work_root(self):
        # Writing straight to the share made each run depend on network write
        # throughput and left a broken shared copy on a partial write.
        assert config.OUTPUT_GPKG == config.OUTPUT_DIR / "HistoricLeakRelocation.gpkg"
        assert str(config.PROJECT_DIR) not in str(config.OUTPUT_GPKG)

    def test_share_location_still_known_for_publishing(self):
        assert config.PUBLISHED_OUTPUT_GPKG.parent == config.PROJECT_DIR

    def test_env_override_restores_the_share(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LEAKRELOCATION_OUTPUT_GPKG", str(tmp_path / "shared.gpkg"))
        import importlib
        reloaded = importlib.reload(config)
        try:
            assert reloaded.OUTPUT_GPKG == tmp_path / "shared.gpkg"
        finally:
            monkeypatch.delenv("LEAKRELOCATION_OUTPUT_GPKG")
            importlib.reload(config)


class TestLoopbackSignIn:
    """The out-of-band page left a tab showing SUCCESS code=... that could not be
    closed programmatically. The redirect now lands on a page this process
    serves, and the browser is opened only once that server is listening, so a
    fast redirect cannot arrive before the socket is bound.

    webbrowser.open is replaced with the callback request itself, which is what
    the browser would do, so these exercise that ordering.
    """

    AUTHORIZE_URL = "https://portal.example/oauth2/authorize?client_id=x"

    @staticmethod
    def _browser_that_calls_back(query, threads, delay=0.3, sink=None):
        """Stand in for the browser: fetch the loopback callback.

        The thread is handed back through `threads` so the test can wait for it.
        Capture returns the moment the server has the code, which is before this
        side has necessarily finished reading the response body, so asserting on
        `sink` without joining first is a race - one that only lost under the
        load of the full suite.
        """
        def opener(url, *args, **kwargs):
            def run():
                time.sleep(delay)
                target = f"http://127.0.0.1:{config.LOOPBACK_OAUTH_PORT}/{query}"
                try:
                    with urllib.request.urlopen(target, timeout=5) as response:
                        if sink is not None:
                            sink["status"] = response.status
                            sink["body"] = response.read().decode()
                except Exception as exc:  # noqa: BLE001 - surfaced via sink
                    if sink is not None:
                        sink["error"] = str(exc)

            thread = threading.Thread(target=run, daemon=True)
            threads.append(thread)
            thread.start()
            return True

        return opener

    def capture(self, monkeypatch, query, sink=None, timeout=15):
        threads = []
        monkeypatch.setattr(lr_auth.webbrowser, "open",
                            self._browser_that_calls_back(query, threads, sink=sink))
        with redirect_stdout(io.StringIO()):
            code = lr_auth.capture_loopback_authorization_code(
                self.AUTHORIZE_URL, timeout_seconds=timeout)
        for thread in threads:
            thread.join(timeout=5)
        return code

    def test_captures_the_code(self, monkeypatch):
        assert self.capture(monkeypatch, "?code=ABC123&state=x") == "ABC123"

    def test_page_never_shows_the_code(self, monkeypatch):
        sink = {}
        self.capture(monkeypatch, "?code=SECRETCODE", sink=sink)
        assert sink["status"] == 200
        assert "SECRETCODE" not in sink["body"]

    def test_page_closes_itself(self, monkeypatch):
        sink = {}
        self.capture(monkeypatch, "?code=X", sink=sink)
        assert "window.close()" in sink["body"]

    def test_releases_the_port_for_the_next_run(self, monkeypatch):
        for expected in ["FIRST", "SECOND"]:
            assert self.capture(monkeypatch, f"?code={expected}") == expected

    def test_the_browser_is_opened_with_the_authorize_url(self, monkeypatch):
        opened = []

        def opener(url, *args, **kwargs):
            opened.append(url)
            threading.Thread(target=lambda: (
                time.sleep(0.3),
                urllib.request.urlopen(
                    f"http://127.0.0.1:{config.LOOPBACK_OAUTH_PORT}/?code=Z", timeout=5),
            ), daemon=True).start()
            return True

        monkeypatch.setattr(lr_auth.webbrowser, "open", opener)
        with redirect_stdout(io.StringIO()):
            lr_auth.capture_loopback_authorization_code(self.AUTHORIZE_URL, timeout_seconds=15)
        assert opened == [self.AUTHORIZE_URL]

    def test_ignores_requests_that_are_not_the_redirect(self, monkeypatch):
        def opener(url, *args, **kwargs):
            def run():
                port = config.LOOPBACK_OAUTH_PORT
                time.sleep(0.3)
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/favicon.ico", timeout=5)
                except Exception:  # noqa: BLE001,S110 - a 204 with no body is fine
                    pass
                time.sleep(0.3)
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/?code=LATER", timeout=5)
                except Exception:  # noqa: BLE001,S110
                    pass

            threading.Thread(target=run, daemon=True).start()
            return True

        monkeypatch.setattr(lr_auth.webbrowser, "open", opener)
        with redirect_stdout(io.StringIO()):
            code = lr_auth.capture_loopback_authorization_code(
                self.AUTHORIZE_URL, timeout_seconds=15)
        assert code == "LATER"

    def test_timeout_returns_none_so_the_caller_can_fall_back(self, monkeypatch):
        monkeypatch.setattr(lr_auth.webbrowser, "open", lambda *a, **k: True)
        started = time.time()
        with redirect_stdout(io.StringIO()):
            code = lr_auth.capture_loopback_authorization_code(
                self.AUTHORIZE_URL, timeout_seconds=2)
        assert code is None
        assert time.time() - started < 10


class TestAuthorizeUrl:
    """The redirect_uri travels as a query value. ":" and "/" are legal there,
    so leaving them unescaped keeps the URL readable and removes
    percent-encoding as a variable when the portal rejects a redirect."""

    def test_loopback_redirect_is_not_over_encoded(self, lr):
        url = lr_auth.build_authorize_url("http://localhost:8770/")
        assert "redirect_uri=http://localhost:8770/" in url
        assert "%3A%2F%2F" not in url

    def test_redirect_uri_round_trips(self, lr):
        import urllib.parse
        for redirect in ["http://localhost:8770/", "http://127.0.0.1:9000",
                         "urn:ietf:wg:oauth:2.0:oob"]:
            url = lr_auth.build_authorize_url(redirect)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            assert query["redirect_uri"][0] == redirect

    def test_required_oauth_parameters_present(self, lr):
        import urllib.parse
        url = lr_auth.build_authorize_url("http://localhost:8770/")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert query["response_type"] == ["code"]
        assert query["client_id"] == [config.ARCGIS_CLIENT_ID]
        assert "expiration" in query


class _Response:
    def __init__(self, status, body, is_json=True):
        self.status_code = status
        self._body = body
        self._is_json = is_json

    def json(self):
        if not self._is_json:
            raise ValueError("not json")
        return self._body

    @property
    def text(self):
        return str(self._body)


class _Session:
    def __init__(self, response):
        self._response = response

    def get(self, url, **kwargs):
        return self._response


class TestAuthorizePreflight:
    """A rejected redirect_uri otherwise appears only as a 400 page inside the
    browser, which the script cannot read."""

    def test_rejects_and_reports_a_400(self, lr, capsys):
        response = _Response(400, {"error": {
            "message": "Invalid redirect_uri",
            "details": ["redirect_uri is not registered"]}})
        assert lr_auth.authorize_url_is_accepted(_Session(response), "u", "http://localhost:8770/") is False
        output = capsys.readouterr().out
        assert "Invalid redirect_uri" in output
        assert "redirect_uri is not registered" in output

    def test_names_the_redirect_uri_that_was_sent(self, lr, capsys):
        response = _Response(400, {"error": {"message": "bad"}})
        lr_auth.authorize_url_is_accepted(_Session(response), "u", "http://localhost:8770/")
        assert "http://localhost:8770/" in capsys.readouterr().out

    def test_accepts_a_sign_in_page(self, lr):
        response = _Response(200, "<html>sign in</html>", is_json=False)
        assert lr_auth.authorize_url_is_accepted(_Session(response), "u", "r") is True

    def test_rejects_an_error_payload_returned_with_200(self, lr, capsys):
        response = _Response(200, {"error": {"message": "Invalid client_id"}})
        assert lr_auth.authorize_url_is_accepted(_Session(response), "u", "r") is False
        assert "Invalid client_id" in capsys.readouterr().out

    def test_accepts_clean_json(self, lr):
        assert lr_auth.authorize_url_is_accepted(_Session(_Response(200, {"ok": True})), "u", "r") is True

    def test_unreachable_preflight_does_not_block_sign_in(self, lr):
        import requests

        class Unreachable:
            def get(self, *args, **kwargs):
                raise requests.RequestException("no route to host")

        with redirect_stdout(io.StringIO()):
            assert lr_auth.authorize_url_is_accepted(Unreachable(), "u", "r") is True


class TestRedirectUriIsConfigurable:
    """The portal matches redirect_uri as a string, so host spelling and the
    trailing slash have to be settable to match the app registration."""

    def test_default_uses_localhost_with_trailing_slash(self):
        assert config.LOOPBACK_REDIRECT_URI == f"http://localhost:{config.LOOPBACK_OAUTH_PORT}/"

    def test_whole_uri_can_be_overridden(self, monkeypatch):
        import importlib
        monkeypatch.setenv("LEAKRELOCATION_LOOPBACK_REDIRECT_URI", "http://127.0.0.1:9999")
        try:
            reloaded = importlib.reload(config)
            assert reloaded.LOOPBACK_REDIRECT_URI == "http://127.0.0.1:9999"
        finally:
            monkeypatch.delenv("LEAKRELOCATION_LOOPBACK_REDIRECT_URI")
            importlib.reload(config)

    def test_host_can_be_overridden(self, monkeypatch):
        import importlib
        monkeypatch.setenv("LEAKRELOCATION_LOOPBACK_HOST", "127.0.0.1")
        try:
            reloaded = importlib.reload(config)
            assert reloaded.LOOPBACK_REDIRECT_URI.startswith("http://127.0.0.1:")
        finally:
            monkeypatch.delenv("LEAKRELOCATION_LOOPBACK_HOST")
            importlib.reload(config)


class TestCacheFreshness:
    """A recent cache is trusted without a server round trip. Checking costs a
    metadata request plus two count queries per layer and needs a token."""

    @pytest.fixture
    def cache(self, lr, tmp_path, monkeypatch):
        monkeypatch.setattr(lr, "LAYER_CACHE_FOLDER", str(tmp_path))
        monkeypatch.setattr(lr, "USE_LAYER_CACHE", True)
        monkeypatch.setattr(lr, "FORCE_LAYER_REFRESH", False)
        return tmp_path

    def write_cache(self, lr, layer, url, where, cached_utc):
        data_path, meta_path = lr.layer_cache_paths(layer)
        with open(data_path, "wb") as handle:
            handle.write(b"placeholder")
        with open(meta_path, "w", encoding="utf-8") as handle:
            json.dump({"layer_url": url, "where_clause": where,
                       "cached_utc": cached_utc}, handle)

    def test_missing_cache_has_no_age(self, lr, cache):
        assert lr.cache_age_seconds("nothing_here", "u", "w") is None

    def test_fresh_cache_reports_small_age(self, lr, cache):
        import datetime as dt
        now = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
        self.write_cache(lr, "fresh_layer", "u", "w", now)
        age = lr.cache_age_seconds("fresh_layer", "u", "w")
        assert age is not None and age < 60

    def test_old_cache_reports_large_age(self, lr, cache):
        import datetime as dt
        old = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=5)).isoformat().replace("+00:00", "Z")
        self.write_cache(lr, "old_layer", "u", "w", old)
        age = lr.cache_age_seconds("old_layer", "u", "w")
        assert age > 4 * 3600

    def test_changed_url_invalidates(self, lr, cache):
        import datetime as dt
        now = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
        self.write_cache(lr, "moved_layer", "old_url", "w", now)
        assert lr.cache_age_seconds("moved_layer", "new_url", "w") is None

    def test_changed_where_invalidates(self, lr, cache):
        import datetime as dt
        now = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
        self.write_cache(lr, "filtered_layer", "u", "old_where", now)
        assert lr.cache_age_seconds("filtered_layer", "u", "new_where") is None

    def test_force_refresh_disables_the_shortcut(self, lr, cache, monkeypatch):
        import datetime as dt
        now = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
        self.write_cache(lr, "forced_layer", "u", "w", now)
        monkeypatch.setattr(lr, "FORCE_LAYER_REFRESH", True)
        assert lr.cache_age_seconds("forced_layer", "u", "w") is None

    def test_corrupt_metadata_is_not_fatal(self, lr, cache):
        data_path, meta_path = lr.layer_cache_paths("broken_layer")
        with open(data_path, "wb") as handle:
            handle.write(b"x")
        with open(meta_path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with redirect_stdout(io.StringIO()):
            assert lr.cache_age_seconds("broken_layer", "u", "w") is None


class TestVerbosity:
    def test_detail_is_silent_by_default(self, lr, monkeypatch):
        monkeypatch.setattr(config, "VERBOSE", False)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            lr.detail("diagnostic noise")
        assert buffer.getvalue() == ""

    def test_detail_prints_when_verbose(self, lr, monkeypatch):
        monkeypatch.setattr(config, "VERBOSE", True)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            lr.detail("diagnostic noise")
        assert "diagnostic noise" in buffer.getvalue()

    def test_warnings_are_always_shown(self, lr):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            lr.warn("something actionable")
        assert "something actionable" in buffer.getvalue()

    def test_timings_report_is_silent_unless_enabled(self, lr, monkeypatch):
        monkeypatch.setattr(config, "TIMINGS", False)
        monkeypatch.setattr(lr, "_TIMINGS", [("stage", 1.0)])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            lr.report_timings()
        assert buffer.getvalue() == ""

    def test_timings_report_lists_stages(self, lr, monkeypatch):
        monkeypatch.setattr(config, "TIMINGS", True)
        monkeypatch.setattr(lr, "_TIMINGS", [("load layers", 12.5), ("match", 3.0)])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            lr.report_timings()
        output = buffer.getvalue()
        assert "load layers" in output and "12.5s" in output
        assert "total" in output and "15.5s" in output

    def test_timed_records_a_stage(self, lr, monkeypatch):
        monkeypatch.setattr(lr, "_TIMINGS", [])
        with lr.timed("a stage"):
            pass
        assert lr._TIMINGS and lr._TIMINGS[0][0] == "a stage"


class TestOneMaterialClassification:
    """Every site must agree, or the viewer and the workflow disagree about what
    a pipe is made of."""

    def test_bbox_server_uses_the_shared_implementation(self):
        import leaflet_bbox_server
        from leakrelocation.assettype import family_from_assettype
        assert leaflet_bbox_server.material_family is family_from_assettype

    @pytest.mark.parametrize("label,family", [
        ("Steel Pipe", "STEEL"),
        ("Cast Iron Pipe", "IRON"),
        ("Copper Pipe", "COPPER"),
        ("Unknown Type", "UNKNOWN"),
        ("Plastic Pipe", "PLASTIC"),
    ])
    def test_pipe_and_type_suffixes_no_longer_collapse_to_plastic(self, label, family):
        # "PE" is a substring of "PIPE" and "TYPE", so substring matching sent
        # every such label to PLASTIC - leaving only PLASTIC and UNKNOWN visible.
        from leakrelocation.assettype import family_from_assettype
        assert family_from_assettype(label) == family
