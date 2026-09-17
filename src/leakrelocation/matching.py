"""Pure classification and normalisation logic for leak-to-pipe matching.

Nothing here touches the network, the filesystem or GeoPandas, so it can be
tested on any machine. The relocation workflow's correctness rests almost
entirely on these functions, which is why they live apart from the I/O.

Key rule: pipe material classification comes from the decoded
ASSETGROUP + ASSETTYPE subtype domains. The DNV `material` field is
Grade/characteristic data and must not be used as the material class.
"""
import math

# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member. spec_from_file_location gives a module no parent
# package, and a relative import then fails with "attempted relative import with
# no known parent package".
import os as _os
import re
import sys as _sys

_PACKAGE_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PACKAGE_PARENT not in _sys.path:
    _sys.path.insert(0, _PACKAGE_PARENT)

from leakrelocation import config

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

# Terms are matched against the label's *tokens*, not as raw substrings, and
# the first family that hits wins so insertion order is significant.
#
# Substring matching was the historical behaviour and it misclassified
# materials whose spelling happens to contain a short abbreviation: "COPPER"
# contains "PE", so with PLASTIC checked first every copper pipe was classified
# PLASTIC and the COPPER family below was unreachable. See match_term().
MATERIAL_FAMILY_TERMS = {
    # HDPE/MDPE are spelled out because token matching will not find the "HD"
    # and "PE" inside them, and they are common in the supplemental leak data.
    "PLASTIC": ["PLASTIC", "POLY", "PE", "PVC", "ABS", "POLYBUTYLENE",
                "HD", "MD", "HDPE", "MDPE"],
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


# A term shorter than this must match a whole token. Longer terms may match a
# token prefix, so "POLY" still catches "POLYETHYLENE" while "PE" cannot match
# inside "COPPER".
PREFIX_MATCH_MIN_LENGTH = 4

_TOKEN_SEPARATOR = re.compile(r"[^A-Z0-9]+")


def tokens_of_label(label):
    """Split an already-decoded label into uppercase alphanumeric tokens.

    Splitting on punctuation means "cast-iron" and "Cast Iron" tokenise the
    same way, so both resolve to the IRON family.
    """
    return [token for token in _TOKEN_SEPARATOR.split(upper(label)) if token]


def material_tokens(value):
    """Tokens for a DNV ASSETTYPE value, decoding a numeric subtype code first."""
    return tokens_of_label(material_label(value))


def match_term(tokens, term):
    """True when a family term matches the given tokens.

    Multi-word terms ("CAST IRON") must appear as consecutive tokens. Single
    terms must equal a token, or prefix one when long enough to be unambiguous
    - see PREFIX_MATCH_MIN_LENGTH.
    """
    term_tokens = term.split()
    if len(term_tokens) > 1:
        width = len(term_tokens)
        return any(tokens[i:i + width] == term_tokens
                   for i in range(len(tokens) - width + 1))

    single = term_tokens[0]
    if single in tokens:
        return True
    if len(single) >= PREFIX_MATCH_MIN_LENGTH:
        return any(token.startswith(single) for token in tokens)
    return False


def family_from_label(label):
    """Family for a label that is already text, falling back to the label itself.

    No numeric decoding happens here, which is what makes this usable for
    material text from another service. material_family goes through
    material_label first, and that decodes any value whose first number matches
    a DNV service-pipe subtype code - so "2 IN PLASTIC" would come back "Cast
    Iron", because it starts with a 2.
    """
    tokens = tokens_of_label(label)
    for family, terms in MATERIAL_FAMILY_TERMS.items():
        if any(match_term(tokens, term) for term in terms):
            return family
    return upper(label)


def material_family(value):
    """Map a DNV ASSETTYPE material to its broad family."""
    return family_from_label(material_label(value))


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


# --- Diameter matching ------------------------------------------------------
#
# Two modes, so the strict output and the widened one can be produced from the
# same code and compared:
#
#   EXACT   the leak's diameter equals the pipe's. The original rule.
#   FUZZY   the pipe may also be one nominal size up or down.
DIAMETER_EXACT = "exact"
DIAMETER_FUZZY = "fuzzy"
DIAMETER_MODES = (DIAMETER_EXACT, DIAMETER_FUZZY)

# The nominal pipeline sizes, in inches. This is the ladder "one size up or
# down" is counted on.
NOMINAL_DIAMETERS_IN = (
    1.0, 1.25, 1.5, 2.0, 4.0, 6.0, 8.0, 12.0, 16.0, 20.0, 24.0, 30.0, 36.0,
    42.0, 48.0,
)

# How a candidate's diameter related to the leak's, for the audit column.
DIAMETER_MATCH_EXACT = "exact"
DIAMETER_MATCH_UP = "one_size_up"
DIAMETER_MATCH_DOWN = "one_size_down"

# Floats read back from a GeoPackage and a CSV do not compare cleanly - 6.0
# from one and 5.999999999999999 from the other are the same pipe. A
# thousandth of an inch is far below any real difference in nominal size.
DIAMETER_TOLERANCE_IN = 0.001


def diameters_equal(left, right, tolerance=DIAMETER_TOLERANCE_IN):
    return abs(float(left) - float(right)) <= tolerance


def adjacent_nominal_sizes(diameter, ladder=NOMINAL_DIAMETERS_IN):
    """The nominal sizes one step below and above `diameter`.

    Either may be None at the ends of the ladder. A diameter that is not itself
    a nominal size is located in the gap it falls in, so the 10 in pipe that
    exists in the data but not on the ladder brackets to (8, 12).
    """
    value = float(diameter)
    below = max((size for size in ladder if size < value - DIAMETER_TOLERANCE_IN),
                default=None)
    above = min((size for size in ladder if size > value + DIAMETER_TOLERANCE_IN),
                default=None)
    return below, above


def diameter_within_one_size(leak_diameter, pipe_diameter,
                             ladder=NOMINAL_DIAMETERS_IN):
    """True when the two diameters are the same nominal size or adjacent ones.

    Stated as: there is no nominal size strictly between them.

    For diameters that are on the ladder this is exactly "one size up or down" -
    8 in reaches 6 and 12, and not 16, because 12 sits between 8 and 16.

    It is written this way rather than as "look up my neighbours" because of the
    uncommon sizes. The data carries diameters the ladder does not list: 0.5 and
    0.75 on service pipes, 3 and 10 on mains. Taking each value's own
    neighbours would make the rule asymmetric at those sizes - a 0.75 in leak
    would reach 1 in, while a 1 in leak would not reach back down to 0.75,
    because 1 is the bottom rung. "Nothing standard in between" gives the same
    answer whichever side you ask from, and applies the same rule to an
    uncommon size as to a listed one.
    """
    low, high = sorted((float(leak_diameter), float(pipe_diameter)))
    if diameters_equal(low, high):
        return True
    return not any(low + DIAMETER_TOLERANCE_IN < size < high - DIAMETER_TOLERANCE_IN
                   for size in ladder)


def diameter_match(leak_diameter, pipe_diameter, mode=None,
                   ladder=NOMINAL_DIAMETERS_IN):
    """How this pipe's diameter matches the leak's, or None if it does not.

    Returns DIAMETER_MATCH_EXACT, DIAMETER_MATCH_UP or DIAMETER_MATCH_DOWN, so
    the audit can record not just that a leak matched but how much slack it
    took to match it. A missing value on either side never matches: a leak with
    no diameter has nothing to compare, and widening the rule does not change
    that.
    """
    if leak_diameter is None or pipe_diameter is None:
        return None
    try:
        leak = float(leak_diameter)
        pipe = float(pipe_diameter)
    except (TypeError, ValueError):
        return None
    if math.isnan(leak) or math.isnan(pipe):
        return None
    if diameters_equal(leak, pipe):
        return DIAMETER_MATCH_EXACT
    if (mode or config.DIAMETER_MATCH_MODE) != DIAMETER_FUZZY:
        return None
    if not diameter_within_one_size(leak, pipe, ladder):
        return None
    return DIAMETER_MATCH_UP if pipe > leak else DIAMETER_MATCH_DOWN


def diameter_matches(leak_diameter, pipe_diameter, mode=None):
    """Whether this pipe's diameter is acceptable for this leak.

    Kept as the boolean form; diameter_match says how.
    """
    return diameter_match(leak_diameter, pipe_diameter, mode) is not None


# An exact diameter beats an adjacent one however far away it is, so a leak
# that already relocated keeps the pipe it had. Sorting purely by distance
# would let a nearer wrong-size pipe steal a match the strict run had made
# correctly, and the two outputs would then differ in ways that have nothing
# to do with the leaks the widened rule was meant to rescue.
DIAMETER_MATCH_RANK = {
    DIAMETER_MATCH_EXACT: 0,
    DIAMETER_MATCH_UP: 1,
    DIAMETER_MATCH_DOWN: 1,
}


def candidate_sort_key(candidate):
    """Best candidate first: exact diameter, then nearest.

    With PREFER_EXACT_DIAMETER off this is distance alone, which makes the
    widened run pick the nearest pipe in the size window even when a further
    exact one exists.
    """
    if not config.PREFER_EXACT_DIAMETER:
        return (0, candidate["distance_ft"])
    rank = DIAMETER_MATCH_RANK.get(candidate.get("diameter_match"), 1)
    return (rank, candidate["distance_ft"])


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


# --- Temporal validity ------------------------------------------------------
#
# A leak can only be relocated onto a pipe that existed when the leak was
# recorded. Which end of the pipe's life is checked depends on the layer:
#
#   IN_SERVICE_AT_LEAK   the live layers. The pipe's record must pre-date the
#                        leak: REVISEDLEAKDATE after CREATIONDATE.
#   RETIRED_AFTER_LEAK   the retired layer. The pipe must still have been in
#                        service: REVISEDLEAKDATE before dateretired.
#
# Both comparisons are strict, as specified - "before" and "after", not "on or
# before". Equal timestamps fail, which for date-only values means a leak
# recorded the same day as the pipe record is not matched to it.
IN_SERVICE_AT_LEAK = "in_service_at_leak"
RETIRED_AFTER_LEAK = "retired_after_leak"
DATE_RULES = (IN_SERVICE_AT_LEAK, RETIRED_AFTER_LEAK)

# Why a candidate was allowed or rejected, for the audit column and the counts.
DATE_OK = "ok"
DATE_NO_LEAK_DATE = "no_leak_date"
DATE_MISSING_PIPE_DATE = "missing_pipe_date"
DATE_PIPE_TOO_NEW = "pipe_created_after_leak"
DATE_PIPE_ALREADY_RETIRED = "pipe_retired_before_leak"


def date_rule_result(rule, leak_ms, created_ms, retired_ms):
    """Whether a pipe was in service when the leak was recorded.

    Returns (allowed, reason). Every argument is epoch milliseconds or None.

    A leak with no date is allowed through every rule, and the reason says so,
    so those matches can be counted and flagged rather than silently dropped -
    dropping them would lose relocations that are produced today.

    A pipe missing the date its rule needs is also allowed through, for the same
    reason: the pipe layers carry nulls, and refusing every one of them would
    quietly discard candidates on a data gap rather than on the rule.
    """
    if rule not in DATE_RULES:
        raise ValueError(f"unknown date rule: {rule!r}")
    if leak_ms is None:
        return True, DATE_NO_LEAK_DATE

    if rule == IN_SERVICE_AT_LEAK:
        if created_ms is None:
            return True, DATE_MISSING_PIPE_DATE
        if leak_ms > created_ms:
            return True, DATE_OK
        return False, DATE_PIPE_TOO_NEW

    if retired_ms is None:
        return True, DATE_MISSING_PIPE_DATE
    if leak_ms < retired_ms:
        return True, DATE_OK
    return False, DATE_PIPE_ALREADY_RETIRED
