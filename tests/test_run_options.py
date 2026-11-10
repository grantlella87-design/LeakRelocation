"""The command-line flags have to reach the code that acts on them.

`python run.py --refresh` printed "caches will be ignored" and then read the
caches. The flag was written to an environment variable, but the settings had
been read from the environment when the config module was first imported -
before any command line had been parsed - so nothing ever saw it. The same
route carried --leak-date-fallback and --diameter, which worked only because
the config module was reloaded in between.

One function applies all three now, to the settings and to the environment,
and these check each flag where it is used rather than where it is set.
"""
import io
import os
import sys
from contextlib import redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from leakrelocation import config

FLAGS = ("LEAKRELOCATION_DIAMETER_MODE", "LEAKRELOCATION_LEAK_DATE_FALLBACK",
         "FORCE_LAYER_REFRESH")


@pytest.fixture(autouse=True)
def settings_put_back(monkeypatch):
    """set_run_options changes this process on purpose; undo it afterwards."""
    for name in ("DIAMETER_MATCH_MODE", "USE_LEAK_DATE_FALLBACK",
                 "FORCE_LAYER_REFRESH"):
        monkeypatch.setattr(config, name, getattr(config, name))
    for name in FLAGS:
        if name in os.environ:
            monkeypatch.setenv(name, os.environ[name])
        else:
            # setenv then delenv, so the variable is removed again on teardown
            # whatever the test leaves in it.
            monkeypatch.setenv(name, "")
            monkeypatch.delenv(name)


class TestSettingTheOptions:
    def test_nothing_asked_for_changes_nothing(self):
        before = (config.DIAMETER_MATCH_MODE, config.USE_LEAK_DATE_FALLBACK,
                  config.FORCE_LAYER_REFRESH)
        assert config.set_run_options() == config.DIAMETER_MATCH_MODE
        assert before == (config.DIAMETER_MATCH_MODE,
                          config.USE_LEAK_DATE_FALLBACK,
                          config.FORCE_LAYER_REFRESH)
        assert not any(name in os.environ for name in FLAGS)

    def test_the_diameter_rule_is_applied_and_returned(self):
        assert config.set_run_options(diameter_mode="Fuzzy") == "fuzzy"
        assert config.DIAMETER_MATCH_MODE == "fuzzy"
        assert config.output_gpkg_for().name.endswith("_fuzzy_diameter.gpkg")

    def test_the_date_fallback_is_applied(self):
        config.set_run_options(leak_date_fallback=True)
        assert config.USE_LEAK_DATE_FALLBACK is True

    def test_the_refresh_is_applied(self):
        config.set_run_options(force_refresh=True)
        assert config.FORCE_LAYER_REFRESH is True

    def test_the_workers_are_told_too(self):
        """The matching workers are separate processes. On Windows they start
        from nothing and read the environment, so a rule set only in this
        process would have the workers matching by a different one."""
        config.set_run_options(diameter_mode="fuzzy", leak_date_fallback=True,
                               force_refresh=True)
        assert os.environ["LEAKRELOCATION_DIAMETER_MODE"] == "fuzzy"
        assert os.environ["LEAKRELOCATION_LEAK_DATE_FALLBACK"] == "1"
        assert os.environ["FORCE_LAYER_REFRESH"] == "1"

    def test_a_fresh_process_reads_the_same_settings(self):
        """What a spawned worker sees: a new interpreter, the same environment."""
        import subprocess
        config.set_run_options(diameter_mode="fuzzy", leak_date_fallback=True,
                               force_refresh=True)
        seen = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, 'src');"
             "from leakrelocation import config;"
             "print(config.DIAMETER_MATCH_MODE, config.USE_LEAK_DATE_FALLBACK,"
             " config.FORCE_LAYER_REFRESH)"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True)
        assert seen.stdout.split() == ["fuzzy", "True", "True"]


class TestRefreshReachesTheCache:
    """The flag where it matters: a cache that is on disk and must be ignored."""

    @pytest.fixture
    def cache(self, tmp_path, monkeypatch):
        pytest.importorskip("geopandas")
        pytest.importorskip("keyring")
        import geopandas as gpd
        from shapely.geometry import Point

        from leakrelocation import layer_cache

        monkeypatch.setattr(layer_cache, "USE_LAYER_CACHE", True)
        monkeypatch.setattr(layer_cache, "LAYER_CACHE_FOLDER", str(tmp_path))
        frame = gpd.GeoDataFrame(
            {"OBJECTID": [1], "LASTUPDATE": [1_700_000_000_000.0]},
            geometry=[Point(0, 0)], crs="EPSG:2249")
        with redirect_stdout(io.StringIO()):
            layer_cache.write_layer_cache(
                "pipes", "https://x/MapServer/6", "1=1", 1, "LASTUPDATE", frame)

        def read():
            with redirect_stdout(io.StringIO()):
                return layer_cache.read_layer_cache(
                    "pipes", "https://x/MapServer/6", "1=1")
        return read

    def test_the_cache_is_used_without_the_flag(self, cache, monkeypatch):
        monkeypatch.setattr(config, "FORCE_LAYER_REFRESH", False)
        frame, _meta = cache()
        assert frame is not None and len(frame) == 1

    def test_the_cache_is_ignored_with_it(self, cache, monkeypatch):
        """Set the way run.py sets it, after everything has been imported -
        which is exactly the order that used to lose it."""
        monkeypatch.setattr(config, "FORCE_LAYER_REFRESH", False)
        config.set_run_options(force_refresh=True)
        frame, _meta = cache()
        assert frame is None


class TestTheEntryPointsUseIt:
    """Neither script may go back to setting the environment by hand."""

    @pytest.mark.parametrize("script", ["run.py", "scripts/maximo_leaks.py"])
    def test_the_flags_go_through_set_run_options(self, script):
        with open(os.path.join(REPO_ROOT, script), encoding="utf-8") as handle:
            source = handle.read()
        assert "config.set_run_options(" in source
        assert 'os.environ["FORCE_LAYER_REFRESH"]' not in source
        assert "importlib.reload" not in source
