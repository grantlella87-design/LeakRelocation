"""The relocation itself: leaks and pipes in, a GeoPackage out.

Reads the MA leaks and the MA distribution and service pipes, takes each
leak's diameter and material from the supplemental CSV, matches each leak to
the nearest pipe that agrees on both, snaps the leak onto that pipe, and writes
relocated points, offset guide lines and an audit table.

The pieces it is built from live beside it: arcgis (requests), layer_cache
(downloads), layer_fields (which fields), native_crs (projections), dates, and
matching (the rules). `relocate` is the entry point a different leak source
calls - scripts/maximo_leaks.py hands it leaks read from SQL.
"""
import datetime as dt
import numbers
import os
import sys
import traceback
from collections import defaultdict
from concurrent import futures

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString
from shapely.ops import nearest_points
from shapely.strtree import STRtree

# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member. See leakrelocation/output.py for the full note.
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PACKAGE_PARENT not in sys.path:
    sys.path.insert(0, _PACKAGE_PARENT)

from leakrelocation import (
    arcgis,
    auth,
    config,
    dates,
    layer_cache,
    layer_fields,
    leak_location as location,
    native_crs,
    output,
    schema,
)
from leakrelocation.assettype import build_assettype_decoder, norm_code
from leakrelocation.matching import (
    DIAMETER_EXACT,
    IN_SERVICE_AT_LEAK,
    NOMINAL_DIAMETERS_IN,
    RETIRED_AFTER_LEAK,
    candidate_sort_key,
    clean,
    date_rule_result,
    diameter_match,
    matched_radius_from_distance,
    material_label,
    material_matches,
    normalize_key,
    parse_number,
    pressure_matches,
    route_layers,
)
from leakrelocation.output import detail, fail, log, step, warn

SUPPLEMENTAL_CSV = str(config.SUPPLEMENTAL_CSV)
# Which diameter rule this run uses, and so which GeoPackage it writes. The
# strict run keeps the original filename; the widened one is suffixed, so the
# two outputs sit beside each other and can be compared instead of one
# replacing the other.
DIAMETER_MATCH_MODE = config.DIAMETER_MATCH_MODE
# Which leak set this run is relocating. "historic" for the LMS leaks from
# layer 206, "maximo" for the enterprise leaks from SQL. Written onto every
# output row, so a GeoPackage says where its leaks came from rather than the
# reader having to remember which file is which.
LEAK_SOURCE = "historic"
# The folder the GeoPackage is written into, derived from the file itself so that
# overriding LEAKRELOCATION_OUTPUT_GPKG moves both. This used to be the network
# share, which ensure_output_folder then tried to *create*: a run on a machine
# that could not reach it failed there, before any work was done, even though
# nothing was ever written to it.
OUTPUT_FOLDER = str(config.OUTPUT_GPKG.parent)
OUTPUT_GPKG = str(config.output_gpkg_for(DIAMETER_MATCH_MODE))
HIST_LEAK_URL = config.HIST_LEAK_URL
DISTRIBUTION_PIPE_URL = config.DISTRIBUTION_PIPE_URL
SERVICE_PIPE_URL = config.SERVICE_PIPE_URL
RETIRED_PIPE_URL = config.RETIRED_PIPE_URL
USE_RETIRED_PIPE_LAYER = config.USE_RETIRED_PIPE_LAYER

WHERE_MA = config.WHERE_MA
# The CRS every layer is projected into for matching. Set by relocate() from
# the distribution pipes' own metadata; nothing is forced to a state plane.
TARGET_CRS = None
INITIAL_RADIUS_FT = config.INITIAL_RADIUS_FT
RADIUS_INCREMENT_FT = config.RADIUS_INCREMENT_FT
MAX_RADIUS_FT = config.MAX_RADIUS_FT
REQUIRE_PRESSURE_MATCH = config.REQUIRE_PRESSURE_MATCH
ALLOW_MATERIAL_FAMILY_FALLBACK = config.ALLOW_MATERIAL_FAMILY_FALLBACK
USE_MULTIPROCESSING = True
WORKERS = max(1, min(8, (os.cpu_count() or 4) - 1))
MESSAGE_EVERY_N_LEAKS = 1000
VERBOSE_PER_LEAK = False
# Per-process spatial indexes, built by init_worker in each matching worker.
WORKER_TREES = None


def verbose(text):
    if VERBOSE_PER_LEAK:
        log(text)


def ensure_output_folder():
    if not os.path.isdir(OUTPUT_FOLDER):
        os.makedirs(OUTPUT_FOLDER)
        log(f"Created output folder: {OUTPUT_FOLDER}")
    else:
        detail(f"Output folder exists: {OUTPUT_FOLDER}")


