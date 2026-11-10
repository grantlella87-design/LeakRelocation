"""Reading enterprise (Maximo) leaks from SQL.

Driven against a fake DB-API connection, so every one of these runs with no
database, no ODBC driver and no credentials. That is the point of the module
taking a connection rather than making one: the boundary is testable.

Three things matter most here and each has its own class:

  * the connection string never reaches a log. It is a credential, and this
    transcript and the run log both persist;
  * a query that returns the wrong columns fails by name, now, rather than
    producing a frame that relocates nothing and looks like a data problem;
  * a row that cannot be matched is counted, not dropped silently and not
    placed at 0, 0 - which is in the Gulf of Guinea and would relocate against
    nothing while still being reported as a leak that was read.
"""
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from leakrelocation import config
from leakrelocation import maximo_sql as mx

ROW = {
    mx.LEAK_ID: "WO-1001",
    mx.LEAK_NUMBER: "1001",
    mx.LEAK_X: -71.0589,
    mx.LEAK_Y: 42.3601,
    mx.LEAK_MATERIAL: "Cast Iron",
    mx.LEAK_DIAMETER: 8.0,
    mx.LEAK_FACILITY: "Distribution Main",
    mx.LEAK_PRESSURE: "LP",
    mx.LEAK_DATE: "2019-04-02",
    mx.LEAK_ADDRESS: "12 Elm St",
    mx.LEAK_CITY: "WATERTOWN",
    mx.LEAK_CROSS_STREET: "OAK ST",
}

ALL_COLUMNS = list(ROW)


class FakeCursor:
    def __init__(self, columns, rows, fail_with=None):
        self._columns = list(columns)
        self._rows = [tuple(row) for row in rows]
        self._fail_with = fail_with
        self.description = None
        self.executed = None
        self.closed = False

    def execute(self, sql):
        if self._fail_with:
            raise self._fail_with
        self.executed = sql
        self.description = [(name,) + (None,) * 6 for name in self._columns]
        return self

    def fetchmany(self, size):
        taken, self._rows = self._rows[:size], self._rows[size:]
        return taken

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, columns=ALL_COLUMNS, rows=None, fail_with=None):
        self.columns = list(columns)
        self.rows = rows if rows is not None else [
            [ROW[name] for name in self.columns]]
        self._fail_with = fail_with
        self.cursors = []
        self.closed = False

    def cursor(self):
        made = FakeCursor(self.columns, self.rows, self._fail_with)
        self.cursors.append(made)
        return made

    def close(self):
        self.closed = True


def connection_of(rows, columns=ALL_COLUMNS):
    """A connection returning `rows` given as dicts."""
    return FakeConnection(
        columns, [[row.get(name) for name in columns] for row in rows])


class TestTheConnectionStringStaysSecret:
    """It carries credentials. The GIS token already has this discipline - only
    its length and expiry are ever printed - and a DSN is no different."""

    @pytest.mark.parametrize("dsn,secret", [
        ("Driver={x};Server=h;Uid=svc;Pwd=hunter2", "hunter2"),
        ("Server=h;PASSWORD=Sekrit!;Database=d", "Sekrit!"),
        ("Server=h;passwd=abc123", "abc123"),
        ("Server=h;Token=eyJhbGciOi", "eyJhbGciOi"),
        ("Server=h;api_key=AKIA123", "AKIA123"),
    ])
    def test_the_password_is_redacted(self, dsn, secret):
        safe = mx.safe_dsn(dsn)
        assert secret not in safe
        assert "***" in safe

    def test_the_rest_is_kept_so_the_line_is_useful(self):
        safe = mx.safe_dsn("Driver={ODBC Driver 18};Server=sql01;Pwd=hunter2")
        assert "sql01" in safe
        assert "ODBC Driver 18" in safe

    def test_an_unset_string_says_so_rather_than_printing_none(self):
        assert mx.safe_dsn("") == "(not set)"
        assert mx.safe_dsn(None) == "(not set)"

    def test_reading_leaks_never_logs_a_password(self, capsys, monkeypatch):
        """The end-to-end form. Any line this module prints during a read is
        checked, not only the one that names the DSN."""
        monkeypatch.setenv(config.MAXIMO_DSN_ENV,
                           "Driver={x};Server=h;Uid=svc;Pwd=hunter2")
        mx.read_leaks(connection=connection_of([ROW]), query="SELECT 1")
        printed = capsys.readouterr().out
        assert "hunter2" not in printed

    def test_a_failed_connection_message_is_redacted(self, monkeypatch, capsys):
        """A driver error can echo the DSN back. It is redacted rather than
        trusted not to."""
        dsn = "Driver={x};Server=h;Pwd=hunter2"
        monkeypatch.setenv(config.MAXIMO_DSN_ENV, dsn)
        fake = type(sys)("pyodbc")
        fake.connect = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError(f"login failed for {dsn}"))
        monkeypatch.setitem(sys.modules, "pyodbc", fake)
        with pytest.raises(RuntimeError) as raised:
            mx.connect()
        assert "hunter2" not in str(raised.value)
        assert "hunter2" not in capsys.readouterr().out

    def test_a_missing_dsn_says_how_to_set_it(self, monkeypatch):
        monkeypatch.delenv(config.MAXIMO_DSN_ENV, raising=False)
        with pytest.raises(RuntimeError, match=config.MAXIMO_DSN_ENV):
            mx.connection_string()


