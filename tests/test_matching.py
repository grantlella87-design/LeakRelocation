"""Tests for the pure leak-to-pipe matching logic.

These run anywhere: no network, no ArcGIS token, no access to the shared
drive. They pin the behaviour the relocation results depend on.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from leakrelocation import config, matching


class TestClean:
    @pytest.mark.parametrize("value,expected", [
        (None, ""),
        ("", ""),
        ("   ", ""),
        ("none", ""),
        ("NULL", ""),
        ("nan", ""),
        ("  Cast   Iron  ", "Cast Iron"),
        ("STEEL", "STEEL"),
        (0, "0"),
    ])
    def test_clean(self, value, expected):
        assert matching.clean(value) == expected

    def test_upper_normalises_dashes(self):
        assert matching.upper("cast–iron") == "CAST-IRON"
        assert matching.upper("cast—iron") == "CAST-IRON"


class TestNormalizeKey:
    @pytest.mark.parametrize("value,expected", [
        ("{ABC}", "ABC"),
        ("123.0", "123"),
        ("12.5", "12.5"),
        ("  leak-1 ", "LEAK-1"),
        (None, ""),
    ])
    def test_normalize_key(self, value, expected):
        assert matching.normalize_key(value) == expected

    def test_trailing_point_zero_only_stripped_for_integers(self):
        # A float that happens to end in .0 is a coerced integer key.
        assert matching.normalize_key("4.0") == "4"
        # ...but a genuine decimal is left alone.
        assert matching.normalize_key("4.05") == "4.05"


class TestParseNumber:
    @pytest.mark.parametrize("value,expected", [
        (None, None),
        ("", None),
        ("abc", None),
        (2, 2.0),
        (2.5, 2.5),
        ("2", 2.0),
        ("12 in", 12.0),
        ("-3", -3.0),
        ("+4", 4.0),
    ])
    def test_parse_number(self, value, expected):
        assert matching.parse_number(value) == expected

    def test_nan_becomes_none(self):
        assert matching.parse_number(float("nan")) is None

    def test_thousands_separator_is_truncated_not_parsed(self):
        # "1,200" yields 1.0, not 1200.0 - the regex stops at the comma.
        assert matching.parse_number("1,200") == 1.0


class TestMaterialLabel:
    def test_decodes_service_assettype_domain(self):
        assert matching.material_label(2) == "Cast Iron"
        assert matching.material_label("9") == "Plastic PE"
        assert matching.material_label(999) == "UNK"

    def test_passes_through_text(self):
        assert matching.material_label("Coated Steel") == "Coated Steel"

    def test_unmapped_code_falls_back_to_cleaned_text(self):
        assert matching.material_label(42) == "42"


class TestMaterialFamily:
    @pytest.mark.parametrize("value,family", [
        ("Plastic PE", "PLASTIC"),
        ("Polyethylene", "PLASTIC"),
        ("Cast Iron", "IRON"),
        ("Ductile Iron", "IRON"),
        ("Wrought Iron", "IRON"),
        ("Bare Steel", "STEEL"),
        ("Galvanized Steel", "STEEL"),
        ("Unknown", "UNKNOWN"),
        ("Composite", "UNKNOWN"),
    ])
    def test_families(self, value, family):
        assert matching.material_family(value) == family


class TestMaterialMatches:
    @pytest.mark.parametrize("leak,pipe", [
        ("PLASTIC", "PLASTIC"),
        ("plastic", "PLASTIC"),
        ("Cast Iron", "CAST IRON"),
        ("Wrought Iron", "Cast Iron"),
        ("Coated Steel", "STEEL"),
    ])
    def test_matching_materials(self, leak, pipe):
        assert matching.material_matches(leak, pipe) is True

    @pytest.mark.parametrize("leak,pipe", [
        ("Cast Iron", "STEEL"),
        ("Copper", "STEEL"),
        (None, "STEEL"),
        ("STEEL", None),
        ("", "STEEL"),
        (None, None),
    ])
    def test_non_matching_materials(self, leak, pipe):
        assert matching.material_matches(leak, pipe) is False

    def test_missing_material_never_matches(self):
        assert matching.material_matches("", "") is False


class TestKnownDefects:
    """Pins existing incorrect behaviour so a future fix is a deliberate,
    reviewed change rather than an accident. See MATERIAL_FAMILY_TERMS."""

    def test_copper_is_classified_as_plastic(self):
        # "COPPER" contains the substring "PE" and PLASTIC is tested first,
        # so the COPPER family is unreachable.
        assert matching.material_family("Copper") == "PLASTIC"

    def test_copper_leak_matches_plastic_pipe(self):
        # Consequence of the above: a copper leak relocates onto plastic pipe.
        assert matching.material_matches("Copper", "Plastic PE") is True
        assert matching.material_matches("Copper", "PLASTIC") is True

    def test_hyphenated_spelling_misses_its_family(self):
        # Terms are spaced ("CAST IRON"), so hyphenated input falls through
        # to the raw label instead of resolving to IRON.
        assert matching.material_family("cast-iron") == "CAST-IRON"
        assert matching.material_family("Cast Iron") == "IRON"


class TestDiameterMatches:
    def test_exact_match_required(self):
        assert matching.diameter_matches(2, 2) is True
        assert matching.diameter_matches(2, 2.0) is True
        assert matching.diameter_matches(2, 4) is False

    def test_missing_never_matches(self):
        assert matching.diameter_matches(None, 2) is False
        assert matching.diameter_matches(2, None) is False
        assert matching.diameter_matches(None, None) is False


class TestPressureMatches:
    def test_advisory_by_default(self):
        assert config.REQUIRE_PRESSURE_MATCH is False
        assert matching.pressure_matches("anything", "different") is True

    def test_enforced_when_required(self, monkeypatch):
        monkeypatch.setattr(config, "REQUIRE_PRESSURE_MATCH", True)
        assert matching.pressure_matches("LP", "LP") is True
        assert matching.pressure_matches("LP", "HP") is False
        assert matching.pressure_matches("", "") is False


class TestRouteLayers:
    @pytest.mark.parametrize("facility,expected", [
        ("Service", ["service"]),
        ("SERVICE LINE", ["service"]),
        ("Main", ["distribution"]),
        ("Distribution", ["distribution"]),
        ("", ["distribution", "service"]),
        (None, ["distribution", "service"]),
        ("Unknown", ["distribution", "service"]),
    ])
    def test_routing(self, facility, expected):
        assert matching.route_layers(facility) == expected


class TestMatchedRadius:
    @pytest.mark.parametrize("distance,expected", [
        (None, None),
        (0, 100.0),
        (50, 100.0),
        (100, 100.0),
        (100.1, 200.0),
        (250, 300.0),
        (999, 1000.0),
        (1000, 1000.0),
    ])
    def test_rounds_up_to_search_ring(self, distance, expected):
        assert matching.matched_radius_from_distance(distance) == expected


class TestResolveFieldName:
    def test_matches_ignoring_case_and_punctuation(self):
        columns = ["OBJECTID", "Nominal_Diameter", "MATERIAL"]
        assert matching.resolve_field_name(columns, ["nominaldiameter"]) == "Nominal_Diameter"
        assert matching.resolve_field_name(columns, ["material"]) == "MATERIAL"

    def test_returns_first_matching_candidate(self):
        columns = ["material", "assettype"]
        assert matching.resolve_field_name(columns, ["assettype", "material"]) == "assettype"

    def test_returns_none_when_absent(self):
        assert matching.resolve_field_name(["a", "b"], ["missing"]) is None

    def test_empty_columns(self):
        assert matching.resolve_field_name([], ["anything"]) is None
