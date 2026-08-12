"""Single source of truth for every path, URL and tuning knob.

Before this module existed the production UNC share was hard-coded in 22 of
the repository's 30 files and the local working root in 15, so relocating
either meant a find-and-replace across the whole tree.

Nothing reads the share any more. The supplemental leak data is committed under
input/, so a clone plus a token is the whole of what a run needs: no mapped
drive, no VPN, no shared folder that has to be found first. Everything that is
written goes under the local work root.

Every value can be overridden with an environment variable, which is what makes
the workflow runnable off one person's workstation, and testable on a machine
with no GIS portal at all.

    LEAKRELOCATION_WORK_ROOT           local scratch/cache root
    LEAKRELOCATION_CACHE_DIR           layer cache (defaults under work root)
    LEAKRELOCATION_OUTPUT_GPKG         production GeoPackage
    LEAKRELOCATION_SUPPLEMENTAL_CSV    supplemental leak CSV (defaults to input/)
    LEAKRELOCATION_GIS_ROOT            ArcGIS server root
    LEAKRELOCATION_PORTAL_ROOT         ArcGIS portal root
"""
import os
from pathlib import Path

# --- Locations ---------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_WORK_ROOT = Path.home() / "Downloads" / "LeakRelocation-GeoPandas"


def _path_from_env(name, default):
    value = os.environ.get(name, "").strip()
    return Path(value) if value else default


WORK_ROOT = _path_from_env("LEAKRELOCATION_WORK_ROOT", DEFAULT_WORK_ROOT)

LAYER_CACHE_DIR = _path_from_env("LEAKRELOCATION_CACHE_DIR", WORK_ROOT / "layer_cache")
OUTPUT_DIR = WORK_ROOT / "production_moved_leak_outputs"
ENRICHMENT_DIR = WORK_ROOT / "assettype_cache_enrichment"
# No VIEWER_DIR any more: the static viewer that wrote GeoJSON into a folder is
# gone, and the map is served from the caches instead.

# The GeoPackage is written locally, under the work root. Writing it to a network
# location made every run depend on network write throughput, and a partial write
# left the shared copy broken. Point LEAKRELOCATION_OUTPUT_GPKG somewhere else to
# publish deliberately.
OUTPUT_GPKG = _path_from_env(
    "LEAKRELOCATION_OUTPUT_GPKG", OUTPUT_DIR / "HistoricLeakRelocation.gpkg")

# Inputs that travel with the repository. The supplemental leak data used to be
# read off a shared network folder, which made a run depend on being on the
# network, on the folder not having been reorganised, and on whichever copy of
# the file happened to be there. It is committed here instead, so a checkout is
# reproducible on its own.
INPUT_DIR = REPO_ROOT / "input"

SUPPLEMENTAL_CSV = _path_from_env(
    "LEAKRELOCATION_SUPPLEMENTAL_CSV", INPUT_DIR / "HL_SupplementalData.csv")

# The workflow module in this repository.
WORKFLOW_SCRIPT = Path(__file__).resolve().parent.parent / "leak_relocation_geopandas.py"

# Committed point-in-time copy of the DNV service metadata. It carries the
# ASSETTYPE subtype domains, which is what makes a material decode possible
# without a token - see assettype.decoder_for_layer.
REFERENCE_DIR = (REPO_ROOT / "reference" / "mapserver_json"
                 / "NY_DNV_Synergi_RiskResults_Assets_NY")

# Leaflet, committed so the map works with no internet and nothing to build
# first. scripts/build_leaflet_context.py used to download it into the work root;
# without a copy in the repo the page falls back to a CDN, and a network that
# blocks unpkg.com gives a blank map.
VENDORED_LEAFLET_DIR = REPO_ROOT / "vendor" / "leaflet"

# --- Services ----------------------------------------------------------------

GIS_ROOT = os.environ.get("LEAKRELOCATION_GIS_ROOT", "https://gis.nationalgrid.com").rstrip("/")
PORTAL_ROOT = os.environ.get("LEAKRELOCATION_PORTAL_ROOT", GIS_ROOT + "/portal").rstrip("/")