class TestTheColumnContract:
    """A query returning the wrong names has to fail here. Three stages later it
    is an empty output and looks like a data problem."""

    def test_every_required_column_present_resolves(self):
        resolved = mx.check_columns(ALL_COLUMNS)
        for name in mx.REQUIRED_COLUMNS:
            assert name in resolved

    @pytest.mark.parametrize("dropped", list(mx.REQUIRED_COLUMNS))
    def test_a_missing_required_column_is_named(self, dropped):
        columns = [name for name in ALL_COLUMNS if name != dropped]
        with pytest.raises(RuntimeError) as raised:
            mx.check_columns(columns)
        assert dropped in str(raised.value)

    def test_the_message_says_what_the_query_did_return(self):
        with pytest.raises(RuntimeError) as raised:
            mx.check_columns(["wonum", "lat", "lon"])
        text = str(raised.value)
        assert "wonum" in text and "lat" in text

    def test_an_empty_result_set_is_refused_clearly(self):
        with pytest.raises(RuntimeError, match="nothing"):
            mx.check_columns([])

    def test_case_does_not_matter(self):
        """SQL Server hands back whatever the query aliased, and leakid,
        LeakID and LeakId are the same column."""
        resolved = mx.check_columns(
            ["leakid", "x", "y", "MATERIAL", "diameter"])
        assert resolved[mx.LEAK_ID] == "leakid"
        assert resolved[mx.LEAK_MATERIAL] == "MATERIAL"

    def test_surrounding_space_does_not_matter(self):
        assert mx.resolve_columns([" LeakId "])[mx.LEAK_ID] == " LeakId "

    def test_the_optional_columns_are_optional(self):
        resolved = mx.check_columns(list(mx.REQUIRED_COLUMNS))
        assert set(resolved) == set(mx.REQUIRED_COLUMNS)

    def test_a_column_the_contract_does_not_know_is_ignored(self):
        resolved = mx.check_columns([*mx.REQUIRED_COLUMNS, "siteid", "status"])
        assert "siteid" not in resolved

    def test_every_optional_column_has_a_stated_cost(self):
        """A warning that says a column is missing without saying what that
        costs is a warning nobody can act on."""
        for name in mx.OPTIONAL_COLUMNS:
            assert name in mx.WHAT_IS_LOST, name
            assert mx.WHAT_IS_LOST[name].strip()

    def test_the_missing_ones_are_reported_with_their_cost(self, capsys):
        mx.describe(mx.resolve_columns(list(mx.REQUIRED_COLUMNS)))
        printed = capsys.readouterr().out
        assert "FacilityType" in printed
        assert "both mains and services" in printed


