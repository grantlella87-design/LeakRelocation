"""Checks the supplemental CSV candidate lists against the committed file.

input/HL_SupplementalData.csv used to live on a network share, which is why the
SUPP_* lists in the workflow were guesses - five of them named a pressure column
that does not exist. The file travels with the repository now, so these names are
checkable offline, exactly like the DNV field names in
tests/test_dnv_service_metadata.py.

Only the header row is read. The file is 34 MB and nothing here needs the rows.
"""
import csv
import importlib.util
import io
import os
import sys
from contextlib import redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from leakrelocation import config, matching

pytestmark = pytest.mark.skipif(
    not config.SUPPLEMENTAL_CSV.is_file(),
    reason="the supplemental CSV is not in this checkout")


@pytest.fixture(scope="module")
def headers():
    with open(config.SUPPLEMENTAL_CSV, encoding="utf-8-sig", newline="") as handle:
        return next(csv.reader(handle))


@pytest.fixture(scope="module")
def workflow():
    pytest.importorskip("geopandas")
    pytest.importorskip("keyring")
    path = os.path.join(REPO_ROOT, "src", "leak_relocation_geopandas.py")
    spec = importlib.util.spec_from_file_location("lr_supp", path)
    module = importlib.util.module_from_spec(spec)
    with redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)
    return module


class TestTheFileIsTheOneWeThinkItIs:
    def test_it_is_where_the_config_points(self):
        assert config.SUPPLEMENTAL_CSV.parent == config.INPUT_DIR

    def test_the_headers_are_the_thirty_three_we_read(self, headers):
        assert len(headers) == 33
        for name in ["LeakNumber", "Name", "Jurisdiction", "Diameter",
                     "LeakMaterialType", "FacilityType", "PipeType"]:
            assert name in headers


class TestEveryCandidateResolves:
    def resolved(self, headers, candidates):
        return matching.resolve_field_name(headers, candidates)

    def test_the_leak_key(self, workflow, headers):
        assert self.resolved(headers, workflow.SUPP_KEY_CANDIDATES) == "LeakNumber"

    def test_the_diameter(self, workflow, headers):
        assert self.resolved(headers, workflow.SUPP_DIAMETER_CANDIDATES) == "Diameter"

    def test_the_material(self, workflow, headers):
        assert self.resolved(headers, workflow.SUPP_MATERIAL_CANDIDATES) == \
            "LeakMaterialType"

    def test_the_facility_type(self, workflow, headers):
        assert self.resolved(headers, workflow.SUPP_FACILITY_CANDIDATES) == \
            "FacilityType"

    @pytest.mark.parametrize("attribute", [
        "SUPP_KEY_CANDIDATES",
        "SUPP_DIAMETER_CANDIDATES",
        "SUPP_MATERIAL_CANDIDATES",
        "SUPP_FACILITY_CANDIDATES",
    ])
    def test_no_dead_spellings(self, workflow, headers, attribute):
        """The old lists carried LMSLEAKNUMBER, LEAKNUMBER, MAOP and four other
        names this file does not have."""
        dead = [name for name in getattr(workflow, attribute) if name not in headers]
        assert dead == [], f"{attribute} names columns the CSV has not got: {dead}"

    @pytest.mark.parametrize("attribute", [
        "SUPP_KEY_CANDIDATES",
        "SUPP_DIAMETER_CANDIDATES",
        "SUPP_MATERIAL_CANDIDATES",
        "SUPP_FACILITY_CANDIDATES",
    ])
    def test_no_unreachable_duplicates(self, workflow, attribute):
        """resolve_field_name strips case and punctuation, so "LEAKNUMBER" beside
        "LeakNumber" is one candidate written twice."""
        candidates = getattr(workflow, attribute)
        simplified = [matching.simplify_field_name(name) for name in candidates]
        assert len(set(simplified)) == len(simplified), candidates


