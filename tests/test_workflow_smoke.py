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
    "ADDRESS", "REVISEDLEAKDATE", "NEARESTXSTREET", "CITY",
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

    def test_every_location_field_is_requested_not_just_the_first(self, lr):
        """These are alternatives for a reader, not for the request: which of
        them this service populates is the open question, so one refresh has to
        collect all of them rather than resolve to the first that exists."""
        with redirect_stdout(io.StringIO()):
            out_fields = lr.build_out_fields(LEAK_META, "historic leaks")
        for name in lr.LEAK_LOCATION_CANDIDATES:
            assert name in out_fields, name

    def test_a_leak_layer_without_the_location_fields_still_resolves(self, lr):
        """Unlike ASSETTYPE these are optional, so a layer that lacks them must
        not fail the run - it just has less to say about where a leak is."""
        meta = {"fields": [{"name": name} for name in [
            "OBJECTID", "GlobalID", "LASTUPDATE", "LMSLEAKNUMBER",
            "jurisdiction", "ADDRESS"]]}
        with redirect_stdout(io.StringIO()):
            out_fields = lr.build_out_fields(meta, "historic leaks")
        assert "ADDRESS" in out_fields
        assert "NEARESTXSTREET" not in out_fields

    def test_the_location_fields_are_not_asked_of_a_pipe_layer(self, lr):
        with redirect_stdout(io.StringIO()):
            out_fields = lr.build_out_fields(PIPE_META, "distribution pipes")
        for name in lr.LEAK_LOCATION_CANDIDATES:
            assert name.lower() not in out_fields.lower(), name

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
        "SUPP_FACILITY_CANDIDATES",
        "LEAK_ADDRESS_CANDIDATES",
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
        # Per layer kind: the digest a leak cache carries is the leak one, and a
        # pipe cache's is not, so adding a leak field cannot invalidate the
        # 1.27 million row service pipe cache.
        assert meta["out_field_signature"] == lr.out_field_request_signature("leaks")
        assert meta["out_field_signature"] != lr.out_field_request_signature(
            "service pipes")
        assert loaded is not None and len(loaded) == 1

    def test_a_leak_field_does_not_invalidate_the_pipe_caches(self, lr, monkeypatch):
        """Collecting one more leak column used to mean re-downloading 1.27
        million service pipes as well, over the same connection that was
        resetting mid-download. The pipe layers are not affected by a leak field
        and their digest must not move for one."""
        before = lr.out_field_request_signature("service pipes")
        monkeypatch.setattr(lr, "LEAK_LOCATION_CANDIDATES",
                            [*lr.LEAK_LOCATION_CANDIDATES, "NEWFIELD"])
        assert lr.out_field_request_signature("service pipes") == before
        assert lr.out_field_request_signature("historic leaks") != before

    def test_a_pipe_field_does_not_invalidate_the_leak_cache(self, lr, monkeypatch):
        before = lr.out_field_request_signature("historic leaks")
        monkeypatch.setattr(lr, "PIPE_DIAMETER_CANDIDATES",
                            [*lr.PIPE_DIAMETER_CANDIDATES, "NEWFIELD"])
        assert lr.out_field_request_signature("historic leaks") == before

    def test_a_shared_field_still_invalidates_both(self, lr, monkeypatch):
        before = {name: lr.out_field_request_signature(name)
                  for name in ("historic leaks", "service pipes")}
        monkeypatch.setattr(lr, "JURISDICTION_CANDIDATES",
                            [*lr.JURISDICTION_CANDIDATES, "NEWFIELD"])
        for name, digest in before.items():
            assert lr.out_field_request_signature(name) != digest, name

    def test_a_cache_that_already_has_the_new_columns_is_kept(
            self, lr, tmp_path, monkeypatch):
        """The signature says the request changed; the columns say this cache
        already answers it. Re-downloading then buys nothing at all."""
        import geopandas as gpd
        from shapely.geometry import Point
        monkeypatch.setattr(lr, "LAYER_CACHE_FOLDER", str(tmp_path))
        monkeypatch.setattr(lr, "USE_LAYER_CACHE", True)
        monkeypatch.setattr(lr, "FORCE_LAYER_REFRESH", False)
        gdf = gpd.GeoDataFrame({"OBJECTID": [1], "ADDRESS": ["12 Elm St"]},
                               geometry=[Point(0, 0)], crs="EPSG:4326")
        with redirect_stdout(io.StringIO()):
            lr.write_layer_cache("leaks", "http://x/206", "1=1", 1, "LASTUPDATE", gdf)
        monkeypatch.setattr(lr, "LEAK_ADDRESS_CANDIDATES", ["ADDRESS", "ADDRESS2"])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            loaded, meta = lr.read_layer_cache("leaks", "http://x/206", "1=1",
                                               "OBJECTID,ADDRESS")
        assert loaded is not None and len(loaded) == 1
        assert "already carries every one of them" in buffer.getvalue()

    def test_a_cache_missing_a_requested_column_is_still_refused(
            self, lr, tmp_path, monkeypatch):
        import geopandas as gpd
        from shapely.geometry import Point
        monkeypatch.setattr(lr, "LAYER_CACHE_FOLDER", str(tmp_path))
        monkeypatch.setattr(lr, "USE_LAYER_CACHE", True)
        monkeypatch.setattr(lr, "FORCE_LAYER_REFRESH", False)
        gdf = gpd.GeoDataFrame({"OBJECTID": [1]}, geometry=[Point(0, 0)],
                               crs="EPSG:4326")
        with redirect_stdout(io.StringIO()):
            lr.write_layer_cache("leaks", "http://x/206", "1=1", 1, "LASTUPDATE", gdf)
        monkeypatch.setattr(lr, "LEAK_ADDRESS_CANDIDATES", ["ADDRESS", "ADDRESS2"])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            loaded, meta = lr.read_layer_cache("leaks", "http://x/206", "1=1",
                                               "OBJECTID,ADDRESS")
        assert loaded is None and meta is None
        assert "no ADDRESS" in buffer.getvalue()

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