class TestWhichRowsAreUsable:
    def test_a_good_row_becomes_a_leak_and_its_attributes(self):
        records, attributes, counters = mx.leak_records([ROW])
        assert counters["usable"] == 1
        assert records[0]["MAXIMOLEAKID"] == "WO-1001"
        assert records[0]["LEAKNUMBER"] == "1001"
        assert records[0]["_x"] == pytest.approx(-71.0589)
        assert attributes["WO-1001"] == {
            "material": "Cast Iron", "diameter": 8.0,
            "pressure": "LP", "facility": "Distribution Main"}

    def test_the_attributes_are_the_shape_prepare_leaks_takes(self):
        """The same four keys load_supplemental builds for the historic leaks.
        That is what lets both sources share the matching."""
        _, attributes, _ = mx.leak_records([ROW])
        assert set(next(iter(attributes.values()))) == {
            "material", "diameter", "pressure", "facility"}

    def test_a_row_with_no_id_is_counted_not_dropped(self):
        _, _, counters = mx.leak_records([{**ROW, mx.LEAK_ID: None}])
        assert counters["no_id"] == 1
        assert counters["usable"] == 0

    @pytest.mark.parametrize("bad", [None, "", "   ", "not a number",
                                     float("nan"), float("inf")])
    def test_a_row_with_no_usable_position_is_counted(self, bad):
        """0, 0 is in the Gulf of Guinea. A leak placed there relocates against
        nothing and is still reported as a leak that was read."""
        _, _, counters = mx.leak_records([{**ROW, mx.LEAK_X: bad}])
        assert counters["no_coordinates"] == 1
        assert counters["usable"] == 0

    def test_a_row_with_no_material_or_diameter_is_counted(self):
        for field in (mx.LEAK_MATERIAL, mx.LEAK_DIAMETER):
            _, _, counters = mx.leak_records([{**ROW, field: None}])
            assert counters["no_match_attributes"] == 1, field

    def test_a_zero_diameter_is_a_diameter(self):
        """Service pipes really do carry nominaldiameter 0.0."""
        _, attributes, counters = mx.leak_records([{**ROW, mx.LEAK_DIAMETER: 0}])
        assert counters["usable"] == 1
        assert attributes["WO-1001"]["diameter"] == 0.0

    def test_a_diameter_as_text_is_read(self):
        _, attributes, _ = mx.leak_records([{**ROW, mx.LEAK_DIAMETER: "8"}])
        assert attributes["WO-1001"]["diameter"] == 8.0

    def test_a_repeated_id_is_skipped_and_counted(self):
        """Joining on a non-unique key is exactly how the historic path once
        gave 8,425 leaks another leak's material and diameter. Here the second
        row is refused rather than silently replacing the first."""
        second = {**ROW, mx.LEAK_MATERIAL: "Plastic PE", mx.LEAK_DIAMETER: 2.0}
        records, attributes, counters = mx.leak_records([ROW, second])
        assert counters["duplicate_id"] == 1
        assert counters["usable"] == 1
        assert len(records) == 1
        assert attributes["WO-1001"]["material"] == "Cast Iron"

    def test_ids_are_normalised_the_way_the_rest_of_the_project_does(self):
        """normalize_key strips braces and upper-cases, so the id matches
        however the query spells it."""
        _, attributes, _ = mx.leak_records([{**ROW, mx.LEAK_ID: " wo-1001 "}])
        assert "WO-1001" in attributes

    def test_the_leak_number_falls_back_to_the_id(self):
        records, _, _ = mx.leak_records([{**ROW, mx.LEAK_NUMBER: None}])
        assert records[0]["LEAKNUMBER"] == "WO-1001"

    def test_missing_optional_columns_leave_blanks_not_errors(self):
        bare = {name: ROW[name] for name in mx.REQUIRED_COLUMNS}
        records, attributes, counters = mx.leak_records([bare])
        assert counters["usable"] == 1
        assert records[0]["ADDRESS"] == ""
        assert attributes["WO-1001"]["facility"] == ""

    def test_the_counters_account_for_every_row(self):
        rows = [ROW,
                {**ROW, mx.LEAK_ID: "WO-2", mx.LEAK_X: None},
                {**ROW, mx.LEAK_ID: "WO-3", mx.LEAK_MATERIAL: ""},
                {**ROW, mx.LEAK_ID: None},
                ROW]
        _, _, counters = mx.leak_records(rows)
        assert counters["rows_read"] == 5
        assert (counters["usable"] + counters["no_id"] + counters["duplicate_id"]
                + counters["no_coordinates"] + counters["no_match_attributes"]
                == 5)

    def test_the_object_ids_are_unique_and_sequential(self):
        """prepare_leaks reads OBJECTID as an int and keys the match results on
        it, so two leaks sharing one would lose a relocation."""
        rows = [{**ROW, mx.LEAK_ID: f"WO-{i}"} for i in range(5)]
        records, _, _ = mx.leak_records(rows)
        oids = [record["OBJECTID"] for record in records]
        assert oids == [1, 2, 3, 4, 5]
        assert len(set(oids)) == len(oids)


