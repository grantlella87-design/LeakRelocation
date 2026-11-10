"""Which fields the workflow reads from each layer, and how they are resolved.

The candidate lists name the fields on the DNV layers and in the supplemental
CSV; `resolved_field` turns a list into the spelling a particular layer or file
actually uses; `build_out_fields` turns the lists into the outFields of a
request. Nothing here touches the network: the functions take layer metadata
that has already been fetched.
"""
import hashlib
import json
import os
import sys

# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member. See leakrelocation/output.py for the full note.
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PACKAGE_PARENT not in sys.path:
    sys.path.insert(0, _PACKAGE_PARENT)

from leakrelocation import leak_location as location, schema
from leakrelocation.matching import (
    IN_SERVICE_AT_LEAK,
    RETIRED_AFTER_LEAK,
    resolve_field_name,
)
from leakrelocation.output import detail, fail, warn

# --- DNV service field names -------------------------------------------------
#
# These are no longer guesses. Every name below was read out of the service's
# own metadata, a copy of which is committed under
# reference/mapserver_json/NY_DNV_Synergi_RiskResults_Assets_NY/ - layer 6
# (Distribution Pipe), layer 7 (Service Pipe) and layer 206 (Hist_GasLeak).
# tests/test_dnv_service_metadata.py checks these lists against that copy, so a
# name that stops existing fails a test instead of silently resolving to
# nothing at runtime.
#
# The lists that used to be here carried 34 spellings that exist on no layer -
# NOMINALDIAMETER, OP_PRESSURE, MODIFIEDDATE, a misspelled LMSLEAKSUMBER and so
# on. Most were case variants, and resolve_field_name compares names with case
# and punctuation stripped, so they could never have resolved differently from
# the entry beside them: one spelling covers every casing of the same name, and
# the resolver returns the layer's own spelling. Where more than one name
# survives below, they are genuinely different fields.
MODIFIED_FIELD_CANDIDATES = ["LASTUPDATE"]

# LMSLEAKNUMBER is the leak key; LEAKNUMBER also exists on layer 206 and is
# kept as the fallback.
# The leak number a person uses. LMSLEAKNUMBER is layer 206's; the Maximo
# query aliases its own to LEAKNUMBER, so one list serves both leak sources.
LEAK_KEY_CANDIDATES = ["LMSLEAKNUMBER", "LEAKNUMBER"]

# The street address of the leak, read out of layer 206's metadata: ADDRESS,
# esriFieldTypeString, alias "Address", 100 characters, no domain. It is not used
# by the matching - it is carried through so a leak can be identified on the map
# and in the audit by where it is rather than only by its number.
LEAK_ADDRESS_CANDIDATES = ["ADDRESS"]

# The rest of what layer 206 knows about where a leak is. ADDRESS is requested
# and is present in the cache as a column, but every one of the 98,501 MA rows
# came back empty; scripts/probe_leak_fields.py asks the service which of the
# three possible reasons that is. Meanwhile these are the only other location
# fields the layer has - both read out of the committed metadata, both
# esriFieldTypeString.
#
# Every one of them is requested rather than the first that resolves: which of
# them this service populates is exactly what is unknown, so one refresh has to
# collect them all. leakrelocation.leak_location decides what to do with them.
LEAK_LOCATION_CANDIDATES = [name for name in location.SERVICE_FIELDS
                            if name not in LEAK_ADDRESS_CANDIDATES]


def is_leak_layer_name(layer_name):
    """Whether a layer name is the historic leak layer.

    One definition, used by build_out_fields and by the cache signature, so the
    two cannot disagree about which fields a layer is expected to carry.
    """
    lowered = str(layer_name).lower()
    return "historic" in lowered or "leak" in lowered

PIPE_DIAMETER_CANDIDATES = ["nominaldiameter", "outsidediameter"]

# ASSETTYPE *is* the pipe material. Its subtype domain value is the material
# name - "Copper", "Cast Iron", "Plastic PE" - and decoding needs ASSETGROUP
# too, because the domain is defined per subtype: code 999 is "UNK" for a pipe
# and "Unknown Type" for the unknown subtype, and codes 281 and 321 exist only
# outside the pipe subtypes.
#
# Both are requested outright. They were previously bolted on by
# apply_pipe_domain_out_fields after the fact, and only when the outFields it
# was handed happened to mention nominaldiameter or operatingpressure - so the
# material type reached the download by way of a heuristic. It is now asked for
# directly, and a pipe layer without it fails immediately rather than
# downloading pipes that have no material.
#
# The DNV `material` field is not requested at all. It holds a spec, grade or
# descriptor - not the material type - and requesting it alongside ASSETTYPE
# only invited the two to be confused for each other.
PIPE_MATERIAL_FIELDS = [schema.ASSETGROUP, schema.ASSETTYPE]

