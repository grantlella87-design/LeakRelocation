"""The numbers behind the relocation-distance dashboard.

The dashboard's whole claim is that a reader can put the line anywhere and be
told exactly how many relocations fall either side of it. That claim rests on
the cumulative curve, so these check it against counts done the slow, obvious
way, and check that the things which are not distances - an unmatched leak, a
null, a NaN - are never counted as a move of zero feet. Reporting an unmatched
leak as a perfect snap would be the worst failure this file can have.
"""
import math
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

pytest.importorskip("pandas")

import pandas as pd
from leakrelocation import distance_report as dr


def audit_frame(rows):
    """An audit-shaped frame from a short list of dicts."""
    base = {
        "LeakKey": "1", "LeakAddress": "", "LeakMaterial": "Cast Iron",
        "PipeMaterial": "Cast Iron", "LeakDiameter": 6.0, "PipeDiameter": 6.0,
        "FacilityType": "Main", "LinkedLayer": "distribution",
        "SearchRadiusFt": 100.0, "DistanceFt": 0.0, "MatchStatus": "Matched",
        "NoMatchReason": "", "DateCheck": "ok", "LeakDate": "",
        "RunUTC": "2026-09-16T04:12:55Z",
    }
    return pd.DataFrame([{**base, **row} for row in rows])


def matched(distances, **extra):
    return audit_frame([{"DistanceFt": d, "LeakKey": str(i), **extra}
                        for i, d in enumerate(distances)])


class TestTheCumulativeCurve:
    """Everything the slider reports is a lookup into this."""

    def test_it_counts_values_at_or_below_each_edge(self):
        assert dr.cumulative([1.0, 2.0, 3.0], [0.0, 1.0, 2.0, 3.0]) == [0, 1, 2, 3]

    def test_a_value_exactly_on_an_edge_is_inside_it(self):
        """"Within 100 ft" has to include a leak that moved exactly 100 ft, or
        the two sides of the slider do not add up to the whole."""
        assert dr.cumulative([100.0], [99.9, 100.0, 100.1]) == [0, 1, 1]

    def test_it_never_decreases(self):
        values = [0.0, 0.4, 5.0, 5.0, 99.9, 2999.0]
        edges = dr.curve_edges(3000.0)
        running = dr.cumulative(values, edges)
        assert running == sorted(running)
        assert running[-1] == len(values)

    def test_it_agrees_with_counting_by_hand(self):
        values = [0.0, 0.05, 0.9, 1.0, 17.4, 99.99, 100.0, 512.5, 2999.9]
        edges = dr.curve_edges(3000.0)
        running = dr.cumulative(values, edges)
        for index, edge in enumerate(edges):
            slow = sum(1 for value in values if value <= edge)
            assert running[index] == slow, edge

    def test_nothing_is_all_zeroes_not_an_error(self):
        edges = dr.curve_edges(100.0)
        assert dr.cumulative([], edges) == [0] * len(edges)

    def test_the_histogram_sums_back_to_the_cumulative(self):
        """The chart re-bins by subtracting cumulative counts, so a histogram
        that did not sum back would draw a different distribution from the one
        the slider is reporting on."""
        values = [0.0, 0.3, 4.9, 12.0, 12.0, 240.0, 1500.0]
        edges = dr.curve_edges(3000.0)
        bars = dr.histogram(values, edges)
        assert sum(bars) == len(values)
        running = dr.cumulative(values, edges)
        for index in range(len(edges)):
            assert sum(bars[:index + 1]) == running[index]


