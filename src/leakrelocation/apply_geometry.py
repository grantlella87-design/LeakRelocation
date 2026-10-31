"""Move the leaks in the source layer to where the relocation put them.

Everything else this project does is additive: it downloads, it computes, it
writes files of its own. This is the one thing that reaches back and changes a
production asset system - layer 206's geometry, in place, for leaks that are
already in service records. There is no undo button on the far side, so the
whole module is arranged around that.

What it does, in order:

  1. reads the relocation outputs and pairs each relocated leak with its
     OBJECTID on the layer;
  2. re-reads the *current* geometry of those features from the service, so the
     rollback is built from what is there now rather than from what was there
     when the relocation ran;
  3. writes the rollback file, and refuses to go on if it cannot;
  4. prints what it is about to do and stops, unless told otherwise;
  5. applies the updates in batches, checking every feature's result.

What stops it:

  * it is a dry run unless `apply=True`. The default of the script is a dry run;
  * a move longer than MAX_APPLY_DISTANCE_FT is held back unless it is allowed
     explicitly. A leak that moved 2,900 ft is a record to check, not an edit to
     push;
  * more than MAX_APPLY_FEATURES at once needs saying so. A run that suddenly
     wants to move every leak on the system is a bug somewhere upstream;
  * a mismatch between the output and the service - a feature that is not there
     any more, or whose geometry has already moved - is reported and skipped
     rather than overwritten.

The token is never printed. Nothing here logs a connection string or a token
value, only counts and object ids.
"""
# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member. See leakrelocation/output.py for the full note.
import os as _os
import sys as _sys

_PACKAGE_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PACKAGE_PARENT not in _sys.path:
    _sys.path.insert(0, _PACKAGE_PARENT)

import datetime as dt
import json
import math

from leakrelocation import config
from leakrelocation.output import fail, log, warn

# How many features go in one applyEdits call. The service takes a list; a list
# of 90,987 would be one request that either works or loses everything, and a
# proxy in the middle that is happy to drop it.
APPLY_BATCH_SIZE = 250

# A relocation longer than this is not applied without being asked for twice.
# The search widens to 3,000 ft, so an output can legitimately contain a leak
# that moved most of a mile - and moving a real asset record that far on the
# strength of a nearest-pipe match is a decision, not a default.
MAX_APPLY_DISTANCE_FT = 100.0

# More than this in one go needs --max-features raised deliberately. It is a
# guard against a bad run, not against a big one.
MAX_APPLY_FEATURES = 25_000

# The service is allowed to tell us a feature has already moved. Past this, the
# output no longer describes the layer and the run stops rather than pushing
# edits computed against something else.
MAX_DRIFTED_FRACTION = 0.02

# Two points this far apart are the same point. Service coordinates come back
# as doubles and a round trip through a projection does not return them bit for
# bit identical.
SAME_POINT_FT = 0.01


def utc_stamp():
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def rollback_path(folder=None, stamp=None):
    """Where the undo file goes. One per run, named for when it ran."""
    folder = folder or config.OUTPUT_GPKG.parent
    return _os.path.join(
        str(folder), f"leak_geometry_rollback_{stamp or utc_stamp()}.json")


def distance_ft(a, b):
    """Straight-line distance between two (x, y) pairs."""
    return math.hypot(a[0] - b[0], a[1] - b[1])


def esri_point(x, y, wkid):
    return {"x": float(x), "y": float(y), "spatialReference": {"wkid": wkid}}


def relocations_from(gdf, object_id_field="OrigLeakOID"):
    """(object id, new x, new y) for every relocated leak in the output.

    Read from the relocated points layer rather than from the audit's NewX/NewY
    columns, because the geometry is what will be written and reading it from
    anywhere else leaves room for the two to disagree.
    """
    if object_id_field not in gdf.columns:
        fail(f"The relocated layer has no {object_id_field} column, so its "
             f"points cannot be matched to features on the service. "
             f"Present: {sorted(gdf.columns)}")
    moves = []
    skipped_no_id = 0
    for object_id, geometry in zip(gdf[object_id_field].tolist(),
                                   gdf.geometry.tolist(), strict=False):
        if object_id is None or geometry is None or geometry.is_empty:
            skipped_no_id += 1
            continue
        try:
            oid = int(object_id)
        except (TypeError, ValueError):
            skipped_no_id += 1
            continue
        moves.append({"object_id": oid, "x": geometry.x, "y": geometry.y})
    return moves, skipped_no_id


