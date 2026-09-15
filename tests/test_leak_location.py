"""One rule for where a leak is, used by the audit table and by the map popup.

The workflow writes it into LeakAddress and the map server into LeakLocation. If
the two ever composed it differently, the same leak would read one way in the
GeoPackage and another in the popup over its own point, which is worse than
either being wrong on its own.

The rule is one street-level part and one municipality part, best available of
each. These check the parts it picks, not just the strings it joins.
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from leakrelocation import leak_location as location


class TestTheFieldLists:
    def test_the_street_part_prefers_a_numbered_address(self):
        assert location.STREET_FIELDS[0] == "ADDRESS"

    def test_the_area_part_prefers_the_service_over_the_csv(self):
        """CITY is the municipality layer 206 holds; SuppTown is the yard-town
        code from the supplemental file, which is a location of last resort."""
        assert location.AREA_FIELDS == ("CITY", "SuppTown")

    def test_supptown_is_never_asked_of_the_service(self):
        """It is not a field on layer 206, and naming a field a layer does not
        have makes the service reject the whole query."""
        assert "SuppTown" not in location.SERVICE_FIELDS
        assert set(location.SERVICE_FIELDS) <= set(location.LOCATION_FIELDS)

    def test_every_service_field_is_on_layer_206(self):
        import json
        path = os.path.join(
            REPO_ROOT, "reference", "mapserver_json",
            "NY_DNV_Synergi_RiskResults_Assets_NY", "layer_206_Hist_GasLeak.json")
        if not os.path.isfile(path):
            return
        with open(path, encoding="utf-8") as handle:
            names = {field["name"] for field in json.load(handle)["fields"]}
        for name in location.SERVICE_FIELDS:
            assert name in names, name


class TestWhatItComposes:
    def test_a_full_address_and_a_city(self):
        assert location.from_values({
            "ADDRESS": "12 Elm St", "NEARESTXSTREET": "OAK ST",
            "CITY": "WATERTOWN", "SuppTown": "WALA-WATERTOWN",
        }) == "12 Elm St / WATERTOWN"

    def test_the_cross_street_stands_in_for_a_missing_address(self):
        assert location.from_values({
            "ADDRESS": "", "NEARESTXSTREET": "OAK ST", "CITY": "WATERTOWN",
        }) == "OAK ST / WATERTOWN"

    def test_the_town_stands_in_for_a_missing_city(self):
        assert location.from_values({
            "NEARESTXSTREET": "OAK ST", "SuppTown": "BOS-DORCHESTER",
        }) == "OAK ST / BOS-DORCHESTER"

    def test_a_municipality_alone_is_better_than_nothing(self):
        assert location.from_values({"CITY": "WATERTOWN"}) == "WATERTOWN"

    def test_a_street_alone_is_not_padded_with_a_separator(self):
        assert location.from_values({"ADDRESS": "12 Elm St"}) == "12 Elm St"

    def test_nothing_at_all_is_empty(self):
        assert location.from_values({}) == ""
        assert location.from_values({"ADDRESS": None, "CITY": ""}) == ""

    def test_a_blank_is_not_a_value(self):
        """Every cached MA row holds a blank ADDRESS. Treating it as a value
        would prefix all 98,501 of them with " / "."""
        assert location.from_values({
            "ADDRESS": "   ", "CITY": "WATERTOWN"}) == "WATERTOWN"

    def test_a_not_a_number_is_not_a_value(self):
        """A missing string in a pandas column reads back as NaN, not None."""
        assert location.from_values({
            "ADDRESS": float("nan"), "CITY": "WATERTOWN"}) == "WATERTOWN"

    def test_the_same_place_is_not_said_twice(self):
        assert location.from_values({
            "NEARESTXSTREET": "WATERTOWN", "CITY": "watertown"}) == "WATERTOWN"

    def test_the_name_is_matched_whatever_its_case(self):
        """The service answers with its own spelling and a cache carries it."""
        assert location.from_values({
            "address": "12 Elm St", "city": "WATERTOWN"}) == "12 Elm St / WATERTOWN"

    def test_a_field_it_does_not_know_is_ignored(self):
        assert location.from_values({
            "COMMENTS": "near the hydrant", "CITY": "WATERTOWN"}) == "WATERTOWN"

    def test_a_number_is_still_a_value(self):
        """Nothing here should assume the column is text."""
        assert location.from_values({"CITY": 12345}) == "12345"


class TestTheParts:
    def test_best_takes_the_first_with_a_value(self):
        assert location.best(["", None, "OAK ST", "ELM ST"]) == "OAK ST"

    def test_best_of_nothing_is_empty(self):
        assert location.best([]) == ""
        assert location.best([None, "", "  "]) == ""

    def test_compose_skips_an_empty_part(self):
        assert location.compose("OAK ST", "") == "OAK ST"
        assert location.compose("", "WATERTOWN") == "WATERTOWN"
        assert location.compose("", "") == ""

    def test_text_trims(self):
        assert location.text("  OAK ST  ") == "OAK ST"

    def test_text_of_a_non_value_is_empty(self):
        assert location.text(None) == ""
        assert location.text(float("nan")) == ""


class TestTheThingsPandasHandsIt:
    """A cache column is read back through pandas, which does not hand back what
    was put in: a missing string arrives as a float NaN whose str() is "nan"."""

    def test_a_missing_string_does_not_become_the_word_nan(self):
        assert location.text(float("nan")) == ""
        assert location.from_values({"ADDRESS": float("nan")}) == ""

    def test_a_real_value_named_like_one_is_untouched(self):
        """Guards the test above against being implemented as a string check."""
        assert location.text("Nanuet") == "Nanuet"