class TestWhichColumnsACacheAlreadyHas:
    """The signature says the request changed; this says whether it matters.

    Without it, reorganising the signature or adding one leak field meant
    re-downloading every layer, including 1.27 million service pipes over the
    connection whose resets started all of this.
    """

    def frame(self, lr, columns):
        import geopandas as gpd
        from shapely.geometry import Point
        return gpd.GeoDataFrame({name: [1] for name in columns},
                                geometry=[Point(0, 0)], crs="EPSG:4326")

    def test_nothing_missing_when_every_column_is_there(self, lr):
        gdf = self.frame(lr, ["OBJECTID", "ADDRESS", "CITY"])
        assert lr.missing_requested_columns(gdf, "OBJECTID,ADDRESS") == []

    def test_the_missing_one_is_named(self, lr):
        gdf = self.frame(lr, ["OBJECTID"])
        assert lr.missing_requested_columns(gdf, "OBJECTID,ADDRESS") == ["ADDRESS"]

    def test_case_does_not_matter(self, lr):
        """The service answers with its own spelling - GLOBALID on the pipe
        layers, GlobalID on the leaks - so matching case-sensitively would
        condemn a perfectly good cache."""
        gdf = self.frame(lr, ["OBJECTID", "GLOBALID"])
        assert lr.missing_requested_columns(gdf, "OBJECTID,GlobalID") == []

    def test_a_list_is_accepted_as_well_as_a_string(self, lr):
        gdf = self.frame(lr, ["OBJECTID"])
        assert lr.missing_requested_columns(gdf, ["OBJECTID"]) == []

    @pytest.mark.parametrize("required", ["*", "", None])
    def test_an_unanswerable_request_is_not_answered(self, lr, required):
        """None means the caller did not say what it wants and "*" means
        everything; in neither case can the columns prove the cache is current,
        so it reports unknown and the caller refreshes rather than assuming."""
        gdf = self.frame(lr, ["OBJECTID"])
        assert lr.missing_requested_columns(gdf, required) is None


