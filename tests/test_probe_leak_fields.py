"""The probe has to tell the three reasons a field is empty apart.

ADDRESS is on layer 206, it is in the outFields this project sends, and all
98,501 cached MA rows are empty. The probe exists to say which of these is true,
and getting the answer wrong sends the fix in the wrong direction entirely:

    1. the request loses it            -> fix the request here;
    2. populated, but not on MA rows   -> nothing here can fill it in;
    3. empty for everyone              -> same, and stop asking.

Every case below is driven against a stub that answers returnCountOnly and
query the way ArcGIS does, because the counts are the whole argument.
"""
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

pytest.importorskip("geopandas")
pytest.importorskip("keyring")

import probe_leak_fields as probe

# name -> (type, rows with a real value on MA, rows with a real value anywhere)
LAYER = {
    "ADDRESS": ("esriFieldTypeString", 0, 0),
    "NEARESTXSTREET": ("esriFieldTypeString", 61_220, 190_400),
    "CITY": ("esriFieldTypeString", 98_400, 240_000),
    "REVISEDLEAKDATE": ("esriFieldTypeDate", 0, 140_000),
    "DISCOVEREDDATE": ("esriFieldTypeDate", 97_930, 239_000),
}
MA_ROWS = 98_501


class TestTheWhereClauses:
    """A blank is not a null, and `<> ''` against a date field is a query the
    service rejects - which is why the type decides the clause."""

    def test_a_text_field_excludes_the_empty_string(self):
        where = probe.has_value("jurisdiction = 'MA'", "ADDRESS", True)
        assert "ADDRESS IS NOT NULL" in where
        assert "ADDRESS <> ''" in where
        assert "jurisdiction = 'MA'" in where

    def test_a_date_field_is_only_checked_for_null(self):
        where = probe.has_value("jurisdiction = 'MA'", "REVISEDLEAKDATE", False)
        assert "IS NOT NULL" in where
        assert "<> ''" not in where

    def test_without_a_scope_there_is_no_empty_conjunction(self):
        assert probe.has_value("", "ADDRESS", True) == "ADDRESS IS NOT NULL AND ADDRESS <> ''"


class TestTheVerdict:
    def test_asked_for_and_present_on_our_rows_is_a_bug_here(self):
        text = probe.verdict({"everywhere": 240_000, "in_scope": 98_000},
                             cached_in_request=True)
        assert "bug here" in text

    def test_present_but_not_requested_says_to_request_it(self):
        text = probe.verdict({"everywhere": 240_000, "in_scope": 98_000},
                             cached_in_request=False)
        assert "does not ask for it" in text

    def test_populated_elsewhere_but_not_on_our_rows(self):
        """The case a MA-only cache cannot tell from an empty field."""
        text = probe.verdict({"everywhere": 140_000, "in_scope": 0},
                             cached_in_request=True)
        assert "none of the rows this project downloads" in text
        assert "bug here" not in text

    def test_empty_everywhere_says_so(self):
        text = probe.verdict({"everywhere": 0, "in_scope": 0},
                             cached_in_request=True)
        assert "empty for every row" in text

    def test_no_answer_is_not_reported_as_an_empty_field(self):
        text = probe.verdict({"everywhere": None, "in_scope": None},
                             cached_in_request=True)
        assert "could not be determined" in text