class TestTheBinEdges:
    def test_fine_near_zero(self):
        """Production distances include 0.036 ft and 16.1 ft, so a 1 ft grid
        would put a third of the data in one bin."""
        edges = dr.curve_edges(3000.0)
        assert edges[0] == 0.0
        assert edges[1] == pytest.approx(0.1)
        assert 0.5 in edges

    def test_coarse_in_the_tail(self):
        edges = dr.curve_edges(3000.0)
        beyond = [edge for edge in edges if edge > dr.FINE_LIMIT_FT]
        assert beyond[1] - beyond[0] == pytest.approx(dr.COARSE_STEP_FT)

    def test_it_reaches_the_maximum(self):
        assert dr.curve_edges(3000.0)[-1] >= 3000.0

    def test_it_is_sorted_and_unique(self):
        edges = dr.curve_edges(3000.0)
        assert edges == sorted(edges)
        assert len(set(edges)) == len(edges)

    def test_a_small_maximum_does_not_produce_an_empty_list(self):
        for top in (0.0, 0.05, 1.0):
            assert dr.curve_edges(top), top


class TestPercentiles:
    def test_the_median_of_an_odd_count(self):
        assert dr.percentile([1.0, 2.0, 3.0], 0.5) == 2.0

    def test_the_median_of_an_even_count_interpolates(self):
        assert dr.percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5

    def test_the_ends(self):
        ordered = [5.0, 10.0, 15.0]
        assert dr.percentile(ordered, 0.0) == 5.0
        assert dr.percentile(ordered, 1.0) == 15.0

    def test_one_value(self):
        assert dr.percentile([7.0], 0.9) == 7.0

    def test_nothing(self):
        assert dr.percentile([], 0.5) is None


class TestWhatCountsAsADistance:
    """An unmatched leak has no distance. Counting its blank as 0.0 would report
    it as having landed exactly on its pipe, which is the opposite of the truth.
    """

    def test_none_and_nan_are_not_distances(self):
        assert dr.numbers(pd.Series([1.0, None, float("nan"), 2.0])) == [1.0, 2.0]

    def test_text_is_not_a_distance(self):
        assert dr.numbers(pd.Series([1.0, "", "x", 3.0], dtype=object)) == [1.0, 3.0]

    def test_infinity_is_not_a_distance(self):
        assert dr.numbers(pd.Series([float("inf"), 4.0])) == [4.0]

    def test_a_numeric_string_is_read(self):
        """A GeoPackage column can come back as object dtype."""
        assert dr.numbers(pd.Series(["2.5", "1.5"], dtype=object)) == [1.5, 2.5]

    def test_zero_is_a_distance(self):
        assert dr.numbers(pd.Series([0.0, 0.0])) == [0.0, 0.0]

    def test_no_column_is_empty(self):
        assert dr.numbers(None) == []


class TestWhichRowsWereRelocated:
    def test_match_status_decides(self):
        frame = audit_frame([
            {"MatchStatus": "Matched", "DistanceFt": 5.0},
            {"MatchStatus": "NoMatch", "DistanceFt": None},
        ])
        assert len(dr.matched_rows(frame)) == 1

    def test_the_status_is_read_loosely(self):
        """Case and stray spaces must not silently halve the numbers."""
        frame = audit_frame([{"MatchStatus": " matched "}, {"MatchStatus": "MATCHED"}])
        assert len(dr.matched_rows(frame)) == 2

    def test_without_the_column_a_distance_is_enough(self):
        frame = matched([1.0, 2.0]).drop(columns=["MatchStatus"])
        assert len(dr.matched_rows(frame)) == 2