PIPE_PRESSURE_CANDIDATES = ["operatingpressure", "maopdesign"]

# --- Temporal validity -------------------------------------------------------
#
# A leak is only relocated onto a pipe that existed when the leak was recorded.
# REVISEDLEAKDATE is on layer 206; CREATIONDATE and dateretired are on layers 6
# and 7. All three were read out of the committed service metadata.
#
# The retired layer lives on a different service, so its own field names are
# resolved from its metadata at run time and the run stops if they are not there
# - see resolve_pipe_date_fields.
# The date the relocation window is measured from. REVISEDLEAKDATE on layer
# 206, MAXIMOLEAKDATE from the SQL query.
LEAK_DATE_CANDIDATES = ["REVISEDLEAKDATE", "MAXIMOLEAKDATE"]

# The other dates layer 206 carries, in the order they stand in for a missing
# REVISEDLEAKDATE.
#
# This matters more than it looks. REVISEDLEAKDATE came back empty on all
# 98,501 MA rows, so DateCheck reads "no_leak_date" for 98.1% of the audit and
# the pipe-life rule decides nothing at all - a leak is matched to a pipe that
# may have been laid years after it was reported, and the only sign is a column
# nobody reads. Discovered, repaired and completed are all real dates on the
# same row, and any of them places the leak in time far better than nothing.
#
# They are always collected and always reported, so the size of the gap is
# visible. They are only *used* when config.USE_LEAK_DATE_FALLBACK says so,
# because using them changes which leaks relocate: a leak that matched
# unchecked may be refused once it has a date to check against. Every row
# records which field supplied its date, in LeakDateSource.
LEAK_DATE_FALLBACK_CANDIDATES = ["DISCOVEREDDATE", "REPAIREDDATE",
                                 "COMPLETEDDATE"]
PIPE_CREATED_CANDIDATES = ["CREATIONDATE"]
PIPE_RETIRED_CANDIDATES = ["dateretired"]

# Which end of a pipe's life each layer is checked against.
LAYER_DATE_RULES = {
    "distribution": IN_SERVICE_AT_LEAK,
    "service": IN_SERVICE_AT_LEAK,
    "retired": RETIRED_AFTER_LEAK,
}

# The pipe layers spell it GLOBALID and layer 206 spells it GlobalID. One entry
# covers both: the resolver ignores case and returns the layer's own spelling.
# The leak's own unique id, and the key its attributes are looked up by. On
# layer 206 that is the GlobalID; the Maximo query aliases its leak id to
# MAXIMOLEAKID. Order matters only in that the first present wins, and no frame
# carries both.
GLOBALID_CANDIDATES = ["GLOBALID", "MAXIMOLEAKID"]
OBJECTID_CANDIDATES = ["OBJECTID"]

# Lower case on all three layers.
JURISDICTION_CANDIDATES = ["jurisdiction"]
# --- Supplemental CSV headers ------------------------------------------------
#
# These were the last unverified names in this file. HL_SupplementalData.csv is
# now committed under input/, so they are facts as well: every name below was read
# off its 33 headers, and tests/test_supplemental_csv.py checks these lists
# against the committed file, offline.
#
# The counts in the comments are from that file: 98,464 MA rows.
#
# The join key is the leak's GlobalID, not its leak number.
#
# HistoricalLeaksID holds layer 206's GlobalID - a braced 36-character GUID on
# every one of the 98,464 rows, and unique on every one of them. LeakNumber is
# not: 25,733 rows (26.1%) share a number with another row, across 10,990
# numbers, and 5,364 of those numbers have rows that disagree about the material
# or the diameter. Keying on the number therefore imported an arbitrary row's
# attributes for those leaks - one real example being leak 1000001, which has a
# Cast Iron 8" main row and a Bare Steel 1.25" service row - and dropped 14,743
# rows outright.
#
# Both sides are normalised with normalize_key, which strips the braces and
# upper-cases, so {abc...} and ABC... join.
SUPP_GLOBALID_CANDIDATES = ["HistoricalLeaksID"]

