"""Read enterprise (Maximo) leaks from a SQL database.

The historic leaks come from ArcGIS layer 206 and get their material and
diameter from a committed CSV, because layer 206 carries neither. The Maximo
leaks come from a SQL query that returns everything at once, so there is no
supplemental join here at all - and so none of the wrong-attribute risk that
join carried.

What this module is responsible for is the boundary: connect, run one query,
check that what came back is usable, and hand the rest of the project a
GeoDataFrame plus a mapping of leak key to its attributes. Those are the same
two things `prepare_leaks` already takes from the historic path, which is why
the matching, the relocation and the outputs are shared rather than copied.

Three rules it keeps.

**The connection string never reaches the log.** It carries credentials. It is
read from the environment, never echoed, and `safe_dsn` is what any message
uses - the same discipline the GIS token already has, where only its length and
expiry are ever printed.

**The query lives in a file, not in here.** It is the part that will change, it
is the part a DBA should be able to read and edit, and it does not belong in
Python. See `config.MAXIMO_QUERY_FILE`.

**The columns are a contract.** A query that returns the wrong names fails here,
by name, rather than producing a frame that relocates nothing and looks like a
data problem.

Any DB-API 2.0 connection works - pyodbc for SQL Server, oracledb for Oracle -
because nothing below uses more than `cursor()`, `execute()`, `description` and
`fetchmany()`. That also makes it testable without a database.
"""
# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member. See leakrelocation/output.py for the full note.
import os as _os
import sys as _sys

_PACKAGE_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PACKAGE_PARENT not in _sys.path:
    _sys.path.insert(0, _PACKAGE_PARENT)

import math
import re

from leakrelocation import config
from leakrelocation.matching import clean, normalize_key, parse_number
from leakrelocation.output import fail, log, step, warn

# What the query has to return, by alias. The names are the contract between
# the .sql file and this project, so a DBA can change every table and join in
# the query and nothing here needs to know.
#
# Required, because a leak without them cannot be matched or placed:
LEAK_ID = "LeakId"            # unique per leak. The relocation is keyed on it.
LEAK_X = "X"                  # easting / longitude, in MAXIMO_LEAK_CRS
LEAK_Y = "Y"                  # northing / latitude, in MAXIMO_LEAK_CRS
LEAK_MATERIAL = "Material"    # the leaking pipe's material
LEAK_DIAMETER = "Diameter"    # its nominal diameter, inches

REQUIRED_COLUMNS = (LEAK_ID, LEAK_X, LEAK_Y, LEAK_MATERIAL, LEAK_DIAMETER)

# Optional. Each one that is missing costs something specific, named in
# `describe` so a run says what it is working without rather than quietly
# producing a lesser answer.
LEAK_NUMBER = "LeakNumber"    # what a person calls the leak. Falls back to LeakId.
LEAK_FACILITY = "FacilityType"  # decides which pipe layers it may relocate onto
LEAK_PRESSURE = "Pressure"
LEAK_DATE = "LeakDate"        # the date the relocation window is measured from
LEAK_ADDRESS = "Address"
LEAK_CITY = "City"
LEAK_CROSS_STREET = "NearestXStreet"

OPTIONAL_COLUMNS = (LEAK_NUMBER, LEAK_FACILITY, LEAK_PRESSURE, LEAK_DATE,
                    LEAK_ADDRESS, LEAK_CITY, LEAK_CROSS_STREET)

WHAT_IS_LOST = {
    LEAK_NUMBER: "leaks are labelled by their id instead of a leak number",
    LEAK_FACILITY: "every leak is searched against both mains and services, "
                   "because nothing says which it belongs to",
    LEAK_PRESSURE: "no pressure comparison (advisory unless "
                   "REQUIRE_PRESSURE_MATCH is set, so usually no loss)",
    LEAK_DATE: "no leak can be checked against the pipe's life, so the date "
               "rule decides nothing",
    LEAK_ADDRESS: "the map popup and the audit have no street address",
    LEAK_CITY: "no municipality to fall back on when the address is empty",
    LEAK_CROSS_STREET: "no cross street to fall back on when the address is empty",
}

# How many rows to pull from the cursor at a time. Large enough that the round
# trips do not dominate, small enough that a query returning far more than
# expected does not fill memory before anything notices.
FETCH_SIZE = 5000

# Anything that looks like a secret inside a connection string. Used only to
# redact; the value is never needed.
_SECRET_KEYS = ("pwd", "password", "passwd", "secret", "token", "apikey",
                "api_key", "accesstoken")
_SECRET_PATTERN = re.compile(
    r"(?i)\b(" + "|".join(_SECRET_KEYS) + r")\s*=\s*([^;]*)")


def safe_dsn(dsn):
    """A connection string with its password removed, for a log line.

    Never log the raw value. A DSN is a credential, this transcript and the
    run log both persist, and "it was only in the error message" is how one
    ends up in a ticket.
    """
    if not dsn:
        return "(not set)"
    return _SECRET_PATTERN.sub(lambda m: f"{m.group(1)}=***", str(dsn))


