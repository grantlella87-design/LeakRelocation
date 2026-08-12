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
        csv_path.write_text("LeakNumber,Diameter,LeakMaterialType,FacilityType\n"
                            "1,2,Cast Iron,Distribution Main\n", encoding="utf-8")
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