def load_supplemental():
    """Read the supplemental CSV, keyed on the leak's GlobalID.

    Keyed on the leak *number* this silently imported the wrong diameter and
    material: the number is not unique in the file, and where its rows disagree
    only one of them survived. See SUPP_GLOBALID_CANDIDATES for the counts.
    """
    step("Loading supplemental CSV")
    log(f"Supplemental CSV: {SUPPLEMENTAL_CSV}")
    if not os.path.isfile(SUPPLEMENTAL_CSV):
        fail(f"Missing supplemental CSV: {SUPPLEMENTAL_CSV}")
    df = pd.read_csv(
        SUPPLEMENTAL_CSV, dtype=str, encoding="utf-8-sig", keep_default_na=False
    )
    log(f"Supplemental rows: {len(df):,}")
    # Required: this is the join key. Falling back to the leak number would
    # reintroduce exactly the wrong-attribute bug this replaced, and silently.
    globalid_field = layer_fields.resolved_field(
        df.columns, layer_fields.SUPP_GLOBALID_CANDIDATES, True, "supplemental leak GlobalID"
    )
    # Carried for labelling and the audit, not used to join.
    number_field = layer_fields.resolved_field(
        df.columns, layer_fields.SUPP_KEY_CANDIDATES, False, "supplemental leak number"
    )
    diameter_field = layer_fields.resolved_field(
        df.columns, layer_fields.SUPP_DIAMETER_CANDIDATES, False, "supplemental diameter"
    )
    material_field = layer_fields.resolved_field(
        df.columns, layer_fields.SUPP_MATERIAL_CANDIDATES, False, "supplemental material"
    )
    pressure_field = layer_fields.resolved_field(
        df.columns, layer_fields.SUPP_PRESSURE_CANDIDATES, False, "supplemental pressure"
    )
    facility_field = layer_fields.resolved_field(
        df.columns, layer_fields.SUPP_FACILITY_CANDIDATES, False, "supplemental facility type"
    )
    records = {}
    skipped_no_key = 0
    duplicate_globalids = 0
    shared_numbers = set()
    seen_numbers = set()
    for _, row in df.iterrows():
        leak_globalid = normalize_key(row.get(globalid_field))
        if not leak_globalid:
            skipped_no_key += 1
            continue
        if leak_globalid in records:
            # Should not happen: the GlobalID is unique on every row of the
            # committed file. If it ever does, the count says so rather than the
            # last row quietly winning.
            duplicate_globalids += 1
        leak_number = normalize_key(row.get(number_field)) if number_field else ""
        if leak_number:
            if leak_number in seen_numbers:
                shared_numbers.add(leak_number)
            seen_numbers.add(leak_number)
        records[leak_globalid] = {
            "leak_number": leak_number,
            "diameter": parse_number(row.get(diameter_field))
            if diameter_field
            else None,
            "material": clean(row.get(material_field)) if material_field else "",
            "pressure": clean(row.get(pressure_field)) if pressure_field else "",
            "facility": clean(row.get(facility_field)) if facility_field else "",
        }
    log(f"Supplemental records keyed by GlobalID: {len(records):,}")
    if skipped_no_key:
        warn(f"Supplemental rows skipped, no GlobalID: {skipped_no_key:,}")
    if duplicate_globalids:
        warn(f"Supplemental rows sharing a GlobalID: {duplicate_globalids:,}. "
             f"The last row for each is the one used.")
    if shared_numbers:
        # Not a problem any more, but worth stating: it is the reason the join is
        # on the GlobalID. Keyed on the number, these leaks took an arbitrary
        # row's diameter and material.
        log(f"Leak numbers appearing on more than one row: {len(shared_numbers):,}. "
            f"They are kept apart because the join is on the GlobalID.")
    if REQUIRE_PRESSURE_MATCH and not pressure_field:
        fail(
            "REQUIRE_PRESSURE_MATCH is True but no supplemental pressure field was resolved."
        )
    if not pressure_field:
        # Not actionable while pressure matching is off, which is the default.
        detail(
            "No supplemental pressure field was resolved. Pressure matching is not enforced."
        )
    return records


def load_retired_pipes(session):
    """Load the retired pipe layer, or report why it cannot be used.

    This layer is on the MA service, and its schema has not been read from that
    service yet - so rather than assume it looks like the DNV pipe layers, the
    run says exactly what it found and carries on with the live layers. A leak
    that belongs on a retired pipe then goes unmatched, which is visible in the
    counters, instead of the whole run ending.

    Run scripts/describe_layer.py against the URL to see its fields, and this
    stops guessing.
    """
    step("Loading retired pipes")
    log(f"Layer: {RETIRED_PIPE_URL}")
    try:
        retired = layer_cache.query_arcgis_layer(session, RETIRED_PIPE_URL, WHERE_MA,
                                     "retired pipes")
        retired = native_crs.assign_native_crs(session, "retired_pipes", retired)
        retired = native_crs.to_analysis_crs(session, "retired_pipes", retired)
        return decode_pipe_materials(session, retired, RETIRED_PIPE_URL,
                                     "retired pipes")
    except Exception as ex:  # noqa: BLE001 - reported, and the run continues
        warn(f"The retired pipe layer could not be used, so relocations onto "
             f"retired pipes are NOT included in this run. Reason: {ex}")
        warn(f"To see what that layer actually has: "
             f"python scripts/describe_layer.py {RETIRED_PIPE_URL}")
        return None


def leak_location(row, fields):
    """The best location this leak actually has, for LeakAddress.

    `fields` maps the name leakrelocation.leak_location knows to the column this
    cache actually carries, which is not always the same spelling. The rule
    itself lives there, shared with the map server so the popup and the audit
    cannot disagree about where a leak is.

    This is a fallback for rows that lack a street address, not a replacement
    for one. Whether the MA rows genuinely lack it is what
    scripts/probe_leak_fields.py answers.
    """
    return location.from_values(
        {name: row.get(column) for name, column in fields.items()})


# Where a leak's date came from, for the LeakDateSource column. The primary
# field is named outright; the fallbacks carry their own field name, so a row
# says "DISCOVEREDDATE" rather than "a fallback".
DATE_SOURCE_NONE = ""


def leak_date_with_source(row, primary_field, fallback_fields, use_fallback):
    """The leak's date in epoch milliseconds, and which column supplied it.

    Returns (epoch_ms, source, had_fallback). `had_fallback` is True whenever a
    fallback column held a date, whether or not it was used - that is how a run
    reports the size of the gap without changing what it does about it.
    """
    primary_ms = (dates.date_value_to_epoch_ms(row.get(primary_field))
                  if primary_field else None)
    available = None
    for field in fallback_fields:
        candidate = dates.date_value_to_epoch_ms(row.get(field))
        if candidate is not None:
            available = (candidate, field)
            break
    if primary_ms is not None:
        return primary_ms, primary_field, available is not None
    if available and use_fallback:
        return available[0], available[1], True
    return None, DATE_SOURCE_NONE, available is not None


