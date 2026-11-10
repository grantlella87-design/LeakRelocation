"""Move the leaks on the source layer to where the relocation put them.

    python scripts/apply_relocated_geometry.py                  dry run
    python scripts/apply_relocated_geometry.py --apply          do it
    python scripts/apply_relocated_geometry.py --rollback FILE  put them back

Every other part of this project is additive - it downloads, computes, and
writes its own files. This one reaches back and edits layer 206's geometry in
place, for leaks that are in production asset records. There is no undo on the
far side, so:

  * it is a dry run unless --apply is given, and --apply still asks for a
    phrase to be typed;
  * a rollback file is written before the first edit, and the run stops if it
    cannot be written;
  * a leak that moved further than --max-distance is held back unless
    --allow-long-moves says otherwise. The search is capped at 100 ft now, so
    this only bites on an output written when it reached further;
  * the current geometry is re-read from the service first, so the rollback
    restores what is there now and a feature someone has already moved by hand
    is reported rather than overwritten.

Start with a dry run and read what it says it would do.
"""
import argparse
import sys

from _bootstrap import config

from leakrelocation import apply_geometry as ag
from leakrelocation import arcgis, auth, schema
from leakrelocation.output import fail, log, step, warn



def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gpkg", default="",
                        help="The relocation output to read. Default: the "
                             "historic GeoPackage for the current diameter "
                             "rule.")
    parser.add_argument("--layer-url", default=config.HIST_LEAK_URL,
                        help="The layer whose geometry is updated. "
                             "Default: %(default)s")
    parser.add_argument("--apply", action="store_true",
                        help="Actually send the edits. Without this the run "
                             "reports what it would do and changes nothing.")
    parser.add_argument("--yes", default=None,
                        help=f'Supply the confirmation phrase instead of '
                             f'typing it: --yes "{ag.CONFIRMATION}". For a '
                             f'scheduled run that has already been reviewed.')
    parser.add_argument("--max-distance", type=float,
                        default=ag.MAX_APPLY_DISTANCE_FT,
                        help="Hold back any leak that moves further than this, "
                             "in the layer's own units. Default: %(default)s")
    parser.add_argument("--allow-long-moves", action="store_true",
                        help="Apply the long moves too. Read the dry run first.")
    parser.add_argument("--max-features", type=int, default=ag.MAX_APPLY_FEATURES,
                        help="Refuse to move more than this many at once. "
                             "Default: %(default)s")
    parser.add_argument("--rollback", default="",
                        help="Apply a rollback file written by an earlier run, "
                             "putting those features back.")
    parser.add_argument("--rollback-dir", default="",
                        help="Where to write the rollback file. Default: "
                             "beside the GeoPackage.")
    return parser.parse_args(argv)


def read_relocated(path):
    from pathlib import Path

    import geopandas as gpd
    if not Path(path).is_file():
        fail(f"No GeoPackage at {path}\n"
             f"  Run the relocation first: python run.py")
    try:
        gdf = gpd.read_file(str(path), layer=schema.RELOCATED_LEAKS_LAYER)
    except Exception as ex:  # noqa: BLE001 - engine-dependent, all mean the same
        fail(f"Could not read the {schema.RELOCATED_LEAKS_LAYER} layer from {path}: {ex}")
    if not len(gdf):
        fail(f"{path} has no relocated leaks, so there is nothing to move.")
    return gdf


def do_rollback(args, session):
    payload = ag.read_rollback(args.rollback)
    updates = payload["updates"]
    layer_url = payload.get("layer_url") or args.layer_url
    step("Rolling back a geometry update")
    log(f"File        : {args.rollback}")
    log(f"Written     : {payload.get('written_utc', 'unknown')}")
    log(f"Layer       : {layer_url}")
    log(f"Features    : {len(updates):,}")
    if not args.apply:
        log("")
        log("Dry run. Nothing was sent. Add --apply to put these features back.")
        return 0
    if not ag.confirmed(len(updates), args.yes):
        fail("Not confirmed. Nothing was sent.")
    # The undo is the same kind of edit as the run it reverses: geometry only.
    ag.check_geometry_only(
        updates, payload.get("object_id_field") or "OBJECTID")
    succeeded, failed = ag.apply_updates(
        session, layer_url, updates, payload.get("wkid"))
    ag.summarise([], [], succeeded, failed)
    return 1 if failed else 0


