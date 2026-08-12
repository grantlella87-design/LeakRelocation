"""The map that shows distribution and service pipes coloured by material.

Two things had to be true for that map to work, and neither was:

1. It read the material from ASSETTYPE_DECODED, a column only
   scripts/enrich_assettype_cache.py writes. A cache straight from a download
   carries ASSETGROUP and ASSETTYPE but not that, so every pipe fell to one
   colour - the same trap the workflow hit, where the codes were there and only
   the decode was missing.
2. The page's colour palette was a second literal, so editing the Python one
   changed nothing on screen.
"""
import io
import os
import sys
from contextlib import redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

pytest.importorskip("geopandas")
pytest.importorskip("pyproj")

from leakrelocation import assettype, config, schema

REFERENCE_PRESENT = config.REFERENCE_DIR.is_dir()


@pytest.fixture(scope="module")
def server():
    import leaflet_bbox_server
    return leaflet_bbox_server


# (ASSETGROUP, ASSETTYPE) -> the label the real domains give it.
PAIRS = ((2, 9), (2, 5), (2, 2), (2, 1), (2, 999), (8, 281))
LABELS = ("Plastic PE", "Copper", "Cast Iron", "Bare Steel", "UNK", "Bond Wire")
FAMILIES = ("PLASTIC", "COPPER", "IRON", "STEEL", "UNKNOWN", "OTHER")


def pipe_frame(pairs=PAIRS, **extra):
    import geopandas as gpd
    from shapely.geometry import LineString
    columns = {
        "OBJECTID": list(range(1, len(pairs) + 1)),
        schema.ASSETGROUP: [group for group, _ in pairs],
        schema.ASSETTYPE: [code for _, code in pairs],
        "nominaldiameter": [4] * len(pairs),
        "geometry": [LineString([(0, i), (1, i + 1)]) for i in range(len(pairs))],
    }
    columns.update(extra)
    return gpd.GeoDataFrame(columns, crs="EPSG:4326")


def quiet(function, *args, **kwargs):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        result = function(*args, **kwargs)
    return result, buffer.getvalue()


@pytest.mark.skipif(not REFERENCE_PRESENT, reason="reference metadata not present")
class TestTheOfflineDecoder:
    """assettype.decoder_for_layer reads the committed service metadata, so a
    material can be named with no token and no enrichment pass."""

    @pytest.mark.parametrize("layer_id", [config.DISTRIBUTION_PIPE_LAYER_ID,
                                          config.SERVICE_PIPE_LAYER_ID])
    def test_both_pipe_layers_decode(self, layer_id):
        field, decoder = assettype.decoder_for_layer(layer_id)
        assert field == "ASSETGROUP"
        assert len(decoder) == 99

    def test_a_layer_without_assettype_domains_gives_nothing(self):
        """Layer 206 has subtypes, but keyed on LEAKCAUSE, not ASSETTYPE."""
        _, decoder = assettype.decoder_for_layer(config.HISTORIC_LEAK_LAYER_ID)
        assert decoder == {}

    def test_an_unknown_layer_id_gives_nothing_rather_than_raising(self):
        assert assettype.decoder_for_layer(4242) == (None, {})

    def test_missing_reference_folder_gives_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "REFERENCE_DIR", tmp_path / "absent")
        assert assettype.decoder_for_layer(6) == (None, {})