_MAP_SERVER = GIS_ROOT + "/dnv/rest/services/NY/DNV_Synergi_RiskResults_Assets_NY/MapServer"

HISTORIC_LEAK_LAYER_ID = 206
DISTRIBUTION_PIPE_LAYER_ID = 6
SERVICE_PIPE_LAYER_ID = 7

HIST_LEAK_URL = f"{_MAP_SERVER}/{HISTORIC_LEAK_LAYER_ID}"
DISTRIBUTION_PIPE_URL = f"{_MAP_SERVER}/{DISTRIBUTION_PIPE_LAYER_ID}"
SERVICE_PIPE_URL = f"{_MAP_SERVER}/{SERVICE_PIPE_LAYER_ID}"

PIPE_LAYER_IDS = (DISTRIBUTION_PIPE_LAYER_ID, SERVICE_PIPE_LAYER_ID)


PORTAL_AUTHORIZE_URL = PORTAL_ROOT + "/sharing/rest/oauth2/authorize"
PORTAL_TOKEN_URL = PORTAL_ROOT + "/sharing/rest/oauth2/token"

# --- Credentials -------------------------------------------------------------

ARCGIS_CLIENT_ID = os.environ.get("LEAKRELOCATION_CLIENT_ID", "48XCGWtLoUxA3klq")
ARCGIS_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"
KEYRING_SERVICE = "NG_GIS_LEAK_RELOCATION"
KEYRING_ACCESS_TOKEN_USER = "arcgis_portal_access_token"
KEYRING_ACCESS_TOKEN_EXPIRES_USER = "arcgis_portal_access_token_expires_epoch"
TOKEN_EXPIRY_SAFETY_SECONDS = 300