class TestTheReport:
    def test_the_thresholds_are_exact_and_add_up(self):
        report = dr.build_report(matched([0.5, 4.0, 9.0, 40.0, 300.0, 1200.0]))
        for row in report["thresholds"]:
            assert row["at_or_under"] + row["over"] == 6, row["ft"]
        by_ft = {row["ft"]: row["at_or_under"] for row in report["thresholds"]}
        assert by_ft[1.0] == 1
        assert by_ft[5.0] == 2
        assert by_ft[10.0] == 3
        assert by_ft[50.0] == 4
        assert by_ft[500.0] == 5
        assert by_ft[1000.0] == 5

    def test_unmatched_leaks_are_audited_but_not_measured(self):
        frame = audit_frame([
            {"MatchStatus": "Matched", "DistanceFt": 10.0},
            {"MatchStatus": "NoMatch", "DistanceFt": None,
             "NoMatchReason": "no_pipe_within_max_radius"},
            {"MatchStatus": "NoMatch", "DistanceFt": None,
             "NoMatchReason": "no_material_match"},
        ])
        report = dr.build_report(frame)
        assert report["totals"]["audited"] == 3
        assert report["totals"]["relocated"] == 1
        assert report["totals"]["unmatched"] == 2
        assert report["distance"]["median"] == 10.0
        assert {row["name"] for row in report["no_match_reasons"]} == {
            "no_pipe_within_max_radius", "no_material_match"}

    def test_the_curve_total_equals_the_relocated_count(self):
        report = dr.build_report(matched([1.0, 2.0, 3.0, 4.0]))
        series = report["curve"]["series"]["All relocations"]
        assert series[-1] == report["totals"]["relocated"] == 4

    def test_there_is_a_curve_per_pipe_layer(self):
        frame = audit_frame([
            {"LinkedLayer": "distribution", "DistanceFt": 5.0},
            {"LinkedLayer": "distribution", "DistanceFt": 15.0},
            {"LinkedLayer": "service", "DistanceFt": 25.0},
        ])
        report = dr.build_report(frame)
        series = report["curve"]["series"]
        assert set(series) == {"All relocations", "distribution", "service"}
        assert series["distribution"][-1] == 2
        assert series["service"][-1] == 1

    def test_the_layer_curves_sum_to_the_whole(self):
        frame = audit_frame([
            {"LinkedLayer": "distribution", "DistanceFt": 5.0},
            {"LinkedLayer": "service", "DistanceFt": 5.0},
            {"LinkedLayer": "service", "DistanceFt": 500.0},
        ])
        report = dr.build_report(frame)
        series = report["curve"]["series"]
        for index in range(len(report["curve"]["edges"])):
            assert (series["distribution"][index] + series["service"][index]
                    == series["All relocations"][index])

    def test_already_on_the_pipe_is_counted(self):
        report = dr.build_report(matched([0.0, 0.5, 1.0, 1.5, 90.0]))
        assert report["distance"]["on_pipe"] == 3

    def test_a_matched_row_with_no_distance_is_warned_about(self):
        frame = audit_frame([
            {"MatchStatus": "Matched", "DistanceFt": 5.0},
            {"MatchStatus": "Matched", "DistanceFt": None},
        ])
        report = dr.build_report(frame)
        assert report["totals"]["relocated"] == 1
        assert any("no usable" in text for text in report["warnings"])

    def test_an_empty_audit_says_so_instead_of_dividing_by_zero(self):
        report = dr.build_report(audit_frame([]).assign(DistanceFt=None))
        assert report["totals"]["relocated"] == 0
        assert report["distance"]["median"] is None
        assert any("No row" in text for text in report["warnings"])

    def test_no_distance_column_is_a_clear_failure(self):
        frame = matched([1.0]).drop(columns=["DistanceFt"])
        with pytest.raises(KeyError, match="DistanceFt"):
            dr.build_report(frame)

    def test_the_curve_reaches_the_configured_maximum_radius(self):
        """The slider should span the whole searchable range even when this run
        happened not to relocate anything that far."""
        report = dr.build_report(matched([1.0, 2.0]), max_radius_ft=3000.0)
        assert report["curve"]["edges"][-1] >= 3000.0

    def test_every_number_survives_json(self):
        """The page embeds this with allow_nan=False, because NaN is not JSON and
        a page whose data block will not parse is a blank page."""
        import json
        frame = audit_frame([
            {"DistanceFt": 5.0, "LeakDiameter": float("nan"),
             "PipeDiameter": None, "SearchRadiusFt": float("nan")},
            {"MatchStatus": "NoMatch", "DistanceFt": None},
        ])
        json.dumps(dr.build_report(frame), allow_nan=False)


