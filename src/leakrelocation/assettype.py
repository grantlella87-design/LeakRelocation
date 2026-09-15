"""Decoding of DNV ASSETGROUP + ASSETTYPE subtype domains.

This logic was duplicated across four enrichment scripts. The decoder bodies
were identical apart from variable names; the family function was identical in
all four.

The family taxonomy here is deliberately not the one in `matching.py`. This one
labels cached pipe rows (`PipeMaterialFamily`) and has an explicit OTHER
bucket; `matching.py` decides whether a leak and a pipe are compatible.
"""
# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member. spec_from_file_location gives a module no parent
# package, and a relative import then fails with "attempted relative import with
# no known parent package".
import os as _os
import sys as _sys

_PACKAGE_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PACKAGE_PARENT not in _sys.path:
    _sys.path.insert(0, _PACKAGE_PARENT)

from leakrelocation import config
from leakrelocation.matching import match_term, material_tokens

# Checked in order; the first family with a matching term wins.
ASSETTYPE_FAMILY_TERMS = {
    "PLASTIC": ["PLASTIC", "POLY", "PE", "PVC", "ABS", "HDPE", "MDPE"],
    "IRON": ["CAST IRON", "DUCTILE", "WROUGHT", "IRON"],
    "STEEL": ["STEEL", "GALVANIZED", "BARE", "COATED"],
    "COPPER": ["COPPER"],
    "UNKNOWN": ["UNKNOWN", "UNK", "COMPOSITE"],
}

UNCLASSIFIED_FAMILY = "OTHER"


def norm_code(value):
    """Normalise a subtype/domain code to a comparable string.

    Codes arrive as ints, floats or strings depending on the endpoint, so a
    trailing '.0' from float coercion is stripped.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(".0") and text[:-2].lstrip("-").isdigit():
        text = text[:-2]
    return text


def family_from_assettype(decoded_assettype):
    """Map a decoded ASSETTYPE label to its material family.

    Terms are matched against tokens rather than as substrings. Substring
    matching classified copper as PLASTIC, because "COPPER" contains "PE" and
    PLASTIC is checked first - the same defect that affected leak/pipe
    comparison in matching.py.
    """
    tokens = material_tokens(decoded_assettype)
    if not tokens:
        return "UNKNOWN"
    for family, terms in ASSETTYPE_FAMILY_TERMS.items():
        if any(match_term(tokens, term) for term in terms):
            return family
    return UNCLASSIFIED_FAMILY


def reference_layer_json(layer_id):
    """The committed metadata copy for a layer id, or None.

    Every folder under reference/mapserver_json/ is searched, and the id is
    compared as a number rather than as a filename fragment. Both of those were
    wrong before, and they hid a layer that was sitting in the checkout:

      - only config.REFERENCE_DIR was searched, which is the DNV NY service, so
        layer 62 under MA_Material_View_MA/ was never seen;
      - the glob was layer_062_*.json, which does not match
        layer_0062_Pipeline_Line_Abandoned.json.

    The effect was that the abandoned pipe layer could not name its materials -
    PipeMaterialDomain came out empty and every retired pipe drew in one colour -
    while the file it needed was committed and two directories away.
    """
    import json

    for path in reference_layer_paths():
        stem = path.name.split("_")
        if len(stem) < 2 or not stem[1].isdigit():
            continue
        if int(stem[1]) != int(layer_id):
            continue
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    return None


def reference_layer_paths():
    """Every committed layer JSON, the configured directory first.

    REFERENCE_DIR stays first so a service this project reads directly keeps
    priority over a copy someone added for reference.
    """
    seen = []
    for directory in (config.REFERENCE_DIR, *sorted(reference_service_dirs())):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("layer_*.json")):
            if path not in seen:
                seen.append(path)
    return seen


def reference_service_dirs():
    """The per-service folders under reference/mapserver_json/."""
    root = getattr(config, "REFERENCE_ROOT", None)
    if root is None or not root.is_dir():
        return []
    return [path for path in root.iterdir() if path.is_dir()]


def decoder_for_layer(layer_id):
    """(type_id_field, decoder) built from the committed metadata for a layer.

    Returns (None, {}) when that copy is not in the checkout, so a caller can say
    so rather than silently colouring everything the same.
    """
    layer_json = reference_layer_json(layer_id)
    if not layer_json:
        return None, {}
    return build_assettype_decoder(layer_json)


def build_assettype_decoder(layer_json):
    """Build a {(assetgroup_code, assettype_code): decoded fields} lookup.

    Returns (type_id_field, decoder). The decoder carries the decoded subtype
    name, the decoded ASSETTYPE label, its domain name and the material family.
    """
    decoder = {}
    type_id_field = layer_json.get("typeIdField")

    for subtype in layer_json.get("types", []) or []:
        assetgroup_code = norm_code(subtype.get("id"))
        assetgroup_name = subtype.get("name")
        domains = subtype.get("domains") or {}

        assettype_domain = None
        for field_name, domain in domains.items():
            if str(field_name).upper() == "ASSETTYPE":
                assettype_domain = domain
                break
        if not assettype_domain:
            continue

        for coded_value in assettype_domain.get("codedValues", []) or []:
            assettype_code = norm_code(coded_value.get("code"))
            assettype_name = coded_value.get("name")
            decoder[assetgroup_code, assettype_code] = {
                "ASSETGROUP_DECODED": assetgroup_name,
                "ASSETTYPE_DECODED": assettype_name,
                "ASSETTYPE_DOMAIN": assettype_domain.get("name"),
                "PipeMaterialFamily": family_from_assettype(assettype_name),
            }

    return type_id_field, decoder