class TestThereIsNoPressureColumn:
    """Five pressure names were guessed for a file nothing could read. The CSV has
    no pressure column of any kind."""

    def test_the_file_has_none(self, headers):
        assert not [name for name in headers
                    if "press" in name.lower() or "maop" in name.lower()]

    def test_the_candidate_list_is_empty(self, workflow):
        assert workflow.SUPP_PRESSURE_CANDIDATES == []

    def test_pressure_matching_is_off(self):
        """Which is what makes the empty list harmless."""
        assert config.REQUIRE_PRESSURE_MATCH is False

    def test_turning_it_on_fails_loudly(self, workflow, monkeypatch, tmp_path):
        """Rather than matching every leak against a blank pressure."""
        csv_path = tmp_path / "supp.csv"
        csv_path.write_text(
            "HistoricalLeaksID,LeakNumber,Diameter,LeakMaterialType,FacilityType\n"
            "{AAAAAAAA-0000-0000-0000-000000000001},1,2,Cast Iron,Distribution Main\n",
            encoding="utf-8")
        monkeypatch.setattr(workflow, "SUPPLEMENTAL_CSV", str(csv_path))
        monkeypatch.setattr(workflow, "REQUIRE_PRESSURE_MATCH", True)
        with pytest.raises(RuntimeError, match="REQUIRE_PRESSURE_MATCH"), \
                redirect_stdout(io.StringIO()):
            workflow.load_supplemental()


class TestTheRealValuesReachThePipeLabels:
    """The CSV's material labels are not the DNV ASSETTYPE domain labels. Every one
    of them still has to reach a pipe, or the leaks carrying it can never relocate.

    These are the eight values in the committed file, read from it rather than
    imagined - "Plastic - MD" alone is 14,419 rows.
    """

    LEAK_LABELS = ("Cast Iron", "Bare Steel", "Plastic - MD", "Coated Steel",
                   "Copper", "Plastic - HD", "Wrought Iron")

    def test_every_label_lands_in_a_real_family(self):
        """family_from_label echoes the label back when no family term matches, so
        the assertion has to be that the answer is one of the families - not
        merely that it is non-empty."""
        families = set(matching.MATERIAL_FAMILY_TERMS)
        for label in self.LEAK_LABELS:
            assert matching.family_from_label(label) in families, label

    def test_every_label_matches_at_least_one_pipe_label(self):
        pipe_labels = set(matching.SERVICE_ASSETTYPE_LABELS.values())
        for label in self.LEAK_LABELS:
            hits = [p for p in pipe_labels if matching.material_matches(label, p)]
            assert hits, f"{label} matches no pipe material"

    def test_the_plastic_grades_only_match_through_the_family(self, monkeypatch):
        """"Plastic - MD" and "Plastic - HD" are not DNV domain labels, so the
        15,313 leaks carrying them depend on the family fallback being on. With it
        off they would match no pipe at all."""
        assert config.ALLOW_MATERIAL_FAMILY_FALLBACK is True
        for label in ["Plastic - MD", "Plastic - HD"]:
            assert label not in set(matching.SERVICE_ASSETTYPE_LABELS.values())
            assert matching.material_matches(label, "Plastic PE")
        # material_matches reads the flag at call time.
        monkeypatch.setattr(config, "ALLOW_MATERIAL_FAMILY_FALLBACK", False)
        for label in ["Plastic - MD", "Plastic - HD"]:
            assert not matching.material_matches(label, "Plastic PE")
        # An exact label still matches with the fallback off.
        assert matching.material_matches("Cast Iron", "Cast Iron")

    def test_copper_does_not_leak_into_another_family(self):
        """Substring matching once made "Copper" match "Copper Coated Steel"."""
        assert not matching.material_matches("Copper", "Bare Steel")
        assert not matching.material_matches("Copper", "Cast Iron")


class TestFacilityRouting:
    """FacilityType decides which pipe layers a leak may relocate onto. These are
    the values in the file."""

    def test_the_real_values_route(self, workflow):
        assert workflow.route_layers("Distribution Main") == ["distribution"]
        assert workflow.route_layers("Service") == ["service"]

    def test_a_blank_tries_both(self, workflow):
        """2,223 rows have no facility type. Trying both layers is the safe
        reading - it cannot wrongly exclude the pipe the leak belongs to."""
        assert sorted(workflow.route_layers("")) == ["distribution", "service"]


