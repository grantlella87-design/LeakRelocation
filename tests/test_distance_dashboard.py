"""The dashboard page: it has to parse, and it has to hold what its JS reads.

The page is generated as a Python string, which is the same arrangement that has
twice shipped a syntax error in the map page's JavaScript - both times every
unit test passed and only the browser caught it, as "Unexpected token '}'" with
the whole pane dead. So these check structure rather than appearance: the data
block parses as JSON, every element the script looks up by id exists, and a
value out of the GeoPackage cannot close a tag or a script.
"""
import json
import os
import re
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

pytest.importorskip("pandas")

import pandas as pd
from leakrelocation import distance_dashboard as dash
from leakrelocation import distance_report as dr


def report_for(rows):
    base = {
        "LeakKey": "1", "LeakAddress": "", "LeakMaterial": "Cast Iron",
        "PipeMaterial": "Cast Iron", "LeakDiameter": 6.0, "PipeDiameter": 6.0,
        "FacilityType": "Main", "LinkedLayer": "distribution",
        "SearchRadiusFt": 100.0, "DistanceFt": 0.0, "MatchStatus": "Matched",
        "NoMatchReason": "", "DateCheck": "ok", "LeakDate": "",
        "RunUTC": "2026-09-16T04:12:55Z",
    }
    frame = pd.DataFrame([{**base, **row} for row in rows])
    return dr.build_report(frame, source="C:\\out\\Test.gpkg", max_radius_ft=3000.0)


@pytest.fixture(scope="module")
def report():
    rows = [{"DistanceFt": d, "LeakKey": str(i),
             "LinkedLayer": "distribution" if i % 2 else "service"}
            for i, d in enumerate([0.0, 0.5, 4.0, 17.0, 60.0, 140.0, 900.0, 2900.0])]
    rows.append({"MatchStatus": "NoMatch", "DistanceFt": None,
                 "NoMatchReason": "no_pipe_within_max_radius"})
    frame = pd.DataFrame([{
        "LeakKey": "1", "LeakAddress": "", "LeakMaterial": "Cast Iron",
        "PipeMaterial": "Cast Iron", "LeakDiameter": 6.0, "PipeDiameter": 6.0,
        "FacilityType": "Main", "LinkedLayer": "distribution",
        "SearchRadiusFt": 100.0, "DistanceFt": 0.0, "MatchStatus": "Matched",
        "NoMatchReason": "", "DateCheck": "ok", "LeakDate": "",
        "RunUTC": "2026-09-16T04:12:55Z", **row} for row in rows])
    return dr.build_report(frame, source="C:\\out\\Test.gpkg", max_radius_ft=3000.0)


@pytest.fixture(scope="module")
def page(report):
    return dash.dashboard_html(report)


def data_block(page_html):
    """The JSON the page embeds, parsed."""
    match = re.search(r"window\.REPORT = (\{.*?\});</script>", page_html, re.S)
    assert match, "the page has no window.REPORT data block"
    return json.loads(match.group(1).replace("<\\/", "</"))


class TestThePageIsWellFormed:
    def test_it_is_a_complete_document(self, page):
        assert page.lstrip().startswith("<!doctype html>")
        assert page.rstrip().endswith("</html>")
        assert page.count("<body>") == page.count("</body>") == 1

    def test_the_data_block_parses(self, page):
        assert data_block(page)["totals"]["relocated"] == 8

    def test_the_data_block_cannot_close_the_script_early(self):
        """A GeoPackage value containing </script> would end the block and drop
        the rest of the page into the document as text."""
        built = dash.dashboard_html(report_for([
            {"DistanceFt": 5.0, "LeakMaterial": "</script><h1>oops</h1>"}]))
        assert "</script><h1>oops" not in built
        assert data_block(built)

    def test_the_script_tags_balance(self, page):
        assert page.count("<script") == page.count("</script>") == 2

    def test_no_nan_reaches_the_page(self, page):
        """NaN is not JSON. JSON.parse would throw and the page would be blank."""
        assert "NaN" not in data_block(page).__repr__()
        built = dash.dashboard_html(report_for([
            {"DistanceFt": 5.0, "LeakDiameter": float("nan"),
             "SearchRadiusFt": float("nan")}]))
        assert data_block(built)