class TestTheFrame:
    def test_it_carries_the_geometry_and_the_crs(self):
        pytest.importorskip("geopandas")
        records, _, _ = mx.leak_records([ROW])
        frame = mx.leaks_frame(records, crs="EPSG:4326")
        assert len(frame) == 1
        assert frame.crs.to_string() == "EPSG:4326"
        assert frame.geometry.iloc[0].x == pytest.approx(-71.0589)
        assert frame.geometry.iloc[0].y == pytest.approx(42.3601)

    def test_the_helper_columns_do_not_reach_the_frame(self):
        pytest.importorskip("geopandas")
        records, _, _ = mx.leak_records([ROW])
        frame = mx.leaks_frame(records)
        assert "_x" not in frame.columns and "_y" not in frame.columns

    def test_a_projected_crs_is_honoured(self):
        """MA state plane feet. Read as longitude these coordinates would put
        every leak off the coast of Africa, and the run would still finish."""
        pytest.importorskip("geopandas")
        records, _, _ = mx.leak_records([
            {**ROW, mx.LEAK_X: 760000.0, mx.LEAK_Y: 2960000.0}])
        frame = mx.leaks_frame(records, crs="EPSG:2249")
        assert frame.crs.to_epsg() == 2249

    def test_no_leaks_is_an_empty_frame_not_an_error(self):
        pytest.importorskip("geopandas")
        frame = mx.leaks_frame([], crs="EPSG:4326")
        assert len(frame) == 0
        assert frame.crs is not None

    def test_the_columns_are_the_ones_the_workflow_resolves(self):
        """The frame is handed straight to prepare_leaks, which finds its
        fields through the workflow's candidate lists."""
        pytest.importorskip("geopandas")
        records, _, _ = mx.leak_records([ROW])
        columns = set(mx.leaks_frame(records).columns)
        for name in ("OBJECTID", "MAXIMOLEAKID", "LEAKNUMBER", "ADDRESS",
                     "CITY", "NEARESTXSTREET", "MAXIMOLEAKDATE"):
            assert name in columns, name


class TestReadingEndToEnd:
    def test_it_runs_the_query_and_returns_everything(self):
        pytest.importorskip("geopandas")
        connection = connection_of([ROW, {**ROW, mx.LEAK_ID: "WO-2"}])
        frame, attributes, counters = mx.read_leaks(
            connection=connection, query="SELECT 1")
        assert len(frame) == 2
        assert set(attributes) == {"WO-1001", "WO-2"}
        assert counters["usable"] == 2
        assert connection.cursors[0].executed == "SELECT 1"

    def test_a_supplied_connection_is_left_open(self):
        """The caller owns it. Closing someone else's connection is how a
        --check followed by a run stops working."""
        pytest.importorskip("geopandas")
        connection = connection_of([ROW])
        mx.read_leaks(connection=connection, query="SELECT 1")
        assert connection.closed is False
        assert connection.cursors[0].closed is True

    def test_the_limit_stops_reading(self):
        pytest.importorskip("geopandas")
        rows = [{**ROW, mx.LEAK_ID: f"WO-{i}"} for i in range(50)]
        frame, _, counters = mx.read_leaks(
            connection=connection_of(rows), query="SELECT 1", limit=10)
        assert counters["rows_read"] == 10
        assert len(frame) == 10

    def test_a_query_missing_a_column_fails_before_any_rows_are_read(self):
        pytest.importorskip("geopandas")
        columns = [name for name in ALL_COLUMNS if name != mx.LEAK_MATERIAL]
        with pytest.raises(RuntimeError, match=mx.LEAK_MATERIAL):
            mx.read_leaks(connection=connection_of([ROW], columns),
                          query="SELECT 1")

    def test_a_cursor_that_will_not_close_does_not_lose_the_result(self):
        """Nothing downstream depends on the close, and raising there would
        throw away rows that were read successfully."""
        pytest.importorskip("geopandas")
        connection = connection_of([ROW])
        original = FakeCursor.close

        def refuse(self):
            raise RuntimeError("connection reset")
        FakeCursor.close = refuse
        try:
            frame, _, _ = mx.read_leaks(connection=connection, query="SELECT 1")
            assert len(frame) == 1
        finally:
            FakeCursor.close = original

    def test_rows_are_fetched_in_batches(self):
        """fetchmany rather than fetchall: a query that returns far more than
        expected should not fill memory before anything notices."""
        pytest.importorskip("geopandas")
        rows = [{**ROW, mx.LEAK_ID: f"WO-{i}"} for i in range(12)]
        connection = connection_of(rows)
        frame, _, _ = mx.read_leaks(connection=connection, query="SELECT 1")
        assert len(frame) == 12
        assert mx.FETCH_SIZE > 1


