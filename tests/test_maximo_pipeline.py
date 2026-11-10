"""Maximo leaks going all the way through the shared relocation path.

The claim scripts/maximo_leaks.py rests on is that once the query has run,
nothing downstream can tell a Maximo leak from a historic one. These check that
by driving the real functions - prepare_leaks, prepare_pipes, run_matching,
write_outputs - with leaks built from a fake SQL result and pipes built here.

If that claim ever stops holding, the alternative is a second copy of the
matching, which would drift from the first and make the two leak sets
incomparable. So it is worth a test that actually relocates something.
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

from leakrelocation import maximo_sql as mx

# MA state plane feet, so distances come out in feet with no reprojection and
# the numbers below are readable as "20 ft away".
CRS = "EPSG:2249"


@pytest.fixture(scope="module")
def workflow():
    path = os.path.join(REPO_ROOT, "src", "leakrelocation", "workflow.py")
    spec = importlib.util.spec_from_file_location("lr_maximo", path)
    module = importlib.util.module_from_spec(spec)
    with redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)
    return module


def sql_row(leak_id, x, y, material="Cast Iron", diameter=8.0,
            facility="Distribution Main", date=None):
    return {
        mx.LEAK_ID: leak_id, mx.LEAK_NUMBER: leak_id,
        mx.LEAK_X: x, mx.LEAK_Y: y,
        mx.LEAK_MATERIAL: material, mx.LEAK_DIAMETER: diameter,
        mx.LEAK_FACILITY: facility, mx.LEAK_PRESSURE: "",
        mx.LEAK_DATE: date, mx.LEAK_ADDRESS: "12 Elm St",
        mx.LEAK_CITY: "WATERTOWN", mx.LEAK_CROSS_STREET: "OAK ST",
    }


def maximo_leaks(rows):
    """What read_leaks would hand back for these SQL rows."""
    records, attributes, counters = mx.leak_records(rows)
    return mx.leaks_frame(records, crs=CRS), attributes, counters


def pipes(entries, layer="distribution"):
    """A decoded pipe frame, the shape decode_pipe_materials leaves behind."""
    import geopandas as gpd
    from shapely.geometry import LineString
    rows = []
    if not entries:
        return gpd.GeoDataFrame(
            {name: [] for name in ("OBJECTID", "GLOBALID", "material",
                                   "nominaldiameter", "operatingpressure",
                                   "CREATIONDATE", "dateretired")},
            geometry=[], crs=CRS)
    for index, (x, y, material, diameter) in enumerate(entries, start=1):
        rows.append({
            "OBJECTID": index,
            "GLOBALID": f"{{PIPE-{index}}}",
            "material": material,
            "nominaldiameter": diameter,
            "operatingpressure": 0.5,
            "CREATIONDATE": 946_684_800_000.0,   # 2000-01-01
            "dateretired": None,
            "geometry": LineString([(x, y - 500), (x, y + 500)]),
        })
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)


def relocate(workflow, leaks, attributes, distribution, service=None, tmp_path=None):
    """prepare -> match -> write, exactly as the workflow does it."""
    # Both layers always, as relocate() builds them: route_layers sends a leak
    # with no facility type to both, and a leak routed to a layer that was
    # never indexed is a KeyError rather than a non-match.
    service = service if service is not None else pipes([], "service")
    out = str(tmp_path / "MaximoLeakRelocation.gpkg")
    saved = workflow.OUTPUT_GPKG
    workflow.OUTPUT_GPKG = out
    try:
        with redirect_stdout(io.StringIO()):
            tasks, initial = workflow.prepare_leaks(leaks, attributes)
            sources = {
                "distribution": workflow.prepare_pipes(distribution, "distribution"),
                "service": workflow.prepare_pipes(service, "service"),
            }
            workflow.init_worker(sources)
            results = [workflow.match_one_leak(task) for task in tasks]
            counters = workflow.write_outputs(tasks, results, sources, initial)
        return tasks, results, counters, out
    finally:
        workflow.OUTPUT_GPKG = saved


class TestAMaximoLeakRelocates:
    def test_a_leak_beside_a_matching_pipe_is_moved_onto_it(
            self, workflow, tmp_path):
        leaks, attributes, _ = maximo_leaks([sql_row("WO-1", 760_000, 2_960_000)])
        distribution = pipes([(760_020, 2_960_000, "Cast Iron", 8.0)])
        tasks, results, counters, _ = relocate(
            workflow, leaks, attributes, distribution, tmp_path=tmp_path)
        assert len(tasks) == 1
        assert results[0]["matched"] is True
        assert results[0]["distance_ft"] == pytest.approx(20.0, abs=0.01)
        assert counters["matched"] == 1

    def test_the_material_and_diameter_come_from_the_query(
            self, workflow, tmp_path):
        """No supplemental join. A pipe of another material is not a match even
        though it is nearer."""
        leaks, attributes, _ = maximo_leaks([
            sql_row("WO-1", 760_000, 2_960_000, material="Cast Iron",
                    diameter=8.0)])
        distribution = pipes([
            (760_005, 2_960_000, "Plastic PE", 8.0),   # nearer, wrong material
            (760_050, 2_960_000, "Cast Iron", 8.0),    # further, right material
        ])
        _, results, _, _ = relocate(
            workflow, leaks, attributes, distribution, tmp_path=tmp_path)
        assert results[0]["matched"] is True
        assert results[0]["distance_ft"] == pytest.approx(50.0, abs=0.01)

    def test_a_wrong_diameter_is_refused_under_the_exact_rule(
            self, workflow, tmp_path):
        leaks, attributes, _ = maximo_leaks([
            sql_row("WO-1", 760_000, 2_960_000, diameter=8.0)])
        distribution = pipes([(760_020, 2_960_000, "Cast Iron", 12.0)])
        _, results, counters, _ = relocate(
            workflow, leaks, attributes, distribution, tmp_path=tmp_path)
        assert results[0]["matched"] is False
        assert counters["unmatched"] == 1

    def test_the_facility_type_routes_the_leak(self, workflow, tmp_path):
        """A service leak is not searched against the mains. The facility type
        comes from the query, so a query without it would search both."""
        leaks, attributes, _ = maximo_leaks([
            sql_row("WO-1", 760_000, 2_960_000, facility="Service")])
        distribution = pipes([(760_010, 2_960_000, "Cast Iron", 8.0)])
        _, results, _, _ = relocate(
            workflow, leaks, attributes, distribution, tmp_path=tmp_path)
        assert results[0]["matched"] is False

    def test_a_leak_with_no_facility_type_is_searched_against_both(
            self, workflow, tmp_path):
        leaks, attributes, _ = maximo_leaks([
            sql_row("WO-1", 760_000, 2_960_000, facility="")])
        distribution = pipes([(760_010, 2_960_000, "Cast Iron", 8.0)])
        _, results, _, _ = relocate(
            workflow, leaks, attributes, distribution, tmp_path=tmp_path)
        assert results[0]["matched"] is True


class TestTheOutputsAreTheSameShape:
    def test_it_writes_the_three_layers(self, workflow, tmp_path):
        import geopandas as gpd
        leaks, attributes, _ = maximo_leaks([sql_row("WO-1", 760_000, 2_960_000)])
        distribution = pipes([(760_020, 2_960_000, "Cast Iron", 8.0)])
        _, _, _, out = relocate(
            workflow, leaks, attributes, distribution, tmp_path=tmp_path)
        for layer in ("relocated_leaks", "relocated_leak_offset_lines",
                      "leak_relocation_audit"):
            frame = gpd.read_file(out, layer=layer)
            assert len(frame) == 1, layer

    def test_the_audit_carries_the_leak_id_and_its_attributes(
            self, workflow, tmp_path):
        import geopandas as gpd
        leaks, attributes, _ = maximo_leaks([sql_row("WO-1", 760_000, 2_960_000)])
        distribution = pipes([(760_020, 2_960_000, "Cast Iron", 8.0)])
        _, _, _, out = relocate(
            workflow, leaks, attributes, distribution, tmp_path=tmp_path)
        audit = gpd.read_file(out, layer="leak_relocation_audit")
        row = audit.iloc[0]
        assert row["LeakGlobalID"] == "WO-1"
        assert row["LeakKey"] == "WO-1"
        assert row["LeakMaterial"] == "Cast Iron"
        assert row["LeakDiameter"] == 8.0
        assert row["MatchStatus"] == "Matched"

    def test_the_address_from_the_query_reaches_the_audit(
            self, workflow, tmp_path):
        import geopandas as gpd
        leaks, attributes, _ = maximo_leaks([sql_row("WO-1", 760_000, 2_960_000)])
        distribution = pipes([(760_020, 2_960_000, "Cast Iron", 8.0)])
        _, _, _, out = relocate(
            workflow, leaks, attributes, distribution, tmp_path=tmp_path)
        audit = gpd.read_file(out, layer="leak_relocation_audit")
        assert audit.iloc[0]["LeakAddress"] == "12 Elm St / WATERTOWN"

    def test_the_diameter_rule_is_recorded_the_same_way(
            self, workflow, tmp_path):
        import geopandas as gpd
        leaks, attributes, _ = maximo_leaks([sql_row("WO-1", 760_000, 2_960_000)])
        distribution = pipes([(760_020, 2_960_000, "Cast Iron", 8.0)])
        _, _, _, out = relocate(
            workflow, leaks, attributes, distribution, tmp_path=tmp_path)
        audit = gpd.read_file(out, layer="leak_relocation_audit")
        assert audit.iloc[0]["DiameterMatch"] == "exact"
        assert audit.iloc[0]["DiameterMode"] == workflow.DIAMETER_MATCH_MODE

    def test_the_dashboard_reads_a_maximo_output(self, workflow, tmp_path):
        """The report is source-agnostic too, so the distance dashboard works
        on this output with nothing changed."""
        import geopandas as gpd
        from leakrelocation import distance_dashboard, distance_report
        leaks, attributes, _ = maximo_leaks([
            sql_row(f"WO-{i}", 760_000 + i * 1000, 2_960_000) for i in range(5)])
        distribution = pipes([
            (760_020 + i * 1000, 2_960_000, "Cast Iron", 8.0) for i in range(5)])
        _, _, _, out = relocate(
            workflow, leaks, attributes, distribution, tmp_path=tmp_path)
        audit = gpd.read_file(out, layer="leak_relocation_audit")
        report = distance_report.build_report(audit, source=out)
        assert report["totals"]["relocated"] == 5
        page = distance_dashboard.dashboard_html(report)
        assert 'id="thresh"' in page


class TestTheDateRuleAppliesToo:
    def test_a_leak_older_than_the_pipe_is_refused(self, workflow, tmp_path):
        """The same rule as the historic leaks: the pipe's record must pre-date
        the leak. The date comes from the query's LeakDate."""
        leaks, attributes, _ = maximo_leaks([
            sql_row("WO-1", 760_000, 2_960_000, date="1995-01-01")])
        distribution = pipes([(760_020, 2_960_000, "Cast Iron", 8.0)])
        _, results, _, _ = relocate(
            workflow, leaks, attributes, distribution, tmp_path=tmp_path)
        assert results[0]["matched"] is False
        assert results[0]["rejected_on_date"] == 1

    def test_a_leak_newer_than_the_pipe_is_allowed(self, workflow, tmp_path):
        leaks, attributes, _ = maximo_leaks([
            sql_row("WO-1", 760_000, 2_960_000, date="2019-04-02")])
        distribution = pipes([(760_020, 2_960_000, "Cast Iron", 8.0)])
        _, results, _, _ = relocate(
            workflow, leaks, attributes, distribution, tmp_path=tmp_path)
        assert results[0]["matched"] is True
        assert results[0]["date_reason"] == "ok"

    def test_a_leak_with_no_date_is_relocated_and_flagged(
            self, workflow, tmp_path):
        leaks, attributes, _ = maximo_leaks([
            sql_row("WO-1", 760_000, 2_960_000, date=None)])
        distribution = pipes([(760_020, 2_960_000, "Cast Iron", 8.0)])
        _, results, _, _ = relocate(
            workflow, leaks, attributes, distribution, tmp_path=tmp_path)
        assert results[0]["matched"] is True
        assert results[0]["date_reason"] == "no_leak_date"


