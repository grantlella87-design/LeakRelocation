"""The fill report has to tell an empty column from a full one.

It reported LeakAddress and LeakDate as 100% filled when every value was the empty
string. The blank check only ran for dtype "object", and pandas 3 gives a text
column dtype "str", so it never ran at all - which is the opposite of what the
report exists to do.
"""
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

pytest.importorskip("pandas")

import field_fill_report as report
import pandas as pd


class TestCountingFilledValues:
    def test_empty_strings_are_not_values(self):
        for dtype in ("str", "object"):
            series = pd.Series(["", "  ", "x"], dtype=dtype)
            assert report.filled(series) == 1, dtype

    def test_nulls_are_not_values(self):
        assert report.filled(pd.Series([None, None, "x"])) == 1
        assert report.filled(pd.Series([float("nan"), 1.0])) == 1

    def test_a_column_of_empty_strings_reads_as_empty(self):
        """The reported case: 90,987 rows, every LeakAddress "", counted as full."""
        assert report.filled(pd.Series([""] * 100, dtype="str")) == 0

    def test_zero_is_a_value(self):
        """Service pipes really do carry nominaldiameter 0.0 and pressures of 0.5,
        so a numeric zero must not be mistaken for a blank."""
        assert report.filled(pd.Series([0, 0, 0])) == 3
        assert report.filled(pd.Series([0.0, 1.5])) == 2

    def test_false_is_a_value(self):
        assert report.filled(pd.Series([False, True])) == 2

    def test_not_a_time_is_not_a_value(self):
        series = pd.Series(pd.to_datetime(["2020-01-01", None]))
        assert report.filled(series) == 1

    def test_an_empty_column_is_zero_not_an_error(self):
        assert report.filled(pd.Series([], dtype="str")) == 0
        assert report.filled(None) == 0

    def test_epoch_dates_are_values(self):
        """The caches hold dates as epoch milliseconds, which are numbers."""
        assert report.filled(pd.Series([1_156_392_000_000.0, None])) == 1


class TestTheSample:
    def test_a_blank_column_samples_as_blank(self):
        assert report.sample(pd.Series(["", "   "], dtype="str")) == ""

    def test_the_first_real_value_is_shown(self):
        assert report.sample(pd.Series(["", "12 Elm St"], dtype="str")) == "12 Elm St"


@pytest.fixture(scope="module")
def leak_fields():
    import json
    path = os.path.join(
        REPO_ROOT, "reference", "mapserver_json",
        "NY_DNV_Synergi_RiskResults_Assets_NY", "layer_206_Hist_GasLeak.json")
    if not os.path.isfile(path):
        pytest.skip("reference metadata is not in this checkout")
    with open(path, encoding="utf-8") as handle:
        return {f["name"] for f in json.load(handle)["fields"]}


class TestTheCandidateFields:
    """--service asks about fields this project does not request, to find out
    which of them MA actually populates. Asking for a field the layer does not
    have makes the service reject the query, so every name has to be real."""

    def test_every_leak_candidate_exists_on_layer_206(self, leak_fields):
        unknown = [name for name in report.CANDIDATES["historic_leaks"]
                   if name not in leak_fields]
        assert unknown == [], f"layer 206 has no such field: {unknown}"

    def test_the_candidates_do_not_repeat_what_is_already_asked(self):
        already = {name.lower() for name in report.EXPECTED["historic_leaks"]}
        repeated = [name for name in report.CANDIDATES["historic_leaks"]
                    if name.lower() in already]
        assert repeated == [], repeated

    def test_the_other_leak_dates_are_all_offered(self, leak_fields):
        """REVISEDLEAKDATE is empty for MA, so the report has to show what the
        alternatives hold rather than leave the choice to guesswork."""
        offered = set(report.CANDIDATES["historic_leaks"])
        for name in ("DISCOVEREDDATE", "REPAIREDDATE", "COMPLETEDDATE"):
            assert name in offered
            assert name in leak_fields

    def test_the_other_location_fields_are_reported_from_the_cache(self, leak_fields):
        """They were candidates while the answer was unknown. The project now
        requests them, so they belong in the cache report - where a blank column
        is a fact about the download rather than a question about the service."""
        expected = set(report.EXPECTED["historic_leaks"])
        for name in ("NEARESTXSTREET", "CITY"):
            assert name in expected
            assert name in leak_fields

    def test_every_expected_leak_field_exists_on_layer_206(self, leak_fields):
        unknown = [name for name in report.EXPECTED["historic_leaks"]
                   if name not in leak_fields]
        assert unknown == [], f"layer 206 has no such field: {unknown}"


