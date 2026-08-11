"""Exercise the workflow's field resolution and outFields building.

A constant removed as "unused" was still read by build_out_fields, and nothing
called it, so the NameError only surfaced mid-run against the live service:

    NameError: name 'PIPE_MATERIAL_CANDIDATES' is not defined

These call the functions rather than inspecting the source, so an undefined name
in any of them fails here instead of in production.
"""
import importlib.util
import io
import os
import sys
from contextlib import redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

pytest.importorskip("geopandas")
pytest.importorskip("keyring")


@pytest.fixture(scope="module")
def lr():
    path = os.path.join(REPO_ROOT, "src", "leak_relocation_geopandas.py")
    spec = importlib.util.spec_from_file_location("lr_smoke", path)
    module = importlib.util.module_from_spec(spec)
    with redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)
    return module


# The shape the DNV layers actually return.
PIPE_META = {"fields": [{"name": name} for name in [
    "OBJECTID", "GlobalID", "LASTUPDATE", "nominaldiameter", "material",
    "ASSETGROUP", "ASSETTYPE", "operatingpressure", "jurisdiction",
]]}

LEAK_META = {"fields": [{"name": name} for name in [
    "OBJECTID", "GlobalID", "LASTUPDATE", "LMSLEAKNUMBER", "jurisdiction",
]]}


class TestBuildOutFields:
    """Which fields are requested from the service. Every candidate group it
    reads must exist, or the call raises mid-run."""

    @pytest.mark.parametrize("layer_name", ["distribution pipes", "service pipes"])
    def test_pipe_layers_resolve(self, lr, layer_name):
        with redirect_stdout(io.StringIO()):
            out_fields = lr.build_out_fields(PIPE_META, layer_name)
        assert out_fields and out_fields != "*"

    @pytest.mark.parametrize("field", ["nominaldiameter", "operatingpressure",
                                       "ASSETGROUP", "ASSETTYPE"])
    def test_pipe_attributes_are_requested(self, lr, field):
        with redirect_stdout(io.StringIO()):
            out_fields = lr.build_out_fields(PIPE_META, "distribution pipes")
        assert field in out_fields

    def test_leak_layer_resolves(self, lr):
        with redirect_stdout(io.StringIO()):
            out_fields = lr.build_out_fields(LEAK_META, "historic leaks")
        assert "LMSLEAKNUMBER" in out_fields
        assert "jurisdiction" in out_fields

    def test_a_pipe_layer_without_assettype_fails_loudly(self, lr):
        """ASSETTYPE is the material type, so a pipe layer that does not have it
        cannot be matched on material. This used to fall back to requesting every
        field, which downloaded pipes with no material type and left the failure
        to be discovered much later as an empty PipeMaterialRaw."""
        with pytest.raises(RuntimeError, match="ASSETTYPE is the material type"), \
                redirect_stdout(io.StringIO()):
            lr.build_out_fields({"fields": [{"name": "zzz"}]}, "distribution pipes")

    def test_empty_metadata_still_falls_back_to_all_fields(self, lr):
        """No field list is not the same as a field list without ASSETTYPE:
        there is nothing to check against, and "*" returns ASSETTYPE if the layer
        has it."""
        with redirect_stdout(io.StringIO()):
            assert lr.build_out_fields({}, "distribution pipes") == "*"


class TestCandidateGroupsExist:
    """build_out_fields reads these by name at call time."""

    @pytest.mark.parametrize("name", [
        "MODIFIED_FIELD_CANDIDATES",
        "LEAK_KEY_CANDIDATES",
        "GLOBALID_CANDIDATES",
        "OBJECTID_CANDIDATES",
        "PIPE_DIAMETER_CANDIDATES",
        "PIPE_MATERIAL_FIELDS",
        "PIPE_PRESSURE_CANDIDATES",
        "SUPP_KEY_CANDIDATES",
        "SUPP_DIAMETER_CANDIDATES",
        "SUPP_MATERIAL_CANDIDATES",
        "SUPP_PRESSURE_CANDIDATES",
        "SUPP_FACILITY_CANDIDATES",
    ])
    def test_defined_and_non_empty(self, lr, name):
        assert getattr(lr, name), name


class TestPreparePipesReadsSchemaMaterial:
    """Distinct from the download question above: this is which cache column the
    matching reads, and it is pinned to the schema."""

    def frame(self, **columns):
        import geopandas as gpd
        from shapely.geometry import LineString
        rows = len(next(iter(columns.values())))
        columns["geometry"] = [
            LineString([(0, i), (1, i + 1)]) for i in range(rows)
        ]
        return gpd.GeoDataFrame(columns, crs="EPSG:4326")

    def test_uses_the_material_column(self, lr):
        gdf = self.frame(OBJECTID=[1, 2], material=["Cast Iron", "Copper"],
                         nominaldiameter=[2, 4])
        with redirect_stdout(io.StringIO()):
            sources = lr.prepare_pipes(gdf, "distribution")
        assert sources

    def test_fails_clearly_when_the_cache_is_not_enriched(self, lr):
        gdf = self.frame(OBJECTID=[1, 2], nominaldiameter=[2, 4])
        with redirect_stdout(io.StringIO()), pytest.raises(RuntimeError) as excinfo:
            lr.prepare_pipes(gdf, "distribution")
        message = str(excinfo.value)
        assert "material" in message
        assert "enrich_assettype_cache" in message
