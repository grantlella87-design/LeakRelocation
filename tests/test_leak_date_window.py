"""A leak is only relocated onto a pipe that existed when it was recorded.

    live layers (6, 7)   REVISEDLEAKDATE after CREATIONDATE
    retired layer (62)   REVISEDLEAKDATE before dateretired

All three field names were read out of the committed service metadata for 206, 6
and 7. The retired layer is on the MA service and no copy of its schema is in
this repository yet, so its field names are resolved from the layer at run time
and the run says what it found - see scripts/describe_layer.py.
"""
import io
import os
import sys
from contextlib import redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from leakrelocation import config
from leakrelocation.matching import (
    DATE_MISSING_PIPE_DATE,
    DATE_NO_LEAK_DATE,
    DATE_OK,
    DATE_PIPE_ALREADY_RETIRED,
    DATE_PIPE_TOO_NEW,
    IN_SERVICE_AT_LEAK,
    RETIRED_AFTER_LEAK,
    date_rule_result,
)
from leakrelocation import dates, layer_fields

DAY = 86_400_000
LEAK = 1_600_000_000_000          # 2020-09-13
BEFORE, AFTER = LEAK - DAY, LEAK + DAY


class TestTheLiveLayerRule:
    """A pipe whose record was created after the leak cannot be the pipe that
    leaked."""

    def test_a_pipe_created_before_the_leak_is_allowed(self):
        assert date_rule_result(IN_SERVICE_AT_LEAK, LEAK, BEFORE, None) == (True, DATE_OK)

    def test_a_pipe_created_after_the_leak_is_rejected(self):
        assert date_rule_result(IN_SERVICE_AT_LEAK, LEAK, AFTER, None) == (
            False, DATE_PIPE_TOO_NEW)

    def test_the_comparison_is_strict(self):
        """"after CREATIONDATE", not "on or after"."""
        assert date_rule_result(IN_SERVICE_AT_LEAK, LEAK, LEAK, None)[0] is False

    def test_the_retirement_date_is_not_consulted(self):
        """A live pipe already retired is still allowed by this rule; only the
        retired layer is checked against dateretired."""
        assert date_rule_result(IN_SERVICE_AT_LEAK, LEAK, BEFORE, BEFORE) == (
            True, DATE_OK)


class TestTheRetiredLayerRule:
    def test_a_pipe_retired_after_the_leak_is_allowed(self):
        assert date_rule_result(RETIRED_AFTER_LEAK, LEAK, None, AFTER) == (True, DATE_OK)

    def test_a_pipe_retired_before_the_leak_is_rejected(self):
        assert date_rule_result(RETIRED_AFTER_LEAK, LEAK, None, BEFORE) == (
            False, DATE_PIPE_ALREADY_RETIRED)

    def test_the_comparison_is_strict(self):
        assert date_rule_result(RETIRED_AFTER_LEAK, LEAK, None, LEAK)[0] is False

    def test_the_creation_date_is_not_consulted(self):
        assert date_rule_result(RETIRED_AFTER_LEAK, LEAK, AFTER, AFTER) == (True, DATE_OK)


class TestMissingDates:
    """Nothing is dropped on a data gap without saying so."""

    @pytest.mark.parametrize("rule", [IN_SERVICE_AT_LEAK, RETIRED_AFTER_LEAK])
    def test_a_leak_with_no_date_matches_and_is_flagged(self, rule):
        allowed, reason = date_rule_result(rule, None, BEFORE, AFTER)
        assert allowed is True
        assert reason == DATE_NO_LEAK_DATE

    @pytest.mark.parametrize(("rule", "created", "retired"), [
        (IN_SERVICE_AT_LEAK, None, AFTER),
        (RETIRED_AFTER_LEAK, BEFORE, None),
    ])
    def test_a_pipe_missing_its_own_date_matches_and_is_flagged(
            self, rule, created, retired):
        allowed, reason = date_rule_result(rule, LEAK, created, retired)
        assert allowed is True
        assert reason == DATE_MISSING_PIPE_DATE

    def test_the_reason_distinguishes_the_two_gaps(self):
        assert DATE_NO_LEAK_DATE != DATE_MISSING_PIPE_DATE


class TestAnUnknownRuleIsNotSilentlyIgnored:
    def test_it_raises(self):
        with pytest.raises(ValueError, match="unknown date rule"):
            date_rule_result("whatever", LEAK, BEFORE, AFTER)


