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
| `scripts/` | Diagnostic and enrichment scripts. |
| `scripts/legacy_patches/` | Historical in-place patchers — **do not run**. |
| `powershell/` | Helper scripts for running the workflow on Windows. |
| `tests/` | Tests that need no network and no shared drive. |

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
the deployed copy on the shared drive, and do not run the scripts in
`scripts/legacy_patches/` — they append code to the deployed file, which is how
the workflow ended up with five functions defined twice.

`src/leak_relocation_geopandas.py` imports the `leakrelocation` package from its
own directory, so when it is copied to the share the `src/leakrelocation/`
folder must be copied alongside it.

## Local workflow

Working files are synchronised from:

- `C:\Users\lellag\Downloads\LeakRelocation-GeoPandas`
- `\\ngusnasnwh001\gasne\GasNE Shared\Shared\ENG\Complex Team\GIS AutoPrint\Distribution Leak Relocation`

Run PowerShell helper scripts from the `powershell/` folder.
