"""Tests for ASSETGROUP/ASSETTYPE decoding and the cache enrichment run.

The enrichment integration test drives the real script against a stub ArcGIS
module and a temporary cache, so no network or shared drive is involved.
"""
import os
import sys
from typing import ClassVar

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from leakrelocation import assettype

pd = pytest.importorskip("pandas")


class TestNormCode:
    @pytest.mark.parametrize("value,expected", [
        (None, None),
        ("", None),
        ("   ", None),
        (5, "5"),
        ("5", "5"),
        (5.0, "5"),
        ("5.0", "5"),
        ("-3.0", "-3"),
        ("5.5", "5.5"),
        ("ABC", "ABC"),
    ])
    def test_norm_code(self, value, expected):
        assert assettype.norm_code(value) == expected


class TestFamilyFromAssettype:
    @pytest.mark.parametrize("label,family", [
        ("Plastic PE", "PLASTIC"),
        ("Polyethylene", "PLASTIC"),
        ("Polybutylene", "PLASTIC"),
        ("HDPE", "PLASTIC"),
        ("MDPE", "PLASTIC"),
        ("Cast Iron", "IRON"),
        ("Ductile Iron", "IRON"),
        ("Wrought Iron", "IRON"),
        ("Reconditioned Cast Iron", "IRON"),
        ("Bare Steel", "STEEL"),
        ("Coated Steel", "STEEL"),
        ("Galvanized Steel", "STEEL"),
        ("Composite", "UNKNOWN"),
        ("Unknown", "UNKNOWN"),
        ("UNK", "UNKNOWN"),
        ("", "UNKNOWN"),
        (None, "UNKNOWN"),
        ("Brass", "OTHER"),
    ])
    def test_families(self, label, family):
        assert assettype.family_from_assettype(label) == family

    @pytest.mark.parametrize("label", ["Copper", "COPPER", "Copper Tubing"])
    def test_copper_is_not_plastic(self, label):
        # Substring matching classified copper as PLASTIC because "COPPER"
        # contains "PE" and PLASTIC is checked first.
        assert assettype.family_from_assettype(label) == "COPPER"


LAYER_JSON = {
    "typeIdField": "ASSETGROUP",
    "types": [
        {
            "id": 1,
            "name": "Distribution Main",
            "domains": {
                "ASSETTYPE": {
                    "name": "MainAssetType",
                    "codedValues": [
                        {"code": 2, "name": "Cast Iron"},
                        {"code": 5, "name": "Copper"},
                        {"code": 9, "name": "Plastic PE"},
                    ],
                }
            },
        },
        {
            "id": 2,
            "name": "Service",
            "domains": {
                "othertype": {"name": "Ignored", "codedValues": [{"code": 1, "name": "Nope"}]},
            },
        },
    ],
}


class TestBuildDecoder:
    def test_decodes_subtype_domains(self):
        type_id_field, decoder = assettype.build_assettype_decoder(LAYER_JSON)
        assert type_id_field == "ASSETGROUP"
        assert decoder[("1", "2")]["ASSETTYPE_DECODED"] == "Cast Iron"
        assert decoder[("1", "2")]["ASSETGROUP_DECODED"] == "Distribution Main"
        assert decoder[("1", "2")]["ASSETTYPE_DOMAIN"] == "MainAssetType"
        assert decoder[("1", "2")]["PipeMaterialFamily"] == "IRON"

    def test_copper_family_in_decoder(self):
        _, decoder = assettype.build_assettype_decoder(LAYER_JSON)
        assert decoder[("1", "5")]["PipeMaterialFamily"] == "COPPER"

    def test_subtypes_without_assettype_domain_are_skipped(self):
        _, decoder = assettype.build_assettype_decoder(LAYER_JSON)
        assert all(key[0] != "2" for key in decoder)

    def test_empty_layer_json(self):
        assert assettype.build_assettype_decoder({}) == (None, {})


class StubResponse:
    pass


class StubWorkflowModule:
    """Stands in for the workflow module: serves layer metadata and pages."""

    DISTRIBUTION_PIPE_URL = "https://stub/MapServer/6"
    SERVICE_PIPE_URL = "https://stub/MapServer/7"

    def __init__(self, features):
        self.features = features
        self.calls = []

    def make_session(self):
        return object()

    def request_json(self, session, url, params=None):
        params = params or {}
        self.calls.append((url, dict(params)))
        if params.get("returnCountOnly") == "true":
            return {"count": len(self.features)}
        if not url.endswith("/query"):
            return LAYER_JSON
        offset = int(params.get("resultOffset", 0))
        size = int(params.get("resultRecordCount", 100))
        page = self.features[offset:offset + size]
        return {"features": [{"attributes": attrs} for attrs in page]}


