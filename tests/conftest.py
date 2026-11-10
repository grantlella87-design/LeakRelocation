"""Shared test setup.

The package keeps a little state at module level - the CRS each layer reported,
the stage timings, one HTTP session per worker thread. A run starts with all of
it empty, so every test does too: without this a test that stubs a layer in
one CRS would hand that answer to the next test that asks about the same layer.
"""
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for folder in ("src", "scripts"):
    path = os.path.join(REPO_ROOT, folder)
    if path not in sys.path:
        sys.path.insert(0, path)


@pytest.fixture(autouse=True)
def fresh_module_state():
    yield
    # Only what has already been imported: a test of the pure modules should
    # not pull in geopandas just to have nothing cleared.
    native_crs = sys.modules.get("leakrelocation.native_crs")
    if native_crs is not None:
        native_crs._NATIVE_CRS_CACHE.clear()
    output = sys.modules.get("leakrelocation.output")
    if output is not None:
        output._TIMINGS.clear()
    arcgis = sys.modules.get("leakrelocation.arcgis")
    if arcgis is not None:
        arcgis._WORKER_STATE.__dict__.clear()
