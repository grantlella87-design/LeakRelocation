"""Tests for the DNV pipe-layer outFields rule.

This logic used to be appended to the end of the production script by
scripts/patch_request_json_append_assettype.py, which redefined request_json
after main(). It now lives in the module as apply_pipe_domain_out_fields.
"""
import importlib.util
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The module imports geopandas/shapely/keyring at top level.
pytest.importorskip("geopandas")
pytest.importorskip("keyring")


@pytest.fixture(scope="module")
def lr():
    path = os.path.join(REPO_ROOT, "src", "leak_relocation_geopandas.py")
    spec = importlib.util.spec_from_file_location("lr_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DISTRIBUTION = "https://host/MapServer/6/query"
SERVICE = "https://host/MapServer/7/query"
LEAKS = "https://host/MapServer/206/query"


class TestAppendsDomainFields:
    def test_distribution_pipe_query_gains_domain_fields(self, lr):
        result = lr.apply_pipe_domain_out_fields(
            DISTRIBUTION, {"outFields": "nominaldiameter,material"})
        assert result["outFields"] == "nominaldiameter,material,ASSETGROUP,ASSETTYPE"

    def test_service_pipe_query_gains_domain_fields(self, lr):
        result = lr.apply_pipe_domain_out_fields(
            SERVICE, {"outFields": "OBJECTID,operatingpressure"})
        assert result["outFields"] == "OBJECTID,operatingpressure,ASSETGROUP,ASSETTYPE"

    def test_out_fields_key_is_matched_case_insensitively(self, lr):
        result = lr.apply_pipe_domain_out_fields(DISTRIBUTION, {"OUTFIELDS": "material"})
        assert result["OUTFIELDS"] == "material,ASSETGROUP,ASSETTYPE"


class TestLeavesOtherQueriesAlone:
    def test_already_has_assettype(self, lr):
        params = {"outFields": "nominaldiameter,ASSETTYPE"}
        assert lr.apply_pipe_domain_out_fields(DISTRIBUTION, params) == params

    def test_leak_layer_untouched(self, lr):
        params = {"outFields": "nominaldiameter,material"}
        assert lr.apply_pipe_domain_out_fields(LEAKS, params) == params

    def test_wildcard_out_fields_untouched(self, lr):
        params = {"outFields": "*"}
        assert lr.apply_pipe_domain_out_fields(DISTRIBUTION, params) == params

    def test_no_out_fields_key(self, lr):
        assert lr.apply_pipe_domain_out_fields(DISTRIBUTION, {}) == {}

    def test_none_params(self, lr):
        assert lr.apply_pipe_domain_out_fields(DISTRIBUTION, None) is None

    def test_non_string_out_fields(self, lr):
        params = {"outFields": ["material"]}
        assert lr.apply_pipe_domain_out_fields(DISTRIBUTION, params) == params


class TestDoesNotMutateCaller:
    def test_input_dict_is_not_modified(self, lr):
        params = {"outFields": "material"}
        lr.apply_pipe_domain_out_fields(DISTRIBUTION, params)
        assert params == {"outFields": "material"}
