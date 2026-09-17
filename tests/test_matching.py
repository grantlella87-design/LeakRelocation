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
    """The default rule, unchanged: the diameters are the same nominal size."""

    def test_exact_match_required(self):
        assert matching.diameter_matches(2, 2) is True
        assert matching.diameter_matches(2, 2.0) is True
        assert matching.diameter_matches(2, 4) is False

    def test_missing_never_matches(self):
        assert matching.diameter_matches(None, 2) is False
        assert matching.diameter_matches(2, None) is False
        assert matching.diameter_matches(None, None) is False

    def test_the_default_mode_is_exact(self):
        """An unchanged command has to produce the output it always did."""
        assert config.DIAMETER_MATCH_MODE == matching.DIAMETER_EXACT
        assert matching.diameter_matches(8, 12) is False


class TestTheNominalLadder:
    """The sizes "one size up or down" is counted on."""

    def test_it_is_the_specified_list(self):
        assert matching.NOMINAL_DIAMETERS_IN == (
            1.0, 1.25, 1.5, 2.0, 4.0, 6.0, 8.0, 12.0, 16.0, 20.0, 24.0, 30.0,
            36.0, 42.0, 48.0)

    def test_it_is_sorted_and_unique(self):
        ladder = matching.NOMINAL_DIAMETERS_IN
        assert list(ladder) == sorted(ladder)
        assert len(set(ladder)) == len(ladder)

    def test_there_is_no_three_or_ten_inch_rung(self):
        """Both exist in the data and neither is a listed size, which is the
        case adjacent_nominal_sizes has to bracket."""
        assert 3.0 not in matching.NOMINAL_DIAMETERS_IN
        assert 10.0 not in matching.NOMINAL_DIAMETERS_IN


class TestAdjacentNominalSizes:
    def test_a_listed_size_gets_its_neighbours(self):
        assert matching.adjacent_nominal_sizes(8) == (6.0, 12.0)
        assert matching.adjacent_nominal_sizes(1.25) == (1.0, 1.5)

    def test_an_unlisted_size_brackets_its_gap(self):
        """The 10 in pipe the data carries sits between the 8 and the 12."""
        assert matching.adjacent_nominal_sizes(10) == (8.0, 12.0)
        assert matching.adjacent_nominal_sizes(3) == (2.0, 4.0)
        assert matching.adjacent_nominal_sizes(14) == (12.0, 16.0)

    def test_the_ends_of_the_ladder_have_one_neighbour(self):
        assert matching.adjacent_nominal_sizes(1) == (None, 1.25)
        assert matching.adjacent_nominal_sizes(48) == (42.0, None)

    def test_below_the_ladder_has_no_lower_neighbour(self):
        assert matching.adjacent_nominal_sizes(0.75) == (None, 1.0)


