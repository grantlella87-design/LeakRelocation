"""Tests for the GISportal MA main lines Leaflet viewer.

The classification tests exist because the relocation viewers each carried their
own JavaScript copy of the material logic, and those copies drifting from the
Python is what made every copper pipe render as plastic. This viewer classifies
in Python and ships the answer in the GeoJSON, so the same drift cannot happen -
these tests hold that line.

The browser test at the end loads the built page in Chromium and checks the map
actually draws, which is the only way to catch a page that renders blank.
"""
import io
import json
import os
import re
import sys
import threading
from contextlib import redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
sys.path.insert(0, os.path.join(REPO_ROOT, "GISportal"))

pytest.importorskip("keyring")

import main_lines_viewer as viewer
import query_ma_main_lines_2022_plus as exporter

LINE = {"type": "LineString", "coordinates": [[-71.0, 42.0], [-71.1, 42.2]]}
MULTILINE = {"type": "MultiLineString",
             "coordinates": [[[-71.5, 42.5], [-71.6, 42.6]],
                             [[-72.0, 43.0], [-72.1, 43.1]]]}


def feature(material=None, geometry=None, **extra):
    properties = dict(extra)
    if material is not None:
        properties["material"] = material
    return {"type": "Feature", "properties": properties,
            "geometry": geometry or LINE}


def quietly(function, *args, **kwargs):
    """Run something that logs to stdout, returning its result."""
    with redirect_stdout(io.StringIO()):
        return function(*args, **kwargs)


class TestMaterialFamilies:
    @pytest.mark.parametrize(("material", "family"), [
        ("Plastic PE", "PLASTIC"),
        ("HDPE", "PLASTIC"),
        ("MDPE", "PLASTIC"),
        ("Polyethylene", "PLASTIC"),
        ("PVC", "PLASTIC"),
        ("Bare Steel", "STEEL"),
        ("Coated Steel", "STEEL"),
        ("Galvanized Steel", "STEEL"),
        ("Cast Iron", "IRON"),
        ("cast-iron", "IRON"),
        ("Ductile Iron", "IRON"),
        ("Wrought Iron", "IRON"),
        ("Copper", "COPPER"),
        ("Unknown", "UNKNOWN"),
        ("UNK", "UNKNOWN"),
        ("Composite", "UNKNOWN"),
    ])
    def test_known_materials(self, material, family):
        assert viewer.family_of({"material": material}) == family

    def test_copper_is_not_plastic(self):
        """"COPPER" contains "PE". Substring matching classified every copper
        pipe as plastic, which is how the map came to show only plastic and
        unknown."""
        assert viewer.family_of({"material": "Copper"}) == "COPPER"

    def test_missing_material_is_unknown(self):
        assert viewer.family_of({}) == "UNKNOWN"
        assert viewer.family_of({"material": ""}) == "UNKNOWN"
        assert viewer.family_of({"material": None}) == "UNKNOWN"

    def test_unrecognised_label_is_other_not_its_own_colour(self):
        """A label nobody planned for gets one bucket, so the legend cannot grow
        a row per spelling variant in the data."""
        assert viewer.family_of({"material": "Reinforced Concrete"}) == "OTHER"

    def test_a_bare_code_is_not_decoded_with_another_services_domain(self):
        """A numeric material here is a code from *this* layer's domain. The
        shared classifier would read 9 as "Plastic PE" using the DNV service
        pipe subtypes, which is a different service entirely."""
        assert viewer.family_of({"material": 9}) == "CODED"
        assert viewer.family_of({"material": "9"}) == "CODED"

    def test_the_label_column_is_preferred_over_the_code(self):
        assert viewer.family_of({"material": 5, "material_LABEL": "Copper"}) == "COPPER"

    def test_a_number_inside_a_label_still_classifies(self):
        assert viewer.family_of({"material": "2 IN PLASTIC"}) == "PLASTIC"

    def test_every_family_has_a_colour(self):
        for family in (*viewer.CLASSIFIER_FAMILIES, viewer.CODED_FAMILY,
                       viewer.OTHER_FAMILY):
            assert family in viewer.FAMILY_COLORS
            assert viewer.FAMILY_COLORS[family].startswith("#")

    def test_families_have_distinct_colours(self):
        colours = list(viewer.FAMILY_COLORS.values())
        assert len(colours) == len(set(colours))