@pytest.fixture(scope="module")
def workflow():
    pytest.importorskip("geopandas")
    pytest.importorskip("keyring")
    from leakrelocation import workflow
    return workflow


class TestTheFieldsAreRequestedAndPinned:
    """The names, checked against the committed metadata by
    tests/test_dnv_service_metadata.py, and requested here."""

    def test_the_leak_date_field(self, workflow):
        """One name per leak source, strict order: layer 206's first, the
        Maximo query's second. No frame carries both, so the order only decides
        which is looked for first."""
        assert layer_fields.LEAK_DATE_CANDIDATES == ["REVISEDLEAKDATE", "MAXIMOLEAKDATE"]

    def test_the_pipe_life_fields(self, workflow):
        assert layer_fields.PIPE_CREATED_CANDIDATES == ["CREATIONDATE"]
        assert layer_fields.PIPE_RETIRED_CANDIDATES == ["dateretired"]

    def test_every_pipe_layer_has_a_rule(self, workflow):
        assert layer_fields.LAYER_DATE_RULES == {
            "distribution": IN_SERVICE_AT_LEAK,
            "service": IN_SERVICE_AT_LEAK,
            "retired": RETIRED_AFTER_LEAK,
        }

    def test_the_leak_download_asks_for_the_date(self, workflow):
        meta = {"fields": [{"name": n} for n in
                           ["OBJECTID", "LMSLEAKNUMBER", "REVISEDLEAKDATE"]],
                "object_id_field": "OBJECTID"}
        with redirect_stdout(io.StringIO()):
            out = layer_fields.build_out_fields(meta, "historic leaks")
        assert "REVISEDLEAKDATE" in out

    def test_the_pipe_download_asks_for_both_life_dates(self, workflow):
        meta = {"fields": [{"name": n} for n in
                           ["OBJECTID", "ASSETGROUP", "ASSETTYPE",
                            "nominaldiameter", "CREATIONDATE", "dateretired"]],
                "object_id_field": "OBJECTID"}
        with redirect_stdout(io.StringIO()):
            out = layer_fields.build_out_fields(meta, "distribution pipes")
        assert "CREATIONDATE" in out
        assert "dateretired" in out


class TestTheRetiredLayerIsConfigured:
    def test_it_points_at_the_ma_service(self):
        """It is not on the DNV NY service: that one has 100 layers and no id
        between 20 and 99."""
        assert "Material_View_MA" in config.RETIRED_PIPE_URL
        assert config.RETIRED_PIPE_URL.rstrip("/").endswith(
            str(config.RETIRED_PIPE_LAYER_ID))

    def test_it_is_not_treated_as_a_dnv_pipe_layer(self):
        """apply_pipe_domain_out_fields appends ASSETGROUP and ASSETTYPE for the
        DNV pipe layers by id; layer 62 is on another service, so its id must not
        collide with that list."""
        assert config.RETIRED_PIPE_LAYER_ID not in config.PIPE_LAYER_IDS

    def test_the_url_and_the_layer_can_be_overridden(self, monkeypatch):
        import importlib
        monkeypatch.setenv("LEAKRELOCATION_RETIRED_PIPE_URL", "https://x/MapServer/9")
        reloaded = importlib.reload(config)
        try:
            assert reloaded.RETIRED_PIPE_URL == "https://x/MapServer/9"
        finally:
            monkeypatch.delenv("LEAKRELOCATION_RETIRED_PIPE_URL")
            importlib.reload(config)


