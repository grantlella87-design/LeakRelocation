"""Tests for the map page and its attribute pane.

The static viewer generator, its committed HTML snapshot and the standalone
context-map builder are gone: src/leaflet_bbox_server.py is the only map now, and
it is a superset of what the static one showed. What is left here covers that
page and the shared attribute pane.
"""
import io
import os
import sys
from contextlib import redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from leakrelocation import viewer_pane


def read_source(relative):
    with open(os.path.join(REPO_ROOT, relative), encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def page():
    """The rendered map page."""
    pytest.importorskip("geopandas")
    import leaflet_bbox_server as server
    saved = server.BOUNDS
    server.BOUNDS = {"west": -72.0, "south": 42.0, "east": -71.0, "north": 43.0,
                     "center_lat": 42.5, "center_lon": -71.5}
    try:
        return server.html_page()
    finally:
        server.BOUNDS = saved


class TestTheMapPageHasThePane:
    """The pane was once wired into only one of three generators, so opening
    either of the others showed no table. There is one page now, and this is it."""

    def test_the_pane_is_in_the_page(self, page):
        assert 'id="attrPane"' in page
        assert "const AttributePane" in page

    def test_every_pane_element_is_present(self, page):
        for element in ['id="attrPane"', 'id="attrPaneTabs"', 'id="attrPaneBody"',
                        'id="attrFilter"', 'id="attrPaneToggle"']:
            assert element in page

    def test_layers_are_registered_and_the_table_is_built(self, page):
        assert page.count("AttributePane.register") >= 1
        # Layers reload on pan and zoom, so the table must re-read them.
        assert "AttributePane.build()" in page

    def test_map_clicks_select_the_row(self, page):
        assert "AttributePane.selectFromMap" in page

    def test_full_height_map_rule_is_not_there(self):
        """The pane makes body a flex column. A leftover "#map{height:100%}"
        makes the map fill the page and hides the table."""
        source = read_source("src/leaflet_bbox_server.py")
        assert "#map{height:100%;width:100%}" not in source
        assert "#map { height: 100%; width: 100%; }" not in source


class TestPaneReadsNestedLayers:
    def test_collect_recurses(self):
        """A layer may be an L.layerGroup wrapping an L.geoJSON, as in the map
        server, so reading one level deep finds no features."""
        assert "function walk(layer)" in viewer_pane.PANE_JS
        assert "if (layer.eachLayer) layer.eachLayer(walk);" in viewer_pane.PANE_JS


class TestAttributePane:
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


class TestLeakAddressReachesTheMap:
    """ADDRESS is on layer 206 and is carried through for identification. It is
    matched on by nothing, so an empty column is the only symptom of losing it."""

    @pytest.fixture
    def server(self):
        pytest.importorskip("geopandas")
        sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
        import leaflet_bbox_server
        return leaflet_bbox_server

    def leak_frame(self, **columns):
        import geopandas as gpd
        from shapely.geometry import Point
        rows = len(next(iter(columns.values())))
        columns["geometry"] = [Point(-71 - i * 0.01, 42) for i in range(rows)]
        return gpd.GeoDataFrame(columns, crs="EPSG:4326")

    def test_limit_columns_keeps_the_address(self, server):
        """limit_columns keeps a column only if it matches a token, and none of
        the others match "ADDRESS" - so it was downloaded and then dropped one
        step before the page."""
        gdf = self.leak_frame(
            OBJECTID=[1, 2],
            LMSLEAKNUMBER=["A1", "A2"],
            ADDRESS=["12 Elm St", "3 Oak Ave"],
        )
        out = server.limit_columns(gdf)
        assert "ADDRESS" in out.columns
        assert out["ADDRESS"].tolist() == ["12 Elm St", "3 Oak Ave"]

    def test_limit_columns_keeps_the_relocated_address(self, server):
        """The relocated points carry it as LeakAddress."""
        gdf = self.leak_frame(OrigLeakOID=[1], LeakAddress=["12 Elm St"])
        assert "LeakAddress" in server.limit_columns(gdf).columns

    def test_a_column_matching_nothing_is_still_dropped(self, server):
        """Guards the test above: it passes because of the token, not because
        limit_columns keeps everything."""
        gdf = self.leak_frame(OBJECTID=[1], ADDRESS=["12 Elm St"],
                              SOMETHINGELSE=["x"])
        assert "SOMETHINGELSE" not in server.limit_columns(gdf).columns

    def test_the_popup_lists_the_address_near_the_top(self, server, monkeypatch):
        monkeypatch.setattr(server, "BOUNDS", {
            "west": -72.0, "south": 42.0, "east": -71.0, "north": 43.0,
            "center_lat": 42.5, "center_lon": -71.5,
        })
        page = server.html_page()
        assert "'ADDRESS'" in page
        assert "'LeakAddress'" in page
        # Ordered above the supplemental attributes: it identifies the leak.
        assert page.index("'ADDRESS'") < page.index("'SuppLeakMaterialType'")