class TestTheJoinKeyIsTheGlobalId:
    """The supplemental join is on the leak's GlobalID, not its leak number.

    In the committed file HistoricalLeaksID is unique on all 98,464 rows, while
    25,733 rows (26.1%) share a LeakNumber with another row across 10,990 numbers -
    and 5,364 of those numbers have rows that disagree about the material or the
    diameter. Keyed on the number, those leaks took an arbitrary row's attributes.
    """

    def test_the_globalid_column_is_in_the_file(self, headers):
        assert "HistoricalLeaksID" in headers

    def test_it_resolves(self, workflow, headers):
        assert matching.resolve_field_name(
            headers, workflow.SUPP_GLOBALID_CANDIDATES) == "HistoricalLeaksID"

    def test_the_globalid_is_unique_and_the_number_is_not(self):
        """Read from the file rather than asserted from a comment. This is the
        whole reason for the change, so it is worth the one full pass."""
        import csv as csv_module
        globalids = set()
        numbers = set()
        repeated_numbers = 0
        rows = 0
        with open(config.SUPPLEMENTAL_CSV, encoding="utf-8-sig", newline="") as handle:
            for row in csv_module.DictReader(handle):
                rows += 1
                globalids.add(row["HistoricalLeaksID"].strip())
                number = row["LeakNumber"].strip()
                if number in numbers:
                    repeated_numbers += 1
                numbers.add(number)
        assert len(globalids) == rows, "the GlobalID is not unique after all"
        assert len(numbers) < rows, "the leak number is unique after all"
        assert repeated_numbers > 0

    def test_both_sides_normalise_to_the_same_shape(self):
        """The CSV writes {ABC...}; the service may return it braced or not, and in
        either case. normalize_key strips the braces and upper-cases, so the two
        sides meet."""
        braced = "{55891E36-306D-4812-B747-7623651D2ECD}"
        assert matching.normalize_key(braced) == \
            matching.normalize_key(braced.lower())
        assert matching.normalize_key(braced) == \
            matching.normalize_key("55891e36-306d-4812-b747-7623651d2ecd")

    def test_rows_sharing_a_number_stay_apart(self, workflow, monkeypatch, tmp_path):
        """The real shape of leak 1000001: a Cast Iron 8" main and a Bare Steel
        1.25" service under one number. Each must keep its own attributes."""
        csv_path = tmp_path / "supp.csv"
        csv_path.write_text(
            "HistoricalLeaksID,LeakNumber,Diameter,LeakMaterialType,FacilityType\n"
            "{55891E36-306D-4812-B747-7623651D2ECD},1000001,8,Cast Iron,Distribution Main\n"
            "{8D0318B7-BEF3-4A38-ADF1-79D26E200FD0},1000001,1.25,Bare Steel,Service\n",
            encoding="utf-8")
        monkeypatch.setattr(workflow, "SUPPLEMENTAL_CSV", str(csv_path))
        with redirect_stdout(io.StringIO()):
            records = workflow.load_supplemental()

        assert len(records) == 2, "the two rows collapsed into one"
        main = records["55891E36-306D-4812-B747-7623651D2ECD"]
        service = records["8D0318B7-BEF3-4A38-ADF1-79D26E200FD0"]
        assert (main["material"], main["diameter"]) == ("Cast Iron", 8.0)
        assert (service["material"], service["diameter"]) == ("Bare Steel", 1.25)
        assert main["facility"] == "Distribution Main"
        assert service["facility"] == "Service"
        # The number is still carried, for labelling and the audit.
        assert main["leak_number"] == service["leak_number"] == "1000001"

    def test_a_leak_gets_its_own_row_not_its_twins(self, workflow, monkeypatch, tmp_path):
        """End to end through prepare_leaks: two leaks sharing a number, each
        pointing at a different supplemental row. Keyed on the number both would
        have taken the same attributes, and one of them would have been wrong."""
        import geopandas as gpd
        from shapely.geometry import Point

        csv_path = tmp_path / "supp.csv"
        csv_path.write_text(
            "HistoricalLeaksID,LeakNumber,Diameter,LeakMaterialType,FacilityType\n"
            "{55891E36-306D-4812-B747-7623651D2ECD},1000001,8,Cast Iron,Distribution Main\n"
            "{8D0318B7-BEF3-4A38-ADF1-79D26E200FD0},1000001,1.25,Bare Steel,Service\n",
            encoding="utf-8")
        monkeypatch.setattr(workflow, "SUPPLEMENTAL_CSV", str(csv_path))
        with redirect_stdout(io.StringIO()):
            records = workflow.load_supplemental()

        leaks = gpd.GeoDataFrame(
            {
                "OBJECTID": [1, 2],
                "LMSLEAKNUMBER": ["1000001", "1000001"],
                # Lower case and unbraced on purpose: the join must not depend on
                # the service spelling it the way the CSV does.
                "GlobalID": ["55891e36-306d-4812-b747-7623651d2ecd",
                             "{8D0318B7-BEF3-4A38-ADF1-79D26E200FD0}"],
            },
            geometry=[Point(0, 0), Point(10, 10)],
            crs="EPSG:2249",
        )
        with redirect_stdout(io.StringIO()):
            tasks, _ = workflow.prepare_leaks(leaks, records)

        assert len(tasks) == 2
        by_oid = {task["leak_oid"]: task for task in tasks}
        assert by_oid[1]["leak_info"]["material"] == "Cast Iron"
        assert by_oid[1]["leak_info"]["diameter"] == 8.0
        assert by_oid[1]["allowed_layers"] == ["distribution"]
        assert by_oid[2]["leak_info"]["material"] == "Bare Steel"
        assert by_oid[2]["leak_info"]["diameter"] == 1.25
        assert by_oid[2]["allowed_layers"] == ["service"]

    def test_a_leak_without_a_globalid_is_counted_not_guessed(
            self, workflow, monkeypatch, tmp_path):
        """Falling back to the leak number here is what imported another leak's
        attributes, so a leak with no GlobalID is left unmatched instead."""
        import geopandas as gpd
        from shapely.geometry import Point

        csv_path = tmp_path / "supp.csv"
        csv_path.write_text(
            "HistoricalLeaksID,LeakNumber,Diameter,LeakMaterialType,FacilityType\n"
            "{55891E36-306D-4812-B747-7623651D2ECD},1000001,8,Cast Iron,Distribution Main\n",
            encoding="utf-8")
        monkeypatch.setattr(workflow, "SUPPLEMENTAL_CSV", str(csv_path))
        with redirect_stdout(io.StringIO()):
            records = workflow.load_supplemental()

        leaks = gpd.GeoDataFrame(
            {"OBJECTID": [1], "LMSLEAKNUMBER": ["1000001"], "GlobalID": [""]},
            geometry=[Point(0, 0)], crs="EPSG:2249",
        )
        with redirect_stdout(io.StringIO()):
            tasks, counters = workflow.prepare_leaks(leaks, records)
        assert tasks == []
        assert counters["unmatched_no_globalid"] == 1

    def test_a_leak_layer_without_the_column_stops_the_run(
            self, workflow, monkeypatch, tmp_path):
        """Rather than joining on the number and producing plausible, wrong
        output."""
        import geopandas as gpd
        from shapely.geometry import Point

        leaks = gpd.GeoDataFrame(
            {"OBJECTID": [1], "LMSLEAKNUMBER": ["1000001"]},
            geometry=[Point(0, 0)], crs="EPSG:2249",
        )
        with pytest.raises(RuntimeError, match="GlobalID"), \
                redirect_stdout(io.StringIO()):
            workflow.prepare_leaks(leaks, {})

    def test_the_csv_without_the_column_stops_the_run(
            self, workflow, monkeypatch, tmp_path):
        csv_path = tmp_path / "supp.csv"
        csv_path.write_text("LeakNumber,Diameter,LeakMaterialType,FacilityType\n"
                            "1000001,8,Cast Iron,Distribution Main\n", encoding="utf-8")
        monkeypatch.setattr(workflow, "SUPPLEMENTAL_CSV", str(csv_path))
        with pytest.raises(RuntimeError, match="GlobalID"), \
                redirect_stdout(io.StringIO()):
            workflow.load_supplemental()