def prepare_leaks(leaks_gdf, supplemental):
    step("Preparing leak records")
    leak_oid_field = layer_fields.resolved_field(
        leaks_gdf.columns, layer_fields.OBJECTID_CANDIDATES, True, "leak OBJECTID"
    )
    leak_key_field = layer_fields.resolved_field(
        leaks_gdf.columns, layer_fields.LEAK_KEY_CANDIDATES, True, "leak key"
    )
    # Required: the supplemental join is on the GlobalID. The leak number is not
    # unique in the supplemental file, so joining on it took an arbitrary row's
    # diameter and material for 5,364 leak numbers.
    leak_globalid_field = layer_fields.resolved_field(
        leaks_gdf.columns, layer_fields.GLOBALID_CANDIDATES, True, "leak GlobalID"
    )
    # Optional for the same reason as the date: a cache written before a field
    # was requested has no such column. None of these is matched on - they say
    # where the leak is - so a run without them is still a correct run.
    #
    # The map of wanted name -> this cache's spelling of it. ADDRESS is the one
    # that matters and the rest are what a row without one still has.
    location_fields = {}
    for name in location.SERVICE_FIELDS:
        column = layer_fields.resolved_field(leaks_gdf.columns, [name], False, f"leak {name}")
        if column:
            location_fields[name] = column
    if not location_fields:
        warn("No leak address or location column. Re-download the leak layer to "
             "include them: python run.py --refresh")
    # Optional: a cache downloaded before the date rule existed has no such
    # column, and every leak then matches unfiltered rather than not at all.
    leak_date_field = layer_fields.resolved_field(
        leaks_gdf.columns, layer_fields.LEAK_DATE_CANDIDATES, False, "leak revised date"
    )
    if not leak_date_field:
        warn("No leak date column, so no relocation can be checked against the "
             "pipe's life. Re-download the leak layer: python run.py --refresh")
    # The other dates on the row. Always resolved, so the run can report how
    # many leaks a fallback would rescue even when it is not using them.
    date_fallback_fields = []
    for name in layer_fields.LEAK_DATE_FALLBACK_CANDIDATES:
        column = layer_fields.resolved_field(leaks_gdf.columns, [name], False, f"leak {name}")
        if column:
            date_fallback_fields.append(column)
    use_date_fallback = config.USE_LEAK_DATE_FALLBACK
    if date_fallback_fields:
        log(f"Other date fields available: {', '.join(date_fallback_fields)}"
            f" ({'in use' if use_date_fallback else 'not used'})")
    prepared = []
    counters = defaultdict(int)
    for _, row in leaks_gdf.iterrows():
        counters["leaks_read"] += 1
        leak_oid = int(row[leak_oid_field])
        leak_number = normalize_key(row.get(leak_key_field))
        leak_globalid = normalize_key(row.get(leak_globalid_field))
        if not leak_globalid:
            # No key, so no supplemental row can be found for it. Counted rather
            # than falling back to the leak number, which would import another
            # leak's diameter and material.
            counters["unmatched_no_globalid"] += 1
            continue
        leak_info = supplemental.get(leak_globalid)
        if not leak_info:
            counters["unmatched_missing_supplemental"] += 1
            continue
        if leak_info["diameter"] is None or not leak_info["material"]:
            counters["unmatched_missing_match_attributes"] += 1
            continue
        leak_date_ms, leak_date_source, had_fallback = leak_date_with_source(
            row, leak_date_field, date_fallback_fields, use_date_fallback)
        if leak_date_ms is None:
            counters["leaks_without_a_revised_date"] += 1
            if had_fallback:
                # The number that makes the case: these leaks have no revised
                # date and do have another one, so the date rule is deciding
                # nothing for them and would not have to.
                counters["leaks_a_date_fallback_would_rescue"] += 1
        elif leak_date_source != leak_date_field:
            counters[f"leak_date_from_{leak_date_source}"] += 1
        prepared.append(
            {
                "leak_oid": leak_oid,
                "leak_number": leak_number,
                # No longer conditional: the GlobalID is the supplemental join
                # key, so a leak without one never reaches this point.
                "leak_globalid": clean(row.get(leak_globalid_field)),
                "leak_address": leak_location(row, location_fields),
                "leak_info": leak_info,
                "leak_date_ms": leak_date_ms,
                "leak_date_source": leak_date_source,
                "allowed_layers": route_layers(leak_info["facility"]),
                "geometry": row.geometry,
            }
        )
    log(f"Prepared leak match tasks: {len(prepared):,}")
    for name in sorted(counters):
        log(f"{name}: {counters[name]:,}")
    return prepared, counters