def connection_string():
    """The DSN from the environment, or a fatal message saying how to set it."""
    dsn = _os.environ.get(config.MAXIMO_DSN_ENV, "").strip()
    if not dsn:
        fail(
            f"No Maximo database connection string.\n"
            f"  Set {config.MAXIMO_DSN_ENV} to an ODBC connection string, for "
            f"example:\n"
            f"    set {config.MAXIMO_DSN_ENV}=Driver={{ODBC Driver 18 for SQL "
            f"Server}};Server=HOST;Database=DB;Trusted_Connection=yes\n"
            f"  It is read from the environment and never written to a file or "
            f"a log, because it carries credentials.")
    return dsn


def connect(dsn=None):
    """A DB-API connection to the Maximo database.

    pyodbc is imported here rather than at module import so that everything
    else - the column contract, the row conversion, the tests - works on a
    machine that has no ODBC driver installed.
    """
    dsn = dsn or connection_string()
    try:
        import pyodbc
    except ImportError:
        fail(
            "pyodbc is not installed, so there is no way to reach the Maximo "
            "database.\n"
            "  python -m pip install pyodbc\n"
            "  It also needs an ODBC driver: 'ODBC Driver 18 for SQL Server'.\n"
            "  Any DB-API connection works here, so another driver can be "
            "passed to read_leaks(connection=...) instead.")
    log(f"Connecting to the Maximo database: {safe_dsn(dsn)}")
    try:
        return pyodbc.connect(dsn)
    except Exception as ex:  # noqa: BLE001 - see below
        # pyodbc raises its own error classes and the useful part is the
        # driver's message. Re-raised as one fatal line that cannot carry the
        # password: str(ex) from a failed connect does not include the DSN, but
        # it is redacted anyway rather than trusted not to.
        fail(f"Could not connect to the Maximo database "
             f"({safe_dsn(dsn)}): {safe_dsn(ex)}")


def read_query(path=None):
    """The SQL to run, from its file."""
    path = path or config.MAXIMO_QUERY_FILE
    if not _os.path.isfile(path):
        fail(f"No Maximo query at {path}\n"
             f"  The query lives in a file so it can be edited without "
             f"touching the code. Start from the committed template and point "
             f"{config.MAXIMO_QUERY_ENV} at your copy.")
    with open(path, encoding="utf-8-sig") as handle:
        sql = handle.read()
    if not sql.strip():
        fail(f"The Maximo query at {path} is empty.")
    return sql


def column_names(cursor):
    """The result's column names, in order."""
    return [str(column[0]) for column in (cursor.description or [])]


def resolve_columns(found):
    """Map each contract name to this result's spelling of it.

    Matched without case, because SQL Server hands back whatever the query
    aliased and `leakid`, `LeakID` and `LeakId` are the same column.
    """
    lowered = {}
    for name in found:
        lowered.setdefault(str(name).strip().lower(), name)
    resolved = {}
    for wanted in REQUIRED_COLUMNS + OPTIONAL_COLUMNS:
        match = lowered.get(wanted.lower())
        if match:
            resolved[wanted] = match
    return resolved


def check_columns(found):
    """Fail unless the result carries everything a relocation needs.

    Named here rather than discovered three stages later as an empty output.
    """
    resolved = resolve_columns(found)
    missing = [name for name in REQUIRED_COLUMNS if name not in resolved]
    if missing:
        fail(
            f"The Maximo query is missing {', '.join(missing)}.\n"
            f"  It returned: {', '.join(str(name) for name in found) or '(nothing)'}\n"
            f"  Every relocation needs {', '.join(REQUIRED_COLUMNS)}. Alias the "
            f"columns in the query to those names - the table and column names "
            f"underneath can be anything.")
    return resolved


def describe(resolved):
    """Say what the query gave and what each absence costs."""
    have = [name for name in OPTIONAL_COLUMNS if name in resolved]
    missing = [name for name in OPTIONAL_COLUMNS if name not in resolved]
    log(f"Required columns: all {len(REQUIRED_COLUMNS)} present.")
    if have:
        log(f"Optional columns present: {', '.join(have)}")
    for name in missing:
        warn(f"No {name} column. Without it, {WHAT_IS_LOST[name]}.")


def coordinate(value):
    """A coordinate as a float, or None when it is not one.

    A leak with no usable position cannot be placed and is counted rather than
    dropped at 0, 0 - which is in the Gulf of Guinea and would relocate against
    nothing while still being reported as a leak that was read.
    """
    number = parse_number(value)
    if number is None or math.isnan(number) or math.isinf(number):
        return None
    return float(number)


def rows_from(cursor, resolved, fetch_size=FETCH_SIZE):
    """Yield each result row as a dict keyed by the contract names."""
    names = column_names(cursor)
    index = {name: names.index(column) for name, column in resolved.items()}
    while True:
        batch = cursor.fetchmany(fetch_size)
        if not batch:
            return
        for row in batch:
            yield {name: row[position] for name, position in index.items()}