@pytest.fixture
def stub():
    """A layer that answers counts from LAYER and hands back matching rows."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    state = {"layer": dict(LAYER), "asked": [], "fetched": [], "reject": set()}

    def counted(where):
        scoped = "jurisdiction = 'MA'" in where
        for name, (_type, on_ma, anywhere) in state["layer"].items():
            if name not in where:
                continue
            if "IS NULL" in where:
                return MA_ROWS - on_ma if scoped else 300_000 - anywhere
            if "= ''" in where and "<>" not in where:
                return 0
            return on_ma if scoped else anywhere
        return MA_ROWS if scoped else 300_000

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def reply(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            where = (query.get("where") or [""])[0]
            if self.path.split("?")[0].rstrip("/").endswith("206"):
                self.reply({
                    "fields": [{"name": name, "type": kind}
                               for name, (kind, _, _) in state["layer"].items()]
                    + [{"name": "OBJECTID", "type": "esriFieldTypeOID"},
                       {"name": "LASTUPDATE", "type": "esriFieldTypeDate"},
                       {"name": "LMSLEAKNUMBER", "type": "esriFieldTypeString"},
                       {"name": "GlobalID", "type": "esriFieldTypeGlobalID"},
                       {"name": "STATE", "type": "esriFieldTypeString"},
                       {"name": "jurisdiction", "type": "esriFieldTypeString"}],
                    "objectIdField": "OBJECTID",
                    "maxRecordCount": 1000,
                    "extent": {"spatialReference": {"wkid": 4326}},
                })
                return
            state["asked"].append(where)
            if any(name in where for name in state["reject"]):
                self.reply({"error": {"code": 400, "message": "Invalid field"}})
                return
            if (query.get("returnCountOnly") or [""])[0] == "true":
                self.reply({"count": counted(where)})
                return
            state["fetched"].append(where)
            wanted = [n.strip() for n in
                      (query.get("outFields") or [""])[0].split(",") if n.strip()]
            self.reply({"features": [
                {"attributes": {name: f"{name}-{index}" for name in wanted}}
                for index in range(1, 3)]})

        do_POST = do_GET

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}/MapServer/206"
    yield state
    server.shutdown()


@pytest.fixture
def workflow(monkeypatch):
    import importlib.util
    import io
    from contextlib import redirect_stdout

    path = os.path.join(REPO_ROOT, "src", "leak_relocation_geopandas.py")
    spec = importlib.util.spec_from_file_location("lr_probe", path)
    module = importlib.util.module_from_spec(spec)
    with redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)

    import requests
    session = requests.Session()
    session._arcgis_access_token = "test-token"
    module.make_session = lambda *a, **k: session
    monkeypatch.setitem(sys.modules, "leak_relocation_geopandas", module)
    return module


def run(stub, fields):
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        probe.main(["--url", stub["url"], "--fields", fields])
    return buffer.getvalue()


class TestAgainstTheService:
    def test_a_field_empty_only_on_our_rows_is_not_called_a_bug(self, stub, workflow):
        """REVISEDLEAKDATE: 140,000 rows on the layer have one, no MA row does.
        Reading that as a bug here sends someone looking through the request for
        a fault that is not in it."""
        printed = run(stub, "REVISEDLEAKDATE")
        assert "none of the rows this project downloads" in printed
        assert "bug here" not in printed

    def test_a_field_empty_everywhere_says_so(self, stub, workflow):
        printed = run(stub, "ADDRESS")
        assert "empty for every row" in printed

    def test_a_field_we_lose_is_named_as_our_bug(self, stub, workflow):
        """CITY is populated on 98,400 MA rows and is in the outFields, so an
        empty cache column can only have been dropped here."""
        printed = run(stub, "CITY")
        assert "bug here" in printed

    def test_a_blank_is_counted_as_empty_not_as_a_value(self, stub, workflow):
        """`ADDRESS IS NOT NULL` is true of the empty string, so counting only
        nulls would report a column of blanks as fully populated - the exact
        mistake the fill report already made once."""
        run(stub, "ADDRESS")
        assert any("<> ''" in where for where in stub["asked"])

    def test_a_date_field_is_never_compared_to_a_string(self, stub, workflow):
        run(stub, "REVISEDLEAKDATE")
        dates = [w for w in stub["asked"] if "REVISEDLEAKDATE" in w]
        assert dates
        assert all("<> ''" not in where for where in dates)

    def test_values_are_pulled_from_rows_that_have_one(self, stub, workflow):
        """The first five rows of a MA-only query are all empty in the case this
        is trying to see, so sampling them would show nothing either way."""
        run(stub, "CITY")
        fetched = [w for w in stub["asked"] if "IS NOT NULL" in w]
        assert fetched

    def test_the_whole_layer_is_asked_not_only_our_rows(self, stub, workflow):
        run(stub, "ADDRESS")
        assert any("jurisdiction" not in where for where in stub["asked"])

    def test_a_rejected_field_does_not_stop_the_probe(self, stub, workflow):
        stub["reject"] = {"NEARESTXSTREET"}
        printed = run(stub, "NEARESTXSTREET,CITY")
        assert "CITY" in printed
        assert "could not be determined" in printed

    def test_a_field_the_layer_does_not_have_is_refused_before_querying(
            self, stub, workflow):
        """The service rejects a query naming a field the layer does not have,
        so the name is checked against the metadata first and the message says
        what the layer does have."""
        import io
        from contextlib import redirect_stdout

        with redirect_stdout(io.StringIO()), pytest.raises(RuntimeError) as raised:
            probe.main(["--url", stub["url"], "--fields", "NOSUCHFIELD"])
        assert "no NOSUCHFIELD field" in str(raised.value)
        assert stub["asked"] == []

    def test_nothing_is_downloaded(self, stub, workflow):
        """A probe that pulls 98,501 features to answer a counting question is
        the slow way to be told what returnCountOnly says in one request."""
        run(stub, "ADDRESS,CITY")
        # One sample fetch per field at most, each capped at --samples rows.
        assert len(stub["fetched"]) <= 2
