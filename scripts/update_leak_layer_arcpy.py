"""Copy the historic leak layer (DNV layer 206) and move its points to the
relocated positions from the relocation GeoPackage.

Meant to run on the ArcGIS VM with the ArcGIS Pro Python (arcpy), signed in to
the National Grid portal:

    "C:\\Program Files\\ArcGIS\\Pro\\bin\\Python\\scripts\\propy.bat" ^
        update_leak_layer_arcpy.py

    --gpkg SOURCE    the relocation GeoPackage: a SharePoint link (the default is
                     the Leak Relocation folder on the Data Analytics site) or a
                     local path
    --gdb PATH       file geodatabase to write the copy into (created if missing;
                     default Documents\\ArcGIS\\LeakRelocation on the VM)
    --name NAME      feature class name for the copy
    --service URL    layer to copy (default: DNV_Synergi_RiskResults_Assets_NY/206)
    --drop-unmatched keep only the relocated leaks in the copy

Getting the GeoPackage from SharePoint
--------------------------------------
The VM cannot download from SharePoint with a plain web request: the site needs
a Microsoft sign-in, which neither arcpy nor the portal token provides. So the
link is turned into the places Windows already exposes that file, tried in order:

  1. A OneDrive-synced copy. In SharePoint, open the Leak Relocation folder and
     choose Sync (or "Add shortcut to My files"); it then appears under
     C:\\Users\\<you>\\National Grid\\... or OneDrive - National Grid\\....
  2. The SharePoint WebDAV path (\\\\nationalgridplc.sharepoint.com@SSL\\...),
     which works when the WebClient service is running and you are signed in.

Whichever is found is copied to a local temp folder before it is read; the
script prints which file it used and when it was last modified, so a stale copy
is visible. If neither works, download the file and pass --gpkg <local path>.

What it does
------------
  1. Exports the service layer to a feature class in the geodatabase, keeping
     the GlobalIDs.
  2. Reads the `relocated_leaks` layer of the GeoPackage. Each row's OrigLeakOID
     is the OBJECTID the leak had in the service layer.
  3. Moves each matched leak in the copy to its relocated point. Leaks with no
     match keep their original location (unless --drop-unmatched).

The copy gets new OBJECTIDs, so rows are tied back to the source through the
leak's GlobalID: SRC_OID is filled from the service, then matched to OrigLeakOID.
Fields added to the copy: SRC_OID, RELOCATED (1 = moved), RELOC_DIST_FT.

The GeoPackage is read with sqlite3 rather than through arcpy, so this does not
depend on which GeoPackage support the installed Pro version has.
"""
import argparse
import datetime
import re
import shutil
import sqlite3
import struct
import tempfile
import urllib.parse
from pathlib import Path

import arcpy

DEFAULT_GPKG = (
    "https://nationalgridplc.sharepoint.com/sites/"
    "GRP-INT-US-Data-Analytics-and-Engineering-Controls-Data-Anal/"
    "Shared Documents/General/Leak Relocation/HistoricLeakRelocation.gpkg")
DEFAULT_GDB = (Path.home() / "Documents" / "ArcGIS" / "LeakRelocation"
               / "HistoricLeaks_Relocated.gdb")
DEFAULT_SERVICE = ("https://gis.nationalgrid.com/dnv/rest/services/NY/"
                   "DNV_Synergi_RiskResults_Assets_NY/MapServer/206")
EXPECTED_PORTAL = "https://gis.nationalgrid.com/portal"
RELOCATED_LAYER = "relocated_leaks"
SQLITE_HEADER = b"SQLite format 3\x00"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gpkg", default=DEFAULT_GPKG,
                        help="SharePoint link or local path to the GeoPackage.")
    parser.add_argument("--gdb", type=Path, default=DEFAULT_GDB)
    parser.add_argument("--name", default="HistoricLeaks_Relocated")
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--drop-unmatched", action="store_true",
                        help="Delete leaks that were not relocated from the copy.")
    # parse_known_args: a notebook or Pro's Python window puts its own
    # arguments in sys.argv, which are not this script's to reject.
    args, ignored = parser.parse_known_args(argv)
    if ignored:
        print(f"(ignoring arguments not meant for this script: {ignored})")
    return args