class TestTheSharedPathIsActuallyShared:
    """If these stop holding, the two entry points have diverged."""

    def test_both_entry_points_call_one_relocate(self):
        source = open(os.path.join(REPO_ROOT, "scripts", "maximo_leaks.py"),
                      encoding="utf-8").read()
        assert "workflow.relocate(" in source
        # Not its own copy of the matching.
        for forked in ("run_matching(", "match_one_leak(", "write_outputs("):
            assert forked not in source, forked

    def test_relocate_takes_the_output_path(self, workflow):
        """Both leak sets writing to one GeoPackage would mean the second run
        silently replacing the first."""
        import inspect
        assert "output_gpkg" in inspect.signature(workflow.relocate).parameters

    def test_relocate_puts_the_output_path_back(self, workflow, tmp_path):
        """It sets a module global. Leaving it changed would send a later
        historic run to the Maximo file."""
        before = workflow.OUTPUT_GPKG
        leaks, attributes, _ = maximo_leaks([sql_row("WO-1", 760_000, 2_960_000)])
        distribution = pipes([(760_020, 2_960_000, "Cast Iron", 8.0)])
        relocate(workflow, leaks, attributes, distribution, tmp_path=tmp_path)
        assert workflow.OUTPUT_GPKG == before

    def test_the_pipe_loader_can_be_asked_for_pipes_alone(self, workflow):
        """The Maximo run has no leak layer to download."""
        import inspect
        assert "with_leaks" in inspect.signature(
            workflow.load_pipe_layers).parameters


