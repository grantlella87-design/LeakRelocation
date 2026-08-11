"""Tests for the viewer HTML and its attribute pane.

viewer/index_basic.html is a committed snapshot of generated output. These
tests keep it consistent with the template that produces it, so the snapshot
cannot silently drift from the generator the way earlier copies in this
repository did.
"""
import io
import os
import re
import sys
from contextlib import redirect_stdout

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


class TestEveryViewerHasThePane:
    """Three separate generators build viewers here. The pane was only wired
    into one of them, so opening either of the others showed no table."""

    def test_relocation_viewer(self, rendered):
        assert 'id="attrPane"' in rendered
        assert "const AttributePane" in rendered

    def test_bbox_server_page(self, monkeypatch):
        pytest.importorskip("geopandas")
        import sys as _sys
        _sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
        import leaflet_bbox_server as server

        monkeypatch.setattr(server, "BOUNDS", {
            "west": -72.0, "south": 42.0, "east": -71.0, "north": 43.0,
            "center_lat": 42.5, "center_lon": -71.5,
        })
        page = server.html_page()
        assert 'id="attrPane"' in page
        assert "const AttributePane" in page
        # One tab per configured layer.
        assert page.count("AttributePane.register") >= 1
        # Layers reload on pan and zoom, so the table must re-read them.
        assert "AttributePane.build()" in page
        assert "AttributePane.selectFromMap" in page

    def test_context_map_builder_wires_the_pane(self):
        path = os.path.join(REPO_ROOT, "scripts", "build_leaflet_context.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        for token in ["__PANE_CSS__", "__PANE_HTML__", "__PANE_JS__", "__PANE_REGISTER__"]:
            assert token in source, token
        assert "AttributePane.register" in source
        assert "AttributePane.build()" in source
        assert "AttributePane.selectFromMap" in source

    def test_full_height_map_rule_removed_where_the_pane_docks(self):
        """The pane makes body a flex column. A leftover "#map{height:100%}"
        makes the map fill the page and hides the table."""
        for name in ["src/leaflet_bbox_server.py", "scripts/build_leaflet_context.py"]:
            with open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
                source = handle.read()
            assert "#map{height:100%;width:100%}" not in source, name
            assert "#map { height: 100%; width: 100%; }" not in source, name


class TestPaneReadsNestedLayers:
    def test_collect_recurses(self):
        """A layer may be an L.layerGroup wrapping an L.geoJSON, as in the bbox
        server, so reading one level deep finds no features."""
        assert "function walk(layer)" in viewer_pane.PANE_JS
        assert "if (layer.eachLayer) layer.eachLayer(walk);" in viewer_pane.PANE_JS


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


class TestBboxServerMaterialColumns:
    """Pipe material comes from ASSETTYPE. The raw subtype code and its domain
    value are different things and must not be conflated: classifying the code
    would compare a number against material names."""

    @pytest.fixture
    def server(self):
        pytest.importorskip("geopandas")
        sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
        import leaflet_bbox_server
        return leaflet_bbox_server

    def frame(self, **columns):
        import geopandas as gpd
        from shapely.geometry import LineString
        rows = len(next(iter(columns.values())))
        columns["geometry"] = [
            LineString([(-71 - i * 0.01, 42), (-71.01 - i * 0.01, 42.01)]) for i in range(rows)
        ]
        return gpd.GeoDataFrame(columns, crs="EPSG:4326")

    def test_raw_is_the_code_and_domain_is_the_name(self, server):
        gdf = self.frame(
            OBJECTID=[1, 2, 3],
            ASSETTYPE=[2, 5, 9],
            ASSETTYPE_DECODED=["Cast Iron", "Copper", "Plastic PE"],
            material=["Cast Iron", "Copper", "Plastic PE"],
        )
        out = server.add_pipe_material_fields(gdf)
        assert list(out["PipeMaterialRaw"]) == [2, 5, 9]
        assert list(out["PipeMaterialDomain"]) == ["Cast Iron", "Copper", "Plastic PE"]
        assert list(out["PipeMaterialFamily"]) == ["IRON", "COPPER", "PLASTIC"]

    def test_no_silent_fallback_to_the_material_column(self, server, capsys):
        """Without ASSETTYPE_DECODED the family is left blank, not derived from
        "material". On an unenriched cache that column holds the DNV Grade field,
        and falling back to it produced confident, wrong colours."""
        gdf = self.frame(
            OBJECTID=[1, 2],
            ASSETTYPE=[2, 5],
            material=["Grade A", "Grade B"],
        )
        out = server.add_pipe_material_fields(gdf, "distribution_pipes")
        assert out["PipeMaterialDomain"].isna().all()
        assert "Grade A" not in out["PipeMaterialDomain"].tolist()
        # This frame has ASSETTYPE but no ASSETGROUP, and the ASSETTYPE domain is
        # defined per subtype, so it cannot be decoded either - which the message
        # has to say rather than leaving the map quietly one colour.
        assert "material cannot be named" in capsys.readouterr().out

    def test_raw_still_populated_without_the_decoded_column(self, server):
        gdf = self.frame(OBJECTID=[1, 2], ASSETTYPE=[2, 5])
        with redirect_stdout(io.StringIO()):
            out = server.add_pipe_material_fields(gdf, "distribution_pipes")
        assert list(out["PipeMaterialRaw"]) == [2, 5]

    def test_colour_follows_the_family(self, server):
        gdf = self.frame(
            OBJECTID=[1, 2],
            ASSETTYPE=[5, 9],
            ASSETTYPE_DECODED=["Copper", "Plastic PE"],
        )
        out = server.add_pipe_material_fields(gdf)
        assert out["PipeMaterialColor"].tolist() == [
            server.MATERIAL_COLORS["COPPER"], server.MATERIAL_COLORS["PLASTIC"]
        ]

    def test_popup_lists_the_domain_value(self, server, monkeypatch):
        monkeypatch.setattr(server, "BOUNDS", {
            "west": -72.0, "south": 42.0, "east": -71.0, "north": 43.0,
            "center_lat": 42.5, "center_lon": -71.5,
        })
        assert "PipeMaterialDomain" in server.html_page()