# Every message goes to print() as well as arcpy: arcpy.AddMessage/AddError are
# only shown when this runs as a geoprocessing tool, so in Pro's Python window,
# a notebook or propy.bat they would show nothing.

def say(message):
    arcpy.AddMessage(message)
    print(message, flush=True)


def warn(message):
    arcpy.AddWarning(message)
    print(f"WARNING: {message}", flush=True)


def die(message):
    """Stop with a message that is visible wherever the script runs.

    Printed before stopping, because Pro's Python window swallows the message
    of sys.exit("...") and would show nothing at all.
    """
    arcpy.AddError(message)
    print(f"ERROR: {message}", flush=True)
    raise SystemExit(1)


# --- Finding the GeoPackage --------------------------------------------------

def sharepoint_candidates(url):
    """Local paths where Windows may expose a SharePoint file.

    Accepts the plain file address or a sharing link of the /:u:/r/sites/...
    form; the query string (?d=...&e=...) is ignored.
    """
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path)
    path = re.sub(r"^/:\w:/r(?=/)", "", path)   # sharing-link prefix
    parts = [part for part in path.split("/") if part]
    # sites/<site>/<library>/<folders...>/<file>
    if len(parts) < 4 or parts[0].lower() not in ("sites", "teams"):
        die(f"Not a SharePoint file address this script understands: {url}")
    name, folders = parts[-1], parts[3:-1]

    candidates = []
    home = Path.home()
    # OneDrive sync of the library or one of its folders, or a "shortcut to My
    # files": the file sits under some suffix of its folder path, one or two
    # levels below the user's home (National Grid\<library>\..., OneDrive -
    # National Grid\<shortcut>\...).
    for start in range(len(folders) + 1):
        tail = "/".join(folders[start:] + [name])
        candidates += sorted(home.glob(f"*/{tail}"))
        candidates += sorted(home.glob(f"*/*/{tail}"))
    # WebDAV: needs the WebClient service and a SharePoint sign-in.
    candidates.append(Path(f"\\\\{parsed.netloc}@SSL\\DavWWWRoot", *parts))
    return candidates


def fetch_gpkg(source):
    """A local copy of the GeoPackage, wherever it lives."""
    if re.match(r"https?://", source, re.IGNORECASE):
        found = None
        for candidate in sharepoint_candidates(source):
            try:
                if candidate.is_file():
                    found = candidate
                    break
            except OSError:
                continue
        if not found:
            die("Could not reach the GeoPackage on SharePoint from this machine.\n"
                "  Either sync the Leak Relocation folder (SharePoint > Sync, or "
                "Add shortcut to My files) and run again, or download the file "
                "and pass --gpkg <local path>.")
    else:
        found = Path(source)
        if not found.is_file():
            die(f"GeoPackage not found: {found}")

    modified = datetime.datetime.fromtimestamp(found.stat().st_mtime)
    say(f"GeoPackage: {found}")
    say(f"  last modified {modified:%Y-%m-%d %H:%M}, "
        f"{found.stat().st_size / 1e6:,.1f} MB")

    # Read a local copy: SQLite over WebDAV or a OneDrive placeholder is slow at
    # best, and copying also makes OneDrive download a cloud-only file.
    local = Path(tempfile.gettempdir()) / "LeakRelocation" / found.name
    if found.resolve() != local.resolve():
        local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(found, local)
    with open(local, "rb") as handle:
        if handle.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
            die(f"{found} is not a GeoPackage (it may be a sign-in page saved "
                f"in its place). Download it again.")
    return local


# --- GeoPackage reading ------------------------------------------------------