# Kept for labelling and the audit table, not for joining. LeakNumber and Name
# hold the same value on all 98,464 rows, so Name is a duplicate column rather
# than a fallback. The old list also carried LMSLEAKNUMBER and LEAKNUMBER, which
# the CSV does not have - and LEAKNUMBER could never have been reached anyway,
# because resolve_field_name strips case and it is "LeakNumber" in different
# clothes.
SUPP_KEY_CANDIDATES = ["LeakNumber", "Name"]

# Diameter is filled on 94.7%. Abdn_Diameter_Main and Abdn_Diameter_Service are
# filled on 100% but are different measurements - the main's diameter and the
# service's - so falling back to either would answer a question about one pipe
# with a number from another. Diameter is the leak's own, so it is the only entry.
SUPP_DIAMETER_CANDIDATES = ["Diameter"]

# LeakMaterialType is filled on 95.8%; Abdn_Material on 24.8%, Adbn_Material_Main
# (spelled that way in the file) on 23.1% and Abdn_Material_Service on 3.0%. All
# four exist, so the three sparse ones sat behind a name that always resolves and
# could never be reached.
#
# The eight values in the file are Cast Iron, Bare Steel, Plastic - MD, Coated
# Steel, Copper, Plastic - HD, Wrought Iron and blank. Note that "Plastic - MD"
# and "Plastic - HD" are not DNV ASSETTYPE domain labels: they match a pipe
# through the material family, so those 15,313 leaks depend on
# ALLOW_MATERIAL_FAMILY_FALLBACK being on.
SUPP_MATERIAL_CANDIDATES = ["LeakMaterialType"]

# The CSV has no pressure column at all - not Pressure, MAOP, MAOPDesign or
# anything else. The five names that used to be here were guesses for a file
# nothing in this repository could read. The list is deliberately empty: pressure
# matching is off by default, load_supplemental says so, and it fails loudly if
# REQUIRE_PRESSURE_MATCH is ever turned on.
SUPP_PRESSURE_CANDIDATES = []

# FacilityType is filled on 97.7% with "Distribution Main" or "Service", which is
# what route_layers reads. PipeType says "Distribution Pipe" or "Service Pipe" on
# 95.1% and is a different field, kept as the fallback.
SUPP_FACILITY_CANDIDATES = ["FacilityType", "PipeType"]


def resolved_field(columns, candidates, required=False, label="field"):
    available = list(columns)
    resolved = resolve_field_name(available, candidates)
    if resolved is not None:
        detail(f"Resolved {label}: {resolved}")
        return resolved
    if required:
        fail(
            f"Could not resolve required {label}. Candidates={candidates}. Available={available}"
        )
    # The caller reports what the missing field means for the run; listing every
    # candidate here only repeats it.
    detail(f"Could not resolve optional {label}. Candidates={candidates}")
    return None


def metadata_field_names(meta):
    return [field.get("name") for field in meta.get("fields", []) if field.get("name")]


def resolve_from_names(field_names, candidates):
    return resolve_field_name(field_names, candidates)


def modified_field_from_meta(meta, layer_name):
    field_names = metadata_field_names(meta)
    field_name = resolve_from_names(field_names, MODIFIED_FIELD_CANDIDATES)
    if field_name:
        detail(f"{layer_name}: using delta modified field: {field_name}")
    else:
        warn(
            f"{layer_name}: no modified field found. Delta cache unavailable; full refresh will be used."
        )
    return field_name


