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
| `src/leak_relocation_geopandas.py` | The production workflow. |
| `src/leaflet_bbox_server.py` | Local map viewer backend. |
| `src/leakrelocation/config.py` | Every path, URL and tuning knob. |
| `src/leakrelocation/matching.py` | Pure material/diameter matching logic. |
| `src/leakrelocation/assettype.py` | ASSETGROUP/ASSETTYPE subtype decoding. |
| `src/leakrelocation/viewer_pane.py` | Attribute table pane shared by the viewers. |
| `src/leakrelocation/viewer_html.py` | Relocation viewer HTML template. |
| `scripts/` | Enrichment, audit and viewer-building tools. |
| `viewer/` | Local map viewer and its server. |
| `tests/` | Tests that need no network and no shared drive. |

### Scripts

| Script | Purpose |
| --- | --- |
| `enrich_assettype_cache.py` | Decode ASSETGROUP/ASSETTYPE into the local pipe caches. |
| `preflight_assettype_cache_check.py` | Confirm the caches are present and enriched. |
| `audit_production_relocation_match_material.py` | Check relocated output against the pipe caches. |
| `inspect_production_moved_leaks.py` | Summarise the relocated leaks in the GeoPackage. |
| `build_local_relocation_viewer.py` | Build the local map of relocated leaks. |
| `build_leaflet_context.py` | Build the standalone Leaflet context map. |

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
| `LEAKRELOCATION_OUTPUT_GPKG` | `<project dir>/HistoricLeakRelocation.gpkg` |
| `LEAKRELOCATION_GIS_ROOT` | `https://gis.nationalgrid.com` |

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

Everything runs through Python directly.

```bash
# the relocation workflow
python src/leak_relocation_geopandas.py

# enrichment and diagnostics
python scripts/preflight_assettype_cache_check.py
python scripts/enrich_assettype_cache.py                 # both pipe layers
python scripts/enrich_assettype_cache.py --workers 1     # unreliable connection
python scripts/enrich_assettype_cache.py --dry-run       # report, write nothing
python scripts/audit_production_relocation_match_material.py

# build the viewers
python scripts/build_local_relocation_viewer.py
python scripts/build_leaflet_context.py

# serve the checked-in viewer
python viewer/serve_viewer.py
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

`viewer/index_basic.html` is a committed snapshot of the generated viewer.
`tests/test_viewer.py` re-renders the template and asserts the snapshot still
matches, so the two cannot drift.

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