def to_service_crs(gdf, wkid):
    """The relocated points in the coordinate system the layer stores.

    applyEdits takes the geometry at face value. Sending analysis-CRS feet to a
    layer that stores degrees writes every leak into the sea, and the service
    accepts it.
    """
    if wkid is None:
        fail("The layer did not report a spatial reference, so there is no way "
             "to know what coordinate system to send geometry in. Refusing to "
             "guess.")
    target = f"EPSG:{wkid}"
    if gdf.crs is None:
        fail(f"The relocated layer has no CRS recorded, so it cannot be "
             f"converted to the service's {target}. Refusing to guess.")
    if gdf.crs.to_string() == target:
        return gdf
    log(f"Reprojecting relocated points from {gdf.crs.to_string()} to {target}")
    return gdf.to_crs(target)


def current_geometry(workflow, session, layer_url, object_ids, wkid,
                     batch_size=APPLY_BATCH_SIZE):
    """Where the service has these features right now.

    Re-read rather than taken from the cache the relocation used. The rollback
    has to restore what is actually there, and a leak that someone moved by hand
    since the download must not be quietly reverted to a stale position.
    """
    found = {}
    ids = list(object_ids)
    for start in range(0, len(ids), batch_size):
        chunk = ids[start:start + batch_size]
        params = {
            "f": "json",
            "objectIds": ",".join(str(value) for value in chunk),
            "outFields": "",
            "returnGeometry": "true",
            "outSR": wkid,
        }
        data = workflow.request_json_post(
            session, layer_url.rstrip("/") + "/query", params)
        if data.get("error"):
            fail(f"Could not read the current geometry of the leak features: "
                 f"{data['error'].get('message', data['error'])}")
        for feature in data.get("features") or []:
            geometry = feature.get("geometry") or {}
            attributes = feature.get("attributes") or {}
            oid = attributes.get("OBJECTID")
            if oid is None:
                # Some services answer objectIds queries without echoing the
                # OID in attributes when outFields is empty.
                continue
            if geometry.get("x") is None or geometry.get("y") is None:
                continue
            found[int(oid)] = (float(geometry["x"]), float(geometry["y"]))
    return found


def classify(moves, current, max_distance_ft, allow_long):
    """Split the proposed moves into what will be sent and what will not.

    Returns (ready, held) where every held entry says why. Nothing is filtered
    silently: a leak that is not going to be moved is reported with its reason,
    because "it did not apply" and "it was not in the file" look the same on
    the far side.
    """
    ready, held = [], []
    for move in moves:
        oid = move["object_id"]
        was = current.get(oid)
        if was is None:
            held.append({**move, "why": "not on the service any more"})
            continue
        moved = distance_ft(was, (move["x"], move["y"]))
        entry = {**move, "from_x": was[0], "from_y": was[1], "moves_by": moved}
        if moved <= SAME_POINT_FT:
            held.append({**entry, "why": "already at the relocated position"})
            continue
        if not allow_long and moved > max_distance_ft:
            held.append({
                **entry,
                "why": f"moves {moved:,.1f} units, beyond the "
                       f"{max_distance_ft:,.1f} limit"})
            continue
        ready.append(entry)
    return ready, held


def endpoint_only(url):
    """A layer URL with any query string removed.

    An ArcGIS URL can carry ?token=... and the rollback file is kept for weeks
    as an undo. A credential written into it outlives the session it came from,
    so the query is dropped before anything is stored or printed.
    """
    return str(url).split("?", 1)[0]


def write_rollback(path, layer_url, wkid, ready, extra=None):
    """The file that puts every edited feature back, written before any edit.

    An applyEdits payload rather than a report, so undoing is running it rather
    than reconstructing it under pressure. It carries the position read from the
    service moments earlier, not the one the relocation started from.
    """
    payload = {
        "written_utc": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "layer_url": endpoint_only(layer_url),
        "wkid": wkid,
        "feature_count": len(ready),
        "how_to_use": (
            "POST the 'updates' list below to {layer_url}/applyEdits with "
            "f=json and a valid token to put every feature back where it was. "
            "scripts/apply_relocated_geometry.py --rollback FILE does it."),
        "updates": [
            {
                "attributes": {"OBJECTID": move["object_id"]},
                "geometry": esri_point(move["from_x"], move["from_y"], wkid),
            }
            for move in ready
        ],
    }
    if extra:
        payload.update(extra)
    folder = _os.path.dirname(path)
    if folder:
        _os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    return path