def leak_records(rows):
    """Split the query's rows into what the pipeline needs.

    Returns (records, attributes, counters):

      records     one dict per usable leak, with its id, label, geometry
                  coordinates and the columns that say where it is;
      attributes  leak key -> material, diameter, pressure, facility. The same
                  shape `load_supplemental` builds for the historic leaks, so
                  `prepare_leaks` cannot tell the two sources apart;
      counters    what was skipped and why.

    A row is skipped when it has no id, no usable coordinates, or neither a
    material nor a diameter - all three make it unmatchable, and counting them
    is how a query returning 40,000 rows and relocating 300 explains itself.
    """
    records = []
    attributes = {}
    counters = {"rows_read": 0, "no_id": 0, "duplicate_id": 0,
                "no_coordinates": 0, "no_match_attributes": 0, "usable": 0}

    for row in rows:
        counters["rows_read"] += 1
        key = normalize_key(row.get(LEAK_ID))
        if not key:
            counters["no_id"] += 1
            continue
        if key in attributes:
            # Two rows for one leak would relocate it twice and double-count it
            # in every figure. The first wins and the rest are reported, which
            # is what the historic path learned to do the hard way: joining on
            # a non-unique key silently took an arbitrary row's attributes.
            counters["duplicate_id"] += 1
            continue

        x = coordinate(row.get(LEAK_X))
        y = coordinate(row.get(LEAK_Y))
        if x is None or y is None:
            counters["no_coordinates"] += 1
            continue

        material = clean(row.get(LEAK_MATERIAL))
        diameter = parse_number(row.get(LEAK_DIAMETER))
        if not material or diameter is None:
            counters["no_match_attributes"] += 1
            continue

        attributes[key] = {
            "material": material,
            "diameter": diameter,
            "pressure": clean(row.get(LEAK_PRESSURE)),
            "facility": clean(row.get(LEAK_FACILITY)),
        }
        records.append({
            # The names the workflow's own resolvers look for, so the frame
            # built from these needs no special handling downstream.
            "OBJECTID": len(records) + 1,
            "MAXIMOLEAKID": key,
            "LEAKNUMBER": clean(row.get(LEAK_NUMBER)) or key,
            "ADDRESS": clean(row.get(LEAK_ADDRESS)),
            "CITY": clean(row.get(LEAK_CITY)),
            "NEARESTXSTREET": clean(row.get(LEAK_CROSS_STREET)),
            "MAXIMOLEAKDATE": row.get(LEAK_DATE),
            "_x": x,
            "_y": y,
        })
        counters["usable"] += 1

    return records, attributes, counters


def leaks_frame(records, crs=None):
    """The usable leaks as a GeoDataFrame, in the CRS their coordinates are in.

    The CRS is configuration, not a guess: reading a projected easting as a
    longitude puts every leak in the wrong hemisphere, and the relocation would
    still run and still produce a GeoPackage.
    """
    import geopandas as gpd
    import pandas as pd

    crs = crs or config.MAXIMO_LEAK_CRS
    frame = pd.DataFrame(records)
    if not len(frame):
        return gpd.GeoDataFrame(
            frame, geometry=gpd.points_from_xy([], []), crs=crs)
    gdf = gpd.GeoDataFrame(
        frame.drop(columns=["_x", "_y"]),
        geometry=gpd.points_from_xy(frame["_x"], frame["_y"]),
        crs=crs,
    )
    return gdf


def report(counters):
    """What the query returned, and what was unusable."""
    log(f"Rows returned by the query: {counters['rows_read']:,}")
    log(f"Usable leaks: {counters['usable']:,}")
    for name, message in (
        ("no_id", "no leak id"),
        ("duplicate_id", "a leak id already seen"),
        ("no_coordinates", "no usable X/Y"),
        ("no_match_attributes", "no material or no diameter"),
    ):
        if counters[name]:
            warn(f"Skipped {counters[name]:,} rows: {message}.")


def read_leaks(connection=None, query=None, crs=None, limit=None):
    """Run the query and return (leaks GeoDataFrame, attributes, counters).

    `connection` takes any DB-API connection, which is what makes this testable
    without a database and usable against a driver other than pyodbc.
    """
    step("Reading enterprise (Maximo) leaks from SQL")
    sql = query if query is not None else read_query()
    owned = connection is None
    connection = connection or connect()
    try:
        cursor = connection.cursor()
        try:
            log(f"Query: {config.MAXIMO_QUERY_FILE}"
                if query is None else "Query: supplied by the caller")
            cursor.execute(sql)
            found = column_names(cursor)
            resolved = check_columns(found)
            describe(resolved)
            rows = rows_from(cursor, resolved)
            if limit:
                rows = (row for index, row in enumerate(rows) if index < limit)
            records, attributes, counters = leak_records(rows)
        finally:
            try:
                cursor.close()
            except Exception:  # noqa: BLE001 - a close that fails is not news
                # Nothing downstream depends on the cursor closing cleanly, and
                # raising here would hide whatever the query already produced.
                pass
    finally:
        if owned:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - as above
                pass

    report(counters)
    frame = leaks_frame(records, crs)
    log(f"Leak CRS: {frame.crs}")
    if len(frame):
        log(f"Leak bounds: {list(frame.total_bounds)}")
    return frame, attributes, counters