class TestDiameterWithinOneSize:
    """The widened rule: no nominal size strictly between the two diameters.

    For listed sizes that is exactly "one size up or down". The formulation
    matters for the uncommon sizes, where taking each value's own neighbours
    would make the rule disagree with itself depending on which side you asked
    from.
    """

    @pytest.mark.parametrize("leak,pipe", [
        (8, 8), (8, 6), (8, 12),
        (1, 1.25), (1.25, 1.5), (1.5, 2), (2, 4), (4, 6),
        (42, 48), (36, 42),
    ])
    def test_the_same_or_an_adjacent_listed_size_matches(self, leak, pipe):
        assert matching.diameter_within_one_size(leak, pipe) is True

    @pytest.mark.parametrize("leak,pipe", [
        (8, 16), (8, 4), (8, 20),
        (1, 1.5), (1, 2), (2, 6), (4, 8),
        (48, 36), (12, 20),
    ])
    def test_two_sizes_away_does_not(self, leak, pipe):
        assert matching.diameter_within_one_size(leak, pipe) is False

    def test_it_is_symmetric(self):
        """Asked either way round it gives the same answer. The reason the rule
        is written as "nothing in between" rather than as a neighbour lookup."""
        for leak, pipe in [(8, 12), (8, 16), (1, 0.75), (10, 12), (3, 6),
                           (0.5, 1), (48, 60), (14, 16)]:
            assert (matching.diameter_within_one_size(leak, pipe)
                    == matching.diameter_within_one_size(pipe, leak)), (leak, pipe)

    @pytest.mark.parametrize("leak,pipe,expected", [
        # The uncommon sizes follow the same rule.
        (10, 8, True), (10, 12, True), (10, 6, False), (10, 16, False),
        (3, 2, True), (3, 4, True), (3, 6, False), (3, 1.5, False),
        (14, 12, True), (14, 16, True), (14, 20, False),
        (0.75, 1, True), (1, 0.75, True),
    ])
    def test_an_uncommon_size_is_treated_the_same_way(self, leak, pipe, expected):
        assert matching.diameter_within_one_size(leak, pipe) is expected

    def test_two_uncommon_sizes_in_one_gap_match(self):
        """2.5 and 3.5 both sit between the 2 and the 4, so nothing listed
        separates them."""
        assert matching.diameter_within_one_size(2.5, 3.5) is True

    def test_a_float_that_is_a_hair_off_still_matches(self):
        """A diameter read from a GeoPackage and one read from a CSV do not
        compare cleanly, and 6.0 against 5.999999999999999 is the same pipe."""
        assert matching.diameter_within_one_size(6.0, 5.999999999999999) is True
        assert matching.diameters_equal(6.0, 5.9999999) is True
        assert matching.diameters_equal(6.0, 5.9) is False

    def test_a_tolerance_wide_value_does_not_skip_a_rung(self):
        """The tolerance must not let 8 reach 16 by rounding."""
        assert matching.diameter_within_one_size(8.0005, 16) is False


class TestDiameterMatchReportsHow:
    """The audit records how much slack a match took, not only that one was
    found, so a widened output can be read row by row."""

    def test_exact_is_reported_in_both_modes(self):
        for mode in matching.DIAMETER_MODES:
            assert matching.diameter_match(8, 8, mode) == \
                matching.DIAMETER_MATCH_EXACT

    def test_exact_mode_refuses_an_adjacent_size(self):
        assert matching.diameter_match(8, 12, matching.DIAMETER_EXACT) is None
        assert matching.diameter_match(8, 6, matching.DIAMETER_EXACT) is None

    def test_fuzzy_mode_names_the_direction(self):
        assert matching.diameter_match(8, 12, "fuzzy") == \
            matching.DIAMETER_MATCH_UP
        assert matching.diameter_match(8, 6, "fuzzy") == \
            matching.DIAMETER_MATCH_DOWN

    def test_fuzzy_mode_still_refuses_two_sizes_away(self):
        assert matching.diameter_match(8, 16, "fuzzy") is None

    @pytest.mark.parametrize("mode", list(matching.DIAMETER_MODES))
    def test_a_missing_diameter_never_matches_in_any_mode(self, mode):
        """Widening the rule does not give a leak with no diameter something to
        compare against."""
        assert matching.diameter_match(None, 8, mode) is None
        assert matching.diameter_match(8, None, mode) is None
        assert matching.diameter_match(None, None, mode) is None

    @pytest.mark.parametrize("mode", list(matching.DIAMETER_MODES))
    def test_a_not_a_number_never_matches(self, mode):
        assert matching.diameter_match(float("nan"), 8, mode) is None
        assert matching.diameter_match(8, float("nan"), mode) is None
        assert matching.diameter_match("", 8, mode) is None
        assert matching.diameter_match("wide", 8, mode) is None

    def test_a_numeric_string_is_read(self):
        assert matching.diameter_match("8", "12", "fuzzy") == \
            matching.DIAMETER_MATCH_UP

    def test_an_unknown_mode_is_treated_as_exact(self):
        """A typo in the environment variable must not silently widen the rule."""
        assert matching.diameter_match(8, 12, "fuzzyy") is None
        assert matching.diameter_match(8, 8, "fuzzyy") == \
            matching.DIAMETER_MATCH_EXACT

    def test_the_boolean_form_agrees_with_it(self):
        for leak, pipe, mode in [(8, 8, "exact"), (8, 12, "exact"),
                                 (8, 12, "fuzzy"), (8, 16, "fuzzy")]:
            assert (matching.diameter_matches(leak, pipe, mode)
                    is (matching.diameter_match(leak, pipe, mode) is not None))