class TestEnrichment:
    def test_family_is_added_to_every_feature(self):
        features = [feature("Copper"), feature("Plastic PE"), feature()]
        viewer.enrich_features(features)
        assert [f["properties"][viewer.FAMILY_PROPERTY] for f in features] == [
            "COPPER", "PLASTIC", "UNKNOWN"]

    def test_counts_are_returned_for_the_legend(self):
        counts = viewer.enrich_features(
            [feature("Copper"), feature("Cast Iron"), feature("Cast Iron")])
        assert counts == {"COPPER": 1, "IRON": 2}

    def test_features_without_properties_are_handled(self):
        features = [{"type": "Feature", "geometry": LINE}]
        counts = viewer.enrich_features(features)
        assert counts == {"UNKNOWN": 1}
        assert features[0]["properties"][viewer.FAMILY_PROPERTY] == "UNKNOWN"


class TestBounds:
    def test_covers_a_linestring(self):
        assert viewer.bounds_of([feature("Steel")]) == (42.0, -71.1, 42.2, -71.0)

    def test_covers_every_part_of_a_multilinestring(self):
        south, west, north, east = viewer.bounds_of(
            [feature("Steel", geometry=MULTILINE)])
        assert (south, west, north, east) == (42.5, -72.1, 43.1, -71.5)

    def test_covers_all_features(self):
        south, west, north, east = viewer.bounds_of(
            [feature("Steel"), feature("Steel", geometry=MULTILINE)])
        assert (south, west, north, east) == (42.0, -72.1, 43.1, -71.0)

    def test_no_geometry_gives_no_bounds(self):
        assert viewer.bounds_of([]) is None
        assert viewer.bounds_of([{"type": "Feature", "geometry": None}]) is None

    def test_a_zero_width_extent_is_padded(self):
        """fitBounds cannot zoom to a box with no width, which a single
        north-south line produces."""
        vertical = {"type": "LineString", "coordinates": [[-71.0, 42.0], [-71.0, 42.5]]}
        south, west, north, east = viewer.bounds_of([{"geometry": vertical}])
        assert west < -71.0 < east
        assert (south, north) == (42.0, 42.5)


@pytest.fixture(scope="module")
def html():
    return viewer.render_html("MA main lines", "leaflet/leaflet.css",
                              "leaflet/leaflet.js", (42.0, -71.5, 42.5, -71.0),
                              "main_lines.geojson")


class TestRenderedPage:
    def test_no_placeholders_remain(self, html):
        assert re.findall(r"__[A-Z_]+__", html) == []

    def test_is_a_complete_document(self, html):
        assert html.startswith("<!doctype html>")
        assert html.rstrip().endswith("</html>")

    def test_bounds_are_substituted(self, html):
        assert "fitBounds([[42.0,-71.5],[42.5,-71.0]]" in html

    def test_the_shared_pane_is_embedded_once(self, html):
        assert html.count('id="attrPane"') == 1
        assert html.count("const AttributePane") == 1

    def test_colours_come_from_python(self, html):
        assert json.dumps(viewer.FAMILY_COLORS) in html

    def test_no_material_classification_javascript(self, html):
        """The families are computed in Python. A JavaScript classifier here
        would be a second source of truth, which is the bug this avoids."""
        assert "MATERIAL_FAMILY_TERMS" not in html
        assert "function materialFamily" not in html

    def test_local_asset_references_are_used_as_given(self, html):
        assert 'href="leaflet/leaflet.css"' in html
        assert 'src="leaflet/leaflet.js"' in html