@pytest.mark.skipif(not REFERENCE_PRESENT, reason="reference metadata not present")
class TestPipesGetTheirMaterialWithoutEnrichment:
    def test_labels_come_from_the_real_domains(self, server):
        labels, _ = quiet(server.decode_from_subtypes, pipe_frame(),
                          config.DISTRIBUTION_PIPE_LAYER_ID, "distribution_pipes")
        assert labels == list(LABELS)

    def test_it_reports_how_many_it_named(self, server):
        _, output = quiet(server.decode_from_subtypes, pipe_frame(),
                          config.DISTRIBUTION_PIPE_LAYER_ID, "distribution_pipes")
        assert "decoded 6/6" in output

    def test_no_codes_means_no_decode_rather_than_a_wrong_one(self, server):
        frame = pipe_frame().drop(columns=[schema.ASSETGROUP])
        labels, _ = quiet(server.decode_from_subtypes, frame,
                          config.DISTRIBUTION_PIPE_LAYER_ID, "distribution_pipes")
        assert labels is None

    @pytest.mark.parametrize("layer_key", ["distribution_pipes", "service_pipes"])
    def test_both_layers_get_a_family_per_pipe(self, server, layer_key):
        result, _ = quiet(server.add_pipe_material_fields, pipe_frame(), layer_key)
        assert list(result["PipeMaterialDomain"]) == list(LABELS)
        assert list(result["PipeMaterialFamily"]) == list(FAMILIES)

    def test_the_raw_assettype_is_kept_beside_the_label(self, server):
        result, _ = quiet(server.add_pipe_material_fields, pipe_frame(),
                          "distribution_pipes")
        # clean_value keeps an int an int, so compare against the codes as given.
        assert list(result[schema.PIPE_MATERIAL_RAW]) == [code for _, code in PAIRS]

    def test_every_family_gets_its_own_colour(self, server):
        result, _ = quiet(server.add_pipe_material_fields, pipe_frame(),
                          "distribution_pipes")
        colours = dict(zip(result["PipeMaterialFamily"], result["PipeMaterialColor"]))
        assert len(set(colours.values())) == len(colours)
        for family, colour in colours.items():
            assert colour == server.MATERIAL_COLORS[family]

    def test_copper_is_not_coloured_as_plastic(self, server):
        result, _ = quiet(server.add_pipe_material_fields, pipe_frame(),
                          "distribution_pipes")
        by_label = dict(zip(result["PipeMaterialDomain"], result["PipeMaterialFamily"]))
        assert by_label["Copper"] == "COPPER"

    def test_an_enriched_cache_is_still_trusted_first(self, server):
        """The enrichment script's column wins, so a repaired cache behaves as
        before and is not decoded twice."""
        frame = pipe_frame(**{schema.ASSETTYPE_DECODED: ["Copper"] * len(PAIRS)})
        result, _ = quiet(server.add_pipe_material_fields, frame, "distribution_pipes")
        assert set(result["PipeMaterialDomain"]) == {"Copper"}
        assert set(result["PipeMaterialFamily"]) == {"COPPER"}

    def test_a_frame_with_no_codes_at_all_says_so(self, server):
        frame = pipe_frame().drop(columns=[schema.ASSETGROUP, schema.ASSETTYPE])
        _, output = quiet(server.add_pipe_material_fields, frame, "distribution_pipes")
        assert "material cannot be named" in output
        assert assettype.UNCLASSIFIED_FAMILY in output


class TestThePageUsesOnePalette:
    def test_the_page_palette_is_generated_from_python(self, server):
        """Commit 87453fb changed the Python palette - PLASTIC to yellow, STEEL
        to blue - and the map did not change, because this line was a separate
        literal holding the old values."""
        import json
        page = server.html_page()
        assert "const MATERIAL_COLORS=" + json.dumps(server.MATERIAL_COLORS) in page

    @pytest.mark.parametrize("family", ["PLASTIC", "STEEL", "IRON", "COPPER",
                                        "UNKNOWN", "OTHER"])
    def test_every_family_colour_reaches_the_page(self, server, family):
        assert server.MATERIAL_COLORS[family] in server.html_page()

    def test_pipes_are_styled_per_feature_not_per_layer(self, server):
        """Colour comes from the feature's family, so one layer shows several
        materials."""
        page = server.html_page()
        assert "MATERIAL_COLORS[fam]" in page
        assert "p.PipeMaterialFamily" in page