class TestTheScriptFindsWhatItLooksFor:
    """The page and the script are written in different places in one module, so
    this is the join that can silently come apart."""

    def test_every_id_the_script_reads_is_in_the_page(self, page):
        wanted = set(re.findall(r'getElementById\("([^"]+)"\)', dash.PAGE_JS))
        assert wanted, "no getElementById calls found - has the script changed?"
        for name in sorted(wanted):
            assert f'id="{name}"' in page, name

    def test_every_selector_the_script_queries_matches_something(self, page):
        """querySelectorAll over a class that no longer exists fails silently:
        the loop runs zero times and a control stops working."""
        for selector in (".presets button", ".toggle button"):
            klass = selector.split()[0].lstrip(".")
            assert f'class="{klass}"' in page, selector

    def test_the_view_toggles_in_the_page_are_the_ones_the_script_knows(self, page):
        in_page = set(re.findall(r'data-view="([^"]+)"', page))
        in_script = set(re.findall(r'view === "([^"]+)"', dash.PAGE_JS))
        # "p99" is the script's default and is not compared against, so it is
        # allowed in the page without appearing in a comparison.
        assert in_script <= in_page, in_script - in_page
        assert in_page == {"p99", "pass", "full"}

    def test_the_preset_values_are_the_reported_thresholds(self, page, report):
        in_page = {float(v) for v in re.findall(r'data-ft="([^"]+)"', page)}
        assert in_page == {row["ft"] for row in report["thresholds"]}

    def test_the_series_name_the_script_keys_on_is_the_one_produced(self, report):
        assert "All relocations" in dash.PAGE_JS
        assert "All relocations" in report["curve"]["series"]

    def test_the_braces_in_the_script_balance(self, page):
        """The specific failure that has happened twice on the map page."""
        for block in re.findall(r"<script>(.*?)</script>", page, re.S):
            if block.startswith("window.REPORT"):
                continue
            depth = 0
            lowest = 0
            for character in block:
                if character == "{":
                    depth += 1
                elif character == "}":
                    depth -= 1
                    lowest = min(lowest, depth)
            assert depth == 0, f"unbalanced braces, ends at {depth}"
            assert lowest == 0, "a closing brace came before its opener"


class TestWhatTheReaderSees:
    def test_the_sliding_scale_is_there(self, page):
        assert 'id="thresh"' in page
        assert 'type="range"' in page
        assert "How many moved further" in page

    def test_both_sides_of_the_scale_are_shown(self, page):
        assert 'id="pctUnder"' in page and 'id="pctOver"' in page
        assert "Within the distance" in page
        assert "Moved further" in page

    def test_the_headline_numbers_are_formatted_with_separators(self, page):
        built = dash.dashboard_html(report_for(
            [{"DistanceFt": 1.0} for _ in range(1234)]))
        assert "1,234" in built

    def test_the_panels_a_reader_was_promised_are_present(self, page):
        for heading in ("Fixed thresholds", "Which pass found the pipe",
                        "By pipe layer", "By leak material", "By facility type",
                        "Material agreement", "Why a leak did not match",
                        "Date check", "Where the relocations fall",
                        "Share within a distance"):
            assert heading in page, heading

    def test_the_review_queue_is_present_and_counted_honestly(self, page, report):
        assert f"The furthest {len(report['furthest'])} relocations" in page

    def test_the_source_and_run_time_are_stated(self, page):
        assert "Test.gpkg" in page
        assert "2026-09-16T04:12:55Z" in page

    def test_a_warning_is_surfaced_not_hidden(self):
        built = dash.dashboard_html(report_for([
            {"MatchStatus": "Matched", "DistanceFt": None}]))
        assert "Worth knowing" in built
        assert "no usable" in built

    def test_no_warning_means_no_banner(self, page):
        assert "Worth knowing" not in page

    def test_the_date_check_caveat_is_shown(self, page):
        """98.1% of a production run reads no_leak_date, so the panel must not
        look like a clean bill of health."""
        assert "the date rule decided nothing" in page

    def test_the_page_says_what_it_does_not_contain(self, page):
        """It is mailable, so what is in it has to be legible to whoever sends
        it - aggregates and the furthest rows, not the whole table."""
        assert "leak_relocation_audit" in page
        assert "Aggregate counts" in page


class TestValuesFromTheDataCannotBreakTheMarkup:
    """A material name or an address comes out of the GeoPackage and lands in two
    places: HTML, where it is escaped, and the JSON data block, where `</` is
    escaped so it cannot end the script. Both paths are checked, because a value
    only has to get through one of them to break the page."""

    NASTY = [
        "<script>alert(1)</script>",
        '"><img src=x onerror=alert(1)>',
        "Cast Iron & Steel",
        "a < b > c",
        "</table></div><h1>out",
    ]

    def markup(self, built):
        """The page with its two script blocks removed - what the HTML parser
        renders as elements."""
        return re.sub(r"<script>.*?</script>", "", built, flags=re.S)

    @pytest.mark.parametrize("nasty", NASTY)
    def test_it_never_reaches_the_markup_unescaped(self, nasty):
        built = dash.dashboard_html(report_for([
            {"DistanceFt": 5.0, "LeakMaterial": nasty}]))
        body = self.markup(built)
        # The value is present, and present only in its escaped form.
        assert dash.escape(nasty) in body
        for fragment in ("<script", "<img", "<h1>out", "</table></div><h1>"):
            if fragment in nasty:
                assert fragment not in body, fragment
        assert '"><img' not in body

    @pytest.mark.parametrize("nasty", NASTY)
    def test_it_cannot_end_the_data_block(self, nasty):
        """Inside the script the value is JSON, so angle brackets are harmless -
        except a literal </script>, which the HTML parser would act on wherever
        it appears. That one sequence is escaped, and the block still parses."""
        built = dash.dashboard_html(report_for([
            {"DistanceFt": 5.0, "LeakMaterial": nasty}]))
        # Only the closing tag ends a script block, so that is what has to stay
        # at two. An opening "<script" inside the JSON is inert, and counting it
        # would be a test of the data rather than of the page.
        assert built.count("</script>") == 2
        names = {row["name"] for row in data_block(built)["by_leak_material"]}
        assert nasty in names, "the value should survive intact as data"

    def test_an_address_in_the_review_queue_is_escaped(self):
        built = dash.dashboard_html(report_for([
            {"DistanceFt": 5.0, "LeakAddress": "<b>12 Elm</b>"}]))
        assert "<b>12 Elm</b>" not in self.markup(built)
        assert "&lt;b&gt;12 Elm" in built

    def test_a_quote_cannot_escape_an_attribute(self):
        """escape() covers the double quote, so a value used in an attribute
        cannot start a new one."""
        assert '"' not in dash.escape('a"b')
        assert dash.escape('a"b') == "a&quot;b"