class TestBuild:
    @pytest.fixture
    def offline(self, monkeypatch):
        """No network in the build tests; the CDN fallback is what is exercised."""
        monkeypatch.setattr(viewer, "download_leaflet", lambda target: False)
        monkeypatch.setattr(viewer, "VENDORED_LEAFLET",
                            viewer.Path("/nonexistent/leaflet"))

    def write_geojson(self, tmp_path, features):
        path = tmp_path / "export.geojson"
        path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
        return path

    def test_writes_the_page_and_the_data(self, tmp_path, offline):
        source = self.write_geojson(tmp_path, [feature("Copper"), feature("Steel")])
        out = tmp_path / "viewer"
        index = quietly(viewer.build, source, out)
        assert index == out / "index.html"
        assert index.exists()
        assert (out / "main_lines.geojson").exists()

    def test_the_served_geojson_carries_the_family(self, tmp_path, offline):
        source = self.write_geojson(tmp_path, [feature("Copper")])
        out = tmp_path / "viewer"
        quietly(viewer.build, source, out)
        data = json.loads((out / "main_lines.geojson").read_text())
        assert data["features"][0]["properties"][viewer.FAMILY_PROPERTY] == "COPPER"

    def test_falls_back_to_the_cdn_and_says_so(self, tmp_path, offline):
        source = self.write_geojson(tmp_path, [feature("Steel")])
        out = tmp_path / "viewer"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            viewer.build(source, out)
        assert "unpkg.com" in (out / "index.html").read_text()
        assert "blank" in buffer.getvalue()

    def test_vendored_leaflet_is_used_when_present(self, tmp_path, monkeypatch):
        vendored = tmp_path / "vendor"
        vendored.mkdir()
        for name in viewer.LEAFLET_FILES:
            (vendored / name).write_text("/* stub */")
        monkeypatch.setattr(viewer, "VENDORED_LEAFLET", vendored)
        monkeypatch.setattr(viewer, "download_leaflet",
                            lambda target: pytest.fail("should not download"))
        source = self.write_geojson(tmp_path, [feature("Steel")])
        out = tmp_path / "viewer"
        quietly(viewer.build, source, out)
        assert (out / "leaflet" / "leaflet.js").exists()
        assert 'src="leaflet/leaflet.js"' in (out / "index.html").read_text()

    def test_a_coded_material_without_a_label_is_called_out(self, tmp_path, offline):
        source = self.write_geojson(tmp_path, [feature(9), feature(3)])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            viewer.build(source, tmp_path / "viewer")
        output = buffer.getvalue()
        assert "cannot be named" in output
        assert "material_LABEL" in output

    def test_a_missing_export_names_the_command_that_makes_it(self, tmp_path):
        with pytest.raises(RuntimeError, match="query_ma_main_lines_2022_plus.py"), \
                redirect_stdout(io.StringIO()):
            viewer.build(tmp_path / "absent.geojson", tmp_path / "viewer")

    def test_an_empty_export_fails_clearly(self, tmp_path):
        source = self.write_geojson(tmp_path, [])
        with pytest.raises(RuntimeError, match="no features"), \
                redirect_stdout(io.StringIO()):
            viewer.build(source, tmp_path / "viewer")

    def test_features_without_line_geometry_fail_rather_than_render_blank(
            self, tmp_path, offline):
        source = self.write_geojson(tmp_path, [{"type": "Feature", "properties": {},
                                               "geometry": None}])
        with pytest.raises(RuntimeError, match="[Nn]o line geometry"), \
                redirect_stdout(io.StringIO()):
            viewer.build(source, tmp_path / "viewer")


# A layer whose material field carries a coded-value domain, as the metadata
# reports it: a "range" domain and a plain field alongside, so only the coded one
# should be picked up.
CODED_METADATA = {
    "fields": [
        {"name": "OBJECTID", "type": "esriFieldTypeOID"},
        {"name": "material", "type": "esriFieldTypeSmallInteger",
         "domain": {"type": "codedValue", "name": "MaterialDomain",
                    "codedValues": [{"code": 5, "name": "Copper"},
                                    {"code": 9, "name": "Plastic PE"}]}},
        {"name": "lifecyclestatus", "type": "esriFieldTypeString",
         "domain": {"type": "range", "name": "NotCoded"}},
        {"name": "assetid", "type": "esriFieldTypeString"},
    ]
}