def parse_gpkg_point(blob):
    """(x, y) from a GeoPackage point geometry blob."""
    if blob is None or blob[:2] != b"GP":
        raise ValueError("not a GeoPackage geometry")
    flags = blob[3]
    if flags & 0x10:                       # empty geometry
        raise ValueError("empty geometry")
    envelope_size = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get((flags >> 1) & 0x07)
    if envelope_size is None:
        raise ValueError("bad envelope flag")
    wkb = 8 + envelope_size
    wkb_order = "<" if blob[wkb] == 1 else ">"
    geom_type = struct.unpack_from(wkb_order + "I", blob, wkb + 1)[0] % 1000
    if geom_type != 1:
        raise ValueError(f"expected a point, got WKB type {geom_type}")
    return struct.unpack_from(wkb_order + "dd", blob, wkb + 5)


# GDAL WKT1 names -> Esri names, for the parameters a projected CRS uses.
GDAL_TO_ESRI_PARAMETERS = {
    "latitude_of_center": "Latitude_Of_Origin",
    "latitude_of_origin": "Latitude_Of_Origin",
    "longitude_of_center": "Central_Meridian",
    "central_meridian": "Central_Meridian",
    "standard_parallel_1": "Standard_Parallel_1",
    "standard_parallel_2": "Standard_Parallel_2",
    "false_easting": "False_Easting",
    "false_northing": "False_Northing",
    "scale_factor": "Scale_Factor",
}


def esri_wkt_from_gdal(wkt):
    """GDAL-style WKT1, as GDAL writes it into a GeoPackage, made Esri-style.

    ArcGIS rejects the GDAL form of the DNV equidistant conic as "The coordinate
    system description is invalid". Esri WKT passes through unchanged, so this
    is safe on a GeoPackage that has already been fixed.
    """
    wkt = re.sub(r',(?:AUTHORITY|AXIS)\[[^\[\]]*\]', "", wkt)
    for gdal, esri in GDAL_TO_ESRI_PARAMETERS.items():
        wkt = wkt.replace(f'PARAMETER["{gdal}"', f'PARAMETER["{esri}"')
    wkt = wkt.replace(
        'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84"',
        'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984"')
    wkt = re.sub(r'UNIT\["US survey foot",[^\]]*\]',
                 'UNIT["Foot_US",0.3048006096012192]', wkt)
    wkt = re.sub(r'UNIT\["metre",[^\]]*\]', 'UNIT["Meter",1.0]', wkt)
    return wkt


def gpkg_spatial_reference(organization, code, definition):
    """The layer's spatial reference, from the GeoPackage's own record of it.

    srs_id in a GeoPackage is only a local key; the organization code (EPSG) or
    the WKT definition is what identifies the coordinate system.
    """
    if organization and organization.upper() == "EPSG" and code and code > 0:
        return arcpy.SpatialReference(int(code))
    if not definition or definition.strip().lower() == "undefined":
        die("The GeoPackage does not say what coordinate system its points "
            "are in.")
    reference = arcpy.SpatialReference()
    try:
        reference.loadFromString(esri_wkt_from_gdal(definition))
    except Exception as ex:  # noqa: BLE001 - arcpy's error types vary by release
        die(f"ArcGIS could not read the GeoPackage's coordinate system ({ex}):\n"
            f"  {definition}")
    return reference


def read_relocated_points(gpkg):
    """{OrigLeakOID: (x, y, distance_ft)} and the layer's spatial reference."""
    connection = sqlite3.connect(f"file:{gpkg.as_posix()}?mode=ro", uri=True)
    try:
        layer = connection.execute(
            "SELECT g.column_name, s.organization, s.organization_coordsys_id, "
            "s.definition FROM gpkg_geometry_columns g "
            "JOIN gpkg_spatial_ref_sys s ON s.srs_id = g.srs_id "
            "WHERE g.table_name = ?", (RELOCATED_LAYER,)).fetchone()
        if not layer:
            die(f"No '{RELOCATED_LAYER}' layer in {gpkg}")
        geometry_column, organization, code, definition = layer
        rows = connection.execute(
            f'SELECT OrigLeakOID, DistanceFt, "{geometry_column}" '
            f'FROM "{RELOCATED_LAYER}"').fetchall()
    finally:
        connection.close()

    points = {}
    for oid, distance, blob in rows:
        x, y = parse_gpkg_point(blob)
        points[int(oid)] = (x, y, distance)
    return points, gpkg_spatial_reference(organization, code, definition)