def decode_pipe_materials(session, pipe_gdf, layer_url, layer_name):
    """Turn ASSETGROUP + ASSETTYPE into the material columns, in this process.

    ASSETTYPE is the material type and its subtype domain value is the material
    name, so once the download carries ASSETGROUP and ASSETTYPE, naming the
    material needs nothing but the layer's own metadata - which this run already
    fetches. Doing it here means one command downloads and decodes;
    scripts/enrich_assettype_cache.py remains for repairing an existing cache.

    The decode itself comes from leakrelocation.assettype, the same function the
    enrichment script uses, so the two cannot disagree.

    Returns the frame unchanged if it already carries a decoded material - a
    cache enriched by the script, or a re-run.
    """
    if schema.MATERIAL in pipe_gdf.columns:
        detail(f"{layer_name}: already carries {schema.MATERIAL}; no decode needed.")
        return pipe_gdf

    missing = [name for name in (schema.ASSETGROUP, schema.ASSETTYPE)
               if name not in pipe_gdf.columns]
    if missing:
        fail(f"{layer_name}: cannot decode the material because {missing} "
             f"is missing from the download. ASSETTYPE is the material type. "
             f"Present: {sorted(pipe_gdf.columns)}")

    layer_json = arcgis.request_json(session, layer_url, {"f": "json"})
    type_id_field, decoder = build_assettype_decoder(layer_json)
    if not decoder:
        fail(f"{layer_name}: the layer metadata carries no ASSETTYPE subtype "
             f"domains, so the material cannot be named. typeIdField="
             f"{type_id_field!r}")

    decoded = [
        decoder.get((norm_code(group), norm_code(asset_type)), {})
        for group, asset_type in zip(pipe_gdf[schema.ASSETGROUP],
                                     pipe_gdf[schema.ASSETTYPE])
    ]

    pipe_gdf = pipe_gdf.copy()
    pipe_gdf[schema.PIPE_MATERIAL_RAW] = pipe_gdf[schema.ASSETTYPE]
    pipe_gdf[schema.ASSETTYPE_DECODED] = [row.get("ASSETTYPE_DECODED") for row in decoded]
    pipe_gdf[schema.PIPE_MATERIAL_FAMILY] = [row.get("PipeMaterialFamily") for row in decoded]
    # The decoded label, under the name the matching reads.
    pipe_gdf[schema.MATERIAL] = pipe_gdf[schema.ASSETTYPE_DECODED]

    named = int(pipe_gdf[schema.MATERIAL].notna().sum())
    log(f"{layer_name}: decoded {named:,}/{len(pipe_gdf):,} pipe materials from "
        f"ASSETGROUP + ASSETTYPE")
    if named < len(pipe_gdf):
        unknown = sorted({
            f"({norm_code(group)}, {norm_code(asset_type)})"
            for group, asset_type, row in zip(
                pipe_gdf[schema.ASSETGROUP], pipe_gdf[schema.ASSETTYPE], decoded)
            if not row
        })
        warn(f"{layer_name}: {len(pipe_gdf) - named:,} rows had an "
             f"(ASSETGROUP, ASSETTYPE) pair the layer's domains do not define: "
             f"{unknown[:10]}")

    counts = pipe_gdf[schema.PIPE_MATERIAL_FAMILY].value_counts(dropna=False)
    for family, count in counts.items():
        log(f"    {family}: {count:,}")
    return pipe_gdf


def resolve_pipe_date_fields(pipe_gdf, layer_name):
    """The pipe's created and retired columns, whichever of them this layer has.

    The live layers carry both. The retired layer is on a different service, so
    its spelling is confirmed here rather than assumed; the rule that layer is
    checked against needs its retirement date, so a run without it stops instead
    of matching every retired pipe unfiltered.
    """
    created = layer_fields.resolved_field(pipe_gdf.columns, layer_fields.PIPE_CREATED_CANDIDATES, False,
                             layer_name + " created date")
    retired = layer_fields.resolved_field(pipe_gdf.columns, layer_fields.PIPE_RETIRED_CANDIDATES, False,
                             layer_name + " retired date")
    rule = layer_fields.LAYER_DATE_RULES.get(layer_name)

    if rule == RETIRED_AFTER_LEAK and not retired:
        fail(f"{layer_name}: no retirement date column, so a leak cannot be "
             f"checked against when the pipe left service. Looked for "
             f"{layer_fields.PIPE_RETIRED_CANDIDATES}. Present: {sorted(pipe_gdf.columns)}")
    if rule == IN_SERVICE_AT_LEAK and not created:
        warn(f"{layer_name}: no {layer_fields.PIPE_CREATED_CANDIDATES[0]} column, so its pipes "
             f"are matched without checking that they pre-date the leak. "
             f"Re-download: python run.py --refresh")
    return created, retired


def prepare_pipes(pipe_gdf, layer_name):
    step(f"Preparing pipe records: {layer_name}")
    pipe_oid_field = layer_fields.resolved_field(
        pipe_gdf.columns, layer_fields.OBJECTID_CANDIDATES, True, layer_name + " OBJECTID"
    )
    # Read by name, not resolved. scripts/enrich_assettype_cache.py publishes the
    # decoded ASSETTYPE as this column specifically for this consumer. Resolving
    # it by candidate list meant that on a cache without it, the search fell
    # through to ASSETTYPE itself and matched on the numeric subtype code - every
    # pipe collapsing into one material family, with nothing reporting it.
    material_field = schema.MATERIAL
    if material_field not in pipe_gdf.columns:
        fail(
            f"{layer_name}: {material_field} is missing, so the ASSETTYPE decode "
            f"has not run on this frame. decode_pipe_materials does it in the "
            f"workflow; scripts/enrich_assettype_cache.py repairs an existing "
            f"cache. Present: {sorted(pipe_gdf.columns)}"
        )
    diameter_field = layer_fields.resolved_field(
        pipe_gdf.columns, layer_fields.PIPE_DIAMETER_CANDIDATES, True, layer_name + " diameter"
    )
    pressure_field = layer_fields.resolved_field(
        pipe_gdf.columns, layer_fields.PIPE_PRESSURE_CANDIDATES, False, layer_name + " pressure"
    )
    globalid_field = layer_fields.resolved_field(
        pipe_gdf.columns, layer_fields.GLOBALID_CANDIDATES, False, layer_name + " GlobalID"
    )
    if REQUIRE_PRESSURE_MATCH and not pressure_field:
        fail(
            f"REQUIRE_PRESSURE_MATCH is True but no pressure field was resolved on {layer_name}."
        )
    created_field, retired_field = resolve_pipe_date_fields(pipe_gdf, layer_name)
    date_rule = layer_fields.LAYER_DATE_RULES.get(layer_name)
    if date_rule is None:
        fail(f"{layer_name}: no date rule is defined for this layer. "
             f"Known: {sorted(layer_fields.LAYER_DATE_RULES)}")

    records = []
    missing_material = 0
    missing_diameter = 0
    for _, row in pipe_gdf.iterrows():
        pipe_oid = int(row[pipe_oid_field])
        mat = material_label(row.get(material_field))
        diam = parse_number(row.get(diameter_field))
        if not mat:
            missing_material += 1
        if diam is None:
            missing_diameter += 1
        records.append(
            {
                "layer": layer_name,
                "pipe_oid": pipe_oid,
                "globalid": clean(row.get(globalid_field)) if globalid_field else "",
                "material": mat,
                "diameter": diam,
                "pressure": clean(row.get(pressure_field)) if pressure_field else "",
                "date_rule": date_rule,
                "created_ms": dates.date_value_to_epoch_ms(row.get(created_field))
                if created_field else None,
                "retired_ms": dates.date_value_to_epoch_ms(row.get(retired_field))
                if retired_field else None,
                "geometry": row.geometry,
            }
        )
    log(f"Prepared {len(records):,} {layer_name} pipes (date rule: {date_rule})")
    log(f"{layer_name} pipes missing material: {missing_material:,}")
    log(f"{layer_name} pipes missing diameter: {missing_diameter:,}")
    return records