class TestCodedValueDomains:
    def test_only_coded_value_domains_are_collected(self):
        domains = exporter.coded_value_domains(CODED_METADATA)
        assert sorted(domains) == ["material"]
        assert domains["material"] == {"5": "Copper", "9": "Plastic PE"}

    def test_no_domains_is_empty_not_an_error(self):
        assert exporter.coded_value_domains({"fields": [{"name": "x"}]}) == {}
        assert exporter.coded_value_domains({}) == {}

    def test_a_label_column_is_added_beside_the_code(self):
        domains = exporter.coded_value_domains(CODED_METADATA)
        row = exporter.normalize_attributes({"material": 5}, set(), domains)
        assert row == {"material": 5, "material_LABEL": "Copper"}

    def test_the_raw_code_is_kept(self):
        """Anything joining on the code still can."""
        domains = exporter.coded_value_domains(CODED_METADATA)
        row = exporter.normalize_attributes({"material": 9}, set(), domains)
        assert row["material"] == 9

    def test_a_code_outside_the_domain_gets_a_blank_label(self):
        domains = exporter.coded_value_domains(CODED_METADATA)
        row = exporter.normalize_attributes({"material": 404}, set(), domains)
        assert row["material_LABEL"] == ""

    def test_fields_without_a_domain_are_untouched(self):
        domains = exporter.coded_value_domains(CODED_METADATA)
        row = exporter.normalize_attributes({"assetid": "A1"}, set(), domains)
        assert row == {"assetid": "A1"}

    def test_dates_are_still_converted(self):
        domains = exporter.coded_value_domains(CODED_METADATA)
        row = exporter.normalize_attributes(
            {"installationdate": 1640995200000}, {"installationdate"}, domains)
        assert row["installationdate"].startswith("2022-01-01")

    def test_the_label_column_reaches_the_csv_header(self, tmp_path):
        features = [{"attributes": {"OBJECTID": 1, "material": 5}}]
        path = tmp_path / "out.csv"
        quietly(exporter.write_csv, features, path, set(),
                exporter.coded_value_domains(CODED_METADATA))
        header = path.read_text(encoding="utf-8-sig").splitlines()[0]
        assert "material_LABEL" in header

    def test_the_viewer_reads_that_label(self, tmp_path):
        """End to end on the naming: the exporter's label column is the one the
        viewer classifies on."""
        features = [{"attributes": {"OBJECTID": 1, "material": 5},
                     "geometry": {"paths": [[[-71.0, 42.0], [-71.1, 42.1]]]}}]
        path = tmp_path / "out.geojson"
        quietly(exporter.write_geojson, features, path, set(),
                exporter.coded_value_domains(CODED_METADATA))
        exported = json.loads(path.read_text())["features"]
        assert viewer.family_of(exported[0]["properties"]) == "COPPER"


class TestViewerFlag:
    def test_viewer_needs_the_geojson(self, monkeypatch, tmp_path):
        """--viewer with --no-geojson has nothing to map; it must say so rather
        than serve an empty page."""
        called = []
        monkeypatch.setattr(exporter, "show_viewer",
                            lambda *args, **kwargs: called.append(args))
        stub_metadata = {"name": "stub", "fields": [
            {"name": "OBJECTID", "type": "esriFieldTypeOID"},
            {"name": "installationdate", "type": "esriFieldTypeDate"},
        ]}

        def request_json(session, url, params=None):
            params = params or {}
            if not url.endswith("/query"):
                return stub_metadata
            if params.get("returnCountOnly") == "true":
                return {"count": 1}
            if params.get("returnIdsOnly") == "true":
                return {"objectIds": [1]}
            return {"features": [{"attributes": {"OBJECTID": 1}}]}

        monkeypatch.setattr(exporter.auth, "request_json", request_json)
        monkeypatch.setattr(exporter.auth, "make_session", lambda: object())

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = exporter.main(["--out-dir", str(tmp_path), "--no-geojson", "--viewer"])
        assert code == 1
        assert called == []
        assert "--no-geojson" in buffer.getvalue()