# --- Layer copy and update ---------------------------------------------------

def find_field(fc, wanted):
    for field in arcpy.ListFields(fc):
        if field.name.lower() == wanted.lower():
            return field.name
    return None


def gid_key(value):
    """A GlobalID in one form, whichever form the cursor returned it in."""
    return str(value).strip().strip("{}").upper() if value else None


def source_globalid_to_oid(service):
    """{GlobalID: OBJECTID} straight from the service layer."""
    gid_field = find_field(service, "GlobalID")
    if not gid_field:
        die("The service layer has no GlobalID field, so the copy cannot be tied "
            "back to the source OBJECTIDs.")
    return {gid_key(gid): oid
            for oid, gid in arcpy.da.SearchCursor(service, ["OID@", gid_field])
            if gid}


def make_copy(service, gdb, name):
    if not arcpy.Exists(str(gdb)):
        say(f"Creating {gdb}")
        gdb.parent.mkdir(parents=True, exist_ok=True)
        arcpy.management.CreateFileGDB(str(gdb.parent), gdb.name)
    arcpy.env.overwriteOutput = True
    # Without this the copy gets new GlobalIDs, and nothing ties its rows back
    # to the service's OBJECTIDs.
    arcpy.env.preserveGlobalIds = True
    say(f"Copying {service}")
    arcpy.conversion.ExportFeatures(service, str(gdb / name))
    return str(gdb / name)


def same_reference(first, second):
    if first.factoryCode and second.factoryCode:
        return first.factoryCode == second.factoryCode
    return first.name == second.name


def check_service_access(service):
    """Stop early, and say why, if the leak layer cannot be read.

    Being signed in to *a* portal is not the test: the DNV service has to accept
    the sign-in. So this reports the portal and sign-in state, then asks arcpy
    to describe the layer itself.
    """
    say("Checking access to the leak layer")
    try:
        portal = arcpy.GetActivePortalURL()
    except Exception:  # noqa: BLE001 - no active portal raises on some releases
        portal = None
    try:
        signed_in = bool(arcpy.GetSigninToken())
    except Exception:  # noqa: BLE001
        signed_in = False
    say(f"  active portal: {portal or 'none'}")
    say(f"  signed in to it: {'yes' if signed_in else 'no'}")
    if not (portal or "").lower().startswith(EXPECTED_PORTAL.lower()):
        warn(f"The active portal is not {EXPECTED_PORTAL}, which is the one "
             f"the DNV service accepts.")

    try:
        shape_type = arcpy.Describe(service).shapeType
    except Exception as ex:  # noqa: BLE001 - arcpy raises OSError or RuntimeError
        die(f"Cannot read {service}\n"
            f"  ({ex})\n"
            f"  In ArcGIS Pro, open Project > Portals, sign in to "
            f"{EXPECTED_PORTAL} and make it the active portal, then run again.")
    if shape_type != "Point":
        die(f"{service} is a {shape_type} layer, not points.")
    say("  the leak layer is readable")