class TestGrouping:
    def test_statistics_per_group_biggest_first(self):
        frame = audit_frame([
            {"LinkedLayer": "service", "DistanceFt": 1.0},
            {"LinkedLayer": "distribution", "DistanceFt": 10.0},
            {"LinkedLayer": "distribution", "DistanceFt": 30.0},
        ])
        rows = dr.grouped(frame, "LinkedLayer")
        assert [row["name"] for row in rows] == ["distribution", "service"]
        assert rows[0]["count"] == 2
        assert rows[0]["median"] == 20.0
        assert rows[0]["max"] == 30.0

    def test_a_blank_group_is_one_bucket(self):
        frame = audit_frame([
            {"LeakMaterial": "", "DistanceFt": 1.0},
            {"LeakMaterial": None, "DistanceFt": 2.0},
            {"LeakMaterial": float("nan"), "DistanceFt": 3.0},
        ])
        rows = dr.grouped(frame, "LeakMaterial")
        assert len(rows) == 1
        assert rows[0]["name"] == "(blank)"
        assert rows[0]["count"] == 3

    def test_a_missing_column_is_empty_not_an_error(self):
        assert dr.grouped(matched([1.0]), "NoSuchColumn") == []
        assert dr.counted(matched([1.0]), "NoSuchColumn") == []

    def test_rows_without_a_distance_do_not_join_a_group(self):
        frame = audit_frame([
            {"LinkedLayer": "service", "DistanceFt": None},
            {"LinkedLayer": "service", "DistanceFt": 4.0},
        ])
        assert dr.grouped(frame, "LinkedLayer")[0]["count"] == 1


class TestTheSearchPasses:
    """Thirty distinct radii is thirty table rows, twenty of them a fraction of
    a percent, so they are bucketed by how far the search had to reach."""

    def test_the_first_pass_stands_alone(self):
        frame = audit_frame([
            {"SearchRadiusFt": 100.0, "DistanceFt": 40.0},
            {"SearchRadiusFt": 200.0, "DistanceFt": 150.0},
        ])
        rows = {row["name"]: row for row in dr.radius_passes(frame)}
        assert rows["100 ft (first pass)"]["count"] == 1
        assert rows["200 to 500 ft"]["count"] == 1

    def test_the_tail_is_gathered(self):
        frame = audit_frame([{"SearchRadiusFt": r, "DistanceFt": r - 10}
                             for r in (2100.0, 2500.0, 3000.0)])
        rows = {row["name"]: row for row in dr.radius_passes(frame)}
        assert rows["Beyond 2,000 ft"]["count"] == 3

    def test_an_empty_bucket_is_left_out(self):
        frame = audit_frame([{"SearchRadiusFt": 100.0, "DistanceFt": 1.0}])
        assert [row["name"] for row in dr.radius_passes(frame)] == \
            ["100 ft (first pass)"]

    def test_every_relocation_lands_in_exactly_one_bucket(self):
        radii = [100.0, 200.0, 500.0, 600.0, 1000.0, 1100.0, 2000.0, 2100.0, 3000.0]
        frame = audit_frame([{"SearchRadiusFt": r, "DistanceFt": 1.0} for r in radii])
        rows = dr.radius_passes(frame)
        assert sum(row["count"] for row in rows) == len(radii)

    def test_a_missing_radius_is_reported_not_dropped(self):
        frame = audit_frame([
            {"SearchRadiusFt": None, "DistanceFt": 5.0},
            {"SearchRadiusFt": 100.0, "DistanceFt": 1.0},
        ])
        rows = {row["name"]: row for row in dr.radius_passes(frame)}
        assert rows["Not recorded"]["count"] == 1
        assert sum(row["count"] for row in rows.values()) == 2