def init_worker(pipe_sources):
    global WORKER_TREES
    WORKER_TREES = {}
    for layer_name, records in pipe_sources.items():
        geoms = [record["geometry"] for record in records]
        tree = STRtree(geoms)
        WORKER_TREES[layer_name] = {"tree": tree, "geoms": geoms, "records": records}


def get_tree_hits(tree_info, query_geometry):
    query_buffer = query_geometry.buffer(MAX_RADIUS_FT)
    raw_hits = tree_info["tree"].query(query_buffer)
    hits = []
    for hit in raw_hits:
        if isinstance(hit, numbers.Integral):
            hits.append(int(hit))
        else:
            try:
                hits.append(tree_info["geoms"].index(hit))
            except ValueError:
                continue
    return hits


def match_one_leak(task):
    leak_oid = task["leak_oid"]
    leak_number = task["leak_number"]
    leak_info = task["leak_info"]
    leak_geometry = task["geometry"]
    leak_date_ms = task.get("leak_date_ms")
    allowed_layers = task["allowed_layers"]
    candidates = []
    rejected_on_date = 0
    for layer_name in allowed_layers:
        tree_info = WORKER_TREES[layer_name]
        for idx in get_tree_hits(tree_info, leak_geometry):
            pipe = tree_info["records"][idx]
            distance_ft = leak_geometry.distance(pipe["geometry"])
            if distance_ft > MAX_RADIUS_FT:
                continue
            diameter_result = diameter_match(
                leak_info["diameter"], pipe["diameter"], DIAMETER_MATCH_MODE)
            if diameter_result is None:
                continue
            if not material_matches(leak_info["material"], pipe["material"]):
                continue
            if not pressure_matches(leak_info["pressure"], pipe["pressure"]):
                continue
            # The pipe has to have been there when the leak was recorded: after
            # CREATIONDATE on the live layers, before dateretired on the retired
            # one. A leak with no date, or a pipe missing the date its rule needs,
            # is allowed through and the reason is carried to the output.
            allowed, date_reason = date_rule_result(
                pipe["date_rule"], leak_date_ms,
                pipe.get("created_ms"), pipe.get("retired_ms"))
            if not allowed:
                rejected_on_date += 1
                continue
            candidates.append(
                {
                    "layer": layer_name,
                    "pipe_oid": pipe["pipe_oid"],
                    "distance_ft": distance_ft,
                    "date_rule": pipe["date_rule"],
                    "date_reason": date_reason,
                    # exact, one_size_up or one_size_down. Carried so the audit
                    # records how much slack the match took, not only that one
                    # was found.
                    "diameter_match": diameter_result,
                }
            )
    if not candidates:
        rule = ("exact diameter" if DIAMETER_MATCH_MODE == DIAMETER_EXACT
                else "diameter within one nominal size")
        # The number, not the name of the constant. "within MAX_RADIUS_FT"
        # told a reader of the audit nothing they could act on.
        reason = (f"No {rule}/material/pressure match found within "
                  f"{MAX_RADIUS_FT:,.0f} ft")
        if rejected_on_date:
            reason = (f"{rejected_on_date:,} pipe(s) matched on "
                      f"diameter/material/pressure but were not in service when "
                      f"the leak was recorded")
        return {
            "leak_oid": leak_oid,
            "matched": False,
            "rejected_on_date": rejected_on_date,
            "reason": reason,
        }
    candidates.sort(key=candidate_sort_key)
    best = candidates[0]
    verbose(
        f"Leak {leak_number}: matched {best['layer']} pipe OID {best['pipe_oid']} at {best['distance_ft']:.2f} ft"
    )
    return {
        "leak_oid": leak_oid,
        "matched": True,
        "layer": best["layer"],
        "pipe_oid": best["pipe_oid"],
        "distance_ft": best["distance_ft"],
        "matched_radius": matched_radius_from_distance(best["distance_ft"]),
        "date_rule": best["date_rule"],
        "date_reason": best["date_reason"],
        "diameter_match": best["diameter_match"],
        "rejected_on_date": rejected_on_date,
        "reason": "",
    }


def run_matching(leak_tasks, pipe_sources):
    step("Running spatial + attribute matching")
    log(f"USE_MULTIPROCESSING: {USE_MULTIPROCESSING}")
    log(f"WORKERS: {WORKERS}")
    if not leak_tasks:
        return []
    if not USE_MULTIPROCESSING or WORKERS <= 1:
        init_worker(pipe_sources)
        results = []
        for index, task in enumerate(leak_tasks, start=1):
            if index == 1 or index % MESSAGE_EVERY_N_LEAKS == 0:
                log(f"Matched task progress: {index:,}/{len(leak_tasks):,}")
            results.append(match_one_leak(task))
        return results
    results = []
    with futures.ProcessPoolExecutor(
        max_workers=WORKERS, initializer=init_worker, initargs=(pipe_sources,)
    ) as executor:
        future_list = [executor.submit(match_one_leak, task) for task in leak_tasks]
        for index, future in enumerate(futures.as_completed(future_list), start=1):
            if index == 1 or index % MESSAGE_EVERY_N_LEAKS == 0:
                log(f"Matched task progress: {index:,}/{len(future_list):,}")
            results.append(future.result())
    log(f"Matching complete. Results: {len(results):,}")
    return results


