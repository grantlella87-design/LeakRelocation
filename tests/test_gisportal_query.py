"""Tests for the GISportal MA main lines export.

Driven against a stub ArcGIS layer, so no network, no token and no portal.
"""
import csv
import io
import json
import os
import sys
from contextlib import redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
sys.path.insert(0, os.path.join(REPO_ROOT, "GISportal"))

pytest.importorskip("keyring")

import query_ma_main_lines_2022_plus as exporter

LAYER_URL = "https://stub/arcgis/rest/services/MA/Material_View_MA/MapServer/341"

FIELDS = [
    {"name": "OBJECTID", "type": "esriFieldTypeOID"},
    {"name": "GLOBALID", "type": "esriFieldTypeGUID"},
    {"name": "material", "type": "esriFieldTypeString"},
    {"name": "nominaldiameter", "type": "esriFieldTypeDouble"},
    {"name": "installationdate", "type": "esriFieldTypeDate"},
    {"name": "CREATIONDATE", "type": "esriFieldTypeDate"},
    {"name": "LASTUPDATE", "type": "esriFieldTypeDate"},
]

FEATURES = [
    {"attributes": {"OBJECTID": 1, "GLOBALID": "{A}", "material": "Steel Pipe",
                    "nominaldiameter": 4, "installationdate": 1640995200000,
                    "CREATIONDATE": 1640995200000, "LASTUPDATE": None},
     "geometry": {"paths": [[[-71.0, 42.0], [-71.01, 42.01]]]}},
    {"attributes": {"OBJECTID": 2, "GLOBALID": "{B}", "material": "Plastic PE",
                    "nominaldiameter": 2, "installationdate": 1672531200000,
                    "CREATIONDATE": 1672531200000, "LASTUPDATE": None},
     "geometry": {"paths": [[[-71.1, 42.1], [-71.11, 42.11]]],}},
    # Two paths -> MultiLineString.
    {"attributes": {"OBJECTID": 3, "GLOBALID": "{C}", "material": "Cast Iron",
                    "nominaldiameter": 6, "installationdate": 1672531200000,
                    "CREATIONDATE": 1672531200000, "LASTUPDATE": None},
     "geometry": {"paths": [[[-71.2, 42.2], [-71.21, 42.21]],
                            [[-71.3, 42.3], [-71.31, 42.31]]]}},
    # No geometry -> CSV only.
    {"attributes": {"OBJECTID": 4, "GLOBALID": "{D}", "material": "Copper",
                    "nominaldiameter": 1, "installationdate": 1672531200000,
                    "CREATIONDATE": 1672531200000, "LASTUPDATE": None},
     "geometry": None},
]


class StubLayer:
    """Answers the ArcGIS REST calls the exporter makes.

    reject_timestamp models a server that only accepts DATE literals;
    timestamp_matches_nothing models one that accepts TIMESTAMP but returns no
    rows for it - the case that used to end the run at zero features.
    """

    def __init__(self, fields=None, features=None, reject_timestamp=False,
                 timestamp_matches_nothing=False):
        self.fields = FIELDS if fields is None else fields
        self.features = FEATURES if features is None else features
        self.reject_timestamp = reject_timestamp
        self.timestamp_matches_nothing = timestamp_matches_nothing
        self.wheres = []
        self.requested_out_fields = []

    def request_json(self, session, url, params=None):
        params = params or {}
        if not url.endswith("/query"):
            return {"name": "Material View MA - Main Lines", "fields": self.fields}

        where = params.get("where")
        if where:
            self.wheres.append(where)
            uses_timestamp = "TIMESTAMP" in where
            if uses_timestamp and self.reject_timestamp:
                raise RuntimeError("Invalid SQL: TIMESTAMP not supported")
            empty = uses_timestamp and self.timestamp_matches_nothing
            if params.get("returnCountOnly") == "true":
                return {"count": 0 if empty else len(self.features)}
            if params.get("returnIdsOnly") == "true":
                ids = [] if empty else [f["attributes"]["OBJECTID"] for f in self.features]
                return {"objectIds": ids}

        if params.get("objectIds"):
            self.requested_out_fields.append(params.get("outFields"))
            wanted = {int(v) for v in str(params["objectIds"]).split(",")}
            selected = [f for f in self.features if f["attributes"]["OBJECTID"] in wanted]
            if params.get("returnGeometry") == "false":
                selected = [{"attributes": f["attributes"]} for f in selected]
            return {"features": selected}

        return {"count": 0}


@pytest.fixture
def layer(monkeypatch):
    stub = StubLayer()
    monkeypatch.setattr(exporter.auth, "request_json", stub.request_json)
    monkeypatch.setattr(exporter.auth, "make_session", lambda: object())
    return stub


def run(argv):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = exporter.main(argv)
    return code, buffer.getvalue()


