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
    "ADDRESS", "REVISEDLEAKDATE",
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

    def test_the_leak_address_is_requested(self, lr):
        """It is not matched on, so nothing else in the run would notice it
        missing - the column would just be empty on the map."""
        with redirect_stdout(io.StringIO()):
            out_fields = lr.build_out_fields(LEAK_META, "historic leaks")
        assert "ADDRESS" in out_fields

    def test_the_address_is_not_asked_of_a_pipe_layer(self, lr):
        """The pipe layers have no address field, and asking for a field a layer
        does not have makes the service reject the whole query."""
        with redirect_stdout(io.StringIO()):
            out_fields = lr.build_out_fields(PIPE_META, "distribution pipes")
        assert "ADDRESS" not in out_fields.upper()

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
        "LEAK_ADDRESS_CANDIDATES",
    ])
    def test_defined_and_non_empty(self, lr, name):
        assert getattr(lr, name), name


class TestCacheKnowsWhichFieldsItHolds:
    """A cache holds the columns that were requested when it was written. Adding
    a field to the request lists does not change it, and the delta refresh only
    re-downloads rows whose LASTUPDATE moved - so a newly requested field would
    arrive for a few changed records and be blank for the rest.

    The signature is stored with the cache and compared on read, so adding a
    field forces one full refresh instead of a half-populated column.
    """

    def test_the_signature_is_stable(self, lr):
        assert lr.out_field_request_signature() == lr.out_field_request_signature()

    def test_the_signature_covers_the_address(self, lr, monkeypatch):
        before = lr.out_field_request_signature()
        monkeypatch.setattr(lr, "LEAK_ADDRESS_CANDIDATES", [])
        assert lr.out_field_request_signature() != before

    @pytest.mark.parametrize("attribute", [
        "MODIFIED_FIELD_CANDIDATES",
        "LEAK_KEY_CANDIDATES",
        "LEAK_DATE_CANDIDATES",
        "PIPE_DIAMETER_CANDIDATES",
        "PIPE_PRESSURE_CANDIDATES",
        "PIPE_MATERIAL_FIELDS",
        "PIPE_CREATED_CANDIDATES",
        "PIPE_RETIRED_CANDIDATES",
        "GLOBALID_CANDIDATES",
        "OBJECTID_CANDIDATES",
        "JURISDICTION_CANDIDATES",
    ])
    def test_every_requested_group_moves_the_signature(self, lr, monkeypatch, attribute):
        before = lr.out_field_request_signature()
        monkeypatch.setattr(lr, attribute, [*getattr(lr, attribute), "NEWFIELD"])
        assert lr.out_field_request_signature() != before, attribute

    def test_case_and_order_do_not_move_the_signature(self, lr, monkeypatch):
        """resolve_field_name ignores case and returns the layer's own spelling,
        so re-casing or reordering a list changes nothing about what comes back.
        Invalidating every cache for that would be a gratuitous re-download."""
        before = lr.out_field_request_signature()
        monkeypatch.setattr(lr, "LEAK_KEY_CANDIDATES",
                            [name.lower() for name in reversed(lr.LEAK_KEY_CANDIDATES)])
        assert lr.out_field_request_signature() == before

    def test_a_written_cache_carries_the_signature(self, lr, tmp_path, monkeypatch):
        import geopandas as gpd
        from shapely.geometry import Point
        monkeypatch.setattr(lr, "LAYER_CACHE_FOLDER", str(tmp_path))
        monkeypatch.setattr(lr, "USE_LAYER_CACHE", True)
        monkeypatch.setattr(lr, "FORCE_LAYER_REFRESH", False)
        gdf = gpd.GeoDataFrame({"OBJECTID": [1]}, geometry=[Point(0, 0)],
                               crs="EPSG:4326")
        with redirect_stdout(io.StringIO()):
            lr.write_layer_cache("leaks", "http://x/206", "1=1", 1, "LASTUPDATE", gdf)
            loaded, meta = lr.read_layer_cache("leaks", "http://x/206", "1=1")
        assert meta["out_field_signature"] == lr.out_field_request_signature()
        assert loaded is not None and len(loaded) == 1

    def test_a_cache_written_for_different_fields_is_refused(
            self, lr, tmp_path, monkeypatch):
        """Refusing it sends the caller down the full-download path, the only one
        that populates a new column for every record."""
        import geopandas as gpd
        from shapely.geometry import Point
        monkeypatch.setattr(lr, "LAYER_CACHE_FOLDER", str(tmp_path))
        monkeypatch.setattr(lr, "USE_LAYER_CACHE", True)
        monkeypatch.setattr(lr, "FORCE_LAYER_REFRESH", False)
        gdf = gpd.GeoDataFrame({"OBJECTID": [1]}, geometry=[Point(0, 0)],
                               crs="EPSG:4326")
        with redirect_stdout(io.StringIO()):
            lr.write_layer_cache("leaks", "http://x/206", "1=1", 1, "LASTUPDATE", gdf)
        # Same cache, read after the code started asking for another field.
        monkeypatch.setattr(lr, "LEAK_ADDRESS_CANDIDATES", ["ADDRESS", "ADDRESS2"])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            loaded, meta = lr.read_layer_cache("leaks", "http://x/206", "1=1")
        assert loaded is None and meta is None
        assert "requested fields have changed" in buffer.getvalue()

    def test_a_cache_from_before_the_check_is_refreshed_once(
            self, lr, tmp_path, monkeypatch):
        import json

        import geopandas as gpd
        from shapely.geometry import Point
        monkeypatch.setattr(lr, "LAYER_CACHE_FOLDER", str(tmp_path))
        monkeypatch.setattr(lr, "USE_LAYER_CACHE", True)
        monkeypatch.setattr(lr, "FORCE_LAYER_REFRESH", False)
        gdf = gpd.GeoDataFrame({"OBJECTID": [1]}, geometry=[Point(0, 0)],
                               crs="EPSG:4326")
        with redirect_stdout(io.StringIO()):
            lr.write_layer_cache("leaks", "http://x/206", "1=1", 1, "LASTUPDATE", gdf)
        _, meta_path = lr.layer_cache_paths("leaks")
        with open(meta_path, encoding="utf-8") as handle:
            meta = json.load(handle)
        del meta["out_field_signature"]
        with open(meta_path, "w", encoding="utf-8") as handle:
            json.dump(meta, handle)
        with redirect_stdout(io.StringIO()):
            loaded, read_meta = lr.read_layer_cache("leaks", "http://x/206", "1=1")
        assert loaded is None and read_meta is None


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