class TestPipeRecordsCarryTheDates:
    def frame(self, **columns):
        gpd = pytest.importorskip("geopandas")
        from shapely.geometry import LineString
        rows = len(next(iter(columns.values())))
        columns["geometry"] = [LineString([(0, i), (1, i + 1)]) for i in range(rows)]
        return gpd.GeoDataFrame(columns, crs="EPSG:4326")

    def test_a_live_layer_record_carries_created_and_the_rule(self, workflow):
        gdf = self.frame(OBJECTID=[1], material=["Copper"], nominaldiameter=[4],
                         CREATIONDATE=[BEFORE], dateretired=[None])
        with redirect_stdout(io.StringIO()):
            records = workflow.prepare_pipes(gdf, "distribution")
        assert records[0]["date_rule"] == IN_SERVICE_AT_LEAK
        assert records[0]["created_ms"] == BEFORE

    def test_a_retired_layer_record_carries_retired(self, workflow):
        gdf = self.frame(OBJECTID=[1], material=["Copper"], nominaldiameter=[4],
                         CREATIONDATE=[BEFORE], dateretired=[AFTER])
        with redirect_stdout(io.StringIO()):
            records = workflow.prepare_pipes(gdf, "retired")
        assert records[0]["date_rule"] == RETIRED_AFTER_LEAK
        assert records[0]["retired_ms"] == AFTER

    def test_the_retired_layer_without_its_date_stops_the_run(self, workflow):
        """Matching every retired pipe unfiltered would relocate leaks onto pipes
        that were already out of service."""
        gdf = self.frame(OBJECTID=[1], material=["Copper"], nominaldiameter=[4])
        with pytest.raises(RuntimeError, match="no retirement date column"), \
                redirect_stdout(io.StringIO()):
            workflow.prepare_pipes(gdf, "retired")

    def test_a_layer_with_no_rule_stops_the_run(self, workflow):
        gdf = self.frame(OBJECTID=[1], material=["Copper"], nominaldiameter=[4])
        with pytest.raises(RuntimeError, match="no date rule"), \
                redirect_stdout(io.StringIO()):
            workflow.prepare_pipes(gdf, "something else")


class TestTheAuditDateColumns:
    def test_epoch_is_rendered_as_a_date(self, workflow):
        assert dates.epoch_ms_to_iso(LEAK) == "2020-09-13"

    def test_a_missing_date_is_blank_not_the_epoch(self, workflow):
        """1970-01-01 in an audit column reads as a real date."""
        assert dates.epoch_ms_to_iso(None) == ""

    def test_an_unrepresentable_value_is_blank(self, workflow):
        assert dates.epoch_ms_to_iso(1e30) == ""


class TestADateIsReadWhateverShapeItArrivesIn:
    """The two leak sources deliver dates differently and both have to work.

    ArcGIS returns epoch milliseconds as a number. A SQL query returns a
    datetime or an ISO string. parse_number takes the *first* number out of
    anything, so "2019-04-02" came back as 2019 - which read as epoch
    milliseconds is two seconds past midnight on 1 January 1970. Every Maximo
    leak would then have been older than every pipe, failed the in-service
    check, and nothing would have said why.
    """

    def test_epoch_milliseconds_still_read_as_epoch_milliseconds(self, workflow):
        for value in (1_156_392_000_000.0, 1_156_392_000_000, "1156392000000"):
            assert dates.date_value_to_epoch_ms(value) == 1_156_392_000_000, value

    @pytest.mark.parametrize("value,iso", [
        ("2019-04-02", "2019-04-02"),
        ("2019-04-02 14:30:00", "2019-04-02"),
        ("2019-04-02T14:30:00Z", "2019-04-02"),
    ])
    def test_a_date_string_is_read_as_a_date(self, workflow, value, iso):
        assert dates.epoch_ms_to_iso(
            dates.date_value_to_epoch_ms(value)) == iso

    def test_a_datetime_object_is_read_as_a_date(self, workflow):
        import datetime as dt
        for value in (dt.datetime(2019, 4, 2), dt.date(2019, 4, 2)):
            assert dates.epoch_ms_to_iso(
                dates.date_value_to_epoch_ms(value)) == "2019-04-02", value

    def test_a_date_string_is_never_read_as_its_year(self, workflow):
        """The specific failure: 2019 milliseconds is 1 January 1970."""
        assert dates.date_value_to_epoch_ms("2019-04-02") != 2019
        assert dates.epoch_ms_to_iso(2019) == "1970-01-01"

    @pytest.mark.parametrize("value", [None, "", "   ", "not a date", float("nan")])
    def test_a_non_date_is_no_date_rather_than_a_wrong_one(self, workflow, value):
        assert dates.date_value_to_epoch_ms(value) is None

    def test_what_counts_as_numeric(self, workflow):
        assert dates.looks_numeric(5) is True
        assert dates.looks_numeric(5.0) is True
        assert dates.looks_numeric(" 1156392000000 ") is True
        assert dates.looks_numeric("2019-04-02") is False
        assert dates.looks_numeric("") is False
        assert dates.looks_numeric(True) is False, "a bool is not a timestamp"

    def test_a_string_date_passes_the_pipe_life_check_it_should(self, workflow):
        """End to end: a 2019 leak against a pipe recorded in 2000 is in
        service. Read as the year 2019 it would have been rejected."""
        leak_ms = dates.date_value_to_epoch_ms("2019-04-02")
        pipe_ms = dates.date_value_to_epoch_ms(946_684_800_000.0)  # 2000-01-01
        allowed, reason = date_rule_result(
            IN_SERVICE_AT_LEAK, leak_ms, pipe_ms, None)
        assert allowed is True
        assert reason == "ok"