class TestWhichCandidateWins:
    """An exact diameter beats an adjacent one at any distance, so every leak
    the strict run relocated keeps the same pipe in the widened run. Without
    that, a nearer wrong-size pipe would steal a match the strict run had made
    correctly, and the two outputs would differ in ways that have nothing to do
    with the leaks the widening was meant to rescue."""

    def candidate(self, distance, tier):
        return {"distance_ft": distance, "diameter_match": tier}

    def test_an_exact_match_wins_over_a_nearer_adjacent_one(self):
        far_exact = self.candidate(900.0, matching.DIAMETER_MATCH_EXACT)
        near_fuzzy = self.candidate(20.0, matching.DIAMETER_MATCH_UP)
        assert sorted([near_fuzzy, far_exact], key=matching.candidate_sort_key)[0] \
            is far_exact

    def test_among_exact_matches_the_nearest_wins(self):
        near = self.candidate(10.0, matching.DIAMETER_MATCH_EXACT)
        far = self.candidate(80.0, matching.DIAMETER_MATCH_EXACT)
        assert sorted([far, near], key=matching.candidate_sort_key)[0] is near

    def test_among_adjacent_matches_the_nearest_wins(self):
        near = self.candidate(10.0, matching.DIAMETER_MATCH_DOWN)
        far = self.candidate(80.0, matching.DIAMETER_MATCH_UP)
        assert sorted([far, near], key=matching.candidate_sort_key)[0] is near

    def test_up_and_down_are_ranked_the_same(self):
        """Neither direction is a better claim than the other."""
        up = self.candidate(50.0, matching.DIAMETER_MATCH_UP)
        down = self.candidate(50.0, matching.DIAMETER_MATCH_DOWN)
        assert matching.candidate_sort_key(up) == matching.candidate_sort_key(down)

    def test_with_the_preference_off_distance_alone_decides(self, monkeypatch):
        monkeypatch.setattr(config, "PREFER_EXACT_DIAMETER", False)
        far_exact = self.candidate(900.0, matching.DIAMETER_MATCH_EXACT)
        near_fuzzy = self.candidate(20.0, matching.DIAMETER_MATCH_UP)
        assert sorted([far_exact, near_fuzzy], key=matching.candidate_sort_key)[0] \
            is near_fuzzy

    def test_a_candidate_with_no_tier_is_not_treated_as_exact(self):
        """Ranking an unknown tier as exact would let it outrank a real one."""
        unknown = self.candidate(5.0, None)
        exact = self.candidate(50.0, matching.DIAMETER_MATCH_EXACT)
        assert sorted([unknown, exact], key=matching.candidate_sort_key)[0] is exact


class TestWhereEachModeWrites:
    """The two rules must not overwrite each other, or there is nothing to
    compare and no way back to the output you had."""

    def test_the_strict_rule_keeps_the_original_filename(self):
        assert config.output_gpkg_for("exact") == config.OUTPUT_GPKG

    def test_no_mode_at_all_is_the_original_filename(self):
        assert config.output_gpkg_for("") == config.OUTPUT_GPKG
        assert config.output_gpkg_for(None) == config.output_gpkg_for(
            config.DIAMETER_MATCH_MODE)

    def test_the_widened_rule_is_suffixed(self):
        path = config.output_gpkg_for("fuzzy")
        assert path != config.OUTPUT_GPKG
        assert path.name == "HistoricLeakRelocation_fuzzy_diameter.gpkg"
        assert path.parent == config.OUTPUT_GPKG.parent
        assert path.suffix == config.OUTPUT_GPKG.suffix

    def test_it_suffixes_whatever_base_it_is_given(self):
        """The map server passes its own OUTPUT_GPKG in, and the tests point it
        at a temporary folder, so the base cannot be read from config alone."""
        from pathlib import Path
        base = Path("/tmp/somewhere/Other.gpkg")
        assert config.output_gpkg_for("exact", base=base) == base
        assert config.output_gpkg_for("fuzzy", base=base) == \
            Path("/tmp/somewhere/Other_fuzzy_diameter.gpkg")

    def test_an_unknown_mode_still_gets_its_own_file(self):
        """Better a file nobody expected than the strict output overwritten by
        a run whose mode was misspelled."""
        assert config.output_gpkg_for("wider") != config.OUTPUT_GPKG


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