@pytest.fixture
def enrich_env(tmp_path, monkeypatch):
    """Point config at a temp cache and provide a stub service."""
    import enrich_assettype_cache as runner

    from leakrelocation import config

    cache_dir = tmp_path / "layer_cache"
    cache_dir.mkdir()
    monkeypatch.setattr(config, "LAYER_CACHE_DIR", cache_dir)
    monkeypatch.setattr(config, "ENRICHMENT_DIR", tmp_path / "enrichment")

    cache = pd.DataFrame({
        "OBJECTID": [1, 2, 3],
        "material": ["Grade A", "Grade B", "Grade C"],
        "nominaldiameter": [2, 4, 6],
    })
    cache.to_pickle(cache_dir / "distribution_pipes.pkl.gz", compression="gzip")

    features = [
        {"OBJECTID": 1, "ASSETGROUP": 1, "ASSETTYPE": 2},   # Cast Iron
        {"OBJECTID": 2, "ASSETGROUP": 1, "ASSETTYPE": 5},   # Copper
        {"OBJECTID": 3, "ASSETGROUP": 1, "ASSETTYPE": 9},   # Plastic PE
    ]
    return runner, StubWorkflowModule(features), cache_dir


def run_enrich(runner, module, **overrides):
    args = runner.argparse.Namespace(
        layer="distribution", workers=overrides.get("workers", 1),
        page_size=overrides.get("page_size", 2), force=overrides.get("force", False),
        dry_run=overrides.get("dry_run", False))
    runner.enrich_layer(module, module.make_session(), "distribution", args)
    return args


class TestEnrichLayer:
    def test_decodes_and_writes_cache(self, enrich_env):
        runner, module, cache_dir = enrich_env
        run_enrich(runner, module)

        df = pd.read_pickle(cache_dir / "distribution_pipes.pkl.gz", compression="gzip")
        by_oid = df.set_index("OBJECTID")

        assert list(by_oid.loc[[1, 2, 3], "ASSETTYPE_DECODED"]) == ["Cast Iron", "Copper", "Plastic PE"]
        assert list(by_oid.loc[[1, 2, 3], "PipeMaterialFamily"]) == ["IRON", "COPPER", "PLASTIC"]

    def test_material_becomes_decoded_assettype(self, enrich_env):
        runner, module, cache_dir = enrich_env
        run_enrich(runner, module)
        df = pd.read_pickle(cache_dir / "distribution_pipes.pkl.gz", compression="gzip")
        assert list(df["material"]) == ["Cast Iron", "Copper", "Plastic PE"]

    def test_pipe_material_raw_holds_the_raw_assettype(self, enrich_env):
        """Pipe material comes from ASSETTYPE: PipeMaterialRaw is the subtype
        code as the service returns it, and "material" is its domain value.
        Both used to be set to the decoded value, so nothing kept the code."""
        runner, module, cache_dir = enrich_env
        run_enrich(runner, module)
        df = pd.read_pickle(cache_dir / "distribution_pipes.pkl.gz", compression="gzip")
        by_oid = df.set_index("OBJECTID")
        assert list(by_oid.loc[[1, 2, 3], "PipeMaterialRaw"]) == [2, 5, 9]
        assert list(by_oid.loc[[1, 2, 3], "ASSETTYPE"]) == [2, 5, 9]

    def test_raw_and_domain_are_different_columns(self, enrich_env):
        runner, module, cache_dir = enrich_env
        run_enrich(runner, module)
        df = pd.read_pickle(cache_dir / "distribution_pipes.pkl.gz", compression="gzip")
        assert list(df["PipeMaterialRaw"]) != list(df["material"])

    def test_family_is_derived_from_the_domain_value(self, enrich_env):
        """Classifying the raw code would compare a number against material
        names and land everything in UNKNOWN."""
        runner, module, cache_dir = enrich_env
        run_enrich(runner, module)
        df = pd.read_pickle(cache_dir / "distribution_pipes.pkl.gz", compression="gzip")
        by_oid = df.set_index("OBJECTID")
        assert list(by_oid.loc[[1, 2, 3], "PipeMaterialFamily"]) == ["IRON", "COPPER", "PLASTIC"]

    def test_original_material_preserved_as_grade(self, enrich_env):
        runner, module, cache_dir = enrich_env
        run_enrich(runner, module)
        df = pd.read_pickle(cache_dir / "distribution_pipes.pkl.gz", compression="gzip")
        assert list(df["GradeMaterial"]) == ["Grade A", "Grade B", "Grade C"]

    def test_backup_written_once(self, enrich_env):
        runner, module, cache_dir = enrich_env
        run_enrich(runner, module)
        backup = cache_dir / "distribution_pipes.pkl.gz.before_assettype_as_material.bak"
        assert backup.exists()
        original = pd.read_pickle(backup, compression="gzip")
        assert list(original["material"]) == ["Grade A", "Grade B", "Grade C"]

    def test_pagination_covers_all_rows(self, enrich_env):
        runner, module, cache_dir = enrich_env
        run_enrich(runner, module, page_size=1)
        df = pd.read_pickle(cache_dir / "distribution_pipes.pkl.gz", compression="gzip")
        assert df["ASSETTYPE_DECODED"].notna().all()

    def test_threaded_matches_serial(self, enrich_env):
        runner, module, cache_dir = enrich_env
        run_enrich(runner, module, workers=4, page_size=1)
        df = pd.read_pickle(cache_dir / "distribution_pipes.pkl.gz", compression="gzip")
        by_oid = df.sort_values("OBJECTID")
        assert list(by_oid["PipeMaterialFamily"]) == ["IRON", "COPPER", "PLASTIC"]

    def test_dry_run_leaves_cache_untouched(self, enrich_env):
        runner, module, cache_dir = enrich_env
        run_enrich(runner, module, dry_run=True)
        df = pd.read_pickle(cache_dir / "distribution_pipes.pkl.gz", compression="gzip")
        assert "ASSETTYPE_DECODED" not in df.columns
        assert list(df["material"]) == ["Grade A", "Grade B", "Grade C"]

    def test_second_run_skips_unless_forced(self, enrich_env, capsys):
        runner, module, _cache_dir = enrich_env
        run_enrich(runner, module)
        capsys.readouterr()
        run_enrich(runner, module)
        assert "SKIP: already decoded" in capsys.readouterr().out

    def test_force_re_enriches_without_duplicating_columns(self, enrich_env):
        runner, module, cache_dir = enrich_env
        run_enrich(runner, module)
        run_enrich(runner, module, force=True)
        df = pd.read_pickle(cache_dir / "distribution_pipes.pkl.gz", compression="gzip")
        assert len(df.columns) == len(set(df.columns))
        assert list(df["GradeMaterial"]) == ["Grade A", "Grade B", "Grade C"]
        assert len(df) == 3


