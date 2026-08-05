"""Pure classification and normalisation logic for leak-to-pipe matching.

Nothing here touches the network, the filesystem or GeoPandas, so it can be
tested on any machine. The relocation workflow's correctness rests almost
entirely on these functions, which is why they live apart from the I/O.

Key rule: pipe material classification comes from the decoded
ASSETGROUP + ASSETTYPE subtype domains. The DNV `material` field is
Grade/characteristic data and must not be used as the material class.
"""
import math
import re

from . import config

# DNV service pipe ASSETTYPE subtype domain.
SERVICE_ASSETTYPE_LABELS = {
    0: "Unknown",
    1: "Bare Steel",
    2: "Cast Iron",
    3: "Coated Steel",
    4: "Composite",
    5: "Copper",
    6: "Ductile Iron",
    7: "Plastic ABS",
    8: "Plastic Other",
    9: "Plastic PE",
    10: "Plastic PVC",
    11: "Reconditioned Cast Iron",
    12: "Wrought Iron",
    13: "Polybutylene",
    14: "Reconditioned Steel",
    15: "Galvanized Steel",
    999: "UNK",
}

# KNOWN DEFECT - behaviour preserved deliberately, see tests/test_matching.py.
# These terms are matched as plain substrings against the uppercased label and
# the first family that hits wins, so insertion order is significant:
#   * "COPPER" contains "PE", and PLASTIC is checked first, so copper is
#     classified PLASTIC and the COPPER family is unreachable. A copper leak
#     therefore family-matches any plastic pipe.
#   * Hyphenated spellings ("cast-iron") miss the spaced terms ("CAST IRON")
#     and fall through to the raw label instead of a family.
# Fixing either changes which pipes leaks relocate onto, so it needs a
# deliberate decision and a production re-run rather than a silent edit.
MATERIAL_FAMILY_TERMS = {
    "PLASTIC": ["PLASTIC", "POLY", "PE", "PVC", "ABS", "POLYBUTYLENE", "HD", "MD"],
    "IRON": ["CAST IRON", "DUCTILE", "WROUGHT IRON", "RECONDITIONED CAST"],
    "STEEL": ["STEEL", "BARE STEEL", "COATED STEEL", "GALVANIZED", "RECONDITIONED STEEL"],
    "COPPER": ["COPPER"],
    "UNKNOWN": ["UNKNOWN", "UNK", "COMPOSITE", "NULL", "NONE"],
}

_WHITESPACE = re.compile(r"\s+")
_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")
_NON_ALNUM = re.compile(r"[^a-z0-9]")


def clean(value):
    """Collapse whitespace and map null-ish placeholders to an empty string."""
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in ("", "none", "null", "nan"):
        return ""
    return _WHITESPACE.sub(" ", text)


def upper(value):
    """Uppercase a cleaned value, normalising en/em dashes to hyphens."""
    return clean(value).upper().replace("–", "-").replace("—", "-")


def normalize_key(value):
    """Normalise a join key, dropping a trailing '.0' left by float coercion."""
    text = clean(value)
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.strip("{} ").upper()


def parse_number(value):
    """Extract the first number from a value, or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return None if math.isnan(float(value)) else float(value)
    text = clean(value)
    if not text:
        return None
    found = _NUMBER.search(text)
    return float(found.group(0)) if found else None


def simplify_field_name(name):
    """Reduce a field name to lowercase alphanumerics for fuzzy lookup."""
    return _NON_ALNUM.sub("", str(name).lower())


def resolve_field_name(field_names, candidates):
    """Return the first candidate present in field_names, ignoring case and
    punctuation, or None."""
    simplified = {simplify_field_name(name): name for name in field_names}
    for candidate in candidates:
        token = simplify_field_name(candidate)
        if token in simplified:
            return simplified[token]
    return None


def material_label(value):
    """Decode a numeric ASSETTYPE subtype code into its domain label."""
    parsed = parse_number(value)
    if parsed is not None and int(parsed) == parsed and int(parsed) in SERVICE_ASSETTYPE_LABELS:
        return SERVICE_ASSETTYPE_LABELS[int(parsed)]
    return clean(value)


def material_family(value):
    """Map a material to its broad family, falling back to the label itself."""
    text = upper(material_label(value))
    for family, terms in MATERIAL_FAMILY_TERMS.items():
        if any(term in text for term in terms):
            return family
    return text


def material_matches(leak_value, pipe_value):
    """True when leak and pipe materials agree exactly, or by family when
    family fallback is enabled."""
    leak_text = upper(material_label(leak_value))
    pipe_text = upper(material_label(pipe_value))
    if not leak_text or not pipe_text:
        return False
    if leak_text == pipe_text:
        return True
    return (config.ALLOW_MATERIAL_FAMILY_FALLBACK
            and material_family(leak_text) == material_family(pipe_text))


def diameter_matches(leak_diameter, pipe_diameter):
    """Diameters must match exactly; a missing value never matches."""
    if leak_diameter is None or pipe_diameter is None:
        return False
    return float(leak_diameter) == float(pipe_diameter)


def pressure_matches(leak_pressure, pipe_pressure):
    """Pressure is advisory unless REQUIRE_PRESSURE_MATCH is set."""
    if not config.REQUIRE_PRESSURE_MATCH:
        return True
    return bool(upper(leak_pressure) and upper(leak_pressure) == upper(pipe_pressure))


def route_layers(facility):
    """Which pipe layers a leak should be searched against."""
    text = upper(facility)
    if "SERVICE" in text:
        return ["service"]
    if "MAIN" in text or "DISTRIBUTION" in text:
        return ["distribution"]
    return ["distribution", "service"]


def matched_radius_from_distance(distance_ft):
    """Round a match distance up to the search ring that would have found it."""
    if distance_ft is None:
        return None
    if distance_ft <= config.INITIAL_RADIUS_FT:
        return config.INITIAL_RADIUS_FT
    steps = math.ceil((distance_ft - config.INITIAL_RADIUS_FT) / config.RADIUS_INCREMENT_FT)
    return config.INITIAL_RADIUS_FT + steps * config.RADIUS_INCREMENT_FT