class TestTheQueryFile:
    def test_the_committed_template_exists(self):
        assert os.path.isfile(config.MAXIMO_QUERY_FILE)

    def test_it_documents_every_required_alias(self):
        text = open(config.MAXIMO_QUERY_FILE, encoding="utf-8").read()
        for name in mx.REQUIRED_COLUMNS:
            assert name in text, name

    def test_it_selects_every_required_alias(self):
        """Not only in the comments: the template has to be a query that would
        satisfy the contract once its tables are replaced."""
        text = open(config.MAXIMO_QUERY_FILE, encoding="utf-8").read()
        body = text[text.upper().index("SELECT"):]
        for name in mx.REQUIRED_COLUMNS:
            assert f"AS {name}" in body, name

    def test_it_says_it_is_a_template(self):
        """The table names are placeholders. Someone running it unchanged
        should learn that from the file, not from a SQL error."""
        text = open(config.MAXIMO_QUERY_FILE, encoding="utf-8").read()
        assert "TEMPLATE" in text.upper()

    def test_it_carries_no_credentials(self):
        """The DSN belongs in the environment. A committed file with a password
        in it is the thing this whole arrangement exists to avoid."""
        text = open(config.MAXIMO_QUERY_FILE, encoding="utf-8").read().lower()
        for marker in ("pwd=", "password=", "uid="):
            assert marker not in text, marker

    def test_a_missing_query_file_says_where_to_put_one(self, tmp_path,
                                                        monkeypatch):
        monkeypatch.setattr(config, "MAXIMO_QUERY_FILE",
                            str(tmp_path / "nothing.sql"))
        with pytest.raises(RuntimeError, match="No Maximo query"):
            mx.read_query()

    def test_an_empty_query_file_is_refused(self, tmp_path):
        empty = tmp_path / "empty.sql"
        empty.write_text("")
        with pytest.raises(RuntimeError, match="empty"):
            mx.read_query(str(empty))

    def test_a_byte_order_mark_does_not_break_the_query(self, tmp_path):
        """Notepad on Windows writes one, and it would otherwise be the first
        character of the SQL."""
        path = tmp_path / "bom.sql"
        path.write_bytes("﻿SELECT 1".encode())
        assert mx.read_query(str(path)).startswith("SELECT")


class TestItsOwnOutput:
    def test_the_maximo_output_is_a_different_file(self):
        """Writing both leak sets to one GeoPackage would mean the second run
        silently replacing the first."""
        assert config.MAXIMO_OUTPUT_GPKG != config.OUTPUT_GPKG
        assert config.MAXIMO_OUTPUT_GPKG.name == "MaximoLeakRelocation.gpkg"

    def test_the_diameter_rule_still_splits_it_further(self):
        widened = config.output_gpkg_for("fuzzy", base=config.MAXIMO_OUTPUT_GPKG)
        assert widened != config.MAXIMO_OUTPUT_GPKG
        assert "fuzzy" in widened.name
        assert "Maximo" in widened.name