class TestLeafletIsAlwaysReferenced:
    """Leaflet is committed under vendor/leaflet, so the map needs no internet and
    nothing built first. scripts/build_leaflet_context.py used to be the only
    thing that put it on disk, and it is gone."""

    @staticmethod
    def no_leaflet_anywhere(server, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "VENDORED_LEAFLET_DIR", tmp_path / "absent-repo")
        monkeypatch.setattr(server, "VENDOR", tmp_path / "absent-work")

    def test_the_copy_committed_in_the_repository_is_found(self, server):
        assert server.leaflet_dir() == config.VENDORED_LEAFLET_DIR
        assert server.leaflet_refs() == ("/leaflet/leaflet.css", "/leaflet/leaflet.js")

    def test_the_committed_copy_is_complete(self):
        """Both files, plus the images leaflet.css asks for - without those the
        layers control draws without its icon."""
        for name in ("leaflet.css", "leaflet.js"):
            assert (config.VENDORED_LEAFLET_DIR / name).is_file(), name
        for name in ("layers.png", "layers-2x.png", "marker-icon.png"):
            assert (config.VENDORED_LEAFLET_DIR / "images" / name).is_file(), name

    def test_the_repository_copy_wins_over_the_work_root_one(
            self, server, monkeypatch, tmp_path):
        work = tmp_path / "leaflet"
        work.mkdir()
        for name in server.LEAFLET_FILES:
            (work / name).write_text("/* stub */")
        monkeypatch.setattr(server, "VENDOR", work)
        assert server.leaflet_dir() == config.VENDORED_LEAFLET_DIR

    def test_a_work_root_copy_is_still_honoured(self, server, monkeypatch, tmp_path):
        work = tmp_path / "leaflet"
        work.mkdir()
        for name in server.LEAFLET_FILES:
            (work / name).write_text("/* stub */")
        monkeypatch.setattr(config, "VENDORED_LEAFLET_DIR", tmp_path / "absent")
        monkeypatch.setattr(server, "VENDOR", work)
        assert server.leaflet_dir() == work

    def test_no_local_copy_falls_back_to_the_cdn_and_warns(
            self, server, monkeypatch, tmp_path):
        """It used to emit /leaflet/leaflet.js regardless; the request 404'd and
        the page rendered as an empty white rectangle, with only
        "L is not defined" in the console to say why."""
        self.no_leaflet_anywhere(server, monkeypatch, tmp_path)
        refs, output = quiet(server.leaflet_refs)
        assert all(ref.startswith(server.LEAFLET_CDN) for ref in refs)
        assert "blank" in output

    def test_the_page_never_references_a_missing_local_asset(
            self, server, monkeypatch, tmp_path):
        self.no_leaflet_anywhere(server, monkeypatch, tmp_path)
        page, _ = quiet(server.html_page)
        assert "/leaflet/leaflet.js" not in page
        assert f"{server.LEAFLET_CDN}/leaflet.js" in page

    @pytest.mark.parametrize(("name", "content_type"), [
        ("leaflet.css", "text/css"),
        ("leaflet.js", "application/javascript"),
        ("layers.png", "image/png"),
        ("anything.else", "application/octet-stream"),
    ])
    def test_assets_are_served_with_a_sensible_content_type(
            self, server, name, content_type):
        assert server.guess_content_type(name) == content_type


class TestTheServerCanBeStartedByRunPy:
    def test_serve_is_callable(self, server):
        """run.py calls this. It used to exist only as module-level code
        under __main__, so nothing could start it."""
        assert callable(server.serve)

    def test_every_pipe_layer_is_configured_with_its_layer_id(self, server):
        """The decode needs the layer the frame came from, including the retired
        one, which is on the MA service rather than the DNV NY service."""
        assert server.PIPE_LAYER_IDS_BY_KEY == {
            "distribution_pipes": config.DISTRIBUTION_PIPE_LAYER_ID,
            "service_pipes": config.SERVICE_PIPE_LAYER_ID,
            "retired_pipes": config.RETIRED_PIPE_LAYER_ID,
        }