# --- The page in a real browser ---------------------------------------------


@pytest.fixture(scope="module")
def leaflet_assets(tmp_path_factory):
    """Leaflet itself, for the browser test. Skips if it cannot be fetched."""
    target = tmp_path_factory.mktemp("leaflet-assets")
    with redirect_stdout(io.StringIO()):
        if not viewer.download_leaflet(target):
            pytest.skip("Leaflet could not be downloaded for the browser test")
    return target


@pytest.fixture(scope="module")
def served_viewer(tmp_path_factory, leaflet_assets):
    """Build a viewer from known features and serve it, as the script does."""
    import functools
    import http.server
    import shutil

    # Each feature gets its own stretch of ground. Overlapping lines blend into
    # each other on the canvas, and then no single colour can be checked.
    def line(west, south):
        return {"type": "LineString",
                "coordinates": [[west, south], [west + 0.05, south + 0.05]]}

    features = [
        feature("Copper", geometry=line(-71.0, 42.0), OBJECTID=1, assetid="A-1"),
        feature("Plastic PE", geometry=line(-71.4, 42.0), OBJECTID=2, assetid="A-2"),
        feature("Cast Iron", OBJECTID=3, assetid="A-3", geometry={
            "type": "MultiLineString",
            "coordinates": [[[-71.0, 42.4], [-70.95, 42.45]],
                            [[-70.8, 42.4], [-70.75, 42.45]]]}),
        feature("Bare Steel", geometry=line(-71.4, 42.4), OBJECTID=4, assetid="A-4"),
    ]
    root = tmp_path_factory.mktemp("viewer-e2e")
    source = root / "export.geojson"
    source.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    out = root / "viewer"
    out.mkdir()
    (out / "leaflet").mkdir()
    for name in viewer.LEAFLET_FILES:
        shutil.copy2(leaflet_assets / name, out / "leaflet" / name)
    quietly(viewer.build, source, out)

    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(out))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/index.html", out
    server.shutdown()
    server.server_close()


# How many line features the map is actually holding.
LINES_ON_MAP = """() => {
  let lines = 0;
  map.eachLayer(layer => {
    if (layer.eachLayer) layer.eachLayer(child => { if (child.getLatLngs) lines++; });
  });
  return lines;
}"""

# Which colours were painted onto the overlay canvas. The lines are drawn there,
# not as SVG paths, because the map is created with preferCanvas - so counting
# <path> elements finds Leaflet's own controls and says nothing about the data.
PAINTED_COLOURS = """() => {
  const canvas = document.querySelector('#map canvas');
  if (!canvas) return null;
  const data = canvas.getContext('2d')
    .getImageData(0, 0, canvas.width, canvas.height).data;
  const counts = {};
  for (let i = 0; i < data.length; i += 4) {
    if (data[i + 3] < 200) continue;   /* antialiased edge, not the stroke */
    const hex = '#' + [data[i], data[i + 1], data[i + 2]]
      .map(v => v.toString(16).padStart(2, '0')).join('');
    counts[hex] = (counts[hex] || 0) + 1;
  }
  return counts;
}"""


def launch_chromium(playwright):
    """A Chromium, or a skip.

    The bundled browser is tried first. When the installed Playwright expects a
    build that is not on the machine, a Chromium that *is* present is used
    instead - otherwise this test silently skips on the exact page it exists to
    check. --no-sandbox because this often runs as root, where the sandbox
    refuses to start.
    """
    attempts = [{}]
    for candidate in (os.environ.get("CHROMIUM_PATH"), "/opt/pw-browsers/chromium"):
        if candidate and os.path.exists(candidate):
            attempts.append({"executable_path": candidate})

    failures = []
    for options in attempts:
        try:
            return playwright.chromium.launch(args=["--no-sandbox"], **options)
        except Exception as exc:  # noqa: BLE001 - try the next candidate
            failures.append(f"{options or 'bundled'}: {str(exc).splitlines()[0]}")
    pytest.skip("No Chromium could be launched. " + "; ".join(failures))
    return None