class TestTheDateFallbackChangesOutcomes:
    """The date fallback is only worth having if it actually decides something.
    These relocate the same leak twice, with it off and on."""

    def leak_with_dates(self, revised, discovered):
        import geopandas as gpd
        import pandas as pd
        from shapely.geometry import Point
        frame = pd.DataFrame([{
            "OBJECTID": 1, "GLOBALID": "{A}", "LMSLEAKNUMBER": "1",
            "ADDRESS": "", "CITY": "", "NEARESTXSTREET": "",
            "REVISEDLEAKDATE": revised, "DISCOVEREDDATE": discovered,
        }])
        leaks = gpd.GeoDataFrame(
            frame, geometry=[Point(760_000, 2_960_000)], crs=CRS)
        attributes = {"A": {"material": "Cast Iron", "diameter": 8.0,
                            "pressure": "", "facility": "Main"}}
        return leaks, attributes

    def test_with_it_off_an_old_leak_relocates_onto_a_newer_pipe(
            self, workflow, tmp_path, monkeypatch):
        """What happens today for 98.1% of the audit: no revised date, so the
        pipe-life rule decides nothing and the leak is matched to a pipe
        recorded years after it was discovered."""
        from leakrelocation import config
        monkeypatch.setattr(config, "USE_LEAK_DATE_FALLBACK", False)
        leaks, attributes = self.leak_with_dates(None, "1995-01-01")
        # The pipe's record is from 2000, five years after the leak.
        distribution = pipes([(760_020, 2_960_000, "Cast Iron", 8.0)])
        _, results, _, _ = relocate(
            workflow, leaks, attributes, distribution, tmp_path=tmp_path)
        assert results[0]["matched"] is True
        assert results[0]["date_reason"] == "no_leak_date"

    def test_with_it_on_the_same_leak_is_refused(self, workflow, tmp_path,
                                                 monkeypatch):
        from leakrelocation import config
        monkeypatch.setattr(config, "USE_LEAK_DATE_FALLBACK", True)
        leaks, attributes = self.leak_with_dates(None, "1995-01-01")
        distribution = pipes([(760_020, 2_960_000, "Cast Iron", 8.0)])
        _, results, _, _ = relocate(
            workflow, leaks, attributes, distribution, tmp_path=tmp_path)
        assert results[0]["matched"] is False
        assert results[0]["rejected_on_date"] == 1

    def test_a_leak_newer_than_the_pipe_still_relocates_with_it_on(
            self, workflow, tmp_path, monkeypatch):
        """The fallback is not a filter - a leak discovered after the pipe was
        recorded passes the check and keeps its relocation."""
        from leakrelocation import config
        monkeypatch.setattr(config, "USE_LEAK_DATE_FALLBACK", True)
        leaks, attributes = self.leak_with_dates(None, "2019-04-02")
        distribution = pipes([(760_020, 2_960_000, "Cast Iron", 8.0)])
        _, results, _, _ = relocate(
            workflow, leaks, attributes, distribution, tmp_path=tmp_path)
        assert results[0]["matched"] is True
        assert results[0]["date_reason"] == "ok"

    def test_the_audit_records_which_field_supplied_the_date(
            self, workflow, tmp_path, monkeypatch):
        import geopandas as gpd
        from leakrelocation import config
        monkeypatch.setattr(config, "USE_LEAK_DATE_FALLBACK", True)
        leaks, attributes = self.leak_with_dates(None, "2019-04-02")
        distribution = pipes([(760_020, 2_960_000, "Cast Iron", 8.0)])
        _, _, _, out = relocate(
            workflow, leaks, attributes, distribution, tmp_path=tmp_path)
        audit = gpd.read_file(out, layer="leak_relocation_audit")
        assert audit.iloc[0]["LeakDateSource"] == "DISCOVEREDDATE"
        assert audit.iloc[0]["LeakDate"] == "2019-04-02"

    def test_the_source_is_blank_when_nothing_supplied_a_date(
            self, workflow, tmp_path, monkeypatch):
        import geopandas as gpd
        from leakrelocation import config
        monkeypatch.setattr(config, "USE_LEAK_DATE_FALLBACK", True)
        leaks, attributes = self.leak_with_dates(None, None)
        distribution = pipes([(760_020, 2_960_000, "Cast Iron", 8.0)])
        _, _, _, out = relocate(
            workflow, leaks, attributes, distribution, tmp_path=tmp_path)
        audit = gpd.read_file(out, layer="leak_relocation_audit")
        assert audit.iloc[0]["LeakDateSource"] == ""

    def test_the_run_counts_what_a_fallback_would_rescue(
            self, workflow, tmp_path, monkeypatch):
        """With it off, the run still has to say how big the gap is."""
        from leakrelocation import config
        monkeypatch.setattr(config, "USE_LEAK_DATE_FALLBACK", False)
        leaks, attributes = self.leak_with_dates(None, "1995-01-01")
        distribution = pipes([(760_020, 2_960_000, "Cast Iron", 8.0)])
        tasks, _, counters, _ = relocate(
            workflow, leaks, attributes, distribution, tmp_path=tmp_path)
        assert counters["leaks_a_date_fallback_would_rescue"] == 1