class TestTheDateFallbackChain:
    """REVISEDLEAKDATE came back empty on all 98,501 MA rows, so DateCheck
    reads "no_leak_date" for 98.1% of the audit and the pipe-life rule decides
    nothing - a leak is matched to a pipe that may have been laid years after
    it was reported, and the only sign is a column nobody reads.

    DISCOVEREDDATE, REPAIREDDATE and COMPLETEDDATE sit on the same row. They are
    always collected and counted, and only used when asked for, because using
    them changes which leaks relocate.
    """

    def test_the_fallback_fields_are_real_columns_on_layer_206(self, workflow):
        import json
        import os
        path = os.path.join(
            REPO_ROOT, "reference", "mapserver_json",
            "NY_DNV_Synergi_RiskResults_Assets_NY", "layer_206_Hist_GasLeak.json")
        if not os.path.isfile(path):
            pytest.skip("reference metadata is not in this checkout")
        with open(path, encoding="utf-8") as handle:
            dates = {field["name"] for field in json.load(handle)["fields"]
                     if field["type"] == "esriFieldTypeDate"}
        for name in layer_fields.LEAK_DATE_FALLBACK_CANDIDATES:
            assert name in dates, name

    def test_they_are_not_the_primary_field(self, workflow):
        overlap = set(layer_fields.LEAK_DATE_FALLBACK_CANDIDATES) & set(
            layer_fields.LEAK_DATE_CANDIDATES)
        assert overlap == set()

    def test_lastupdate_is_not_among_them(self, workflow):
        """LASTUPDATE is when the GIS row was touched, not when the leak
        happened, and the delta refresh already keys on it. Using it as a leak
        date would compare a pipe's life against an edit timestamp."""
        assert "LASTUPDATE" not in layer_fields.LEAK_DATE_FALLBACK_CANDIDATES
        assert "DUEDATE" not in layer_fields.LEAK_DATE_FALLBACK_CANDIDATES

    def test_the_primary_date_wins_when_it_is_there(self, workflow):
        row = {"REVISEDLEAKDATE": "2019-04-02", "DISCOVEREDDATE": "2015-01-01"}
        ms, source, _ = workflow.leak_date_with_source(
            row, "REVISEDLEAKDATE", ["DISCOVEREDDATE"], True)
        assert dates.epoch_ms_to_iso(ms) == "2019-04-02"
        assert source == "REVISEDLEAKDATE"

    def test_the_first_available_fallback_is_used(self, workflow):
        row = {"REVISEDLEAKDATE": None, "DISCOVEREDDATE": None,
               "REPAIREDDATE": "2016-06-01", "COMPLETEDDATE": "2017-01-01"}
        ms, source, had = workflow.leak_date_with_source(
            row, "REVISEDLEAKDATE",
            ["DISCOVEREDDATE", "REPAIREDDATE", "COMPLETEDDATE"], True)
        assert dates.epoch_ms_to_iso(ms) == "2016-06-01"
        assert source == "REPAIREDDATE"
        assert had is True

    def test_with_the_fallback_off_the_date_stays_absent(self, workflow):
        row = {"REVISEDLEAKDATE": None, "DISCOVEREDDATE": "2015-01-01"}
        ms, source, had = workflow.leak_date_with_source(
            row, "REVISEDLEAKDATE", ["DISCOVEREDDATE"], False)
        assert ms is None
        assert source == ""
        # Still reported, which is how a run says what it would rescue.
        assert had is True

    def test_a_row_with_no_date_anywhere_reports_nothing_to_rescue(self, workflow):
        row = {"REVISEDLEAKDATE": None, "DISCOVEREDDATE": None}
        ms, source, had = workflow.leak_date_with_source(
            row, "REVISEDLEAKDATE", ["DISCOVEREDDATE"], True)
        assert (ms, source, had) == (None, "", False)

    def test_no_fallback_columns_at_all_is_not_an_error(self, workflow):
        """A cache downloaded before these were requested has none of them."""
        ms, source, had = workflow.leak_date_with_source(
            {"REVISEDLEAKDATE": "2019-04-02"}, "REVISEDLEAKDATE", [], True)
        assert dates.epoch_ms_to_iso(ms) == "2019-04-02"
        assert had is False

    def test_no_primary_column_at_all_still_uses_a_fallback(self, workflow):
        ms, source, _ = workflow.leak_date_with_source(
            {"DISCOVEREDDATE": "2015-01-01"}, None, ["DISCOVEREDDATE"], True)
        assert source == "DISCOVEREDDATE"
        assert dates.epoch_ms_to_iso(ms) == "2015-01-01"

    def test_a_fallback_date_is_checked_against_the_pipe_the_same_way(
            self, workflow):
        """The point of the whole thing: a date from DISCOVEREDDATE goes
        through the same rule, so a pipe laid after the leak is refused."""
        row = {"REVISEDLEAKDATE": None, "DISCOVEREDDATE": "1995-01-01"}
        ms, _, _ = workflow.leak_date_with_source(
            row, "REVISEDLEAKDATE", ["DISCOVEREDDATE"], True)
        pipe_created = dates.date_value_to_epoch_ms(946_684_800_000.0)
        allowed, reason = date_rule_result(
            IN_SERVICE_AT_LEAK, ms, pipe_created, None)
        assert allowed is False
        assert reason == "pipe_created_after_leak"

    def test_the_same_leak_is_unchecked_with_the_fallback_off(self, workflow):
        """And this is what it costs to leave it off: the leak relocates onto a
        pipe recorded five years after it was discovered, flagged only by a
        column reading no_leak_date."""
        row = {"REVISEDLEAKDATE": None, "DISCOVEREDDATE": "1995-01-01"}
        ms, _, _ = workflow.leak_date_with_source(
            row, "REVISEDLEAKDATE", ["DISCOVEREDDATE"], False)
        allowed, reason = date_rule_result(
            IN_SERVICE_AT_LEAK, ms, dates.date_value_to_epoch_ms(946_684_800_000.0),
            None)
        assert allowed is True
        assert reason == "no_leak_date"

    def test_the_fields_are_requested_from_the_service(self, workflow):
        """Collected whether or not they are used - the count of what they
        would rescue is the case for using them."""
        import io
        from contextlib import redirect_stdout
        meta = {"fields": [{"name": name} for name in [
            "OBJECTID", "GlobalID", "LASTUPDATE", "LMSLEAKNUMBER",
            "jurisdiction", "ADDRESS", "REVISEDLEAKDATE", "DISCOVEREDDATE",
            "REPAIREDDATE", "COMPLETEDDATE"]]}
        with redirect_stdout(io.StringIO()):
            out_fields = layer_fields.build_out_fields(meta, "historic leaks")
        for name in layer_fields.LEAK_DATE_FALLBACK_CANDIDATES:
            assert name in out_fields, name

    def test_they_are_not_asked_of_a_pipe_layer(self, workflow):
        import io
        from contextlib import redirect_stdout
        meta = {"fields": [{"name": name} for name in [
            "OBJECTID", "GlobalID", "LASTUPDATE", "nominaldiameter",
            "ASSETGROUP", "ASSETTYPE", "operatingpressure", "jurisdiction"]]}
        with redirect_stdout(io.StringIO()):
            out_fields = layer_fields.build_out_fields(meta, "distribution pipes")
        for name in layer_fields.LEAK_DATE_FALLBACK_CANDIDATES:
            assert name not in out_fields, name

    def test_adding_them_moves_the_leak_cache_signature_only(self, workflow,
                                                             monkeypatch):
        """A newly requested column has to force one full refresh of the leak
        layer, and must not invalidate the 1.27 million row pipe cache."""
        before_leak = layer_fields.out_field_request_signature("historic leaks")
        before_pipe = layer_fields.out_field_request_signature("service pipes")
        monkeypatch.setattr(layer_fields, "LEAK_DATE_FALLBACK_CANDIDATES",
                            [*layer_fields.LEAK_DATE_FALLBACK_CANDIDATES, "NEWDATE"])
        assert layer_fields.out_field_request_signature("historic leaks") != before_leak
        assert layer_fields.out_field_request_signature("service pipes") == before_pipe

    def test_it_is_off_by_default(self, workflow):
        """Turning it on changes which leaks relocate, so it is asked for."""
        from leakrelocation import config
        assert config.USE_LEAK_DATE_FALLBACK is False
