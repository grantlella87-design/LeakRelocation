"""The column names this project writes, stated once.

These are not guesses. Every name here is written by code in this repository -
the pipe cache columns by `scripts/enrich_assettype_cache.py`, the GeoPackage
columns by `write_outputs` in the workflow - so reading them back by candidate
list was sniffing for something we already knew, and picking the wrong column
silently when the guess missed.

Field names that come from *outside* the project are a different problem and are
not listed here: the DNV service field names and the supplemental CSV headers are
external schemas, and the workflow still resolves those from candidate lists.
"""

# --- Enriched pipe caches (layer_cache/*.pkl.gz) -----------------------------
#
# Written by scripts/enrich_assettype_cache.py.

ASSETGROUP = "ASSETGROUP"
ASSETTYPE = "ASSETTYPE"
ASSETGROUP_DECODED = "ASSETGROUP_DECODED"
ASSETTYPE_DECODED = "ASSETTYPE_DECODED"
ASSETTYPE_DOMAIN = "ASSETTYPE_DOMAIN"

# The raw ASSETTYPE subtype code, kept so a decode can be traced back.
PIPE_MATERIAL_RAW = "PipeMaterialRaw"
# The decoded ASSETTYPE label, classified into a family.
PIPE_MATERIAL_FAMILY = "PipeMaterialFamily"
# Legacy. This held the DNV `material` field - a spec, grade or descriptor, not
# the material type - carried across so republishing "material" as the decoded
# ASSETTYPE did not destroy it. The workflow no longer downloads that field, so
# new caches will not have this column; the enrichment still preserves it where
# an older cache already carries one. Nothing reads it.
GRADE_MATERIAL = "GradeMaterial"
# The decoded ASSETTYPE, under the name the relocation logic reads.
MATERIAL = "material"

OBJECTID = "OBJECTID"

PIPE_CACHE_COLUMNS = (
    OBJECTID,
    ASSETGROUP,
    ASSETTYPE,
    ASSETGROUP_DECODED,
    ASSETTYPE_DECODED,
    ASSETTYPE_DOMAIN,
    PIPE_MATERIAL_FAMILY,
    PIPE_MATERIAL_RAW,
    GRADE_MATERIAL,
    MATERIAL,
)

# Blank values in these make the workflow and the viewers wrong rather than
# merely incomplete.
PIPE_CACHE_MUST_BE_POPULATED = (
    ASSETTYPE,
    ASSETTYPE_DECODED,
    PIPE_MATERIAL_RAW,
    MATERIAL,
)

# --- Relocation GeoPackage ---------------------------------------------------
#
# Written by write_outputs() in src/leak_relocation_geopandas.py.

RELOCATED_LEAKS_LAYER = "relocated_leaks"
OFFSET_LINES_LAYER = "relocated_leak_offset_lines"

LEAK_KEY = "LeakKey"
# The material of the pipe a leak was matched to.
MATCH_MATERIAL = "MatchMaterial"
MATCH_DIAMETER = "MatchDiameter"
MATCH_PRESSURE = "MatchPressure"
MATCH_STATUS = "MatchStatus"
# The pipe's own attributes, as recorded at match time.
PIPE_MATERIAL = "PipeMaterial"
PIPE_DIAMETER = "PipeDiameter"
# The leak's supplemented attributes.
LEAK_MATERIAL = "LeakMaterial"
LEAK_DIAMETER = "LeakDiameter"
LINKED_LAYER = "LinkedLayer"
DISTANCE_FT = "DistanceFt"


def require(frame, columns, what):
    """Raise unless every column is present, naming what is missing.

    Used instead of falling back to a different column, which is how a wrong
    guess became a wrong answer nobody noticed.
    """
    missing = [name for name in columns if name not in frame.columns]
    if missing:
        raise KeyError(
            f"{what} is missing {missing}. Present: {sorted(frame.columns)}"
        )
