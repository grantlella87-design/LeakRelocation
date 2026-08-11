# LeakRelocation

Historic leak relocation workflow for DNV / GeoPandas production processing.

Reads MA historic leaks, MA distribution pipes and MA service pipes from the
ArcGIS REST services, supplements leak attributes from the shared CSV, matches
each leak to the nearest pipe with an exact diameter and material/family match,
snaps the leak to that pipe, and writes a GeoPackage with relocated points,
offset guide lines and an audit table.

## Key rule

Use decoded `ASSETGROUP + ASSETTYPE` for pipe material classification. Do not
use the DNV `material` Grade field as the material class for relocation
assessment.

## Layout

| Path | Contents |
| --- | --- |
| `run.py` | Single entry point: download, decode, match, write, map. |
| `src/leak_relocation_geopandas.py` | The production workflow. |
| `src/leaflet_bbox_server.py` | Local map viewer backend. |
| `src/leakrelocation/config.py` | Every path, URL and tuning knob. |
| `src/leakrelocation/matching.py` | Pure material/diameter matching logic. |
| `src/leakrelocation/assettype.py` | ASSETGROUP/ASSETTYPE subtype decoding. |
| `src/leakrelocation/viewer_pane.py` | Attribute table pane docked under the map. |
| `scripts/` | Sign-in and cache-repair tools. |
| `reference/` | Service metadata copies the tests check against. |
| `vendor/leaflet/` | Leaflet, committed so the map needs no internet. |
| `tests/` | Tests that need no network and no shared drive. |

### Service metadata reference

`reference/mapserver_json/NY_DNV_Synergi_RiskResults_Assets_NY/` is a
point-in-time copy of the DNV MapServer metadata, taken 2026-08-07 from

    https://gis.nationalgrid.com/dnv/rest/services/NY/DNV_Synergi_RiskResults_Assets_NY/MapServer?f=pjson

It is the reason the field names in the workflow are facts rather than guesses.
`tests/test_dnv_service_metadata.py` reads it to confirm that every field name
the workflow uses still exists, that `SERVICE_ASSETTYPE_LABELS` matches the real
ASSETTYPE domain, and that every domain label lands in the expected material
family — offline, with no token.

Only the layers this project reads are kept:

| File | Contents |
| --- | --- |
| `layer_006_Distribution_Pipe.json` | Distribution pipes, with the ASSETTYPE subtype domains. |
| `layer_007_Service_Pipe.json` | Service pipes, same domains. |
| `layer_206_Hist_GasLeak.json` | Historic leaks. Carries no material or diameter field, which is why both come from the supplemental CSV. |
| `manifest.json` | Every layer id on the service and its source URL. |
| `service.json` | Service-level metadata. |

The original copy held all 102 layer JSONs, 150 MB, of which these were the only
files anything read. Re-copy from the URL above if another layer is ever needed.

### Scripts

| Script | Purpose |
| --- | --- |
| `arcgis_signin.py` | Sign in on its own, inspect or clear the cached token, test the redirect. |
| `preflight_assettype_cache_check.py` | Report which pipe caches are present. |
| `enrich_assettype_cache.py` | Repair an existing cache. Not needed for a normal run — `run.py` decodes the material itself. |

The viewer builders, the audit and the inspect scripts are gone: `run.py` serves
one map that shows everything they reported on, and the material decode moved
into the workflow.

## Configuration

Paths and endpoints used to be hard-coded in most files. They now live in
`src/leakrelocation/config.py`, and every value has an environment-variable
override. The defaults reproduce the previous behaviour, so nothing needs to be
set to run as before.

| Variable | Default |
| --- | --- |
| `LEAKRELOCATION_WORK_ROOT` | `~/Downloads/LeakRelocation-GeoPandas` |
| `LEAKRELOCATION_PROJECT_DIR` | the `\\ngusnasnwh001\...` share |
| `LEAKRELOCATION_CACHE_DIR` | `<work root>/layer_cache` |
| `LEAKRELOCATION_OUTPUT_GPKG` | `<work root>/production_moved_leak_outputs/HistoricLeakRelocation.gpkg` |
| `LEAKRELOCATION_SUPPLEMENTAL_CSV` | `<project dir>/HL_SupplementalData.csv` |
| `LEAKRELOCATION_GIS_ROOT` | `https://gis.nationalgrid.com` |
| `LEAKRELOCATION_VERBOSE` | `0` — set to `1` for field resolution and setup detail |
| `LEAKRELOCATION_TIMINGS` | `0` — set to `1` for per-stage elapsed times |
| `LEAKRELOCATION_CACHE_FRESH_SECONDS` | `3600` — trust a cache younger than this without asking the server; `0` always checks |
| `LEAKRELOCATION_PARALLEL_LOAD` | `1` — load the three layers concurrently |
| `LEAKRELOCATION_LOOPBACK_OAUTH` | `1` — sign in via a loopback redirect; `0` uses the out-of-band page |
| `LEAKRELOCATION_LOOPBACK_PORT` | `8080` — must match a redirect URI on the portal app registration |

### Output location

The GeoPackage is written **locally**, under
`<work root>/production_moved_leak_outputs/`. Writing straight to the share made
every run depend on network write throughput, and a partial write left the
shared copy broken. Publish it deliberately when a run looks good, or set
`LEAKRELOCATION_OUTPUT_GPKG` to the share path to restore the old behaviour.