class TestTheReviewQueue:
    def test_the_furthest_come_first(self):
        report = dr.build_report(matched([5.0, 2000.0, 100.0, 1.0]))
        assert [row["DistanceFt"] for row in report["furthest"]] == \
            [2000.0, 100.0, 5.0, 1.0]

    def test_it_is_capped(self):
        report = dr.build_report(matched([float(i) for i in range(400)]))
        assert len(report["furthest"]) == dr.FURTHEST_ROWS

    def test_rows_without_a_distance_are_not_listed(self):
        frame = audit_frame([
            {"MatchStatus": "Matched", "DistanceFt": None, "LeakKey": "blank"},
            {"MatchStatus": "Matched", "DistanceFt": 9.0, "LeakKey": "real"},
        ])
        keys = [row["LeakKey"] for row in dr.furthest(dr.matched_rows(frame))]
        assert keys == ["real"]

    def test_a_nan_becomes_null_not_the_word_nan(self):
        """str(float("nan")) is "nan", which would print in a table cell."""
        frame = matched([5.0], LeakDiameter=float("nan"))
        row = dr.furthest(frame)[0]
        assert row["LeakDiameter"] is None


class TestMaterialAgreement:
    def test_exact_and_inexact_are_separated(self):
        frame = audit_frame([
            {"LeakMaterial": "Cast Iron", "PipeMaterial": "Cast Iron"},
            {"LeakMaterial": "Cast Iron", "PipeMaterial": "Plastic PE"},
            {"LeakMaterial": "", "PipeMaterial": "Plastic PE"},
        ])
        assert dr.material_agreement(frame) == {
            "exact": 1, "family_or_other": 1, "unknown": 1}

    def test_case_is_not_a_difference(self):
        frame = audit_frame([{"LeakMaterial": "cast iron", "PipeMaterial": "Cast Iron"}])
        assert dr.material_agreement(frame)["exact"] == 1

    def test_without_the_columns_it_is_not_claimed(self):
        frame = matched([1.0]).drop(columns=["PipeMaterial"])
        assert dr.material_agreement(frame) is None


class TestAgainstAProductionShapedFrame:
    """One frame the size and shape of a real run, checked end to end. The unit
    tests above use a handful of rows; this is the only place the arithmetic is
    exercised at the scale it actually runs at."""

    @staticmethod
    @pytest.fixture(scope="class")
    def report():
        import random
        random.seed(11)
        rows = []
        for index in range(20_000):
            roll = random.random()
            if roll < 0.93:
                distance = abs(random.gauss(0, 14))
            elif roll < 0.99:
                distance = random.uniform(40, 300)
            else:
                distance = random.uniform(300, 2980)
            rows.append({
                "DistanceFt": distance,
                "LeakKey": str(index),
                "LinkedLayer": "distribution" if index % 3 else "service",
                "SearchRadiusFt": math.ceil(max(distance, 1) / 100.0) * 100.0,
            })
        for index in range(400):
            rows.append({"MatchStatus": "NoMatch", "DistanceFt": None,
                         "LeakKey": f"n{index}",
                         "NoMatchReason": "no_pipe_within_max_radius"})
        return dr.build_report(audit_frame(rows), max_radius_ft=3000.0)

    def test_the_totals_are_right(self, report):
        assert report["totals"]["audited"] == 20_400
        assert report["totals"]["relocated"] == 20_000
        assert report["totals"]["unmatched"] == 400

    def test_percentiles_are_ordered(self, report):
        values = [report["distance"]["percentiles"][f"p{p}"] for p in dr.PERCENTILES]
        assert values == sorted(values)
        assert report["distance"]["median"] <= report["distance"]["percentiles"]["p90"]
        assert report["distance"]["percentiles"]["p99"] <= report["distance"]["max"]

    def test_the_curve_is_monotonic_and_complete(self, report):
        series = report["curve"]["series"]["All relocations"]
        assert series == sorted(series)
        assert series[0] >= 0
        assert series[-1] == 20_000

    def test_the_curve_agrees_with_the_fixed_thresholds(self, report):
        """The slider reads the curve and the table is computed separately, so
        the two disagreeing is exactly the bug a reader would never catch."""
        edges = report["curve"]["edges"]
        series = report["curve"]["series"]["All relocations"]
        for row in report["thresholds"]:
            index = max(i for i, edge in enumerate(edges) if edge <= row["ft"])
            assert series[index] == row["at_or_under"], row["ft"]

    def test_every_search_pass_bucket_is_accounted_for(self, report):
        assert sum(row["count"] for row in report["by_radius"]) == 20_000