def main(argv=None):
    args = parse_args(argv)
    session = auth.make_session()

    if args.rollback:
        return do_rollback(args, session)

    gpkg = args.gpkg or str(config.output_gpkg_for(config.DIAMETER_MATCH_MODE))
    step("Updating the leak layer's geometry from the relocation")
    log(f"Relocation output : {gpkg}")
    log(f"Target layer      : {args.layer_url}")
    log(f"Mode              : {'APPLY' if args.apply else 'dry run'}")

    relocated = read_relocated(gpkg)
    log(f"Relocated leaks in the output: {len(relocated):,}")

    meta = arcgis.layer_metadata(session, args.layer_url.rstrip("/"))
    wkid = meta["wkid"]
    # Read off the layer, not assumed. A layer is free to call its object id
    # something other than OBJECTID, and sending the wrong key identifies no
    # row at all.
    object_id_field = meta["object_id_field"] or "OBJECTID"
    globalid_field = ag.globalid_field_of(meta)
    log(f"Layer spatial reference: wkid {wkid}")
    log(f"Object id field        : {object_id_field}")
    log(f"GlobalID field         : {globalid_field or 'none on this layer'}")
    log("This updates geometry only. Every feature keeps its GlobalID: an "
        "update is not a delete and an add, and nothing but the object id "
        "goes in the payload.")

    relocated = ag.to_service_crs(relocated, wkid)
    moves, skipped = ag.relocations_from(relocated)
    if skipped:
        warn(f"{skipped:,} relocated points carry no usable OBJECTID or "
             f"geometry and cannot be matched to a feature.")
    if not moves:
        fail("No relocated point could be paired with a feature on the layer.")

    step("Reading the current geometry and GlobalIDs from the service")
    current = ag.current_state(
        session, args.layer_url, [m["object_id"] for m in moves],
        wkid, object_id_field=object_id_field, globalid_field=globalid_field)
    log(f"Features found on the service: {len(current):,} of {len(moves):,}")
    with_ids = sum(1 for entry in current.values() if entry["globalid"])
    if globalid_field:
        log(f"GlobalIDs read for {with_ids:,} of them, to be checked again "
            f"after the edit.")

    ready, held = ag.classify(
        moves, current, args.max_distance, args.allow_long_moves)

    drifted = sum(1 for entry in held
                  if entry["why"] == "already at the relocated position")
    missing = sum(1 for entry in held
                  if entry["why"] == "not on the service any more")
    if missing and missing / max(1, len(moves)) > ag.MAX_DRIFTED_FRACTION:
        fail(f"{missing:,} of {len(moves):,} relocated leaks are not on the "
             f"layer any more. The output no longer describes this layer, so "
             f"these edits are computed against something else. Re-run the "
             f"relocation before applying anything: python run.py --refresh")

    ag.summarise(ready, held)
    if drifted:
        log(f"({drifted:,} are already where the relocation would put them - "
            f"an earlier run, most likely.)")

    if not ready:
        log("")
        log("Nothing to move.")
        return 0

    if len(ready) > args.max_features:
        fail(f"{len(ready):,} features would be moved, over the "
             f"{args.max_features:,} limit.\n"
             f"  That limit exists to catch a bad run, not to stop a big one. "
             f"If this number is right, raise it: --max-features {len(ready)}")

    if not args.apply:
        log("")
        log("Dry run. Nothing was sent and nothing changed.")
        log("  Read the numbers above, then: "
            "python scripts/apply_relocated_geometry.py --apply")
        return 0

    # Before the first edit, not after it.
    step("Writing the rollback file")
    path = ag.rollback_path(args.rollback_dir or None)
    try:
        ag.write_rollback(path, args.layer_url, wkid, ready,
                          extra={"source_gpkg": gpkg},
                          object_id_field=object_id_field)
    except OSError as ex:
        fail(f"Could not write the rollback file to {path}: {ex}\n"
             f"  Nothing has been sent. The rollback is written before any "
             f"edit precisely so that a run cannot start without one.")
    log(f"Rollback file: {path}")
    log(f"  Undo this run with: python scripts/apply_relocated_geometry.py "
        f"--rollback {path} --apply")

    if not ag.confirmed(len(ready), args.yes):
        fail("Not confirmed. Nothing was sent.")

    step("Applying")
    updates = ag.check_geometry_only(
        [ag.geometry_only(object_id_field, move["object_id"],
                          move["x"], move["y"], wkid)
         for move in ready],
        object_id_field)
    succeeded, failed = ag.apply_updates(
        session, args.layer_url, updates, wkid)
    ag.summarise(ready, held, succeeded, failed)

    step("Checking the GlobalIDs survived")
    applied = {move["object_id"] for move in ready} & set(succeeded or [])
    changed = ag.verify_globalids(
        session, args.layer_url,
        [move for move in ready if move["object_id"] in applied], wkid,
        object_id_field=object_id_field, globalid_field=globalid_field)

    log("")
    log(f"Rollback file: {path}")
    return 1 if (failed or changed) else 0


if __name__ == "__main__":
    sys.exit(main())