class TestTheEmptyAndOddCases:
    def test_an_empty_report_still_renders(self):
        frame = pd.DataFrame([{"DistanceFt": None, "MatchStatus": "NoMatch",
                               "NoMatchReason": "no_pipe_within_max_radius"}])
        built = dash.dashboard_html(dr.build_report(frame))
        assert "<!doctype html>" in built
        assert "No row in the audit layer" in built
        assert data_block(built)

    def test_a_report_with_one_relocation_renders(self):
        built = dash.dashboard_html(report_for([{"DistanceFt": 3.5}]))
        assert data_block(built)["totals"]["relocated"] == 1

    def test_a_missing_column_leaves_a_stated_gap_not_a_crash(self):
        frame = pd.DataFrame([{"DistanceFt": 5.0, "MatchStatus": "Matched"}])
        built = dash.dashboard_html(dr.build_report(frame))
        assert "Not recorded in this GeoPackage" in built

    def test_the_default_threshold_is_the_first_search_pass(self, report):
        assert data_block(dash.dashboard_html(report))["defaultThresholdFt"] == 100.0

    def test_a_short_run_does_not_open_on_a_meaningless_hundred_percent(self):
        """With every relocation under 100 ft the page would load reading
        "100% within 100 ft", which tells a reader nothing."""
        built = dash.dashboard_html(report_for(
            [{"DistanceFt": d} for d in (1.0, 2.0, 3.0, 9.0)]))
        assert data_block(built)["defaultThresholdFt"] < 100.0


class TestNothingIsFetchedFromTheInternet:
    """The workflow runs behind a TLS-intercepting proxy - that is why Leaflet is
    vendored - so a dashboard that linked out to a CDN would not draw."""

    def test_nothing_is_loaded_from_a_url(self, page):
        """The SVG namespace is an http:// string in the script and is not a
        fetch, so this looks for the things that actually load: a src, an href to
        a resource, a stylesheet link, an @import."""
        for pattern in (r'src\s*=\s*["\']?https?:', r'<link\b',
                        r'@import', r'//cdn\.', r'url\(\s*["\']?https?:'):
            assert not re.search(pattern, page, re.I), pattern

    def test_the_only_links_are_internal(self, page):
        for href in re.findall(r'href="([^"]*)"', page):
            assert not href.lower().startswith(("http:", "https:", "//")), href

    def test_the_style_and_script_are_inline(self, page):
        assert "<style>" in page
        assert "<script>" in page
        head = page.split("<body>")[0]
        assert "src=" not in head

    def test_no_web_font_is_requested(self, page):
        assert "fonts.googleapis" not in page
        assert "@font-face" not in page


class TestItFitsOnAPhone:
    """minmax(430px,1fr) cannot shrink below its own minimum, so on a 390px
    screen every panel was 430px wide and the whole page scrolled sideways. The
    min() guard is what stops that, and a browser is the only thing that catches
    it going away, so it is asserted here instead."""

    def test_the_grids_can_shrink_below_their_preferred_width(self, page):
        for track in re.findall(r"grid-template-columns:repeat\(auto-fit,([^;]+)\)",
                                dash.PAGE_CSS):
            assert "min(" in track, track

    def test_wide_tables_scroll_inside_their_panel(self, page):
        """A table is allowed to be wider than a phone; the page is not."""
        assert ".tw{overflow-x:auto" in dash.PAGE_CSS
        assert page.count('<div class="tw">') >= 5

    def test_the_page_declares_a_viewport(self, page):
        assert 'name="viewport"' in page
        assert "width=device-width" in page

    def test_the_readout_stacks_on_a_narrow_screen(self, page):
        narrow = dash.PAGE_CSS.split("@media (max-width:640px)")[1]
        assert "grid-template-columns:1fr" in narrow