def mode_frame(rows, mode):
    """An audit frame tagged with the diameter rule that produced it."""
    return audit_frame([{**row, "DiameterMode": mode} for row in rows])


def relocated(oid, distance, leak_d=8.0, pipe_d=8.0, tier="exact",
              pipe_oid=None, layer="distribution"):
    return {"LeakOID": oid, "LeakKey": str(oid), "DistanceFt": distance,
            "LeakDiameter": leak_d, "PipeDiameter": pipe_d,
            "DiameterMatch": tier, "MatchStatus": "Matched",
            "LinkedLayer": layer,
            "MatchedPipeOID": pipe_oid if pipe_oid is not None else 900000 + oid}


def unrelocated(oid, reason="no_pipe_within_max_radius"):
    return {"LeakOID": oid, "LeakKey": str(oid), "DistanceFt": None,
            "LeakDiameter": 8.0, "PipeDiameter": None, "DiameterMatch": "",
            "MatchStatus": "NoMatch", "NoMatchReason": reason,
            "MatchedPipeOID": None}


class TestWhichRuleWroteThisOutput:
    def test_it_is_read_off_the_rows(self):
        assert dr.diameter_mode_of(mode_frame([relocated(1, 5.0)], "fuzzy")) == "fuzzy"

    def test_an_older_output_without_the_column_says_nothing(self):
        frame = matched([5.0])
        assert dr.diameter_mode_of(frame) is None

    def test_a_mixed_output_is_reported_as_mixed(self):
        """Two rules in one file should not read as either of them."""
        frame = audit_frame([{**relocated(1, 5.0), "DiameterMode": "exact"},
                             {**relocated(2, 5.0), "DiameterMode": "fuzzy"}])
        assert dr.diameter_mode_of(frame) == "exact/fuzzy"