class TestWhereALeakIs:
    """leak_location maps this cache's column spellings onto the shared rule.

    The rule itself is tested in tests/test_leak_location.py; what matters here
    is that the mapping reaches it, because a cache carries whatever spelling
    the service answered with.
    """

    def test_the_columns_are_mapped_onto_the_rule(self, lr):
        row = {"addr": "12 Elm St", "town": "WATERTOWN"}
        fields = {"ADDRESS": "addr", "CITY": "town"}
        assert lr.leak_location(row, fields) == "12 Elm St / WATERTOWN"

    def test_a_cache_without_the_address_column_still_places_the_leak(self, lr):
        """A cache downloaded before ADDRESS was requested has no such column,
        and resolved_field leaves it out of the mapping entirely."""
        row = {"NEARESTXSTREET": "OAK ST", "CITY": "WATERTOWN"}
        fields = {"NEARESTXSTREET": "NEARESTXSTREET", "CITY": "CITY"}
        assert lr.leak_location(row, fields) == "OAK ST / WATERTOWN"

    def test_no_location_columns_at_all_is_empty_not_an_error(self, lr):
        assert lr.leak_location({}, {}) == ""

    def test_only_the_service_fields_are_requested(self, lr):
        """SuppTown is read from the supplemental CSV. Asking the service for a
        field the layer does not have makes it reject the whole query."""
        assert "SuppTown" not in lr.LEAK_LOCATION_CANDIDATES
        assert "ADDRESS" not in lr.LEAK_LOCATION_CANDIDATES  # already requested
        assert lr.LEAK_LOCATION_CANDIDATES == ["NEARESTXSTREET", "CITY"]


class TestTheDiameterRuleReachesTheMatcher:
    """The rule is chosen by configuration and read at import, because the
    workflow module binds OUTPUT_GPKG from it - so setting the mode after the
    import would write the widened output over the strict one."""

    def test_the_default_is_the_strict_rule(self, lr):
        assert lr.DIAMETER_MATCH_MODE == "exact"
        assert lr.OUTPUT_GPKG.endswith("HistoricLeakRelocation.gpkg")

    def test_the_mode_is_passed_to_every_candidate(self, lr):
        """Not read from config inside diameter_match: the matcher runs in
        worker processes, and passing it keeps the rule with the run."""
        source = open(
            os.path.join(REPO_ROOT, "src", "leak_relocation_geopandas.py"),
            encoding="utf-8").read()
        call = source[source.index("diameter_result = diameter_match("):]
        call = call[:call.index(")") + 1]
        assert "DIAMETER_MATCH_MODE" in call

    def test_the_candidates_are_sorted_by_the_shared_key(self, lr):
        """Sorting by distance alone would let the widened run take a match
        away from the strict one."""
        source = open(
            os.path.join(REPO_ROOT, "src", "leak_relocation_geopandas.py"),
            encoding="utf-8").read()
        assert "candidates.sort(key=candidate_sort_key)" in source
        assert 'candidates.sort(key=lambda item: item["distance_ft"])' not in source

    def test_a_widened_run_writes_a_different_file(self, lr, monkeypatch):
        import importlib
        monkeypatch.setenv("LEAKRELOCATION_DIAMETER_MODE", "fuzzy")
        from leakrelocation import config as live
        importlib.reload(live)
        try:
            assert live.DIAMETER_MATCH_MODE == "fuzzy"
            assert live.output_gpkg_for() != live.OUTPUT_GPKG
        finally:
            monkeypatch.delenv("LEAKRELOCATION_DIAMETER_MODE")
            importlib.reload(live)

    def test_the_audit_records_the_rule_and_the_slack(self, lr):
        """Without both columns a GeoPackage cannot say which rule produced it,
        and a row cannot say whether its diameter was a fact or an assumption."""
        source = open(
            os.path.join(REPO_ROOT, "src", "leak_relocation_geopandas.py"),
            encoding="utf-8").read()
        assert source.count('"DiameterMode": DIAMETER_MATCH_MODE') == 3
        assert '"DiameterMatch"' in source
        from leakrelocation import schema
        assert schema.DIAMETER_MATCH == "DiameterMatch"
        assert schema.DIAMETER_MODE == "DiameterMode"

    def test_the_no_match_reason_names_the_rule_that_was_applied(self, lr):
        """"No exact diameter match" on a widened run would be a lie about what
        was tried."""
        source = open(
            os.path.join(REPO_ROOT, "src", "leak_relocation_geopandas.py"),
            encoding="utf-8").read()
        assert "diameter within one nominal size" in source
        assert 'reason = ("No exact diameter/material/pressure match' not in source