class TestNoDuplicateModules:
    """GISportal carried byte-identical copies of auth.py and output.py, and
    both were broken: their sys.path setup was written for a module inside
    src/leakrelocation/."""

    def test_copies_are_gone(self):
        for name in ["auth.py", "output.py"]:
            assert not os.path.exists(os.path.join(REPO_ROOT, "GISportal", name)), name

    def test_uses_the_shared_package(self):
        from leakrelocation import auth as shared
        assert exporter.auth is shared

    def test_bootstrap_exposes_the_shared_modules(self):
        import _bootstrap

        from leakrelocation import auth, config, output
        assert (_bootstrap.auth, _bootstrap.config, _bootstrap.output) == (auth, config, output)


class TestDateFieldsComeFromMetadata:
    """A WHERE naming a field the layer lacks fails the whole query with a
    syntax error that says nothing about the cause."""

    def test_resolves_the_fields_present(self):
        install, creation = exporter.resolve_date_fields({"fields": FIELDS})
        assert install == "installationdate"
        assert creation == "CREATIONDATE"

    def test_falls_back_to_alternative_names(self):
        fields = [{"name": "inservicedate", "type": "esriFieldTypeDate"},
                  {"name": "created_date", "type": "esriFieldTypeDate"}]
        install, creation = exporter.resolve_date_fields({"fields": fields})
        assert install == "inservicedate"
        assert creation == "created_date"

    def test_one_missing_is_a_warning_not_a_failure(self, capsys):
        fields = [{"name": "CREATIONDATE", "type": "esriFieldTypeDate"}]
        install, creation = exporter.resolve_date_fields({"fields": fields})
        assert install is None
        assert creation == "CREATIONDATE"
        assert "No installation date field" in capsys.readouterr().out

    def test_neither_present_fails_naming_what_exists(self):
        fields = [{"name": "some_other_date", "type": "esriFieldTypeDate"}]
        with pytest.raises(RuntimeError, match="some_other_date"):
            exporter.resolve_date_fields({"fields": fields})

    def test_date_fields_read_from_metadata_types(self):
        assert exporter.date_field_names({"fields": FIELDS}) == {
            "installationdate", "CREATIONDATE", "LASTUPDATE"}


class TestWhereSelection:
    def test_only_distinct_syntaxes_are_tried(self):
        """SQL keywords are case-insensitive, so upper and lower spellings of
        the same keyword are the same clause. The original tried four variants
        that were really two."""
        variants = list(exporter.where_variants("installationdate", "CREATIONDATE", "2022-01-01"))
        assert len(variants) == 2
        assert len({v.upper() for v in variants}) == 2

    def test_prefers_a_clause_that_matches_rows(self, monkeypatch):
        """A clause the server accepts but which matches nothing used to be
        taken as success, ending the run at zero features."""
        stub = StubLayer(timestamp_matches_nothing=True)
        monkeypatch.setattr(exporter.auth, "request_json", stub.request_json)
        with redirect_stdout(io.StringIO()):
            where, count = exporter.choose_where(
                None, LAYER_URL, "installationdate", "CREATIONDATE", "2022-01-01")
        assert count == len(FEATURES)
        assert "DATE '2022-01-01'" in where

    def test_falls_back_when_a_syntax_is_rejected(self, monkeypatch):
        stub = StubLayer(reject_timestamp=True)
        monkeypatch.setattr(exporter.auth, "request_json", stub.request_json)
        with redirect_stdout(io.StringIO()):
            where, count = exporter.choose_where(
                None, LAYER_URL, "installationdate", "CREATIONDATE", "2022-01-01")
        assert count == len(FEATURES)
        assert "TIMESTAMP" not in where

    def test_genuinely_empty_layer_reports_zero_without_failing(self, monkeypatch):
        stub = StubLayer(features=[])
        monkeypatch.setattr(exporter.auth, "request_json", stub.request_json)
        with redirect_stdout(io.StringIO()):
            where, count = exporter.choose_where(
                None, LAYER_URL, "installationdate", "CREATIONDATE", "2022-01-01")
        assert count == 0
        assert where

    def test_single_field_clause_when_only_one_exists(self):
        variants = list(exporter.where_variants("installationdate", None, "2022-01-01"))
        assert all("CREATIONDATE" not in v for v in variants)
        assert all(v.count(">=") == 1 for v in variants)


class TestCuratedFields:
    def test_all_fields_by_default(self):
        assert exporter.resolve_out_fields({"fields": FIELDS}, curated=False) == "*"

    def test_curated_keeps_only_what_the_layer_has(self, capsys):
        out_fields = exporter.resolve_out_fields({"fields": FIELDS}, curated=True)
        requested = out_fields.split(",")
        assert "material" in requested
        assert "workorderid" not in requested
        assert "not on this layer" in capsys.readouterr().out

    def test_fails_when_no_curated_field_exists(self):
        with pytest.raises(RuntimeError, match="No curated field"):
            exporter.resolve_out_fields({"fields": [{"name": "zzz"}]}, curated=True)


