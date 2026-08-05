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


class TestCopperIsNotPlastic:
    """Regression tests for the substring-matching defect.

    Family terms used to be matched as raw substrings. "COPPER" contains "PE"
    and PLASTIC was tested first, so every copper pipe was classified PLASTIC,
    the COPPER family was unreachable, and a copper leak relocated onto plastic
    pipe.
    """

    @pytest.mark.parametrize("value", ["Copper", "COPPER", "Copper Tubing", 5])
    def test_copper_classifies_as_copper(self, value):
        assert matching.material_family(value) == "COPPER"

    @pytest.mark.parametrize("pipe", [
        "Plastic PE", "PLASTIC", "Polyethylene", "Plastic PVC",
        "Plastic ABS", "Polybutylene", "HDPE", "MDPE",
    ])
    def test_copper_leak_does_not_match_plastic_pipe(self, pipe):
        assert matching.material_matches("Copper", pipe) is False
        assert matching.material_matches(pipe, "Copper") is False

    def test_copper_still_matches_copper(self):
        assert matching.material_matches("Copper", "Copper") is True
        assert matching.material_matches("Copper", 5) is True

    def test_short_abbreviations_do_not_match_inside_words(self):
        # The mechanism behind the defect: "PE" must be a token, not a substring.
        assert matching.match_term(["COPPER"], "PE") is False
        assert matching.match_term(["PE"], "PE") is True


class TestHyphenatedSpellings:
    """Terms are spaced ("CAST IRON"); tokenising on punctuation means
    hyphenated spellings resolve to the same family."""

    @pytest.mark.parametrize("value", ["cast-iron", "Cast-Iron", "Cast Iron", "CAST  IRON"])
    def test_cast_iron_variants_resolve_to_iron(self, value):
        assert matching.material_family(value) == "IRON"

    def test_hyphenated_matches_spaced(self):
        assert matching.material_matches("Cast Iron", "cast-iron") is True
        assert matching.material_matches("ductile-iron", "Ductile Iron") is True


class TestPlasticAbbreviations:
    """HDPE/MDPE are spelled out in the term list because token matching will
    not find the "HD"/"PE" inside them."""

    @pytest.mark.parametrize("value", ["HDPE", "MDPE", "PE", "Polyethylene", "Polybutylene", "PVC"])
    def test_plastic_abbreviations(self, value):
        assert matching.material_family(value) == "PLASTIC"

    def test_long_terms_may_prefix_match(self):
        # "POLY" is long enough to prefix-match POLYETHYLENE.
        assert matching.match_term(["POLYETHYLENE"], "POLY") is True

    def test_short_terms_must_match_whole_token(self):
        assert len("MD") < matching.PREFIX_MATCH_MIN_LENGTH
        assert matching.match_term(["MDPE"], "MD") is False


class TestMaterialTokens:
    @pytest.mark.parametrize("value,tokens", [
        ("Cast Iron", ["CAST", "IRON"]),
        ("cast-iron", ["CAST", "IRON"]),
        ("Plastic PE", ["PLASTIC", "PE"]),
        ("", []),
        (None, []),
    ])
    def test_tokenisation(self, value, tokens):
        assert matching.material_tokens(value) == tokens

    def test_multi_word_terms_need_consecutive_tokens(self):
        assert matching.match_term(["CAST", "IRON"], "CAST IRON") is True
        assert matching.match_term(["CAST", "STEEL", "IRON"], "CAST IRON") is False


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
