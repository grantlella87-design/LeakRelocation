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
    serves."""

    @staticmethod
    def _request_later(url, delay=0.5):
        result = {}

        def run():
            time.sleep(delay)
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    result["status"] = response.status
                    result["body"] = response.read().decode()
            except Exception as exc:  # noqa: BLE001 - surfaced via result
                result["error"] = str(exc)

        thread = threading.Thread(target=run)
        thread.start()
        return thread, result

    def test_captures_the_code(self, lr):
        port = config.LOOPBACK_OAUTH_PORT
        thread, _ = self._request_later(f"http://127.0.0.1:{port}/?code=ABC123&state=x")
        with redirect_stdout(io.StringIO()):
            code = lr.capture_loopback_authorization_code(timeout_seconds=15)
        thread.join()
        assert code == "ABC123"

    def test_page_never_shows_the_code(self, lr):
        port = config.LOOPBACK_OAUTH_PORT
        thread, result = self._request_later(f"http://127.0.0.1:{port}/?code=SECRETCODE")
        with redirect_stdout(io.StringIO()):
            lr.capture_loopback_authorization_code(timeout_seconds=15)
        thread.join()
        assert result["status"] == 200
        assert "SECRETCODE" not in result["body"]

    def test_page_closes_itself(self, lr):
        port = config.LOOPBACK_OAUTH_PORT
        thread, result = self._request_later(f"http://127.0.0.1:{port}/?code=X")
        with redirect_stdout(io.StringIO()):
            lr.capture_loopback_authorization_code(timeout_seconds=15)
        thread.join()
        assert "window.close()" in result["body"]

    def test_releases_the_port_for_the_next_run(self, lr):
        port = config.LOOPBACK_OAUTH_PORT
        for expected in ["FIRST", "SECOND"]:
            thread, _ = self._request_later(f"http://127.0.0.1:{port}/?code={expected}")
            with redirect_stdout(io.StringIO()):
                code = lr.capture_loopback_authorization_code(timeout_seconds=15)
            thread.join()
            assert code == expected

    def test_ignores_requests_that_are_not_the_redirect(self, lr):
        port = config.LOOPBACK_OAUTH_PORT

        def noise_then_redirect():
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

        thread = threading.Thread(target=noise_then_redirect)
        thread.start()
        with redirect_stdout(io.StringIO()):
            code = lr.capture_loopback_authorization_code(timeout_seconds=15)
        thread.join()
        assert code == "LATER"

    def test_timeout_returns_none_so_the_caller_can_fall_back(self, lr):
        started = time.time()
        with redirect_stdout(io.StringIO()):
            code = lr.capture_loopback_authorization_code(timeout_seconds=2)
        assert code is None
        assert time.time() - started < 10


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
