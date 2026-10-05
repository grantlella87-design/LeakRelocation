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
    import leak_relocation_geopandas
    return leak_relocation_geopandas


class TestTheFieldsAreRequestedAndPinned:
    """The names, checked against the committed metadata by
    tests/test_dnv_service_metadata.py, and requested here."""

    def test_the_leak_date_field(self, workflow):
        """One name per leak source, strict order: layer 206's first, the
        Maximo query's second. No frame carries both, so the order only decides
        which is looked for first."""
        assert workflow.LEAK_DATE_CANDIDATES == ["REVISEDLEAKDATE", "MAXIMOLEAKDATE"]

    def test_the_pipe_life_fields(self, workflow):
        assert workflow.PIPE_CREATED_CANDIDATES == ["CREATIONDATE"]
        assert workflow.PIPE_RETIRED_CANDIDATES == ["dateretired"]

    def test_every_pipe_layer_has_a_rule(self, workflow):
        assert workflow.LAYER_DATE_RULES == {
            "distribution": IN_SERVICE_AT_LEAK,
            "service": IN_SERVICE_AT_LEAK,
            "retired": RETIRED_AFTER_LEAK,
        }

    def test_the_leak_download_asks_for_the_date(self, workflow):
        meta = {"fields": [{"name": n} for n in
                           ["OBJECTID", "LMSLEAKNUMBER", "REVISEDLEAKDATE"]],
                "object_id_field": "OBJECTID"}
        with redirect_stdout(io.StringIO()):
            out = workflow.build_out_fields(meta, "historic leaks")
        assert "REVISEDLEAKDATE" in out

    def test_the_pipe_download_asks_for_both_life_dates(self, workflow):
        meta = {"fields": [{"name": n} for n in
                           ["OBJECTID", "ASSETGROUP", "ASSETTYPE",
                            "nominaldiameter", "CREATIONDATE", "dateretired"]],
                "object_id_field": "OBJECTID"}
        with redirect_stdout(io.StringIO()):
            out = workflow.build_out_fields(meta, "distribution pipes")
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
        assert workflow.epoch_ms_to_iso(LEAK) == "2020-09-13"

    def test_a_missing_date_is_blank_not_the_epoch(self, workflow):
        """1970-01-01 in an audit column reads as a real date."""
        assert workflow.epoch_ms_to_iso(None) == ""

    def test_an_unrepresentable_value_is_blank(self, workflow):
        assert workflow.epoch_ms_to_iso(1e30) == ""


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
            assert workflow.date_value_to_epoch_ms(value) == 1_156_392_000_000, value

    @pytest.mark.parametrize("value,iso", [
        ("2019-04-02", "2019-04-02"),
        ("2019-04-02 14:30:00", "2019-04-02"),
        ("2019-04-02T14:30:00Z", "2019-04-02"),
    ])
    def test_a_date_string_is_read_as_a_date(self, workflow, value, iso):
        assert workflow.epoch_ms_to_iso(
            workflow.date_value_to_epoch_ms(value)) == iso

    def test_a_datetime_object_is_read_as_a_date(self, workflow):
        import datetime as dt
        for value in (dt.datetime(2019, 4, 2), dt.date(2019, 4, 2)):
            assert workflow.epoch_ms_to_iso(
                workflow.date_value_to_epoch_ms(value)) == "2019-04-02", value

    def test_a_date_string_is_never_read_as_its_year(self, workflow):
        """The specific failure: 2019 milliseconds is 1 January 1970."""
        assert workflow.date_value_to_epoch_ms("2019-04-02") != 2019
        assert workflow.epoch_ms_to_iso(2019) == "1970-01-01"

    @pytest.mark.parametrize("value", [None, "", "   ", "not a date", float("nan")])
    def test_a_non_date_is_no_date_rather_than_a_wrong_one(self, workflow, value):
        assert workflow.date_value_to_epoch_ms(value) is None

    def test_what_counts_as_numeric(self, workflow):
        assert workflow.looks_numeric(5) is True
        assert workflow.looks_numeric(5.0) is True
        assert workflow.looks_numeric(" 1156392000000 ") is True
        assert workflow.looks_numeric("2019-04-02") is False
        assert workflow.looks_numeric("") is False
        assert workflow.looks_numeric(True) is False, "a bool is not a timestamp"

    def test_a_string_date_passes_the_pipe_life_check_it_should(self, workflow):
        """End to end: a 2019 leak against a pipe recorded in 2000 is in
        service. Read as the year 2019 it would have been rejected."""
        leak_ms = workflow.date_value_to_epoch_ms("2019-04-02")
        pipe_ms = workflow.date_value_to_epoch_ms(946_684_800_000.0)  # 2000-01-01
        allowed, reason = date_rule_result(
            IN_SERVICE_AT_LEAK, leak_ms, pipe_ms, None)
        assert allowed is True
        assert reason == "ok"