def pipe_lookup(pipe_sources):
    lookup = {}
    for layer_name, records in pipe_sources.items():
        lookup[layer_name] = {record["pipe_oid"]: record for record in records}
    return lookup


def empty_or_frame(rows, crs):
    """A GeoDataFrame from these rows, or an empty one that still has geometry.

    gpd.GeoDataFrame([], geometry="geometry") raises, because there is no such
    column to promote. An empty frame has to be built the other way round.
    """
    if not rows:
        return gpd.GeoDataFrame([], geometry=[], crs=crs)
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=crs)


def write_outputs(leak_tasks, match_results, pipe_sources, initial_counters):
    step("Writing GeoPackage outputs")
    task_lookup = {task["leak_oid"]: task for task in leak_tasks}
    pipes = pipe_lookup(pipe_sources)
    point_rows = []
    line_rows = []
    audit_rows = []
    counters = defaultdict(int)
    for name, value in initial_counters.items():
        counters[name] += value
    run_utc = dt.datetime.now(dt.UTC)
    for result in match_results:
        task = task_lookup[result["leak_oid"]]
        leak_oid = task["leak_oid"]
        leak_number = task["leak_number"]
        leak_globalid = task["leak_globalid"]
        leak_address = task.get("leak_address", "")
        leak_info = task["leak_info"]
        original_geometry = task["geometry"]
        original_point = (
            original_geometry
            if original_geometry.geom_type == "Point"
            else original_geometry.centroid
        )
        if not result["matched"]:
            counters["unmatched"] += 1
            audit_rows.append(
                {
                    "LeakOID": leak_oid,
                    "LeakKey": leak_number,
                    "LeakGlobalID": leak_globalid,
                    "LeakAddress": leak_address,
                    "LeakMaterial": leak_info["material"],
                    "LeakDiameter": leak_info["diameter"],
                    "LeakPressure": leak_info["pressure"],
                    "FacilityType": leak_info["facility"],
                    "LinkedLayer": ",".join(task["allowed_layers"]),
                    "MatchedPipeOID": None,
                    "MatchedPipeGID": "",
                    "PipeMaterial": "",
                    "PipeDiameter": None,
                    "PipePressure": "",
                    "SearchRadiusFt": None,
                    "DistanceFt": None,
                    "DiameterMatch": "",
                    "DiameterMode": DIAMETER_MATCH_MODE,
                    "LeakSource": LEAK_SOURCE,
                    "LeakDate": dates.epoch_ms_to_iso(task.get("leak_date_ms")),
                    "LeakDateSource": task.get("leak_date_source", ""),
                    "PipesRejectedOnDate": result.get("rejected_on_date", 0),
                    "MatchStatus": "NoMatch",
                    "NoMatchReason": result["reason"],
                    "OrigX": original_point.x,
                    "OrigY": original_point.y,
                    "NewX": None,
                    "NewY": None,
                    "RunUTC": run_utc,
                }
            )
            continue
        pipe = pipes[result["layer"]][result["pipe_oid"]]
        snap_pair = nearest_points(original_point, pipe["geometry"])
        snapped_point = snap_pair[1]
        guide_line = LineString([original_point, snapped_point])
        point_rows.append(
            {
                "OrigLeakOID": leak_oid,
                "LeakKey": leak_number,
                "LeakAddress": leak_address,
                "LinkedLayer": result["layer"],
                "MatchedPipeOID": result["pipe_oid"],
                "MatchedPipeGID": pipe["globalid"],
                "DistanceFt": result["distance_ft"],
                "SearchRadiusFt": result["matched_radius"],
                "MatchMaterial": pipe["material"],
                "MatchDiameter": pipe["diameter"],
                "MatchPressure": pipe["pressure"],
                # How much diameter slack this relocation took: exact,
                # one_size_up or one_size_down. On the map it is the difference
                # between a relocation the strict rule would also have made and
                # one only the widened rule found.
                "DiameterMatch": result.get("diameter_match", ""),
                "DiameterMode": DIAMETER_MATCH_MODE,
                "LeakSource": LEAK_SOURCE,
                "LeakDate": dates.epoch_ms_to_iso(task.get("leak_date_ms")),
                "LeakDateSource": task.get("leak_date_source", ""),
                "DateRule": result.get("date_rule", ""),
                "DateCheck": result.get("date_reason", ""),
                "geometry": snapped_point,
            }
        )
        line_rows.append(
            {
                "LeakOID": leak_oid,
                "LeakKey": leak_number,
                "LinkedLayer": result["layer"],
                "DistanceFt": result["distance_ft"],
                "MatchedPipeOID": result["pipe_oid"],
                "geometry": guide_line,
            }
        )
        audit_rows.append(
            {
                "LeakOID": leak_oid,
                "LeakKey": leak_number,
                "LeakGlobalID": leak_globalid,
                "LeakAddress": leak_address,
                "LeakMaterial": leak_info["material"],
                "LeakDiameter": leak_info["diameter"],
                "LeakPressure": leak_info["pressure"],
                "FacilityType": leak_info["facility"],
                "LinkedLayer": result["layer"],
                "MatchedPipeOID": result["pipe_oid"],
                "MatchedPipeGID": pipe["globalid"],
                "PipeMaterial": pipe["material"],
                "PipeDiameter": pipe["diameter"],
                "PipePressure": pipe["pressure"],
                "SearchRadiusFt": result["matched_radius"],
                "DistanceFt": result["distance_ft"],
                "DiameterMatch": result.get("diameter_match", ""),
                "DiameterMode": DIAMETER_MATCH_MODE,
                "LeakSource": LEAK_SOURCE,
                "LeakDate": dates.epoch_ms_to_iso(task.get("leak_date_ms")),
                "LeakDateSource": task.get("leak_date_source", ""),
                "PipeCreated": dates.epoch_ms_to_iso(pipe.get("created_ms")),
                "PipeRetired": dates.epoch_ms_to_iso(pipe.get("retired_ms")),
                "DateRule": result.get("date_rule", ""),
                # "ok" means the dates were compared and the pipe was in service.
                # Anything else says why the check could not be made.
                "DateCheck": result.get("date_reason", ""),
                "PipesRejectedOnDate": result.get("rejected_on_date", 0),
                "MatchStatus": "Matched",
                "NoMatchReason": "",
                "OrigX": original_point.x,
                "OrigY": original_point.y,
                "NewX": snapped_point.x,
                "NewY": snapped_point.y,
                "RunUTC": run_utc,
            }
        )
        counters["matched"] += 1
    if os.path.exists(OUTPUT_GPKG):
        os.remove(OUTPUT_GPKG)
        log(f"Deleted old GeoPackage: {OUTPUT_GPKG}")
    # A run that matched nothing still has to write its outputs and say so.
    # Building a GeoDataFrame from an empty list and naming a geometry column
    # that is not there raises "Unknown column geometry", so the whole run died
    # at the last step with a message about a column rather than the news that
    # nothing matched. That is the shape of a first run against a new leak
    # source whose coordinates are being read in the wrong CRS: every leak lands
    # somewhere there are no pipes, nothing matches, and the error says nothing
    # about it. The audit layer below already handled this; these two did not.
    relocated_gdf = empty_or_frame(point_rows, TARGET_CRS)
    lines_gdf = empty_or_frame(line_rows, TARGET_CRS)
    if not point_rows:
        warn("No leak was relocated. The outputs are written and empty.\n"
             "  Check that the leaks and the pipes are in the same part of the "
             "world: a leak layer read in the wrong CRS lands where there are "
             "no pipes and matches nothing.")
    audit_df = pd.DataFrame(audit_rows)
    if len(audit_df):
        audit_gdf = gpd.GeoDataFrame(
            audit_df,
            geometry=gpd.points_from_xy(
                audit_df["OrigX"], audit_df["OrigY"], crs=TARGET_CRS
            ),
        )
    else:
        audit_gdf = gpd.GeoDataFrame(audit_df, geometry=[], crs=TARGET_CRS)
    relocated_gdf.to_file(
        OUTPUT_GPKG, layer=schema.RELOCATED_LEAKS_LAYER, driver="GPKG")
    lines_gdf.to_file(
        OUTPUT_GPKG, layer=schema.OFFSET_LINES_LAYER, driver="GPKG")
    audit_gdf.to_file(OUTPUT_GPKG, layer=schema.AUDIT_LAYER, driver="GPKG")
    log(f"Wrote relocated_leaks: {len(relocated_gdf):,}")
    log(f"Wrote relocated_leak_offset_lines: {len(lines_gdf):,}")
    log(f"Wrote leak_relocation_audit: {len(audit_gdf):,}")
    log(f"Output GeoPackage: {OUTPUT_GPKG}")
    return counters