### Signing in

Authentication uses a loopback redirect: the browser lands on a page this
process serves, which reports success and closes itself. Nothing displays the
authorization code and no tab is left behind.

This requires `http://localhost:8080/` to be listed as a redirect URI on the
portal app registration. If it is not, the run warns and falls back to the
out-of-band page — the one that shows `SUCCESS code=...` and cannot be closed
programmatically, because that page belongs to the portal rather than to this
process.

A token is cached in Windows Credential Manager, and a cache younger than
`LEAKRELOCATION_CACHE_FRESH_SECONDS` skips the server check entirely, so most
repeat runs need no sign-in at all.

### If a run is slow

```bash
LEAKRELOCATION_TIMINGS=1 python src/leak_relocation_geopandas.py
```

prints elapsed time per stage — supplemental CSV, layer loading, CRS
normalisation, index building, matching, writing — so the slow stage is
identified rather than guessed at.

To see what the workflow has resolved:

```bash
python -c "import sys; sys.path.insert(0,'src'); from leakrelocation import config; print(config.describe())"
```

## Setup

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Running

One command does everything: check the token, download the three layers, decode
the pipe material from ASSETGROUP + ASSETTYPE, match, write the GeoPackage, and
serve the map.

```bat
python run.py
```

| Flag | Effect |
| --- | --- |
| `--no-view` | Stop after the GeoPackage. |
| `--view-only` | Skip the workflow and serve the map. |
| `--refresh` | Ignore the layer caches and re-download. |
| `--port 8800` | Serve the map somewhere else. |
| `--no-browser` | Serve without opening a browser. |
| `--skip-signin-check` | Do not verify the token first. |

### The map

`run.py` serves `src/leaflet_bbox_server.py`, which shows, as switchable layers:

- **distribution pipes** and **service pipes**, each line coloured by its
  material — plastic, steel, iron, copper, unknown;
- the historic leaks in their original positions;
- the relocated leak points and the trace lines back to where each leak was.

Material comes from the decoded `ASSETGROUP + ASSETTYPE` subtype domains, which
is what the matching uses too, so the map and the match agree. Colours live in
one place, `MATERIAL_COLORS` in that file; the page's palette is generated from
it, so editing it changes the map.

The pipe layers are read from the downloaded caches and served by bounding box, a
capped number of features at a time, re-fetched as the map moves. They are far
too large to write into a single GeoJSON and hand to a browser.

Leaflet is committed under `vendor/leaflet/`, so the page has it with no internet
and nothing to build first. The map falls back to a CDN only if that copy is
missing, and warns when it does.

There is no longer an enrichment step to remember. The material decode happens
inside the workflow, which is why the order can no longer be got wrong; it used
to stop after the download with `material is missing, so this cache is not
ASSETTYPE-enriched` and have to be restarted after a second command.

Every stage still runs on its own:

```bat
python scripts\arcgis_signin.py --test-query      :: sign in, prove the token works
python scripts\arcgis_signin.py --check           :: test the redirect only
python src\leak_relocation_geopandas.py           :: download, decode, match, write
python src\leaflet_bbox_server.py                 :: serve the map on its own

:: diagnostics
python scripts\preflight_assettype_cache_check.py
python scripts\enrich_assettype_cache.py          :: repair an existing cache
python scripts\enrich_assettype_cache.py --dry-run       :: report, write nothing
```

### Attribute pane

The relocation viewer docks an attribute table under the map, one tab per
layer:

- Click a row to zoom to that feature and open its popup.
- Click a feature on the map to select and scroll to its row.
- Click a column header to sort; numeric columns sort numerically.
- Filter rows with the search box (matches any attribute value).
- **Hide** collapses the pane and gives the space back to the map.

Rendering is capped at 500 rows per view — a production layer holds tens of
thousands of features and an uncapped table would lock the browser. The row
count shows `showing N of M` when the cap applies; narrow the set with the
filter.

## Tests

The matching logic is tested without network access, an ArcGIS token or the
shared drive:

```bash
python -m pytest
```

`tests/characterize.py` snapshots the behaviour of the workflow's pure
functions. Run it before and after a change and diff the two files to confirm
a refactor did not alter results:

```bash
python tests/characterize.py before.json
# ...make changes...
python tests/characterize.py after.json
diff before.json after.json
```

## Making changes

Edit the source in this repository, run the tests, then deploy. Do **not** edit
the deployed copy on the shared drive — patching it in place is how the
workflow ended up with five functions defined twice. See the history section in
`docs/PROJECT_STATE.md`.

`src/leak_relocation_geopandas.py` imports the `leakrelocation` package from its
own directory, so when it is copied to the share the `src/leakrelocation/`
folder must be copied alongside it.

## Local workflow

Working data lives in:

- `~/Downloads/LeakRelocation-GeoPandas` (caches and generated output)
- `\\ngusnasnwh001\gasne\GasNE Shared\Shared\ENG\Complex Team\GIS AutoPrint\Distribution Leak Relocation` (shared inputs and the production GeoPackage)

Both are overridable via `LEAKRELOCATION_WORK_ROOT` and
`LEAKRELOCATION_PROJECT_DIR`.

Changes reach the repository through git directly. The previous
`Sync-LeakRelocation-ToGit.ps1` copy-and-commit step has been removed: it
rewrote tracked files from the local working folder on every run, which is how
edits made here were repeatedly overwritten.