@pytest.fixture(scope="module")
def page_state(served_viewer):
    """Load the served page in Chromium and report what it drew."""
    sync_playwright = pytest.importorskip(
        "playwright.sync_api", reason="playwright not installed").sync_playwright
    url, _ = served_viewer

    errors = []
    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        page = browser.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        # Tile requests go to the internet and may fail; the map does not need
        # them, so only the page's own script errors matter.
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_function("document.body.dataset.mainLinesLoaded", timeout=20000)

        state = {
            "errors": errors,
            "loaded": page.evaluate("document.body.dataset.mainLinesLoaded"),
            "load_error": page.evaluate("document.body.dataset.mainLinesError || ''"),
            "lines_on_map": page.evaluate(LINES_ON_MAP),
            "painted": page.evaluate(PAINTED_COLOURS),
            "legend": page.locator("#info").inner_text(),
            "overlay_labels": page.locator(
                ".leaflet-control-layers-overlays label").all_inner_texts(),
            "tabs": page.locator(".attr-tab").all_inner_texts(),
            "columns": page.locator("#attrTable thead th").all_inner_texts(),
            "rows": page.locator("#attrTable tbody tr").count(),
        }
        # Clicking a row is what was broken when the pane could not see features
        # nested inside a layer group.
        page.locator("#attrTable tbody tr").first.click()
        state["selected_after_click"] = page.locator("#attrTable tbody tr.selected").count()
        state["popups_after_click"] = page.locator(".leaflet-popup").count()
        browser.close()
    return state


class TestInChromium:
    def test_the_page_has_no_script_errors(self, page_state):
        assert page_state["errors"] == []

    def test_the_geojson_loads(self, page_state):
        assert page_state["load_error"] == ""
        assert page_state["loaded"] == "4"

    def test_every_feature_reaches_the_map(self, page_state):
        assert page_state["lines_on_map"] == 4

    def test_the_lines_are_actually_painted(self, page_state):
        """Pixels on the canvas, not just layers in memory. A page can hold
        every feature and still render blank."""
        painted = page_state["painted"]
        assert painted, "no overlay canvas on the page"
        assert sum(painted.values()) > 100

    @pytest.mark.parametrize("family", ["COPPER", "PLASTIC", "IRON", "STEEL"])
    def test_each_family_is_painted_in_its_own_colour(self, page_state, family):
        """The colour on screen comes from Python's FAMILY_COLORS. Copper
        painted green would be the old bug, visible here."""
        expected = viewer.FAMILY_COLORS[family].lower()
        assert page_state["painted"].get(expected, 0) > 0, (
            f"{family} ({expected}) never reached the canvas; "
            f"painted: {sorted(page_state['painted'])}")

    def test_one_overlay_per_material_family(self, page_state):
        labels = " ".join(page_state["overlay_labels"])
        for family in ("COPPER", "PLASTIC", "IRON", "STEEL"):
            assert family in labels

    def test_the_legend_reports_the_families_and_counts(self, page_state):
        legend = page_state["legend"]
        assert "main lines: 4" in legend
        for family in ("COPPER", "PLASTIC", "IRON", "STEEL"):
            assert family in legend

    def test_copper_is_not_shown_as_plastic(self, page_state):
        """The regression, checked in the rendered page rather than in Python."""
        assert "COPPER 1" in page_state["legend"]
        assert "PLASTIC 1" in page_state["legend"]

    def test_the_attribute_table_is_populated(self, page_state):
        assert page_state["rows"] >= 1
        assert "material" in page_state["columns"]
        assert viewer.FAMILY_PROPERTY in page_state["columns"]

    def test_a_tab_per_family(self, page_state):
        assert len(page_state["tabs"]) == 4

    def test_clicking_a_row_selects_it_and_opens_the_feature(self, page_state):
        assert page_state["selected_after_click"] == 1
        assert page_state["popups_after_click"] == 1
