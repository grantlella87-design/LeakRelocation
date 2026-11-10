"""Moving the leaks on the source layer, driven against a stub service.

This is the only thing the project does that changes a production asset system,
so what is tested here is mostly what stops it: the dry run, the confirmation,
the rollback written before the first edit, and every way a run can be refused.

A test that only proved the happy path would be the wrong test. The failures
below are the ones that matter - a feature that has moved since the download, a
batch the service half rejects, a rollback file that cannot be written.
"""
import io
import json
import os
import sys
import threading
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

pytest.importorskip("geopandas")
pytest.importorskip("keyring")

from leakrelocation import apply_geometry as ag
from leakrelocation import arcgis, auth

WKID = 2249


@pytest.fixture
def service():
    """A layer that answers metadata, objectIds queries and applyEdits."""
    state = {
        # OBJECTID -> (x, y) as the service currently holds it.
        "geometry": {1: (760_000.0, 2_960_000.0),
                     2: (760_100.0, 2_960_000.0),
                     3: (760_200.0, 2_960_000.0)},
        # A GlobalID per feature. An update must not change these.
        "globalid": {1: "{AAAAAAAA-0000-0000-0000-000000000001}",
                     2: "{AAAAAAAA-0000-0000-0000-000000000002}",
                     3: "{AAAAAAAA-0000-0000-0000-000000000003}"},
        "applied": [],       # every updates list the service was sent
        "sent_params": [],   # the whole form of every applyEdits call
        "reject": set(),     # OBJECTIDs applyEdits refuses
        "refuse_batch": False,
        "renumber": False,   # mint a new GlobalID on update, as a delete+add would
        "oid_field": "OBJECTID",
        "has_globalid": True,
        "queries": 0,
        "out_fields": [],    # what each query asked for
    }

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def reply(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def read_params(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode() if length else ""
            params = parse_qs(urlparse(self.path).query)
            params.update(parse_qs(raw))
            return {key: value[0] for key, value in params.items()}

        def do_GET(self):
            path = urlparse(self.path).path.rstrip("/")
            params = self.read_params()
            if path.endswith("/applyEdits"):
                self.apply_edits(params)
            elif path.endswith("/query"):
                self.query(params)
            else:
                fields = [{"name": state["oid_field"],
                           "type": "esriFieldTypeOID"}]
                if state["has_globalid"]:
                    fields.append({"name": "GlobalID",
                                   "type": "esriFieldTypeGlobalID"})
                self.reply({
                    "objectIdField": state["oid_field"],
                    "fields": fields,
                    "maxRecordCount": 1000,
                    "extent": {"spatialReference": {"wkid": WKID}},
                })

        do_POST = do_GET

        def query(self, params):
            state["queries"] += 1
            state["out_fields"].append(params.get("outFields", ""))
            wanted = [int(value) for value in
                      (params.get("objectIds") or "").split(",") if value.strip()]
            features = []
            for oid in wanted:
                if oid not in state["geometry"]:
                    continue
                x, y = state["geometry"][oid]
                attributes = {state["oid_field"]: oid}
                if state["has_globalid"]:
                    attributes["GlobalID"] = state["globalid"].get(oid)
                features.append({
                    "attributes": attributes,
                    "geometry": {"x": x, "y": y},
                })
            self.reply({"features": features})

        def apply_edits(self, params):
            if state["refuse_batch"]:
                self.reply({"error": {"code": 400, "message": "Invalid token"}})
                return
            state["sent_params"].append(dict(params))
            updates = json.loads(params.get("updates") or "[]")
            state["applied"].append(updates)
            results = []
            for update in updates:
                oid = update["attributes"][state["oid_field"]]
                if oid in state["reject"]:
                    results.append({"objectId": oid, "success": False,
                                    "error": {"code": 1000,
                                              "description": "row is locked"}})
                    continue
                geometry = update["geometry"]
                state["geometry"][oid] = (geometry["x"], geometry["y"])
                if state["renumber"]:
                    # What a delete-and-re-add looks like from outside: same
                    # object id, brand new GlobalID.
                    state["globalid"][oid] = (
                        f"{{BBBBBBBB-0000-0000-0000-{oid:012d}}}")
                results.append({"objectId": oid, "success": True})
            self.reply({"updateResults": results})

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}/MapServer/206"
    yield state
    server.shutdown()


@pytest.fixture
def session():
    import requests
    made = requests.Session()
    made._arcgis_access_token = "test-token"
    return made


def relocated_frame(points, crs=f"EPSG:{WKID}"):
    """A relocated_leaks-shaped frame: OrigLeakOID plus the new point."""
    import geopandas as gpd
    from shapely.geometry import Point
    return gpd.GeoDataFrame(
        [{"OrigLeakOID": oid} for oid, _, _ in points],
        geometry=[Point(x, y) for _, x, y in points], crs=crs)


class TestWhatItProposes:
    def test_a_moved_leak_is_ready_to_apply(self):
        moves = [{"object_id": 1, "x": 760_020.0, "y": 2_960_000.0}]
        current = {1: (760_000.0, 2_960_000.0)}
        ready, held = ag.classify(moves, current, 100.0, False)
        assert len(ready) == 1
        assert ready[0]["moves_by"] == pytest.approx(20.0)
        assert held == []

    def test_a_leak_already_at_the_new_position_is_not_re_sent(self):
        """A second run of the same output should do nothing, not rewrite every
        feature with the coordinates it already has."""
        moves = [{"object_id": 1, "x": 760_000.0, "y": 2_960_000.0}]
        ready, held = ag.classify(moves, {1: (760_000.0, 2_960_000.0)},
                                  100.0, False)
        assert ready == []
        assert held[0]["why"] == "already at the relocated position"

    def test_a_feature_that_is_gone_is_reported_not_skipped_silently(self):
        ready, held = ag.classify(
            [{"object_id": 9, "x": 1.0, "y": 2.0}], {}, 100.0, False)
        assert ready == []
        assert held[0]["why"] == "not on the service any more"

    def test_a_long_move_is_held_back_by_default(self):
        """The search widens to 3,000 ft. A leak that only matched at 2,900 ft
        is a record to check, not an edit to push."""
        moves = [{"object_id": 1, "x": 762_900.0, "y": 2_960_000.0}]
        ready, held = ag.classify(moves, {1: (760_000.0, 2_960_000.0)},
                                  100.0, False)
        assert ready == []
        assert "beyond the" in held[0]["why"]
        assert "2,900" in held[0]["why"]

    def test_a_long_move_applies_when_asked_for_explicitly(self):
        moves = [{"object_id": 1, "x": 762_900.0, "y": 2_960_000.0}]
        ready, held = ag.classify(moves, {1: (760_000.0, 2_960_000.0)},
                                  100.0, True)
        assert len(ready) == 1 and held == []

    def test_the_default_limit_is_the_first_search_pass(self):
        assert ag.MAX_APPLY_DISTANCE_FT == 100.0

    def test_every_held_entry_says_why(self):
        moves = [{"object_id": 1, "x": 760_000.0, "y": 2_960_000.0},
                 {"object_id": 2, "x": 999_999.0, "y": 2_960_000.0},
                 {"object_id": 9, "x": 1.0, "y": 2.0}]
        _, held = ag.classify(moves, {1: (760_000.0, 2_960_000.0),
                                      2: (760_100.0, 2_960_000.0)}, 100.0, False)
        assert len(held) == 3
        assert all(entry["why"] for entry in held)


class TestReadingTheOutput:
    def test_object_ids_and_points_come_from_the_relocated_layer(self):
        frame = relocated_frame([(1, 760_020.0, 2_960_000.0),
                                 (2, 760_120.0, 2_960_000.0)])
        moves, skipped = ag.relocations_from(frame)
        assert skipped == 0
        assert [m["object_id"] for m in moves] == [1, 2]
        assert moves[0]["x"] == pytest.approx(760_020.0)

    def test_a_row_without_an_object_id_is_counted(self):
        frame = relocated_frame([(1, 760_020.0, 2_960_000.0),
                                 (None, 760_120.0, 2_960_000.0)])
        moves, skipped = ag.relocations_from(frame)
        assert len(moves) == 1 and skipped == 1

    def test_a_layer_without_the_id_column_fails_by_name(self):
        frame = relocated_frame([(1, 1.0, 2.0)]).drop(columns=["OrigLeakOID"])
        with pytest.raises(RuntimeError, match="OrigLeakOID"):
            ag.relocations_from(frame)


class TestTheCoordinateSystem:
    """applyEdits takes geometry at face value. Sending analysis-CRS feet to a
    layer that stores degrees writes every leak into the sea, and the service
    accepts it."""

    def test_a_matching_crs_is_left_alone(self):
        frame = relocated_frame([(1, 760_020.0, 2_960_000.0)])
        assert ag.to_service_crs(frame, WKID) is frame

    def test_a_different_crs_is_reprojected(self):
        frame = relocated_frame([(1, -71.0589, 42.3601)], crs="EPSG:4326")
        with redirect_stdout(io.StringIO()):
            converted = ag.to_service_crs(frame, WKID)
        assert converted.crs.to_epsg() == WKID
        assert converted.geometry.iloc[0].x > 100_000

    def test_a_frame_with_no_crs_is_refused_rather_than_guessed(self):
        import geopandas as gpd
        from shapely.geometry import Point
        frame = gpd.GeoDataFrame([{"OrigLeakOID": 1}], geometry=[Point(1, 2)])
        with pytest.raises(RuntimeError, match="Refusing to guess"):
            ag.to_service_crs(frame, WKID)

    def test_a_layer_with_no_spatial_reference_is_refused(self):
        frame = relocated_frame([(1, 1.0, 2.0)])
        with pytest.raises(RuntimeError, match="Refusing to guess"):
            ag.to_service_crs(frame, None)


class TestTheRollbackFile:
    def test_it_holds_the_position_read_from_the_service(self, tmp_path):
        """Not the one the relocation started from. A leak someone moved by
        hand since the download must be restored to where it is now."""
        ready = [{"object_id": 1, "x": 760_020.0, "y": 2_960_000.0,
                  "from_x": 760_005.0, "from_y": 2_960_000.0, "moves_by": 15.0}]
        path = ag.write_rollback(str(tmp_path / "rb.json"), "http://x/206",
                                 WKID, ready)
        payload = json.load(open(path))
        update = payload["updates"][0]
        assert update["attributes"]["OBJECTID"] == 1
        assert update["geometry"]["x"] == pytest.approx(760_005.0)
        assert update["geometry"]["spatialReference"]["wkid"] == WKID

    def test_it_is_an_applyedits_payload_not_a_report(self):
        """Undoing should be running the file, not reconstructing it under
        pressure."""
        ready = [{"object_id": 1, "x": 1.0, "y": 2.0, "from_x": 3.0,
                  "from_y": 4.0, "moves_by": 5.0}]
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = ag.write_rollback(os.path.join(folder, "rb.json"),
                                     "http://x/206", WKID, ready)
            payload = json.load(open(path))
        assert "updates" in payload
        assert set(payload["updates"][0]) == {"attributes", "geometry"}
        assert "applyEdits" in payload["how_to_use"]

    def test_it_records_the_layer_and_when(self, tmp_path):
        path = ag.write_rollback(str(tmp_path / "rb.json"), "http://x/206",
                                 WKID, [], extra={"source_gpkg": "out.gpkg"})
        payload = json.load(open(path))
        assert payload["layer_url"] == "http://x/206"
        assert payload["written_utc"].endswith("Z")
        assert payload["source_gpkg"] == "out.gpkg"

    def test_each_run_gets_its_own_file(self):
        first = ag.rollback_path("/tmp", stamp="20260101T000000Z")
        second = ag.rollback_path("/tmp", stamp="20260101T000001Z")
        assert first != second
        assert first.endswith(".json")

    def test_reading_back_a_good_file_works(self, tmp_path):
        ready = [{"object_id": 1, "x": 1.0, "y": 2.0, "from_x": 3.0,
                  "from_y": 4.0, "moves_by": 5.0}]
        path = ag.write_rollback(str(tmp_path / "rb.json"), "http://x/206",
                                 WKID, ready)
        assert len(ag.read_rollback(path)["updates"]) == 1

    def test_a_missing_file_is_refused(self, tmp_path):
        with pytest.raises(RuntimeError, match="No rollback file"):
            ag.read_rollback(str(tmp_path / "nothing.json"))

    def test_an_empty_file_is_refused(self, tmp_path):
        path = tmp_path / "rb.json"
        path.write_text(json.dumps({"updates": []}))
        with pytest.raises(RuntimeError, match="no updates"):
            ag.read_rollback(str(path))

    def test_a_malformed_entry_is_refused(self, tmp_path):
        path = tmp_path / "rb.json"
        path.write_text(json.dumps({"updates": [{"attributes": {}}]}))
        with pytest.raises(RuntimeError, match="not a usable applyEdits"):
            ag.read_rollback(str(path))


class TestTheConfirmation:
    def test_the_exact_phrase_passes(self):
        with redirect_stdout(io.StringIO()):
            assert ag.confirmed(10, ag.CONFIRMATION) is True

    def test_surrounding_space_is_forgiven(self):
        with redirect_stdout(io.StringIO()):
            assert ag.confirmed(10, f"  {ag.CONFIRMATION}  ") is True

    @pytest.mark.parametrize("typed", ["", "yes", "y", "move the leaks",
                                       "MOVE THE LEAK"])
    def test_anything_else_does_not(self, typed):
        with redirect_stdout(io.StringIO()):
            assert ag.confirmed(10, typed) is False

    def test_it_says_how_many_and_that_it_cannot_be_undone(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            ag.confirmed(1234, "no")
        printed = buffer.getvalue()
        assert "1,234" in printed
        assert "cannot be undone" in printed


class TestAgainstTheService:
    def test_the_current_geometry_is_re_read(self, session, service):
        with redirect_stdout(io.StringIO()):
            found = ag.current_state(
                session, service["url"], [1, 2, 3], WKID)
        assert {oid: entry["xy"] for oid, entry in found.items()} == (
            service["geometry"])
        assert service["queries"] >= 1

    def test_the_updates_are_applied_and_counted(self, session,
                                                 service):
        updates = [{"attributes": {"OBJECTID": 1},
                    "geometry": ag.esri_point(760_020.0, 2_960_000.0, WKID)}]
        with redirect_stdout(io.StringIO()):
            succeeded, failed = ag.apply_updates(
                session, service["url"], updates, WKID)
        assert succeeded == [1] and failed == []
        assert service["geometry"][1] == (760_020.0, 2_960_000.0)

    def test_a_feature_the_service_refuses_is_reported_not_counted_as_done(
            self, session, service):
        """applyEdits answers per feature. A batch can come back HTTP 200 with
        half of it rejected, and reporting that as success is the worst kind of
        wrong: the run says it moved leaks it did not."""
        service["reject"] = {2}
        updates = [
            {"attributes": {"OBJECTID": oid},
             "geometry": ag.esri_point(760_500.0, 2_960_000.0, WKID)}
            for oid in (1, 2)
        ]
        with redirect_stdout(io.StringIO()):
            succeeded, failed = ag.apply_updates(
                session, service["url"], updates, WKID)
        assert succeeded == [1]
        assert len(failed) == 1
        assert failed[0]["object_id"] == 2
        assert "locked" in failed[0]["why"]

    def test_a_refused_batch_stops_the_run_and_says_what_was_applied(
            self, session, service):
        service["refuse_batch"] = True
        updates = [{"attributes": {"OBJECTID": 1},
                    "geometry": ag.esri_point(1.0, 2.0, WKID)}]
        with redirect_stdout(io.StringIO()), pytest.raises(RuntimeError) as raised:
            ag.apply_updates(session, service["url"], updates, WKID)
        message = str(raised.value)
        assert "refused" in message
        # The part that only matters mid-run: earlier batches are already
        # applied, and the rollback file is how they come back.
        assert "earlier batches were" in message
        assert "rollback file" in message

    def test_it_sends_in_batches(self, session, service):
        service["geometry"] = {oid: (760_000.0 + oid, 2_960_000.0)
                               for oid in range(1, 12)}
        updates = [
            {"attributes": {"OBJECTID": oid},
             "geometry": ag.esri_point(770_000.0, 2_960_000.0, WKID)}
            for oid in range(1, 12)
        ]
        with redirect_stdout(io.StringIO()):
            succeeded, _ = ag.apply_updates(
                session, service["url"], updates, WKID, batch_size=4)
        assert len(succeeded) == 11
        assert len(service["applied"]) == 3

    def test_each_batch_is_one_transaction(self, session, service, monkeypatch):
        """A half-applied batch is the worst outcome: some leaks moved, some
        not, and no clean way to say which."""
        updates = [{"attributes": {"OBJECTID": 1},
                    "geometry": ag.esri_point(1.0, 2.0, WKID)}]
        sent = {}
        original = arcgis.request_json_post

        def capture(session_, url, params):
            sent.update(params)
            return original(session_, url, params)
        monkeypatch.setattr(arcgis, "request_json_post", capture)
        with redirect_stdout(io.StringIO()):
            ag.apply_updates(session, service["url"], updates, WKID)
        assert sent["rollbackOnFailure"] == "true"


class TestTheScriptEndToEnd:
    """The dry run, the refusals and a real apply, through main()."""

    @pytest.fixture
    def script(self, session, monkeypatch):
        """The script, with its session already carrying a token.

        Without this, main() builds a real session, request_json_post finds no
        token and starts an interactive sign-in, and the test waits on a
        loopback OAuth redirect that is never coming.
        """
        sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
        import apply_relocated_geometry
        monkeypatch.setattr(auth, "make_session", lambda *a, **k: session)
        return apply_relocated_geometry

    @pytest.fixture
    def gpkg(self, tmp_path):
        """A relocation output whose leaks have moved 20 ft each."""
        frame = relocated_frame([(1, 760_020.0, 2_960_000.0),
                                 (2, 760_120.0, 2_960_000.0)])
        path = tmp_path / "HistoricLeakRelocation.gpkg"
        frame.to_file(str(path), layer="relocated_leaks", driver="GPKG")
        return str(path)

    def run(self, script, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = script.main(argv)
        return code, buffer.getvalue()

    def test_the_default_is_a_dry_run_that_changes_nothing(
            self, script, gpkg, service):
        before = dict(service["geometry"])
        code, printed = self.run(script, [
            "--gpkg", gpkg, "--layer-url", service["url"]])
        assert code == 0
        assert "Dry run" in printed
        assert "Features to move : 2" in printed
        assert service["geometry"] == before
        assert service["applied"] == []

    def test_applying_moves_them_and_writes_a_rollback_first(
            self, script, gpkg, service, tmp_path):
        code, printed = self.run(script, [
            "--gpkg", gpkg, "--layer-url", service["url"], "--apply",
            "--yes", ag.CONFIRMATION, "--rollback-dir", str(tmp_path)])
        assert code == 0
        assert service["geometry"][1] == (760_020.0, 2_960_000.0)
        assert service["geometry"][2] == (760_120.0, 2_960_000.0)
        rollbacks = list(tmp_path.glob("leak_geometry_rollback_*.json"))
        assert len(rollbacks) == 1
        payload = json.load(open(rollbacks[0]))
        # The file restores the ORIGINAL positions.
        restored = {u["attributes"]["OBJECTID"]: u["geometry"]["x"]
                    for u in payload["updates"]}
        assert restored[1] == pytest.approx(760_000.0)

    def test_the_rollback_file_actually_puts_them_back(
            self, script, gpkg, service, tmp_path):
        before = dict(service["geometry"])
        self.run(script, ["--gpkg", gpkg, "--layer-url", service["url"],
                          "--apply", "--yes", ag.CONFIRMATION,
                          "--rollback-dir", str(tmp_path)])
        assert service["geometry"] != before
        rollback = str(next(tmp_path.glob("leak_geometry_rollback_*.json")))
        code, _ = self.run(script, ["--rollback", rollback, "--apply",
                                    "--yes", ag.CONFIRMATION])
        assert code == 0
        for oid, position in before.items():
            if oid in (1, 2):
                assert service["geometry"][oid] == pytest.approx(position)

    def test_a_rollback_is_a_dry_run_too_without_apply(
            self, script, gpkg, service, tmp_path):
        self.run(script, ["--gpkg", gpkg, "--layer-url", service["url"],
                          "--apply", "--yes", ag.CONFIRMATION,
                          "--rollback-dir", str(tmp_path)])
        moved = dict(service["geometry"])
        rollback = str(next(tmp_path.glob("leak_geometry_rollback_*.json")))
        code, printed = self.run(script, ["--rollback", rollback])
        assert code == 0
        assert "Dry run" in printed
        assert service["geometry"] == moved

    def test_without_the_phrase_nothing_is_sent(self, script, gpkg, service,
                                                tmp_path):
        before = dict(service["geometry"])
        with pytest.raises(RuntimeError, match="Not confirmed"):
            self.run(script, ["--gpkg", gpkg, "--layer-url", service["url"],
                              "--apply", "--yes", "yes please",
                              "--rollback-dir", str(tmp_path)])
        assert service["geometry"] == before
        assert service["applied"] == []

    def test_the_rollback_is_written_even_when_the_phrase_is_refused(
            self, script, gpkg, service, tmp_path):
        """It is written before the confirmation, so a run that is stopped at
        the last moment still leaves the file. Harmless, and it means the file
        can never be missing when it is needed."""
        with pytest.raises(RuntimeError, match="Not confirmed"):
            self.run(script, ["--gpkg", gpkg, "--layer-url", service["url"],
                              "--apply", "--yes", "no",
                              "--rollback-dir", str(tmp_path)])
        assert list(tmp_path.glob("leak_geometry_rollback_*.json"))

    def test_long_moves_are_held_back_and_named(self, script, service, tmp_path):
        frame = relocated_frame([(1, 762_900.0, 2_960_000.0)])
        path = tmp_path / "Far.gpkg"
        frame.to_file(str(path), layer="relocated_leaks", driver="GPKG")
        code, printed = self.run(script, [
            "--gpkg", str(path), "--layer-url", service["url"]])
        assert code == 0
        assert "Nothing to move" in printed
        assert "beyond the" in printed

    def test_too_many_features_stops_the_run(self, script, gpkg, service):
        with pytest.raises(RuntimeError, match="over the"):
            self.run(script, ["--gpkg", gpkg, "--layer-url", service["url"],
                              "--apply", "--yes", ag.CONFIRMATION,
                              "--max-features", "1"])

    def test_a_second_run_of_the_same_output_moves_nothing(
            self, script, gpkg, service, tmp_path):
        """Re-running must be a no-op, not a rewrite of every feature."""
        self.run(script, ["--gpkg", gpkg, "--layer-url", service["url"],
                          "--apply", "--yes", ag.CONFIRMATION,
                          "--rollback-dir", str(tmp_path)])
        sent_before = len(service["applied"])
        code, printed = self.run(script, [
            "--gpkg", gpkg, "--layer-url", service["url"], "--apply",
            "--yes", ag.CONFIRMATION, "--rollback-dir", str(tmp_path)])
        assert code == 0
        assert "Nothing to move" in printed
        assert len(service["applied"]) == sent_before

    def test_an_output_that_no_longer_describes_the_layer_is_refused(
            self, script, service, tmp_path):
        """Most of the features are gone, so these edits were computed against
        something else."""
        frame = relocated_frame([(oid, 760_000.0 + oid, 2_960_000.0)
                                 for oid in range(100, 200)])
        path = tmp_path / "Stale.gpkg"
        frame.to_file(str(path), layer="relocated_leaks", driver="GPKG")
        with pytest.raises(RuntimeError, match="no longer describes this layer"):
            self.run(script, ["--gpkg", str(path), "--layer-url", service["url"]])

    def test_a_missing_geopackage_says_what_to_run(self, script, tmp_path,
                                                   service):
        with pytest.raises(RuntimeError, match="python run.py"):
            self.run(script, ["--gpkg", str(tmp_path / "nothing.gpkg"),
                              "--layer-url", service["url"]])

    def test_the_dry_run_names_the_command_that_would_apply(
            self, script, gpkg, service):
        _, printed = self.run(script, [
            "--gpkg", gpkg, "--layer-url", service["url"]])
        assert "--apply" in printed

    def test_applying_reports_the_rollback_path_at_the_end(
            self, script, gpkg, service, tmp_path):
        """It is the last thing on screen, which is where someone looks when
        they realise something is wrong."""
        _, printed = self.run(script, [
            "--gpkg", gpkg, "--layer-url", service["url"], "--apply",
            "--yes", ag.CONFIRMATION, "--rollback-dir", str(tmp_path)])
        assert printed.rstrip().endswith(".json")
        assert "--rollback" in printed


class TestNothingSecretIsPrinted:
    def test_the_token_never_appears(self, session, service, tmp_path,
                                     monkeypatch):
        """The session carries a token. Nothing here should ever echo it."""
        session._arcgis_access_token = "SECRET-TOKEN-VALUE"
        updates = [{"attributes": {"OBJECTID": 1},
                    "geometry": ag.esri_point(1.0, 2.0, WKID)}]
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            ag.apply_updates(session, service["url"], updates, WKID)
        assert "SECRET-TOKEN-VALUE" not in buffer.getvalue()

    def test_the_rollback_file_carries_no_credential(self, tmp_path):
        """It holds object ids and coordinates. A token in a file that is kept
        for weeks as an undo is a token that outlives its session."""
        ready = [{"object_id": 1, "x": 1.0, "y": 2.0, "from_x": 3.0,
                  "from_y": 4.0, "moves_by": 5.0}]
        path = ag.write_rollback(str(tmp_path / "rb.json"),
                                 "http://x/206?token=SECRET-TOKEN-VALUE",
                                 WKID, ready)
        text = open(path).read()
        assert "SECRET-TOKEN-VALUE" not in text
        for marker in ('"token"', "access_token", "Bearer "):
            assert marker not in text, marker


class TestItUpdatesGeometryAndNothingElse:
    """The GlobalID is the thing every downstream reference to a leak hangs
    off. It is minted when a feature is created, so a workflow that removed and
    re-inserted a leak - rather than updating it - would renumber it and break
    every one of those references, silently.

    An `updates` call cannot do that: the row is the same row. These hold the
    things that would make it one anyway.
    """

    def test_the_payload_carries_only_the_object_id(self):
        update = ag.geometry_only("OBJECTID", 7, 1.0, 2.0, WKID)
        assert list(update["attributes"]) == ["OBJECTID"]
        assert update["attributes"]["OBJECTID"] == 7
        assert set(update) == {"attributes", "geometry"}

    def test_the_object_id_field_is_whatever_the_layer_calls_it(self):
        update = ag.geometry_only("FID", 7, 1.0, 2.0, WKID)
        assert list(update["attributes"]) == ["FID"]

    def test_an_extra_attribute_is_refused(self):
        """Including a GlobalID, which some services accept - and which is
        exactly what must not move."""
        for extra in ("GlobalID", "LEAKSTATUS", "ADDRESS"):
            bad = [{"attributes": {"OBJECTID": 1, extra: "x"},
                    "geometry": ag.esri_point(1.0, 2.0, WKID)}]
            with pytest.raises(RuntimeError, match=extra):
                ag.check_geometry_only(bad, "OBJECTID")

    def test_an_attribute_only_edit_is_refused(self):
        with pytest.raises(RuntimeError, match="no geometry"):
            ag.check_geometry_only([{"attributes": {"OBJECTID": 1}}], "OBJECTID")

    def test_a_clean_payload_passes_through_unchanged(self):
        updates = [ag.geometry_only("OBJECTID", 1, 1.0, 2.0, WKID)]
        assert ag.check_geometry_only(updates, "OBJECTID") is updates

    def test_the_globalid_field_is_read_off_the_layer(self):
        meta = {"fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"},
                           {"name": "GLOBALID", "type": "esriFieldTypeGlobalID"}]}
        assert ag.globalid_field_of(meta) == "GLOBALID"

    def test_a_layer_without_one_reports_none(self):
        meta = {"fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"}]}
        assert ag.globalid_field_of(meta) is None

    @pytest.mark.parametrize("left,right", [
        ("{ABC-123}", "abc-123"),
        ("ABC-123", "{ABC-123}"),
        (" {abc-123} ", "ABC-123"),
    ])
    def test_the_same_id_written_two_ways_is_the_same_id(self, left, right):
        """Services are inconsistent about the braces and the case. Comparing
        them raw would report every feature as renumbered."""
        assert ag.normalise_globalid(left) == ag.normalise_globalid(right)

    def test_two_different_ids_stay_different(self):
        assert ag.normalise_globalid("{ABC}") != ag.normalise_globalid("{DEF}")