@pytest.fixture(scope="module")
def run_py():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "run_entry", os.path.join(REPO_ROOT, "run.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestRunPyServesThisMap:
    """run.py used to build a static GeoJSON map and need --pipes for a second
    one that showed everything the first did. It now serves this map only."""

    def test_it_serves_the_bbox_map(self, run_py):
        assert callable(run_py.serve_map)
        assert not hasattr(run_py, "build_view"), (
            "the static build is no longer part of this path")

    def test_there_is_no_pipes_flag_left(self, run_py):
        """The pipes are the default now, so a flag for them would be a lie."""
        args = run_py.parse_args([])
        assert not hasattr(args, "pipes")

    def test_opposite_flags_are_rejected(self, run_py):
        with pytest.raises(RuntimeError, match="opposite"), \
                redirect_stdout(io.StringIO()):
            run_py.main(["--no-view", "--view-only"])

    def test_missing_pipe_caches_are_detected(self, run_py, monkeypatch, tmp_path):
        monkeypatch.setattr(run_py.config, "LAYER_CACHE_DIR", tmp_path)
        assert run_py.missing_pipe_caches() == list(run_py.PIPE_CACHES)

        (tmp_path / "distribution_pipes.pkl.gz").write_bytes(b"")
        assert run_py.missing_pipe_caches() == ["service_pipes"]

    def test_serving_without_caches_names_the_command_that_makes_them(
            self, run_py, monkeypatch, tmp_path):
        monkeypatch.setattr(run_py.config, "LAYER_CACHE_DIR", tmp_path)
        with pytest.raises(RuntimeError, match="run.py --no-view"), \
                redirect_stdout(io.StringIO()):
            run_py.serve_map(None, open_browser=False)

    def test_the_port_falls_through_to_the_map_servers_own(
            self, run_py, monkeypatch, tmp_path):
        for name in run_py.PIPE_CACHES:
            (tmp_path / f"{name}.pkl.gz").write_bytes(b"")
        monkeypatch.setattr(run_py.config, "LAYER_CACHE_DIR", tmp_path)

        import leaflet_bbox_server as map_server
        served = {}
        monkeypatch.setattr(map_server, "serve",
                            lambda port, open_browser: served.update(port=port))

        with redirect_stdout(io.StringIO()):
            run_py.serve_map(None, open_browser=False)
        assert served["port"] == map_server.PORT

        with redirect_stdout(io.StringIO()):
            run_py.serve_map(8800, open_browser=False)
        assert served["port"] == 8800


class TestBothPipeLayersAreOnAtStartup:
    """They used to be off, so the map opened with no pipes on it and the two
    layers the whole thing is about had to be found in the control first."""

    @pytest.mark.parametrize("key", ["main_lines", "service_lines"])
    def test_the_pipe_layer_is_a_default(self, server, key):
        assert server.LAYERS[key]["default"] is True

    def test_every_layer_is_on(self, server):
        off = [key for key, cfg in server.LAYERS.items() if not cfg["default"]]
        assert off == []

    def test_the_page_adds_defaults_to_the_map(self, server):
        """One loop reads the flag, so the config above is what the page acts on."""
        assert "if(LAYER_CONFIG[key].default){groups[key].addTo(map)" in server.html_page()


class TestTheMapZoomsFurtherThanTheTiles:
    def test_the_map_max_zoom_is_past_the_tile_providers(self, server):
        assert server.MAX_ZOOM > server.OSM_MAX_NATIVE_ZOOM
        assert server.MAX_ZOOM > server.ESRI_MAX_NATIVE_ZOOM

    def test_the_page_sets_the_map_max_zoom(self, server):
        assert f"maxZoom:{server.MAX_ZOOM}" in server.html_page()

    def test_every_tile_layer_upscales_past_its_native_zoom(self, server):
        """Without maxNativeZoom a tile layer refuses to render past its own
        maxZoom, and Leaflet caps the map at the same place - so zooming stops."""
        page = server.html_page()
        assert page.count("maxNativeZoom:") == 3
        for native in (server.OSM_MAX_NATIVE_ZOOM, server.ESRI_MAX_NATIVE_ZOOM):
            assert f"maxNativeZoom:{native}" in page

    def test_no_tile_layer_still_caps_the_map_at_its_own_zoom(self, server):
        page = server.html_page()
        assert f"maxZoom:{server.OSM_MAX_NATIVE_ZOOM}," not in page


class TestMainsAndServicesAreParentGroups:
    """Each parent carries the four things belonging to one kind of leak: the
    pipe, the original location, the relocated location and the line joining
    them. Leaflet's own control is a flat list, so the control is built here."""

    def test_each_parent_has_its_four_children(self, server):
        for group, expected in [("mains", ["main_lines", "main_leaks_original",
                                           "main_leaks_relocated", "main_connectors"]),
                                ("services", ["service_lines", "service_leaks_original",
                                              "service_leaks_relocated",
                                              "service_connectors"])]:
            children = [k for k, v in server.LAYERS.items() if v["group"] == group]
            assert children == expected

    def test_the_pipe_layers_read_the_downloaded_caches(self, server):
        assert server.LAYERS["main_lines"]["cache"] == "distribution_pipes"
        assert server.LAYERS["service_lines"]["cache"] == "service_pipes"

    def test_the_relocated_layers_are_one_source_split_by_linkedlayer(self, server):
        assert server.LAYERS["main_leaks_relocated"]["linked"] == "distribution"
        assert server.LAYERS["service_leaks_relocated"]["linked"] == "service"
        assert (server.LAYERS["main_leaks_relocated"]["layer"]
                == server.LAYERS["service_leaks_relocated"]["layer"])

    def test_the_original_leaks_are_split_by_facility(self, server):
        assert server.LAYERS["main_leaks_original"]["facility"] == "distribution"
        assert server.LAYERS["service_leaks_original"]["facility"] == "service"

    def test_the_control_markup_has_a_parent_and_children_per_group(self, server):
        html = server.grouped_control_html()
        assert html.count('class="parent"') == len(server.GROUPS)
        assert html.count('class="child"') == len(server.LAYERS)
        for group in server.GROUPS:
            assert f'data-group="{group}"' in html
        for key in server.LAYERS:
            assert f'data-layer="{key}"' in html

    def test_the_page_carries_the_markup_and_the_toggle(self, server):
        page = server.html_page()
        assert "GROUP_CONTROL_HTML" in page
        assert "function setLayer" in page
        # A parent switching its children, and children updating their parent.
        assert "box.dataset.group" in page
        assert "function syncParents" in page

    def test_the_data_layers_are_not_in_leaflets_own_control(self, server):
        """A child added to the map through a parent is not on the map as itself,
        so Leaflet's checkbox would contradict what is drawn."""
        page = server.html_page()
        assert "L.control.layers(baseMaps,{'Red data extent box'" in page

    @pytest.mark.parametrize(("facility", "group"), [
        ("Main", "distribution"), ("Distribution", "distribution"),
        ("Service", "service"), ("Service Line", "service"),
    ])
    def test_leaks_are_split_the_way_the_matcher_routed_them(
            self, server, facility, group):
        """Same routing the workflow used, so the map agrees with the output."""
        gpd = pytest.importorskip("geopandas")
        from shapely.geometry import Point
        frame = gpd.GeoDataFrame({"SuppFacilityType": [facility],
                                  "geometry": [Point(0, 0)]}, crs="EPSG:4326")
        kept, _ = quiet(server.leak_belongs_to, frame, group)
        assert len(kept) == 1
        other = "service" if group == "distribution" else "distribution"
        kept_other, _ = quiet(server.leak_belongs_to, frame, other)
        assert len(kept_other) == 0

    def test_an_unrecognised_facility_appears_under_both(self, server):
        """route_layers searches both when it cannot tell, so the map shows it in
        both rather than hiding it from one."""
        gpd = pytest.importorskip("geopandas")
        from shapely.geometry import Point
        frame = gpd.GeoDataFrame({"SuppFacilityType": ["something else"],
                                  "geometry": [Point(0, 0)]}, crs="EPSG:4326")
        for group in ("distribution", "service"):
            kept, _ = quiet(server.leak_belongs_to, frame, group)
            assert len(kept) == 1
