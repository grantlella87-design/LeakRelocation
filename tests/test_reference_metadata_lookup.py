"""The committed metadata for a layer must be found wherever it lives.

The abandoned pipe layer could not name its materials - PipeMaterialDomain came
out empty and every retired pipe drew in one colour - while the file it needed was
committed and two directories away. Two independent mismatches hid it:

  * only config.REFERENCE_DIR was searched, which is the DNV NY service, while
    layer 62 is on the MA service and was saved under MA_Material_View_MA/;
  * the glob was layer_062_*.json, which does not match the file's actual name,
    layer_0062_Pipeline_Line_Abandoned.json.

Either one alone was enough to lose it.
"""
import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from leakrelocation import assettype, config


class TestTheRetiredLayerDecodes:
    def test_layer_62_metadata_is_in_the_checkout(self):
        found = [path.name for path in assettype.reference_layer_paths()]
        assert any("0062" in name or "_62_" in name for name in found), found

    def test_it_is_not_in_the_configured_directory(self):
        """Which is the point: it is on a different service, so a lookup that only
        searches REFERENCE_DIR cannot find it."""
        assert not list(config.REFERENCE_DIR.glob("layer_*62*.json"))

    def test_the_decoder_is_built_for_it(self):
        type_id_field, decoder = assettype.decoder_for_layer(
            config.RETIRED_PIPE_LAYER_ID)
        assert type_id_field == "ASSETGROUP"
        assert len(decoder) > 50, "no subtype domains were read"

    def test_the_pipe_layers_still_decode(self):
        for layer_id in (config.DISTRIBUTION_PIPE_LAYER_ID,
                         config.SERVICE_PIPE_LAYER_ID):
            type_id_field, decoder = assettype.decoder_for_layer(layer_id)
            assert type_id_field == "ASSETGROUP"
            assert len(decoder) > 50

    def test_a_layer_with_no_copy_still_returns_nothing(self):
        assert assettype.decoder_for_layer(9999) == (None, {})


class TestFindingALayerFile:
    def test_zero_padding_does_not_matter(self, tmp_path, monkeypatch):
        """layer_62_, layer_062_ and layer_0062_ all name the same layer. The
        original glob pinned one width and missed the file that was there."""
        root = tmp_path / "mapserver_json"
        service = root / "Some_Service"
        service.mkdir(parents=True)
        monkeypatch.setattr(config, "REFERENCE_ROOT", root)
        monkeypatch.setattr(config, "REFERENCE_DIR", root / "not_here")

        for name in ("layer_62_A.json", "layer_062_B.json", "layer_0062_C.json"):
            (service / name).write_text(json.dumps({"id": 62, "fields": []}))
        assert assettype.reference_layer_json(62) is not None

    def test_a_number_inside_the_name_is_not_a_match(self, tmp_path, monkeypatch):
        """layer_206_Hist_GasLeak.json must not answer a request for layer 6, and
        it did not before either - but matching on the digits between the
        underscores is what keeps that true."""
        root = tmp_path / "mapserver_json"
        service = root / "Some_Service"
        service.mkdir(parents=True)
        monkeypatch.setattr(config, "REFERENCE_ROOT", root)
        monkeypatch.setattr(config, "REFERENCE_DIR", root / "not_here")

        (service / "layer_206_Hist_GasLeak.json").write_text(
            json.dumps({"id": 206, "fields": []}))
        assert assettype.reference_layer_json(206) is not None
        assert assettype.reference_layer_json(6) is None
        assert assettype.reference_layer_json(20) is None

    def test_the_configured_directory_wins(self, tmp_path, monkeypatch):
        """A service this project reads directly keeps priority over a copy
        someone added for reference."""
        root = tmp_path / "mapserver_json"
        primary = root / "Primary"
        other = root / "Other"
        primary.mkdir(parents=True)
        other.mkdir(parents=True)
        monkeypatch.setattr(config, "REFERENCE_ROOT", root)
        monkeypatch.setattr(config, "REFERENCE_DIR", primary)

        (primary / "layer_006_Primary.json").write_text(
            json.dumps({"id": 6, "name": "primary", "fields": []}))
        (other / "layer_006_Other.json").write_text(
            json.dumps({"id": 6, "name": "other", "fields": []}))
        assert assettype.reference_layer_json(6)["name"] == "primary"

    def test_a_missing_root_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "REFERENCE_ROOT", tmp_path / "nothing")
        monkeypatch.setattr(config, "REFERENCE_DIR", tmp_path / "nothing either")
        assert assettype.reference_layer_json(6) is None
        assert assettype.reference_layer_paths() == []

    def test_a_file_that_is_not_a_layer_is_skipped(self, tmp_path, monkeypatch):
        """manifest.json and service.json sit in the same folder."""
        root = tmp_path / "mapserver_json"
        service = root / "Some_Service"
        service.mkdir(parents=True)
        monkeypatch.setattr(config, "REFERENCE_ROOT", root)
        monkeypatch.setattr(config, "REFERENCE_DIR", root / "not_here")

        (service / "manifest.json").write_text("{}")
        (service / "layer_notanumber_x.json").write_text("{}")
        assert assettype.reference_layer_json(6) is None


class TestTheMapCanNameRetiredPipeMaterials:
    """The end of the chain the user sees: a retired pipe cache carrying the two
    codes must come out with a named material."""

    @pytest.fixture
    def server(self):
        pytest.importorskip("geopandas")
        sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
        import leaflet_bbox_server
        return leaflet_bbox_server

    def test_retired_pipes_get_a_material_domain(self, server):
        import io
        from contextlib import redirect_stdout

        import geopandas as gpd
        from shapely.geometry import LineString

        # ASSETGROUP/ASSETTYPE pairs taken from the committed layer 62 domains.
        _, decoder = assettype.decoder_for_layer(config.RETIRED_PIPE_LAYER_ID)
        (group, asset_type), entry = next(iter(sorted(decoder.items())))

        frame = gpd.GeoDataFrame(
            {"OBJECTID": [1], "ASSETGROUP": [group], "ASSETTYPE": [asset_type]},
            geometry=[LineString([(-71, 42), (-71.01, 42.01)])], crs="EPSG:4326")
        with redirect_stdout(io.StringIO()):
            out = server.add_pipe_material_fields(frame, "retired_pipes")

        assert out["PipeMaterialDomain"].iloc[0] == entry["ASSETTYPE_DECODED"]
        assert out["PipeMaterialDomain"].notna().all(), "the material stayed empty"

    def test_the_layer_id_is_mapped_for_that_cache(self, server):
        """Without this entry the decode is asked for layer None."""
        assert server.PIPE_LAYER_IDS_BY_KEY["retired_pipes"] == \
            config.RETIRED_PIPE_LAYER_ID