def banner(leak_source, output_gpkg):
    """What this run is about to do, before it does any of it."""
    step("Starting non-ArcPy GeoPandas leak relocation")
    log(f"Leak source: {leak_source}")
    log(f"Output GeoPackage: {output_gpkg}")
    log("Analysis CRS: native per-layer (no reprojection before matching)")
    log(f"MA layer filter: {WHERE_MA}")
    log(f"Search radius: {INITIAL_RADIUS_FT:,.0f} ft, widening by "
        f"{RADIUS_INCREMENT_FT:,.0f} ft to {MAX_RADIUS_FT:,.0f} ft"
        + (" (one pass, no widening)"
           if MAX_RADIUS_FT <= INITIAL_RADIUS_FT else ""))
    log(f"Require pressure match: {REQUIRE_PRESSURE_MATCH}")
    log(f"Material family fallback: {ALLOW_MATERIAL_FAMILY_FALLBACK}")
    log(f"Diameter match mode: {DIAMETER_MATCH_MODE}")
    log(f"Leak date fallback: "
        f"{'on - ' + ', '.join(layer_fields.LEAK_DATE_FALLBACK_CANDIDATES) if config.USE_LEAK_DATE_FALLBACK else 'off'}")
    if DIAMETER_MATCH_MODE != DIAMETER_EXACT:
        log(f"  A pipe may be one nominal size up or down from the leak's "
            f"diameter. Nominal sizes (in): "
            f"{', '.join(format(size, 'g') for size in NOMINAL_DIAMETERS_IN)}")
        log(f"  Exact diameters still win over adjacent ones: "
            f"PREFER_EXACT_DIAMETER={config.PREFER_EXACT_DIAMETER}")


def load_pipe_layers(session, with_leaks=None):
    """The pipe layers, and optionally the historic leak layer alongside them.

    `with_leaks` names the leak layer to download in the same parallel pass.
    A leak set that does not come from the service - the Maximo leaks come from
    SQL - passes None and only the pipes are fetched.
    """
    layer_specs = [
        ("distribution pipes", DISTRIBUTION_PIPE_URL),
        ("service pipes", SERVICE_PIPE_URL),
    ]
    if with_leaks:
        layer_specs.insert(0, with_leaks)

    if config.PARALLEL_LAYER_LOAD:
        # Independent reads. Fetch the token once up front so the workers do
        # not race into three interactive sign-ins.
        auth.get_arcgis_token(session)
        with futures.ThreadPoolExecutor(max_workers=len(layer_specs)) as pool:
            submitted = {
                name: pool.submit(layer_cache.query_arcgis_layer, session, url, WHERE_MA, name)
                for name, url in layer_specs
            }
            return {name: future.result() for name, future in submitted.items()}
    return {
        name: layer_cache.query_arcgis_layer(session, url, WHERE_MA, name)
        for name, url in layer_specs
    }