def read_rollback(path):
    """A rollback file, checked enough to be worth applying."""
    if not _os.path.isfile(path):
        fail(f"No rollback file at {path}")
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    updates = payload.get("updates")
    if not isinstance(updates, list) or not updates:
        fail(f"{path} carries no updates to apply.")
    for update in updates:
        if "attributes" not in update or "geometry" not in update:
            fail(f"{path} is not a usable applyEdits payload: an entry has no "
                 f"attributes or no geometry.")
    return payload


def apply_updates(workflow, session, layer_url, updates, wkid,
                  batch_size=APPLY_BATCH_SIZE):
    """Send the updates, in batches, checking every feature's result.

    applyEdits answers per feature. A batch can come back HTTP 200 with half
    its features rejected, so the only honest way to report this is to count
    what the service said about each one.
    """
    succeeded, failed = [], []
    total = len(updates)
    batches = max(1, math.ceil(total / batch_size))
    for index, start in enumerate(range(0, total, batch_size), start=1):
        chunk = updates[start:start + batch_size]
        params = {
            "f": "json",
            "updates": json.dumps(chunk),
            # One batch is one transaction. A half-applied batch is the worst
            # outcome: some leaks moved, some not, and no clean way to say which.
            "rollbackOnFailure": "true",
        }
        try:
            data = workflow.request_json_post(
                session, layer_url.rstrip("/") + "/applyEdits", params)
        except Exception as ex:  # noqa: BLE001 - re-raised with what matters
            # request_json_post already turns an error response into an
            # exception, and it names the endpoint and the service's message.
            # What it cannot know is that this is the middle of a run: batches
            # before this one went through, and the thing someone needs on
            # screen at that moment is that the rollback file exists.
            fail(f"applyEdits batch {index:,}/{batches:,} was refused.\n"
                 f"  {ex}\n"
                 f"  Nothing in this batch was applied (rollbackOnFailure), "
                 f"and {len(succeeded):,} features in earlier batches were. "
                 f"The rollback file written before this run undoes them.")
        if data.get("error"):
            message = data["error"].get("message", data["error"])
            fail(f"applyEdits batch {index:,}/{batches:,} was refused: "
                 f"{message}\n"
                 f"  Nothing in this batch was applied (rollbackOnFailure), "
                 f"and {len(succeeded):,} features in earlier batches were. "
                 f"The rollback file written before this run undoes them.")
        results = data.get("updateResults") or []
        if len(results) != len(chunk):
            warn(f"Batch {index:,}/{batches:,}: sent {len(chunk):,} features "
                 f"and got {len(results):,} results back.")
        for result in results:
            if result.get("success"):
                succeeded.append(result.get("objectId"))
            else:
                error = result.get("error") or {}
                failed.append({
                    "object_id": result.get("objectId"),
                    "why": error.get("description") or error.get("code") or
                           "the service did not say",
                })
        log(f"applyEdits batch {index:,}/{batches:,}: "
            f"{len(succeeded):,} updated so far, {len(failed):,} refused")
    return succeeded, failed


def summarise(ready, held, succeeded=None, failed=None):
    """What happened, or what would happen."""
    by_reason = {}
    for entry in held:
        by_reason[entry["why"]] = by_reason.get(entry["why"], 0) + 1
    log("")
    log(f"Features to move : {len(ready):,}")
    if ready:
        distances = sorted(entry["moves_by"] for entry in ready)
        middle = distances[len(distances) // 2]
        log(f"  median move    : {middle:,.1f}")
        log(f"  largest move   : {distances[-1]:,.1f}")
    for reason, count in sorted(by_reason.items(), key=lambda row: -row[1]):
        log(f"Held back        : {count:,} - {reason}")
    if succeeded is not None:
        log("")
        log(f"Updated on the service : {len(succeeded):,}")
        if failed:
            warn(f"Refused by the service : {len(failed):,}")
            for entry in failed[:10]:
                warn(f"  OBJECTID {entry['object_id']}: {entry['why']}")
            if len(failed) > 10:
                warn(f"  ... and {len(failed) - 10:,} more")


CONFIRMATION = "MOVE THE LEAKS"


def confirmed(expected_count, typed=None):
    """Ask for the phrase to be typed before a production layer is edited.

    A flag is easy to add to a command line that was copied from somewhere. A
    phrase typed next to the number of features being changed is the last point
    at which someone can notice the number is wrong.
    """
    log("")
    warn(f"About to move {expected_count:,} leak features on the live service.")
    warn("This edits a production layer in place and cannot be undone from "
         "here. The rollback file is written first.")
    if typed is not None:
        return str(typed).strip() == CONFIRMATION
    try:
        answer = input(f'Type "{CONFIRMATION}" to go ahead: ')
    except EOFError:
        return False
    return answer.strip() == CONFIRMATION
