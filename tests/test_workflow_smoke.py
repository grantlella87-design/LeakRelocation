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
        "SUPP_FACILITY_CANDIDATES",
    ])
    def test_defined_and_non_empty(self, lr, name):
        assert getattr(lr, name), name

    def test_the_pressure_list_is_defined_and_deliberately_empty(self, lr):
        """The supplemental CSV has no pressure column of any kind, so there is no
        honest name to put here. It still has to exist: load_supplemental reads it
        by name, and an absent attribute is a NameError mid-run.

        tests/test_supplemental_csv.py checks the emptiness against the committed
        file rather than against this comment.
        """
        assert isinstance(lr.SUPP_PRESSURE_CANDIDATES, list)
        assert lr.SUPP_PRESSURE_CANDIDATES == []


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


class TestNarrowedExceptionHandlers:
    """These handlers used to catch bare Exception. Narrowing them risks turning
    a swallowed error into a crash, so the inputs they have to absorb are pinned
    here.

    The exception classes were read off the libraries rather than assumed - and
    one of them defeats intuition: shapely's GeometryTypeError descends from
    ShapelyError, not TypeError, so catching TypeError alone would let an
    unknown geometry "type" through.
    """

    @pytest.mark.parametrize("geometry", [
        None,
        {},
        {"type": "Nope", "coordinates": []},      # GeometryTypeError
        {"type": "Point"},                        # KeyError
        {"nope": 1},                              # AttributeError
        {"type": "Point", "coordinates": "xx"},   # TypeError
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 1]]]},   # ValueError
        "not a dict",
        {"paths": []},
        {"x": 1},
    ])
    def test_malformed_geometry_becomes_none(self, lr, geometry):
        assert lr.esri_geometry_to_shape(geometry) is None

    @pytest.mark.parametrize("value", [
        "not a date",
        None,
        [],
        float("nan"),
    ])
    def test_unparseable_dates_become_none(self, lr, value):
        assert lr.date_value_to_epoch_ms(value) is None

    def test_a_real_epoch_still_converts(self, lr):
        assert lr.date_value_to_epoch_ms(1640995200000) == 1640995200000

    @pytest.mark.parametrize(("value", "result"), [
        ("2022-01-01", 2022),
        ("9999999-01-01", 9999999),
        ({"a": 1}, 1),
    ])
    def test_a_value_containing_a_digit_returns_that_digit(self, lr, value, result):
        """Recording existing behaviour, not endorsing it.

        parse_number runs first and pulls the first number out of str(value), so
        anything with a digit in it short-circuits the date parse: the ISO string
        "2022-01-01" becomes 2022 milliseconds after the epoch. This is upstream
        of the narrowed handler and unchanged by it. It only matters for the delta
        watermark, and it fails safe - a garbage watermark falls back to a full
        refresh - which is presumably why it has gone unnoticed.
        """
        assert lr.date_value_to_epoch_ms(value) == result

    @pytest.mark.parametrize("value", [
        None,
        "abc",                # not numeric
        float("inf"),         # not finite
        float("nan"),
        -5,                   # <= 0
        0,
        1e30,                 # outside the representable range
    ])
    def test_bad_watermarks_fall_back_to_the_safe_default(self, lr, value):
        with redirect_stdout(io.StringIO()):
            assert lr.epoch_ms_to_sql_timestamp(value) == "timestamp '1970-01-01 00:00:00'"

    def test_a_real_watermark_still_converts(self, lr):
        with redirect_stdout(io.StringIO()):
            result = lr.epoch_ms_to_sql_timestamp(1640995200000)
        assert result == "timestamp '2022-01-01 00:00:00'"