def relocate(session, leaks, supplemental, distribution, service,
             output_gpkg=None, leak_label="leaks", leak_source="historic"):
    """Match a set of leaks to the pipes and write the outputs.

    Everything after the leaks have been obtained, and the whole of what the
    two leak sources share. The historic (LMS) leaks arrive from layer 206 and
    the enterprise (Maximo) leaks from a SQL query, but by this point both are
    a GeoDataFrame plus a mapping of leak key -> material, diameter, pressure
    and facility type, and nothing below here can tell them apart.

    `output_gpkg` keeps the two leak sets in separate files. Writing both to one
    would mean the second run silently replacing the first.

    `leak_source` also decides where the leaks' coordinate system comes from.
    Historic leaks were downloaded from layer 206 and are in its native CRS;
    leaks from anywhere else are projected from the CRS their frame carries.
    """
    previous_output = OUTPUT_GPKG
    previous_source = LEAK_SOURCE
    if output_gpkg:
        globals()["OUTPUT_GPKG"] = str(output_gpkg)
    globals()["LEAK_SOURCE"] = leak_source
    leak_layer_key = "historic_leaks" if leak_source == "historic" else None
    try:
        with output.timed("normalize CRS"):
            leaks, distribution, service, ANALYSIS_CRS = (
                native_crs.normalize_loaded_layers_to_analysis_crs(
                    session, leaks, distribution, service,
                    leak_layer_key=leak_layer_key)
            )
        globals()["TARGET_CRS"] = ANALYSIS_CRS

        detail("CRS sanity check after normalization:")
        for label, gdf in [(leak_label, leaks),
                           ("distribution pipes", distribution),
                           ("service pipes", service)]:
            detail(f"  {label}: CRS={gdf.crs} bounds={list(gdf.total_bounds)}")

        with output.timed("decode pipe materials"):
            distribution = decode_pipe_materials(
                session, distribution, DISTRIBUTION_PIPE_URL, "distribution pipes")
            service = decode_pipe_materials(
                session, service, SERVICE_PIPE_URL, "service pipes")

        retired = None
        if USE_RETIRED_PIPE_LAYER:
            with output.timed("load retired pipes"):
                retired = load_retired_pipes(session)

        with output.timed("prepare leaks"):
            leak_tasks, initial_counters = prepare_leaks(leaks, supplemental)
        with output.timed("build pipe indexes"):
            pipe_sources = {
                "distribution": prepare_pipes(distribution, "distribution"),
                "service": prepare_pipes(service, "service"),
            }
            if retired is not None and len(retired):
                pipe_sources["retired"] = prepare_pipes(retired, "retired")
        with output.timed("match leaks to pipes"):
            match_results = run_matching(leak_tasks, pipe_sources)
        with output.timed("write outputs"):
            counters = write_outputs(
                leak_tasks, match_results, pipe_sources, initial_counters)

        step("Finished")
        for name in sorted(counters):
            log(f"{name}: {counters[name]:,}")
        rescuable = counters.get("leaks_a_date_fallback_would_rescue", 0)
        if rescuable and not config.USE_LEAK_DATE_FALLBACK:
            warn(f"{rescuable:,} leaks have no "
                 f"{'/'.join(layer_fields.LEAK_DATE_CANDIDATES)} but do carry a "
                 f"{', '.join(layer_fields.LEAK_DATE_FALLBACK_CANDIDATES)}. For those the "
                 f"pipe-life rule decided nothing at all.\n"
                 f"  To check them against the pipe's life as well: "
                 f"python run.py --leak-date-fallback\n"
                 f"  It will refuse some relocations that this run allowed. "
                 f"Every row records which field supplied its date, in "
                 f"LeakDateSource.")
        log(f"GeoPackage: {OUTPUT_GPKG}")
        output.report_timings()
        return counters
    finally:
        globals()["OUTPUT_GPKG"] = previous_output
        globals()["LEAK_SOURCE"] = previous_source


def main():
    banner(f"historic (LMS) leaks, layer 206 - {HIST_LEAK_URL}", OUTPUT_GPKG)
    log(f"Supplemental CSV: {SUPPLEMENTAL_CSV}")
    ensure_output_folder()

    with output.timed("supplemental CSV"):
        supplemental = load_supplemental()

    session = auth.make_session()

    with output.timed("load layers"):
        loaded = load_pipe_layers(
            session, with_leaks=("historic leaks", HIST_LEAK_URL))

    return relocate(
        session,
        loaded["historic leaks"],
        supplemental,
        loaded["distribution pipes"],
        loaded["service pipes"],
        leak_label="historic leaks",
    )


def _cli(argv=None):
    """`python -m leakrelocation.workflow` - the historic relocation, no map.

    run.py is the normal entry point; this exists so the workflow can be run
    on its own, and so --help answers without starting a download.
    """
    import argparse
    argparse.ArgumentParser(
        prog="python -m leakrelocation.workflow",
        description="Relocate the historic (LMS) leaks and write the "
                    "GeoPackage. Normally started by run.py.").parse_args(argv)
    try:
        main()
    except Exception:  # noqa: BLE001 - the top-level handler catches everything
        # Its whole purpose: print the traceback and exit non-zero, rather than
        # let a bare stack trace scroll past in a console that then closes.
        print(traceback.format_exc(), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