class TestTheGlobalIdsAreCheckedAfterwards:
    def test_they_are_read_before_the_edit(self, session, service):
        with redirect_stdout(io.StringIO()):
            state = ag.current_state(
                session, service["url"], [1, 2], WKID,
                globalid_field="GlobalID")
        assert state[1]["globalid"] == "AAAAAAAA-0000-0000-0000-000000000001"
        assert state[1]["xy"] == (760_000.0, 2_960_000.0)
        assert any("GlobalID" in asked for asked in service["out_fields"])

    def test_an_ordinary_update_leaves_them_alone(self, session,
                                                  service):
        before = dict(service["globalid"])
        updates = [ag.geometry_only("OBJECTID", 1, 760_020.0, 2_960_000.0, WKID)]
        with redirect_stdout(io.StringIO()):
            ag.apply_updates(session, service["url"], updates, WKID)
        assert service["globalid"] == before
        assert service["geometry"][1] == (760_020.0, 2_960_000.0)

    def test_verification_passes_when_nothing_was_renumbered(
            self, session, service):
        ready = [{"object_id": 1, "x": 760_020.0, "y": 2_960_000.0,
                  "globalid": ag.normalise_globalid(service["globalid"][1])}]
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            changed = ag.verify_globalids(
                session, service["url"], ready, WKID,
                globalid_field="GlobalID")
        assert changed == []
        assert "all 1 unchanged" in buffer.getvalue()

    def test_a_renumbered_feature_is_caught_and_named(self, session,
                                                      service):
        """What a delete-and-re-add looks like from outside: same object id,
        brand new GlobalID. If a service ever did that, every reference to
        those leaks would point at nothing - so it is checked, not assumed."""
        before = ag.normalise_globalid(service["globalid"][1])
        service["renumber"] = True
        updates = [ag.geometry_only("OBJECTID", 1, 760_020.0, 2_960_000.0, WKID)]
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            ag.apply_updates(session, service["url"], updates, WKID)
            changed = ag.verify_globalids(
                session, service["url"],
                [{"object_id": 1, "globalid": before}], WKID,
                globalid_field="GlobalID")
        assert len(changed) == 1
        assert changed[0]["before"] == before
        assert changed[0]["after"] != before
        printed = buffer.getvalue()
        assert "do not have the GlobalID they had" in printed
        assert "points at nothing" in printed

    def test_a_feature_that_vanished_is_reported_as_changed(
            self, session, service):
        with redirect_stdout(io.StringIO()):
            changed = ag.verify_globalids(
                session, service["url"],
                [{"object_id": 99, "globalid": "SOMETHING"}], WKID,
                globalid_field="GlobalID")
        assert len(changed) == 1
        assert "could not be read back" in changed[0]["after"]

    def test_a_layer_without_globalids_says_so_rather_than_claiming_a_check(
            self, session, service):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            changed = ag.verify_globalids(
                session, service["url"],
                [{"object_id": 1, "globalid": None}], WKID,
                globalid_field=None)
        assert changed == []
        assert "nothing to check against" in buffer.getvalue()
        assert "still geometry-only" in buffer.getvalue()