class TestComparingTheTwoRules:
    """The whole reason both outputs exist. What matters is the difference, and
    that the difference reads the same whichever page the reader has open."""

    def exact_and_fuzzy(self):
        strict = mode_frame([relocated(1, 10.0), relocated(2, 20.0),
                             unrelocated(3), unrelocated(4)], "exact")
        wide = mode_frame([relocated(1, 10.0), relocated(2, 20.0),
                           relocated(3, 300.0, 8.0, 12.0, "one_size_up"),
                           unrelocated(4)], "fuzzy")
        return strict, wide

    def test_the_widened_rule_gains_the_leak_it_rescued(self):
        strict, wide = self.exact_and_fuzzy()
        result = dr.compare_outputs(strict, wide, "exact", "fuzzy")
        assert result["usable"]
        assert result["gained"] == 1
        assert result["lost"] == 0
        assert result["moved_to_another_pipe"] == 0
        assert result["base_relocated"] == 2
        assert result["other_relocated"] == 3

    def test_the_gained_leaks_are_described(self):
        strict, wide = self.exact_and_fuzzy()
        result = dr.compare_outputs(strict, wide, "exact", "fuzzy")
        assert result["gained_distance"]["count"] == 1
        assert result["gained_distance"]["median"] == 300.0
        assert result["gained_by_tier"] == [{"name": "one_size_up", "count": 1}]
        assert result["gained_by_layer"] == [{"name": "distribution", "count": 1}]
        assert result["gained_by_size_step"][0]["count"] == 1
        assert "8" in result["gained_by_size_step"][0]["name"]
        assert "12" in result["gained_by_size_step"][0]["name"]

    def test_the_direction_does_not_depend_on_which_output_is_open(self):
        """The bug this exists to stop: read from the fuzzy page, the 1,104
        leaks the widened rule added were reported as Lost, which looks like an
        alarm when nothing is wrong."""
        strict, wide = self.exact_and_fuzzy()
        from_strict = dr.build_report(strict, compare_audit=wide)["comparison"]
        from_wide = dr.build_report(wide, compare_audit=strict)["comparison"]
        for field in ("gained", "lost", "moved_to_another_pipe",
                      "base_label", "other_label", "base_relocated",
                      "other_relocated"):
            assert from_strict[field] == from_wide[field], field
        assert from_strict["gained"] == 1
        assert from_strict["base_label"] == "exact"
        assert from_strict["other_label"] == "fuzzy"

    def test_neither_page_warns_when_nothing_was_lost(self):
        strict, wide = self.exact_and_fuzzy()
        for base, other in ((strict, wide), (wide, strict)):
            report = dr.build_report(base, compare_audit=other)
            assert not [text for text in report["warnings"] if "not under" in text]

    def test_a_lost_relocation_is_warned_about(self):
        """Widening should only add. If it removed one, that is the finding."""
        strict = mode_frame([relocated(1, 10.0), relocated(2, 20.0)], "exact")
        wide = mode_frame([relocated(1, 10.0), unrelocated(2)], "fuzzy")
        report = dr.build_report(strict, compare_audit=wide)
        assert report["comparison"]["lost"] == 1
        assert any("never remove them" in text for text in report["warnings"])

    def test_a_changed_pipe_is_counted_and_warned_about(self):
        strict = mode_frame([relocated(1, 90.0, pipe_oid=111)], "exact")
        wide = mode_frame([relocated(1, 9.0, 8.0, 6.0, "one_size_down",
                                     pipe_oid=222)], "fuzzy")
        report = dr.build_report(strict, compare_audit=wide)
        comparison = report["comparison"]
        assert comparison["moved_to_another_pipe"] == 1
        assert comparison["unchanged"] == 0
        assert comparison["moved_examples"][0]["from_pipe"] == 111
        assert comparison["moved_examples"][0]["to_pipe"] == 222
        assert any("should be zero" in text for text in report["warnings"])

    def test_identical_outputs_gain_nothing(self):
        strict = mode_frame([relocated(1, 10.0)], "exact")
        result = dr.compare_outputs(strict, strict.copy(), "exact", "fuzzy")
        assert result["gained"] == 0 and result["lost"] == 0
        assert result["unchanged"] == 1

    def test_it_pairs_on_the_leak_id_not_the_leak_number(self):
        """25,733 rows of the supplemental file share a leak number, so joining
        on the number would pair different leaks with each other."""
        strict, wide = self.exact_and_fuzzy()
        assert dr.compare_outputs(strict, wide)["key"] == "LeakOID"
        assert dr.COMPARE_KEYS[0] == "LeakOID"

    def test_it_falls_back_through_the_identity_columns(self):
        strict, wide = self.exact_and_fuzzy()
        strict = strict.drop(columns=["LeakOID"])
        wide = wide.drop(columns=["LeakOID"])
        assert dr.compare_outputs(strict, wide)["key"] == "LeakGlobalID" \
            or dr.compare_outputs(strict, wide)["key"] == "LeakKey"

    def test_outputs_with_no_common_identity_say_so(self):
        strict, wide = self.exact_and_fuzzy()
        stripped = wide.drop(columns=[c for c in dr.COMPARE_KEYS
                                      if c in wide.columns])
        result = dr.compare_outputs(strict, stripped)
        assert result["usable"] is False
        assert "cannot be paired" in result["why"]

    def test_no_comparison_frame_means_no_comparison_block(self):
        assert dr.build_report(matched([5.0]))["comparison"] is None

    def test_an_empty_comparison_frame_is_not_compared_against(self):
        """An output that exists but holds nothing would otherwise read as
        every relocation having been lost."""
        import pandas as pd
        empty = pd.DataFrame(columns=list(matched([5.0]).columns))
        report = dr.build_report(mode_frame([relocated(1, 5.0)], "exact"),
                                 compare_audit=empty)
        assert report["comparison"] is None

    def test_the_comparison_survives_json(self):
        import json
        strict, wide = self.exact_and_fuzzy()
        json.dumps(dr.build_report(strict, compare_audit=wide), allow_nan=False)