def main(argv=None):
    args = parse_args(argv)

    check_service_access(args.service)

    gpkg = fetch_gpkg(args.gpkg)
    points, source_sr = read_relocated_points(gpkg)
    say(f"{len(points):,} relocated leaks (coordinate system: {source_sr.name})")
    if not points:
        die("The GeoPackage has no relocated leaks, so there is nothing to apply.")

    say("Reading source GlobalIDs and OBJECTIDs from the service")
    gid_to_oid = source_globalid_to_oid(args.service)
    source_oids = set(gid_to_oid.values())
    unknown = [oid for oid in points if oid not in source_oids]
    if unknown:
        die(f"{len(unknown):,} OrigLeakOID values are not OBJECTIDs in the "
            f"service layer (for example {sorted(unknown)[:5]}). The GeoPackage "
            f"may have been made from a different version of the layer; re-run "
            f"the workflow and try again.")

    fc = make_copy(args.service, args.gdb, args.name)
    copied = int(arcpy.management.GetCount(fc)[0])
    if copied != len(gid_to_oid):
        warn(f"The copy has {copied:,} features; the service reported "
             f"{len(gid_to_oid):,} with GlobalIDs.")

    desc = arcpy.Describe(fc)
    if desc.shapeType != "Point":
        die(f"The copy is {desc.shapeType}, not Point; refusing to move points "
            f"into it.")
    copy_gid = find_field(fc, "GlobalID")
    if not copy_gid:
        die(f"The copy at {fc} did not keep the GlobalID field, so its rows "
            f"cannot be tied back to the source. No points were moved.")

    arcpy.management.AddField(fc, "SRC_OID", "LONG")
    arcpy.management.AddField(fc, "RELOCATED", "SHORT")
    arcpy.management.AddField(fc, "RELOC_DIST_FT", "DOUBLE")

    tied = 0
    with arcpy.da.UpdateCursor(fc, [copy_gid, "SRC_OID", "RELOCATED"]) as cursor:
        for gid, _, _ in cursor:
            src_oid = gid_to_oid.get(gid_key(gid))
            tied += src_oid is not None
            cursor.updateRow([gid, src_oid, 0])
    if not tied:
        die(f"None of the copy's GlobalIDs match the service's, so the "
            f"GlobalIDs were not preserved in the export. No points were moved "
            f"in {fc}.")
    if tied < copied:
        warn(f"{copied - tied:,} rows of the copy could not be tied back to a "
             f"source OBJECTID; they keep their original location.")

    # Points arrive in the GeoPackage's coordinate system; the copy keeps the
    # service's. Project when they differ, with a datum transformation when the
    # two are on different datums.
    target_sr = desc.spatialReference
    reproject = not same_reference(source_sr, target_sr)
    transformation = None
    if reproject:
        if not same_reference(source_sr.GCS, target_sr.GCS):
            options = arcpy.ListTransformations(source_sr, target_sr)
            transformation = options[0] if options else None
        say(f"Projecting points from {source_sr.name} to {target_sr.name}"
            f"{f' using {transformation}' if transformation else ''}")

    moved = 0
    missing = set(points)
    with arcpy.da.UpdateCursor(
            fc, ["SRC_OID", "SHAPE@", "RELOCATED", "RELOC_DIST_FT"]) as cursor:
        for src_oid, _, _, _ in cursor:
            if src_oid in points:
                x, y, distance = points[src_oid]
                geometry = arcpy.PointGeometry(arcpy.Point(x, y), source_sr)
                if reproject:
                    geometry = (geometry.projectAs(target_sr, transformation)
                                if transformation
                                else geometry.projectAs(target_sr))
                cursor.updateRow([src_oid, geometry, 1, distance])
                moved += 1
                missing.discard(src_oid)
            elif args.drop_unmatched:
                cursor.deleteRow()

    say(f"Moved {moved:,} of {len(points):,} relocated leaks in {fc}")
    if missing:
        warn(f"{len(missing):,} relocated leaks were not found in the copy "
             f"(for example {sorted(missing)[:5]}).")
    final = int(arcpy.management.GetCount(fc)[0])
    say(f"Copy now holds {final:,} features"
        f"{' (unmatched dropped)' if args.drop_unmatched else ''}.")
    return 0


if __name__ == "__main__":
    # No sys.exit(main()): in Pro's Python window a successful run would end in
    # a SystemExit. Errors still exit with status 1 through die().
    main()
