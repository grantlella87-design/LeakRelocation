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

    config.REFERENCE_DIR holds one file per layer, named layer_006_*.json and so
    on. Reading the domains from there means a tool can name a pipe material with
    no token and no network - which is what lets the map colour pipes straight
    from a downloaded cache.
    """
    import json

    if not config.REFERENCE_DIR.is_dir():
        return None
    matches = sorted(config.REFERENCE_DIR.glob(f"layer_{int(layer_id):03d}_*.json"))
    if not matches:
        return None
    with open(matches[0], encoding="utf-8") as handle:
        return json.load(handle)


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