def build_out_fields(meta, layer_name):
    field_names = metadata_field_names(meta)
    object_id_field = meta.get("object_id_field")
    modified_field = modified_field_from_meta(meta, layer_name)
    wanted = []
    if object_id_field:
        wanted.append(object_id_field)
    if modified_field:
        wanted.append(modified_field)
    if is_leak_layer_name(layer_name):
        candidate_groups = [
            LEAK_KEY_CANDIDATES,
            GLOBALID_CANDIDATES,
            JURISDICTION_CANDIDATES,
            # The date the relocation window is measured from.
            LEAK_DATE_CANDIDATES,
            # Carried for identification, not used by the matching.
            LEAK_ADDRESS_CANDIDATES,
        ]
        # Every location field, not the first that resolves: the point is to
        # collect whatever the service populates, and which one that is is
        # exactly what is unknown.
        for name in LEAK_LOCATION_CANDIDATES if field_names else []:
            resolved = resolve_from_names(field_names, [name])
            if resolved:
                wanted.append(resolved)
        # The same again for the other dates. Collected whether or not they are
        # used, so a run can say how many leaks have no revised date but do
        # have a discovered one - which is the whole case for using them.
        for name in LEAK_DATE_FALLBACK_CANDIDATES if field_names else []:
            resolved = resolve_from_names(field_names, [name])
            if resolved:
                wanted.append(resolved)
    else:
        candidate_groups = [
            PIPE_DIAMETER_CANDIDATES,
            PIPE_PRESSURE_CANDIDATES,
            GLOBALID_CANDIDATES,
            JURISDICTION_CANDIDATES,
            # Both ends of the pipe's life. Which one is checked depends on the
            # layer, but asking for both keeps one outFields list for all of them.
            PIPE_CREATED_CANDIDATES,
            PIPE_RETIRED_CANDIDATES,
        ]
        # The material type. Every one of these is required, not a first match
        # among alternatives, so they are added outright rather than resolved as
        # a candidate group.
        #
        # Only checked when the metadata actually lists fields. With no field
        # list there is nothing to check against, and the fallback at the end of
        # this function requests every field - which includes these two - so
        # failing there would break a run that would otherwise have worked.
        for name in PIPE_MATERIAL_FIELDS if field_names else []:
            resolved = resolve_from_names(field_names, [name])
            if resolved:
                wanted.append(resolved)
            else:
                fail(f"{layer_name}: the layer has no {name} field, so pipe "
                     f"material cannot be read. ASSETTYPE is the material type. "
                     f"Fields: {sorted(field_names)}")
    for group in candidate_groups:
        resolved = resolve_from_names(field_names, group)
        if resolved:
            wanted.append(resolved)
    deduped = []
    for field in wanted:
        if field and field not in deduped:
            deduped.append(field)
    if not deduped:
        warn(f"{layer_name}: could not narrow outFields; requesting all fields")
        return "*"
    out_fields = ",".join(deduped)
    detail(f"{layer_name}: using outFields={out_fields}")
    return out_fields


def out_field_request_signature(layer_name=None):
    """A digest of the fields this code asks the service for.

    A cache holds the columns that were requested when it was written. Adding a
    field to the lists above does not change the cache, and the delta refresh
    only re-downloads rows whose LASTUPDATE moved - so a new field would arrive
    for a handful of changed records and be blank for the rest, which looks like
    a service that has stopped populating it rather than a stale cache.

    The signature is stored with the cache and compared on read. It is built from
    the request configuration alone, so it can be computed without contacting the
    service, and it changes only when the set of requested names changes.

    It is computed per layer kind. A single digest over every list meant that
    adding one leak field invalidated the pipe caches too, and re-downloading
    1.27 million service pipes to collect a leak's street address is a long wait
    for nothing - the wait during which the connection resets that started all
    this. `layer_name` is matched the same way build_out_fields matches it.
    """
    shared = [
        MODIFIED_FIELD_CANDIDATES,
        GLOBALID_CANDIDATES,
        OBJECTID_CANDIDATES,
        JURISDICTION_CANDIDATES,
    ]
    leak = [
        LEAK_KEY_CANDIDATES,
        LEAK_ADDRESS_CANDIDATES,
        LEAK_LOCATION_CANDIDATES,
        LEAK_DATE_CANDIDATES,
        LEAK_DATE_FALLBACK_CANDIDATES,
    ]
    pipe = [
        PIPE_DIAMETER_CANDIDATES,
        PIPE_PRESSURE_CANDIDATES,
        PIPE_MATERIAL_FIELDS,
        PIPE_CREATED_CANDIDATES,
        PIPE_RETIRED_CANDIDATES,
    ]
    if layer_name is None:
        groups = shared + leak + pipe
    elif is_leak_layer_name(layer_name):
        groups = shared + leak
    else:
        groups = shared + pipe
    # Case and order within a group are irrelevant to what comes back, so they
    # are normalised out: a rename that the resolver would treat as the same
    # name must not invalidate every cache.
    canonical = [sorted(name.lower() for name in group) for group in groups]
    payload = json.dumps(canonical, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]
