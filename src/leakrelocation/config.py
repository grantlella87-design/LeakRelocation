"""Single source of truth for every path, URL and tuning knob.

Before this module existed the production UNC share was hard-coded in 22 of
the repository's 30 files and the local working root in 15, so relocating
either meant a find-and-replace across the whole tree.

Every value can be overridden with an environment variable, which is what
makes the workflow runnable off one person's workstation (and testable on a
machine that has neither the share nor the GIS portal). The defaults
reproduce the previous hard-coded values exactly, so behaviour is unchanged
when nothing is set.

    LEAKRELOCATION_WORK_ROOT     local scratch/cache root
    LEAKRELOCATION_PROJECT_DIR   shared network project folder
    LEAKRELOCATION_CACHE_DIR     layer cache (defaults under work root)
    LEAKRELOCATION_OUTPUT_GPKG   production GeoPackage
    LEAKRELOCATION_GIS_ROOT      ArcGIS server root
    LEAKRELOCATION_PORTAL_ROOT   ArcGIS portal root
"""
import os
from pathlib import Path

# --- Locations ---------------------------------------------------------------

DEFAULT_WORK_ROOT = Path.home() / "Downloads" / "LeakRelocation-GeoPandas"
DEFAULT_PROJECT_DIR = Path(
    r"\\ngusnasnwh001\gasne\GasNE Shared\Shared\ENG\Complex Team"
    r"\GIS AutoPrint\Distribution Leak Relocation"
)


def _path_from_env(name, default):
    value = os.environ.get(name, "").strip()
    return Path(value) if value else default


WORK_ROOT = _path_from_env("LEAKRELOCATION_WORK_ROOT", DEFAULT_WORK_ROOT)
PROJECT_DIR = _path_from_env("LEAKRELOCATION_PROJECT_DIR", DEFAULT_PROJECT_DIR)

LAYER_CACHE_DIR = _path_from_env("LEAKRELOCATION_CACHE_DIR", WORK_ROOT / "layer_cache")
OUTPUT_DIR = WORK_ROOT / "production_moved_leak_outputs"
ENRICHMENT_DIR = WORK_ROOT / "assettype_cache_enrichment"
VIEWER_DIR = OUTPUT_DIR / "local_leaflet_view"
VIEWER_WITH_PIPES_DIR = OUTPUT_DIR / "local_leaflet_view_with_pipes"

OUTPUT_GPKG = _path_from_env(
    "LEAKRELOCATION_OUTPUT_GPKG", PROJECT_DIR / "HistoricLeakRelocation.gpkg")
SUPPLEMENTAL_CSV = PROJECT_DIR / "HL_SupplementalData.csv"

# The ArcPy-era script still executed from the share. Several diagnostic
# scripts import it by path to reuse its request helpers.
NETWORK_MAIN_SCRIPT = PROJECT_DIR / "Arcpy Code" / "leak reolcation - geopandas.py"

# --- Services ----------------------------------------------------------------

GIS_ROOT = os.environ.get("LEAKRELOCATION_GIS_ROOT", "https://gis.nationalgrid.com").rstrip("/")
PORTAL_ROOT = os.environ.get("LEAKRELOCATION_PORTAL_ROOT", GIS_ROOT + "/portal").rstrip("/")

_MAP_SERVER = GIS_ROOT + "/dnv/rest/services/NY/DNV_Synergi_RiskResults_Assets_NY/MapServer"

HISTORIC_LEAK_LAYER_ID = 206
DISTRIBUTION_PIPE_LAYER_ID = 6
SERVICE_PIPE_LAYER_ID = 7

HIST_LEAK_URL = "{0}/{1}".format(_MAP_SERVER, HISTORIC_LEAK_LAYER_ID)
DISTRIBUTION_PIPE_URL = "{0}/{1}".format(_MAP_SERVER, DISTRIBUTION_PIPE_LAYER_ID)
SERVICE_PIPE_URL = "{0}/{1}".format(_MAP_SERVER, SERVICE_PIPE_LAYER_ID)

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


def describe():
    """Return the resolved configuration, for logging at startup."""
    return {
        "work_root": str(WORK_ROOT),
        "project_dir": str(PROJECT_DIR),
        "layer_cache_dir": str(LAYER_CACHE_DIR),
        "output_gpkg": str(OUTPUT_GPKG),
        "gis_root": GIS_ROOT,
        "verify_ssl": VERIFY_SSL,
        "use_layer_cache": USE_LAYER_CACHE,
        "force_layer_refresh": FORCE_LAYER_REFRESH,
    }