class TestPreflightDiagnosesWhyRawIsEmpty:
    """PipeMaterialRaw can be blank for three different reasons, which look
    identical in the viewer. The preflight has to tell them apart."""

    @pytest.fixture
    def preflight(self):
        import preflight_assettype_cache_check
        return preflight_assettype_cache_check

    def write(self, cache_dir, **columns):
        frame = pd.DataFrame(columns)
        for name in ["distribution_pipes", "service_pipes"]:
            frame.to_pickle(cache_dir / f"{name}.pkl.gz", compression="gzip")

    @pytest.fixture
    def cache_dir(self, tmp_path, monkeypatch):
        from leakrelocation import config
        directory = tmp_path / "layer_cache"
        directory.mkdir()
        monkeypatch.setattr(config, "LAYER_CACHE_DIR", directory)
        return directory

    ENRICHED: ClassVar[dict] = {
        "OBJECTID": [1, 2, 3],
        "ASSETGROUP": [1, 1, 1],
        "ASSETTYPE": [2, 5, 9],
        "ASSETGROUP_DECODED": ["Main"] * 3,
        "ASSETTYPE_DECODED": ["Cast Iron", "Copper", "Plastic PE"],
        "ASSETTYPE_DOMAIN": ["D"] * 3,
        "PipeMaterialFamily": ["IRON", "COPPER", "PLASTIC"],
        "PipeMaterialRaw": [2, 5, 9],
        "GradeMaterial": ["G1", "G2", "G3"],
        "material": ["Cast Iron", "Copper", "Plastic PE"],
    }

    def test_a_populated_cache_passes(self, preflight, cache_dir, capsys):
        self.write(cache_dir, **self.ENRICHED)
        assert preflight.main() == 0
        assert "PASS" in capsys.readouterr().out

    def test_missing_cache_is_reported(self, preflight, cache_dir, capsys):
        assert preflight.main() == 1
        assert "MISSING CACHE" in capsys.readouterr().out

    def test_unenriched_cache_names_the_grade_risk(self, preflight, cache_dir, capsys):
        # material still holds the DNV Grade field, so families would come from
        # grade data - the thing the project's key rule forbids.
        self.write(cache_dir, OBJECTID=[1, 2], material=["Grade A", "Grade B"])
        assert preflight.main() == 1
        output = capsys.readouterr().out
        assert "NOT ENRICHED" in output
        assert "Grade" in output

    def test_enriched_but_all_null_is_distinguished(self, preflight, cache_dir, capsys):
        columns = {key: ([None] * 3 if key not in ("OBJECTID", "GradeMaterial") else value)
                   for key, value in self.ENRICHED.items()}
        self.write(cache_dir, **columns)
        assert preflight.main() == 1
        output = capsys.readouterr().out
        assert "ENRICHED BUT EMPTY" in output
        assert "--force" in output

    def test_single_family_across_a_layer_is_flagged(self, preflight, cache_dir, capsys):
        columns = dict(self.ENRICHED)
        columns["ASSETTYPE_DECODED"] = ["Steel Pipe"] * 3
        columns["material"] = ["Steel Pipe"] * 3
        columns["PipeMaterialFamily"] = ["PLASTIC"] * 3
        self.write(cache_dir, **columns)
        preflight.main()
        assert "every row classifies as" in capsys.readouterr().out