class TestExport:
    def test_writes_csv_and_geojson(self, layer, tmp_path):
        code, _ = run(["--out-dir", str(tmp_path), "--basename", "mains"])
        assert code == 0
        assert (tmp_path / "mains.csv").exists()
        assert (tmp_path / "mains.geojson").exists()

    def test_csv_has_a_row_per_feature(self, layer, tmp_path):
        run(["--out-dir", str(tmp_path), "--basename", "mains"])
        with open(tmp_path / "mains.csv", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == len(FEATURES)
        assert {r["material"] for r in rows} == {"Steel Pipe", "Plastic PE", "Cast Iron", "Copper"}

    def test_dates_are_converted_from_epoch_milliseconds(self, layer, tmp_path):
        run(["--out-dir", str(tmp_path), "--basename", "mains"])
        with open(tmp_path / "mains.csv", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        assert rows[0]["installationdate"].startswith("2022-01-01")
        assert rows[0]["LASTUPDATE"] == ""

    def test_geojson_geometry_types(self, layer, tmp_path):
        run(["--out-dir", str(tmp_path), "--basename", "mains"])
        payload = json.loads((tmp_path / "mains.geojson").read_text())
        types = [f["geometry"]["type"] for f in payload["features"]]
        assert types.count("LineString") == 2
        assert types.count("MultiLineString") == 1

    def test_features_without_geometry_are_reported_not_dropped_silently(self, layer, tmp_path, capsys):
        run(["--out-dir", str(tmp_path), "--basename", "mains"])
        payload = json.loads((tmp_path / "mains.geojson").read_text())
        # Four features in, three with geometry.
        assert len(payload["features"]) == 3
        with open(tmp_path / "mains.csv", encoding="utf-8-sig") as handle:
            assert len(list(csv.DictReader(handle))) == 4

    def test_no_geojson_writes_only_csv(self, layer, tmp_path):
        run(["--out-dir", str(tmp_path), "--basename", "mains", "--no-geojson"])
        assert (tmp_path / "mains.csv").exists()
        assert not (tmp_path / "mains.geojson").exists()

    def test_count_only_writes_nothing(self, layer, tmp_path):
        code, output = run(["--out-dir", str(tmp_path), "--count-only"])
        assert code == 0
        assert f"{len(FEATURES):,} main lines" in output
        assert list(tmp_path.iterdir()) == []

    def test_curated_fields_are_requested(self, layer, tmp_path):
        run(["--out-dir", str(tmp_path), "--basename", "mains", "--curated-fields"])
        assert layer.requested_out_fields
        assert "*" not in layer.requested_out_fields

    def test_chunking_covers_every_id(self, layer, tmp_path):
        run(["--out-dir", str(tmp_path), "--basename", "mains", "--chunk-size", "1"])
        with open(tmp_path / "mains.csv", encoding="utf-8-sig") as handle:
            assert len(list(csv.DictReader(handle))) == len(FEATURES)

    def test_empty_layer_exports_nothing_and_succeeds(self, monkeypatch, tmp_path):
        stub = StubLayer(features=[])
        monkeypatch.setattr(exporter.auth, "request_json", stub.request_json)
        monkeypatch.setattr(exporter.auth, "make_session", lambda: object())
        code, output = run(["--out-dir", str(tmp_path)])
        assert code == 0
        assert "Nothing to export" in output

    def test_bad_since_date_is_rejected(self, layer, tmp_path):
        with pytest.raises(RuntimeError, match="YYYY-MM-DD"):
            run(["--since", "01/01/2022", "--out-dir", str(tmp_path)])


class TestGeometryConversion:
    def test_single_path_is_a_linestring(self):
        assert exporter.paths_to_geojson_geometry(
            {"paths": [[[0, 0], [1, 1]]]})["type"] == "LineString"

    def test_multiple_paths_is_a_multilinestring(self):
        assert exporter.paths_to_geojson_geometry(
            {"paths": [[[0, 0], [1, 1]], [[2, 2], [3, 3]]]})["type"] == "MultiLineString"

    @pytest.mark.parametrize("geometry", [None, {}, {"paths": []}])
    def test_no_usable_geometry(self, geometry):
        assert exporter.paths_to_geojson_geometry(geometry) is None


class TestEpochConversion:
    def test_converts_milliseconds(self):
        assert exporter.epoch_ms_to_iso(1640995200000).startswith("2022-01-01")

    @pytest.mark.parametrize("value", [None, ""])
    def test_blank_stays_blank(self, value):
        assert exporter.epoch_ms_to_iso(value) == ""

    def test_unconvertible_value_is_passed_through(self):
        assert exporter.epoch_ms_to_iso("not a date") == "not a date"
