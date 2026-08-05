"""Tests for the viewer HTML and its attribute pane.

viewer/index_basic.html is a committed snapshot of generated output. These
tests keep it consistent with the template that produces it, so the snapshot
cannot silently drift from the generator the way earlier copies in this
repository did.
"""
import os
import re
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from leakrelocation import viewer_html, viewer_pane

SNAPSHOT = os.path.join(REPO_ROOT, "viewer", "index_basic.html")


@pytest.fixture(scope="module")
def snapshot():
    with open(SNAPSHOT, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def rendered():
    return viewer_html.render("leaflet/leaflet.css", "leaflet/leaflet.js",
                              41.5, -72.2, 42.9, -69.9)


class TestRender:
    def test_no_placeholders_remain(self, rendered):
        leftovers = re.findall(r"__[A-Z_]+__", rendered)
        assert leftovers == []

    def test_is_a_complete_document(self, rendered):
        assert rendered.startswith("<!doctype html>")
        assert rendered.rstrip().endswith("</html>")

    def test_bounds_substituted(self, rendered):
        assert "fitBounds([[41.5,-72.2],[42.9,-69.9]]" in rendered

    def test_pane_assets_embedded_once(self, rendered):
        assert rendered.count('id="attrPane"') == 1
        assert rendered.count("const AttributePane") == 1


class TestSnapshotMatchesTemplate:
    def test_snapshot_is_current(self, snapshot):
        """Regenerate with scripts/build_local_relocation_viewer.py if this
        fails, or re-render viewer_html.render with the snapshot's bounds."""
        match = re.search(r"fitBounds\(\[\[([-\d.]+),([-\d.]+)\],\[([-\d.]+),([-\d.]+)\]\]", snapshot)
        assert match, "snapshot has no fitBounds call"
        south, west, north, east = match.groups()
        expected = viewer_html.render("leaflet/leaflet.css", "leaflet/leaflet.js",
                                      south, west, north, east)
        assert snapshot == expected


class TestAttributePane:
    def test_pane_markup_present(self, snapshot):
        for element in ['id="attrPane"', 'id="attrPaneTabs"', 'id="attrPaneBody"',
                        'id="attrFilter"', 'id="attrPaneToggle"']:
            assert element in snapshot

    def test_both_layers_registered(self, snapshot):
        assert "AttributePane.register('leaks','Relocated leaks',leakLayer)" in snapshot
        assert "AttributePane.register('traces','Trace lines',traceLayer)" in snapshot
        assert "AttributePane.build()" in snapshot

    def test_map_clicks_select_the_row(self, snapshot):
        assert "AttributePane.selectFromMap(l)" in snapshot

    def test_pane_exposes_its_public_api(self):
        for name in ["register:", "build:", "select:", "selectFromMap:"]:
            assert name in viewer_pane.PANE_JS

    def test_row_rendering_is_capped(self):
        # A production layer holds tens of thousands of features; an uncapped
        # table would lock the browser.
        assert "MAX_ROWS" in viewer_pane.PANE_JS
        assert "slice(0, MAX_ROWS)" in viewer_pane.PANE_JS

    def test_values_are_escaped(self):
        assert "escapeHtml" in viewer_pane.PANE_JS
        assert "replaceAll('<', '&lt;')" in viewer_pane.PANE_JS

    def test_layout_leaves_room_for_the_map(self):
        # The pane docks below the map rather than covering it.
        assert "flex-direction:column" in viewer_pane.PANE_CSS
        assert "#map{flex:1 1 auto" in viewer_pane.PANE_CSS

    def test_collapse_resizes_the_map(self):
        assert "invalidateSize" in viewer_pane.PANE_JS

    def test_pane_assets_helper(self):
        css, html, js = viewer_pane.pane_assets()
        assert css is viewer_pane.PANE_CSS
        assert html is viewer_pane.PANE_HTML
        assert js is viewer_pane.PANE_JS