class TestTheWholeRunKeepsThem:
    """Through main(), against the stub."""

    @pytest.fixture
    def script(self, session, monkeypatch):
        sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
        import apply_relocated_geometry
        monkeypatch.setattr(auth, "make_session", lambda *a, **k: session)
        return apply_relocated_geometry

    @pytest.fixture
    def gpkg(self, tmp_path):
        frame = relocated_frame([(1, 760_020.0, 2_960_000.0),
                                 (2, 760_120.0, 2_960_000.0)])
        path = tmp_path / "HistoricLeakRelocation.gpkg"
        frame.to_file(str(path), layer="relocated_leaks", driver="GPKG")
        return str(path)

    def run(self, script, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = script.main(argv)
        return code, buffer.getvalue()

    def test_a_full_apply_keeps_every_globalid(self, script, gpkg, service,
                                               tmp_path):
        before = dict(service["globalid"])
        code, printed = self.run(script, [
            "--gpkg", gpkg, "--layer-url", service["url"], "--apply",
            "--yes", ag.CONFIRMATION, "--rollback-dir", str(tmp_path)])
        assert code == 0
        assert service["globalid"] == before
        assert "all 2 unchanged" in printed

    def test_nothing_but_the_object_id_is_ever_sent(self, script, gpkg,
                                                    service, tmp_path):
        self.run(script, ["--gpkg", gpkg, "--layer-url", service["url"],
                          "--apply", "--yes", ag.CONFIRMATION,
                          "--rollback-dir", str(tmp_path)])
        assert service["applied"]
        for batch in service["applied"]:
            for update in batch:
                assert list(update["attributes"]) == ["OBJECTID"]
                assert "geometry" in update

    def test_no_add_or_delete_is_ever_sent(self, script, gpkg, service,
                                           tmp_path):
        """The one thing that would renumber a feature. applyEdits takes adds
        and deletes in the same call as updates, so their absence is worth
        asserting rather than assuming."""
        self.run(script, ["--gpkg", gpkg, "--layer-url", service["url"],
                          "--apply", "--yes", ag.CONFIRMATION,
                          "--rollback-dir", str(tmp_path)])
        assert service["sent_params"]
        for params in service["sent_params"]:
            assert "adds" not in params
            assert "deletes" not in params
            assert params.get("updates")

    def test_a_renumbering_service_fails_the_run(self, script, gpkg, service,
                                                 tmp_path):
        """Exit code and all: a run that moved the leaks but lost their ids is
        not a successful run."""
        service["renumber"] = True
        code, printed = self.run(script, [
            "--gpkg", gpkg, "--layer-url", service["url"], "--apply",
            "--yes", ag.CONFIRMATION, "--rollback-dir", str(tmp_path)])
        assert code == 1
        assert "do not have the GlobalID they had" in printed

    def test_the_run_says_up_front_that_it_is_geometry_only(
            self, script, gpkg, service):
        _, printed = self.run(script, [
            "--gpkg", gpkg, "--layer-url", service["url"]])
        assert "updates geometry only" in printed
        assert "keeps its GlobalID" in printed

    def test_the_layers_own_object_id_field_is_used(self, script, service,
                                                    tmp_path):
        """A layer is free to call it something else, and sending the wrong key
        identifies no row at all."""
        service["oid_field"] = "FID"
        frame = relocated_frame([(1, 760_020.0, 2_960_000.0)])
        path = tmp_path / "Other.gpkg"
        frame.to_file(str(path), layer="relocated_leaks", driver="GPKG")
        code, printed = self.run(script, [
            "--gpkg", str(path), "--layer-url", service["url"], "--apply",
            "--yes", ag.CONFIRMATION, "--rollback-dir", str(tmp_path)])
        assert code == 0
        assert "Object id field        : FID" in printed
        for batch in service["applied"]:
            for update in batch:
                assert list(update["attributes"]) == ["FID"]

    def test_the_rollback_is_geometry_only_too(self, script, gpkg, service,
                                               tmp_path):
        self.run(script, ["--gpkg", gpkg, "--layer-url", service["url"],
                          "--apply", "--yes", ag.CONFIRMATION,
                          "--rollback-dir", str(tmp_path)])
        rollback = next(tmp_path.glob("leak_geometry_rollback_*.json"))
        payload = json.load(open(rollback))
        for update in payload["updates"]:
            assert list(update["attributes"]) == ["OBJECTID"]
            assert "geometry" in update

    def test_the_rollback_records_the_globalids_without_sending_them(
            self, script, gpkg, service, tmp_path):
        """So the features an undo puts back can be confirmed to be the same
        features - but the payload still changes geometry and nothing else."""
        self.run(script, ["--gpkg", gpkg, "--layer-url", service["url"],
                          "--apply", "--yes", ag.CONFIRMATION,
                          "--rollback-dir", str(tmp_path)])
        payload = json.load(open(
            next(tmp_path.glob("leak_geometry_rollback_*.json"))))
        assert payload["globalids"]["1"] == \
            ag.normalise_globalid(service["globalid"][1])
        assert all("GlobalID" not in update["attributes"]
                   for update in payload["updates"])

    def test_the_undo_also_keeps_them(self, script, gpkg, service, tmp_path):
        before = dict(service["globalid"])
        self.run(script, ["--gpkg", gpkg, "--layer-url", service["url"],
                          "--apply", "--yes", ag.CONFIRMATION,
                          "--rollback-dir", str(tmp_path)])
        rollback = str(next(tmp_path.glob("leak_geometry_rollback_*.json")))
        self.run(script, ["--rollback", rollback, "--apply",
                          "--yes", ag.CONFIRMATION])
        assert service["globalid"] == before

    def test_a_layer_with_no_globalid_field_still_runs(self, script, gpkg,
                                                       service, tmp_path):
        service["has_globalid"] = False
        code, printed = self.run(script, [
            "--gpkg", gpkg, "--layer-url", service["url"], "--apply",
            "--yes", ag.CONFIRMATION, "--rollback-dir", str(tmp_path)])
        assert code == 0
        assert "none on this layer" in printed
        assert "nothing to check against" in printed
