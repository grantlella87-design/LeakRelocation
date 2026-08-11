"""Checks this project's assumptions against the DNV service's own metadata.

reference/mapserver_json/NY_DNV_Synergi_RiskResults_Assets_NY/ holds a
point-in-time copy of the MapServer metadata and every layer JSON. That makes
the field names and subtype domains checkable offline, so the names this code
uses are facts rather than guesses, and a name that stops existing fails here
instead of silently resolving to nothing during a production run.

If these fail after the service changes, re-copy the reference folder and read
what actually moved - do not widen a list back into guesses.
"""
import io
import json
import os
import sys
from contextlib import redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from leakrelocation import assettype, config, matching

REFERENCE = os.path.join(REPO_ROOT, "reference", "mapserver_json",
                         "NY_DNV_Synergi_RiskResults_Assets_NY")

DISTRIBUTION_PIPE = "layer_006_Distribution_Pipe.json"
SERVICE_PIPE = "layer_007_Service_Pipe.json"
HISTORIC_LEAK = "layer_206_Hist_GasLeak.json"

pytestmark = pytest.mark.skipif(
    not os.path.isdir(REFERENCE),
    reason="reference service metadata is not in this checkout")


def load_layer(filename):
    with open(os.path.join(REFERENCE, filename), encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def distribution():
    return load_layer(DISTRIBUTION_PIPE)


@pytest.fixture(scope="module")
def service():
    return load_layer(SERVICE_PIPE)


@pytest.fixture(scope="module")
def leaks():
    return load_layer(HISTORIC_LEAK)


def names(layer):
    return [field["name"] for field in layer.get("fields") or []]


@pytest.fixture(scope="module")
def workflow():
    """The workflow module, imported without its heavy geospatial stack."""
    pytest.importorskip("geopandas")
    pytest.importorskip("keyring")
    sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
    import leak_relocation_geopandas
    return leak_relocation_geopandas


class TestTheReferenceIsWhatWeThinkItIs:
    def test_the_layers_are_the_ones_the_workflow_reads(self, distribution, service, leaks):
        assert distribution["name"] == "Distribution Pipe"
        assert service["name"] == "Service Pipe"
        assert leaks["name"] == "Hist_GasLeak"

    def test_the_configured_layer_ids_match_the_reference_filenames(self):
        assert config.DISTRIBUTION_PIPE_LAYER_ID == 6
        assert config.SERVICE_PIPE_LAYER_ID == 7
        assert config.HISTORIC_LEAK_LAYER_ID == 206

    def test_pipes_are_lines_and_leaks_are_points(self, distribution, service, leaks):
        assert distribution["geometryType"] == "esriGeometryPolyline"
        assert service["geometryType"] == "esriGeometryPolyline"
        assert leaks["geometryType"] == "esriGeometryPoint"


class TestFieldNamesUsedByTheWorkflow:
    """Each list must resolve on the layers it is used against, and every
    spelling in it must be real - a list that resolves while carrying six dead
    spellings hides which name is the true one."""

    def resolved(self, workflow, layer, candidates):
        return matching.resolve_field_name(names(layer), candidates)

    def test_modified_field(self, workflow, distribution, service, leaks):
        for layer in (distribution, service, leaks):
            assert self.resolved(workflow, layer,
                                 workflow.MODIFIED_FIELD_CANDIDATES) == "LASTUPDATE"

    def test_leak_key(self, workflow, leaks):
        assert self.resolved(workflow, leaks,
                             workflow.LEAK_KEY_CANDIDATES) == "LMSLEAKNUMBER"

    def test_pipe_diameter(self, workflow, distribution, service):
        for layer in (distribution, service):
            assert self.resolved(workflow, layer,
                                 workflow.PIPE_DIAMETER_CANDIDATES) == "nominaldiameter"

    def test_pipe_material_is_assettype(self, workflow, distribution, service):
        """ASSETTYPE is the pipe material, with ASSETGROUP selecting the subtype
        domain that names it. Both are required, so both are requested."""
        assert workflow.PIPE_MATERIAL_FIELDS == ["ASSETGROUP", "ASSETTYPE"]
        for layer in (distribution, service):
            for name in workflow.PIPE_MATERIAL_FIELDS:
                assert self.resolved(workflow, layer, [name]) == name

    def test_the_dnv_material_field_is_not_requested(self, workflow):
        """It is a spec, grade or descriptor - not the material type - and is of
        no use to this workflow."""
        for attribute in dir(workflow):
            if not attribute.endswith("_CANDIDATES") or attribute.startswith("SUPP_"):
                continue
            values = getattr(workflow, attribute)
            if not isinstance(values, list):
                continue
            assert "material" not in [str(v).lower() for v in values], attribute

    def test_pipe_pressure(self, workflow, distribution, service):
        for layer in (distribution, service):
            assert self.resolved(workflow, layer,
                                 workflow.PIPE_PRESSURE_CANDIDATES) == "operatingpressure"

    def test_globalid_resolves_to_each_layers_own_spelling(
            self, workflow, distribution, service, leaks):
        """The pipe layers spell it GLOBALID, layer 206 spells it GlobalID, and
        the single candidate resolves to whichever the layer uses."""
        assert self.resolved(workflow, distribution, workflow.GLOBALID_CANDIDATES) == "GLOBALID"
        assert self.resolved(workflow, service, workflow.GLOBALID_CANDIDATES) == "GLOBALID"
        assert self.resolved(workflow, leaks, workflow.GLOBALID_CANDIDATES) == "GlobalID"

    def test_objectid(self, workflow, distribution, service, leaks):
        for layer in (distribution, service, leaks):
            assert self.resolved(workflow, layer,
                                 workflow.OBJECTID_CANDIDATES) == "OBJECTID"

    def test_jurisdiction(self, workflow, distribution, service, leaks):
        for layer in (distribution, service, leaks):
            assert self.resolved(workflow, layer,
                                 workflow.JURISDICTION_CANDIDATES) == "jurisdiction"

    @pytest.mark.parametrize("attribute", [
        "MODIFIED_FIELD_CANDIDATES",
        "LEAK_KEY_CANDIDATES",
        "PIPE_DIAMETER_CANDIDATES",
        "PIPE_PRESSURE_CANDIDATES",
        "GLOBALID_CANDIDATES",
        "OBJECTID_CANDIDATES",
        "JURISDICTION_CANDIDATES",
    ])
    def test_no_dead_spellings(self, workflow, distribution, service, leaks, attribute):
        """Every candidate exists on at least one of the three layers.

        These lists used to carry 34 names that exist nowhere on this service,
        including a misspelled "LMSLEAKSUMBER".
        """
        available = set(names(distribution)) | set(names(service)) | set(names(leaks))
        dead = [name for name in getattr(workflow, attribute) if name not in available]
        assert dead == [], f"{attribute} carries names no layer has: {dead}"

    @pytest.mark.parametrize("attribute", [
        "MODIFIED_FIELD_CANDIDATES",
        "LEAK_KEY_CANDIDATES",
        "PIPE_DIAMETER_CANDIDATES",
        "PIPE_PRESSURE_CANDIDATES",
        "GLOBALID_CANDIDATES",
        "OBJECTID_CANDIDATES",
        "JURISDICTION_CANDIDATES",
    ])
    def test_no_two_candidates_are_the_same_name_in_different_clothes(
            self, workflow, attribute):
        """resolve_field_name compares names with case and punctuation stripped,
        so "NOMINALDIAMETER" beside "nominaldiameter" is one candidate written
        twice - it can never be reached. A second entry must be a different
        field, not a different casing."""
        candidates = getattr(workflow, attribute)
        simplified = [matching.simplify_field_name(name) for name in candidates]
        assert len(set(simplified)) == len(simplified), (
            f"{attribute} has unreachable duplicates: {candidates}")


class TestLeakLayerHasNoMaterial:
    def test_the_leak_layer_carries_no_material_or_diameter(self, leaks):
        """Which is why leak material and diameter come from the supplemental
        CSV. Nothing in the service can supply them."""
        lowered = {name.lower() for name in names(leaks)}
        assert "material" not in lowered
        assert not [name for name in lowered if "diameter" in name]


class TestAssetTypeDomains:
    """The material class comes from these domains, so they are pinned here."""

    def test_the_subtype_field_is_assetgroup(self, distribution, service):
        for layer in (distribution, service):
            assert layer.get("typeIdField") == "ASSETGROUP"

    def test_every_subtype_pair_decodes(self, distribution):
        field, decoder = assettype.build_assettype_decoder(distribution)
        assert field == "ASSETGROUP"
        assert len(decoder) == 99
        assert all(info["ASSETTYPE_DECODED"] for info in decoder.values())

    def test_the_pipe_domain_is_the_hardcoded_fallback_table(self, distribution):
        """matching.SERVICE_ASSETTYPE_LABELS decodes codes when a value arrives
        without its layer metadata. It must equal the real domain for the pipe
        subtypes, or a decoded label is fiction."""
        for subtype in distribution["types"]:
            if subtype["name"] not in ("Service Pipe", "Distribution Pipe"):
                continue
            real = {int(entry["code"]): entry["name"]
                    for entry in subtype["domains"]["ASSETTYPE"]["codedValues"]}
            assert real == matching.SERVICE_ASSETTYPE_LABELS

    def test_codes_outside_the_pipe_subtypes_are_not_in_the_fallback_table(self, distribution):
        """Bond Wire (281) and Rectifier Cable (321) live on the same layer but
        are not pipes. The fallback table is the pipe domain, so it must not
        claim to know them; the per-subtype decoder handles them."""
        _, decoder = assettype.build_assettype_decoder(distribution)
        assert 281 not in matching.SERVICE_ASSETTYPE_LABELS
        assert 321 not in matching.SERVICE_ASSETTYPE_LABELS
        assert decoder[("8", "281")]["ASSETTYPE_DECODED"] == "Bond Wire"
        assert decoder[("9", "321")]["ASSETTYPE_DECODED"] == "Rectifier Cable"

    def test_the_same_code_means_different_things_by_subtype(self, distribution):
        """Which is why decoding is keyed on (ASSETGROUP, ASSETTYPE) and a flat
        table cannot be the whole answer: 999 is "UNK" for a pipe and "Unknown
        Type" for the unknown subtype."""
        _, decoder = assettype.build_assettype_decoder(distribution)
        assert decoder[("2", "999")]["ASSETTYPE_DECODED"] == "UNK"
        assert decoder[("999", "999")]["ASSETTYPE_DECODED"] == "Unknown Type"


@pytest.fixture(scope="module")
def families():
    _, decoder = assettype.build_assettype_decoder(load_layer(DISTRIBUTION_PIPE))
    return decoder


class TestMaterialFamiliesOverTheRealDomain:

    def test_no_copper_is_classified_as_plastic(self, families):
        """The bug that made the map show only plastic and unknown: "COPPER"
        contains "PE"."""
        copper = [info for info in families.values()
                  if info["ASSETTYPE_DECODED"] == "Copper"]
        assert copper, "the real domain has no Copper entry"
        assert all(info["PipeMaterialFamily"] == "COPPER" for info in copper)

    @pytest.mark.parametrize(("label", "family"), [
        ("Bare Steel", "STEEL"),
        ("Coated Steel", "STEEL"),
        ("Galvanized Steel", "STEEL"),
        ("Reconditioned Steel", "STEEL"),
        ("Cast Iron", "IRON"),
        ("Ductile Iron", "IRON"),
        ("Wrought Iron", "IRON"),
        ("Reconditioned Cast Iron", "IRON"),
        ("Plastic ABS", "PLASTIC"),
        ("Plastic PE", "PLASTIC"),
        ("Plastic PVC", "PLASTIC"),
        ("Plastic Other", "PLASTIC"),
        ("Polybutylene", "PLASTIC"),
        ("Copper", "COPPER"),
        ("Composite", "UNKNOWN"),
        ("Unknown", "UNKNOWN"),
        ("UNK", "UNKNOWN"),
        ("Unknown Type", "UNKNOWN"),
    ])
    def test_every_real_label_lands_in_the_expected_family(self, families, label, family):
        matches = {info["PipeMaterialFamily"] for info in families.values()
                   if info["ASSETTYPE_DECODED"] == label}
        assert matches == {family}, f"{label!r} classified as {matches}"

    def test_only_the_non_pipe_labels_are_unclassified(self, families):
        """A growing OTHER bucket would mean the taxonomy has stopped covering
        the domain. Today it is exactly the two non-pipe assets."""
        other = {info["ASSETTYPE_DECODED"] for info in families.values()
                 if info["PipeMaterialFamily"] == assettype.UNCLASSIFIED_FAMILY}
        assert other == {"Bond Wire", "Rectifier Cable"}

    def test_every_label_in_the_domain_is_classified_somehow(self, families):
        assert all(info["PipeMaterialFamily"] for info in families.values())


@pytest.fixture(scope="module")
def real_layer_ids():
    with open(os.path.join(REFERENCE, "manifest.json"), encoding="utf-8") as handle:
        return [entry["id"] for entry in json.load(handle)["layers"]]


class TestPipeLayerUrlMatching:
    """apply_pipe_domain_out_fields must fire for the pipe layers and only
    those. It used to test for "/mapserver/6" as a substring."""

    def url(self, layer_id, operation=""):
        return f"{config.GIS_ROOT}/dnv/rest/services/NY/X/MapServer/{layer_id}{operation}"

    def test_matches_the_pipe_layers(self, workflow):
        for layer_id in config.PIPE_LAYER_IDS:
            assert workflow.is_pipe_layer_url(self.url(layer_id))
            assert workflow.is_pipe_layer_url(self.url(layer_id, "/query"))

    def test_does_not_match_the_leak_layer(self, workflow):
        assert not workflow.is_pipe_layer_url(self.url(config.HISTORIC_LEAK_LAYER_ID))

    def test_does_not_match_layers_whose_id_merely_starts_with_a_pipe_id(
            self, workflow, real_layer_ids):
        """600, 601, 602 and 603 are real layers on this service, and
        "/mapserver/6" is a prefix of every one of them."""
        prefixed = [i for i in real_layer_ids
                    if i not in config.PIPE_LAYER_IDS
                    and any(str(i).startswith(str(p)) for p in config.PIPE_LAYER_IDS)]
        assert prefixed, "expected the service to have such layers"
        for layer_id in prefixed:
            assert not workflow.is_pipe_layer_url(self.url(layer_id)), layer_id
            assert not workflow.is_pipe_layer_url(self.url(layer_id, "/query")), layer_id

    def test_domain_fields_are_appended_only_for_pipes(self, workflow):
        params = {"outFields": "OBJECTID,nominaldiameter,material"}
        pipe = workflow.apply_pipe_domain_out_fields(
            self.url(config.DISTRIBUTION_PIPE_LAYER_ID, "/query"), params)
        assert pipe["outFields"].endswith(",ASSETGROUP,ASSETTYPE")

        other = workflow.apply_pipe_domain_out_fields(self.url(602, "/query"), params)
        assert other["outFields"] == params["outFields"]


# Pairs taken from the real domains, including the three a flat code table gets
# wrong: 999 differs by subtype, and 281 exists only outside the pipe subtypes.
DECODE_PAIRS = ((2, 9), (2, 5), (2, 2), (2, 1), (1, 15), (2, 999), (8, 281),
                (999, 999), (2, 13))
DECODE_EXPECTED = ("Plastic PE", "Copper", "Cast Iron", "Bare Steel",
                   "Galvanized Steel", "UNK", "Bond Wire", "Unknown Type",
                   "Polybutylene")


class TestTheWorkflowDecodesTheMaterialItself:
    """The download carries ASSETGROUP and ASSETTYPE, and the layer metadata
    names the material, so the workflow decodes it in-process. Before this, a
    run stopped after the download with "material is missing, so this cache is
    not ASSETTYPE-enriched" and had to be restarted after a separate script.
    """


    def frame(self, pairs=None, **extra):
        gpd = pytest.importorskip("geopandas")
        from shapely.geometry import LineString
        pairs = list(DECODE_PAIRS) if pairs is None else pairs
        columns = {
            "OBJECTID": list(range(1, len(pairs) + 1)),
            "ASSETGROUP": [group for group, _ in pairs],
            "ASSETTYPE": [code for _, code in pairs],
            "nominaldiameter": [4] * len(pairs),
            "geometry": [LineString([(0, i), (1, i + 1)]) for i in range(len(pairs))],
        }
        columns.update(extra)
        return gpd.GeoDataFrame(columns, crs="EPSG:4326")

    def decode(self, workflow, distribution, frame, monkeypatch):
        monkeypatch.setattr(workflow, "request_json",
                            lambda session, url, params=None: distribution)
        with redirect_stdout(io.StringIO()) as buffer:
            result = workflow.decode_pipe_materials(
                object(), frame, "https://stub/MapServer/6", "distribution pipes")
        return result, buffer.getvalue()

    def test_every_pair_decodes_to_its_domain_label(
            self, workflow, distribution, monkeypatch):
        result, _ = self.decode(workflow, distribution, self.frame(), monkeypatch)
        assert list(result["material"]) == list(DECODE_EXPECTED)

    def test_the_raw_assettype_is_kept(self, workflow, distribution, monkeypatch):
        result, _ = self.decode(workflow, distribution, self.frame(), monkeypatch)
        assert list(result["PipeMaterialRaw"]) == [code for _, code in DECODE_PAIRS]

    def test_copper_is_copper(self, workflow, distribution, monkeypatch):
        result, _ = self.decode(workflow, distribution, self.frame(), monkeypatch)
        families = dict(zip(result["material"], result["PipeMaterialFamily"]))
        assert families["Copper"] == "COPPER"
        assert families["Plastic PE"] == "PLASTIC"

    def test_the_same_code_decodes_per_subtype(
            self, workflow, distribution, monkeypatch):
        """999 is "UNK" under a pipe subtype and "Unknown Type" under the unknown
        subtype. A flat code table cannot express that."""
        result, _ = self.decode(workflow, distribution,
                                self.frame([(2, 999), (999, 999)]), monkeypatch)
        assert list(result["material"]) == ["UNK", "Unknown Type"]

    def test_the_decoded_frame_is_accepted_by_prepare_pipes(
            self, workflow, distribution, monkeypatch):
        """The two halves have to agree: this is the failure the user hit."""
        result, _ = self.decode(workflow, distribution, self.frame(), monkeypatch)
        with redirect_stdout(io.StringIO()):
            assert workflow.prepare_pipes(result, "distribution")

    def test_an_already_decoded_frame_passes_through(
            self, workflow, distribution, monkeypatch):
        """A cache enriched by the script, or a re-run, must not be re-decoded -
        that is what destroyed the original GradeMaterial once before."""
        frame = self.frame(material=["Copper"] * len(DECODE_PAIRS))
        result, _ = self.decode(workflow, distribution, frame, monkeypatch)
        assert list(result["material"]) == ["Copper"] * len(DECODE_PAIRS)

    def test_a_missing_domain_field_fails_naming_assettype(
            self, workflow, distribution, monkeypatch):
        frame = self.frame().drop(columns=["ASSETGROUP"])
        monkeypatch.setattr(workflow, "request_json",
                            lambda session, url, params=None: distribution)
        with pytest.raises(RuntimeError, match="ASSETTYPE is the material type"), \
                redirect_stdout(io.StringIO()):
            workflow.decode_pipe_materials(
                object(), frame, "https://stub/MapServer/6", "distribution pipes")

    def test_an_undefined_pair_is_reported_not_silently_blank(
            self, workflow, distribution, monkeypatch):
        _, output = self.decode(workflow, distribution,
                                self.frame([(2, 9), (77, 88)]), monkeypatch)
        assert "domains do not define" in output
        assert "(77, 88)" in output