def _int_from_env(name, default):
    try:
        return int(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default


def _flag_from_env(name, default=False):
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


# Retired pipe, for leaks that pre-date a pipe's retirement. This one is on the
# MA service rather than the DNV NY service - the DNV service has no layer 62 and
# no ids between 20 and 99 at all - so it gets its own MapServer root.
RETIRED_PIPE_LAYER_ID = _int_from_env("LEAKRELOCATION_RETIRED_LAYER_ID", 62)
_MA_MAP_SERVER = os.environ.get(
    "LEAKRELOCATION_MA_MAP_SERVER",
    GIS_ROOT + "/arcgis/rest/services/MA/Material_View_MA/MapServer").rstrip("/")
RETIRED_PIPE_URL = os.environ.get(
    "LEAKRELOCATION_RETIRED_PIPE_URL",
    f"{_MA_MAP_SERVER}/{RETIRED_PIPE_LAYER_ID}")

# Include the retired layer in matching. Its schema has not been read yet, so a
# run reports what it finds and carries on with the live layers if it cannot use
# it; set this to 0 to skip it outright.
USE_RETIRED_PIPE_LAYER = _flag_from_env("LEAKRELOCATION_USE_RETIRED_LAYER", True)

# --- Matching / request tuning ------------------------------------------------

WHERE_MA = "jurisdiction = 'MA'"
INITIAL_RADIUS_FT = 100.0
RADIUS_INCREMENT_FT = 100.0
MAX_RADIUS_FT = 3000.0
REQUIRE_PRESSURE_MATCH = False
ALLOW_MATERIAL_FAMILY_FALLBACK = True

REQUEST_PAGE_SIZE = _int_from_env("LEAKRELOCATION_PAGE_SIZE", 2000)
REQUEST_TIMEOUT_SECONDS = _int_from_env("LEAKRELOCATION_TIMEOUT", 120)
OBJECTID_BATCH_SIZE = _int_from_env("LEAKRELOCATION_BATCH_SIZE", 2000)
OBJECTID_DOWNLOAD_WORKERS = _int_from_env("LEAKRELOCATION_DOWNLOAD_WORKERS", 8)
VERIFY_SSL = _flag_from_env("LEAKRELOCATION_VERIFY_SSL", True)

USE_LAYER_CACHE = _flag_from_env("USE_LAYER_CACHE", True)
FORCE_LAYER_REFRESH = _flag_from_env("FORCE_LAYER_REFRESH", False)
DELTA_REFRESH_SAFETY_SECONDS = 300

# A cache younger than this is trusted without asking the server whether it has
# changed. Each check costs a count query plus a delta query per layer, and
# needs a valid token, so a short window makes repeat runs start immediately.
# Set to 0 to check the server every run.
CACHE_FRESH_SECONDS = _int_from_env("LEAKRELOCATION_CACHE_FRESH_SECONDS", 3600)

# Load the three layers concurrently. They are independent reads.
PARALLEL_LAYER_LOAD = _flag_from_env("LEAKRELOCATION_PARALLEL_LOAD", True)

# --- Map ---------------------------------------------------------------------

# How far the map will zoom in. The tile providers stop at 19, so past that the
# deepest tile is upscaled while the pipes stay sharp - they are vectors.
#
# 28 is as deep as Leaflet goes without complaint: checked in a browser at 22, 24,
# 25, 26 and 28, all of which zoomed and drew without a page error. For scale, a
# pixel is roughly 4 cm at zoom 22, 9 mm at 24 and 0.6 mm at 28 - well past survey
# accuracy, so the extra levels are for separating overlapping lines rather than
# for measuring anything.
MAP_MAX_ZOOM = _int_from_env("LEAKRELOCATION_MAP_MAX_ZOOM", 28)

# Deepest zoom each basemap actually has tiles for. Leaflet upscales beyond it.
TILE_MAX_NATIVE_ZOOM = _int_from_env("LEAKRELOCATION_TILE_MAX_NATIVE_ZOOM", 19)

# --- Output verbosity -------------------------------------------------------

# Field resolution, proxy/TLS setup and outFields lists are diagnostic detail.
# They are hidden unless something needs debugging.
VERBOSE = _flag_from_env("LEAKRELOCATION_VERBOSE", False)

# Print per-stage elapsed times, to show where a slow run spends its time.
TIMINGS = _flag_from_env("LEAKRELOCATION_TIMINGS", False)

# --- Authentication UX -----------------------------------------------------

# Capture the OAuth code on a loopback redirect instead of the out-of-band page.
# The browser lands on a page this process serves, which reports success and
# closes itself, so no code is shown and no tab is left behind.
#
# The portal app registration must list the redirect URI below. If it does not,
# authentication fails and the out-of-band flow is used instead.
USE_LOOPBACK_OAUTH = _flag_from_env("LEAKRELOCATION_LOOPBACK_OAUTH", True)
LOOPBACK_OAUTH_PORT = _int_from_env("LEAKRELOCATION_LOOPBACK_PORT", 8080)

# The portal compares redirect_uri against the app registration as a string, so
# host spelling and the trailing slash both matter: "localhost" and "127.0.0.1"
# are different values, and ".../" differs from "...". Set the whole URI to
# match the registration exactly when the defaults do not.
LOOPBACK_OAUTH_HOST = os.environ.get("LEAKRELOCATION_LOOPBACK_HOST", "localhost").strip()
LOOPBACK_REDIRECT_URI = (
    os.environ.get("LEAKRELOCATION_LOOPBACK_REDIRECT_URI", "").strip()
    or f"http://{LOOPBACK_OAUTH_HOST}:{LOOPBACK_OAUTH_PORT}/"
)


def describe():
    """Return the resolved configuration, for logging at startup."""
    return {
        "work_root": str(WORK_ROOT),
        "input_dir": str(INPUT_DIR),
        "supplemental_csv": str(SUPPLEMENTAL_CSV),
        "layer_cache_dir": str(LAYER_CACHE_DIR),
        "output_gpkg": str(OUTPUT_GPKG),
        "gis_root": GIS_ROOT,
        "verify_ssl": VERIFY_SSL,
        "use_layer_cache": USE_LAYER_CACHE,
        "force_layer_refresh": FORCE_LAYER_REFRESH,
    }