class TestAskingTheService:
    """--service drives the same request path as a run, against a stub that
    answers returnCountOnly the way ArcGIS does. It was shipped without ever
    being exercised, which is how the two failure paths below went unnoticed."""

    @pytest.fixture
    def stub(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs, urlparse

        state = {"counts": {}, "total": 1000, "reject": set(), "countless": False,
                 "asked": []}

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def reply(self, payload):
                body = payload.encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                where = (parse_qs(urlparse(self.path).query).get("where") or [""])[0]
                state["asked"].append(where)
                if state["countless"]:
                    self.reply('{"objectIdFieldName": "OBJECTID"}')
                    return
                if "IS NOT NULL" not in where:
                    self.reply(f'{{"count": {state["total"]}}}')
                    return
                field = where.split("AND")[-1].replace("IS NOT NULL", "").strip()
                if field in state["reject"]:
                    self.reply('{"error": {"code": 400, "message": "Invalid field"}}')
                    return
                self.reply(f'{{"count": {state["counts"].get(field, 0)}}}')

            do_POST = do_GET

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        state["url"] = f"http://127.0.0.1:{server.server_address[1]}"
        yield state
        server.shutdown()

    @pytest.fixture
    def workflow(self, monkeypatch):
        import importlib.util
        import io
        from contextlib import redirect_stdout

        pytest.importorskip("geopandas")
        pytest.importorskip("keyring")
        path = os.path.join(REPO_ROOT, "src", "leak_relocation_geopandas.py")
        spec = importlib.util.spec_from_file_location("lr_fill", path)
        module = importlib.util.module_from_spec(spec)
        with redirect_stdout(io.StringIO()):
            spec.loader.exec_module(module)

        import requests
        session = requests.Session()
        session._arcgis_access_token = "test-token"
        module.make_session = lambda *a, **k: session
        monkeypatch.setitem(sys.modules, "leak_relocation_geopandas", module)
        return module

    def run(self, stub, fields, candidates=()):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            report.service_counts(stub["url"], "historic leaks", fields,
                                  "jurisdiction = 'MA'", candidates)
        return buffer.getvalue()

    def test_an_empty_field_is_marked(self, stub, workflow):
        stub["counts"] = {"REVISEDLEAKDATE": 0, "LMSLEAKNUMBER": 1000}
        printed = self.run(stub, ("LMSLEAKNUMBER", "REVISEDLEAKDATE"))
        assert "<-REVISEDLEAKDATE" in printed
        assert "<-LMSLEAKNUMBER" not in printed

    def test_the_candidates_are_asked_too(self, stub, workflow):
        stub["counts"] = {"REVISEDLEAKDATE": 0, "DISCOVEREDDATE": 990}
        printed = self.run(stub, ("REVISEDLEAKDATE",), ("DISCOVEREDDATE",))
        assert "not requested by this project" in printed
        assert "DISCOVEREDDATE" in printed
        assert any("DISCOVEREDDATE IS NOT NULL" in w for w in stub["asked"])

    def test_a_rejected_field_does_not_stop_the_report(self, stub, workflow):
        """Asking for a field the layer does not have makes the service reject
        that query; the remaining fields still have to be reported."""
        stub["counts"] = {"CITY": 900}
        stub["reject"] = {"NOSUCHFIELD"}
        printed = self.run(stub, (), ("NOSUCHFIELD", "CITY"))
        assert "could not ask" in printed
        assert "CITY" in printed

    def test_a_reply_with_no_count_is_reported(self, stub, workflow):
        """query_count returns None there, and everything downstream divides by
        it."""
        stub["countless"] = True
        printed = self.run(stub, ("REVISEDLEAKDATE",))
        assert "did not return a row count" in printed

    def test_one_query_per_field_and_no_data_is_downloaded(self, stub, workflow):
        stub["counts"] = {"A": 1, "B": 2, "C": 3}
        self.run(stub, ("A", "B", "C"))
        # One for the total, three for the fields.
        assert len(stub["asked"]) == 4
        assert all("returnCountOnly" not in w for w in stub["asked"])