class TestTheDiameterBreakdown:
    def test_the_tiers_are_counted(self):
        frame = mode_frame([
            relocated(1, 5.0), relocated(2, 6.0),
            relocated(3, 40.0, 8.0, 12.0, "one_size_up"),
            relocated(4, 50.0, 8.0, 6.0, "one_size_down"),
        ], "fuzzy")
        counts = {row["name"]: row["count"]
                  for row in dr.build_report(frame)["diameter_match"]}
        assert counts == {"exact": 2, "one_size_up": 1, "one_size_down": 1}

    def test_the_tiers_carry_distance_statistics(self):
        frame = mode_frame([
            relocated(1, 5.0),
            relocated(2, 100.0, 8.0, 12.0, "one_size_up"),
            relocated(3, 200.0, 8.0, 12.0, "one_size_up"),
        ], "fuzzy")
        rows = {row["name"]: row for row in dr.build_report(frame)["by_diameter_match"]}
        assert rows["one_size_up"]["count"] == 2
        assert rows["one_size_up"]["median"] == 150.0

    def test_an_older_output_has_an_empty_breakdown(self):
        assert dr.build_report(matched([5.0]))["diameter_match"] == []


class TestPipeIdentityAcrossOutputs:
    """A pipe OID from an output whose column holds a null arrives as a float,
    and one from a column of whole numbers arrives as an int. Comparing their
    text forms reported every unchanged relocation as having moved to another
    pipe - 90,987 false findings on a real pair of outputs, and the one figure
    on the page that is meant to mean something is wrong."""

    def test_the_same_pipe_across_dtypes_is_the_same_pipe(self):
        assert dr.same_identity(900.0, 900) is True
        assert dr.same_identity(900, 900.0) is True

    def test_different_pipes_are_different(self):
        assert dr.same_identity(900.0, 901) is False

    def test_text_identities_still_compare(self):
        assert dr.same_identity("{ABC}", "{ABC}") is True
        assert dr.same_identity("{ABC}", "{DEF}") is False

    def test_a_null_is_not_a_pipe(self):
        assert dr.same_identity(None, 900) is False
        assert dr.same_identity(None, None) is True

    def test_an_unchanged_pair_reads_as_unchanged_across_dtypes(self):
        """The end-to-end form: the strict output carries unmatched rows, which
        makes its MatchedPipeOID column a float column."""
        strict = mode_frame([relocated(1, 5.0, pipe_oid=900), unrelocated(2)],
                            "exact")
        wide = mode_frame([relocated(1, 5.0, pipe_oid=900),
                           relocated(2, 60.0, 8.0, 12.0, "one_size_up",
                                     pipe_oid=700)], "fuzzy")
        assert strict["MatchedPipeOID"].dtype != wide["MatchedPipeOID"].dtype
        result = dr.compare_outputs(strict, wide, "exact", "fuzzy")
        assert result["moved_to_another_pipe"] == 0
        assert result["unchanged"] == 1
        assert result["gained"] == 1
