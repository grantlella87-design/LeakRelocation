# LeakRelocation

Moves gas leak points onto the pipe they were on.

A leak is recorded where someone stood, not where the pipe is. This reads the
Massachusetts leaks and the distribution and service pipes from the ArcGIS REST
services, matches each leak to the nearest pipe of the same material and
diameter that existed at the time, snaps the leak onto it, and writes a
GeoPackage with the relocated points, a guide line from old position to new,
and an audit row for every leak saying what happened and why.

A clone and a GIS sign-in are all a run needs. Nothing reads a shared drive.

## Quick start

Python 3.11 or newer, on Windows:

```bat
python bootstrap.py
.venv\Scripts\activate
python run.py
```

`bootstrap.py` creates `.venv` and installs the requirements, going through the
corporate proxy when it detects one. `run.py` signs in, downloads the layers,
relocates the leaks, and opens a map of the result at <http://127.0.0.1:8765/>.

The first run downloads well over a million pipes and takes a while. Later runs
fetch only what changed.

More on installing, proxies and certificates: [docs/setup.md](docs/setup.md).

## Commands

| | |
| --- | --- |
| `python run.py` | Relocate the historic (LMS) leaks and open the map. |
| `python run.py --no-view` | Stop after writing the GeoPackage. |
| `python run.py --view-only` | Just the map, from the last run's output. |
| `python run.py --refresh` | Ignore the layer caches and download again. |
| `python run.py --diameter fuzzy` | Also accept a pipe one nominal size up or down. Writes its own GeoPackage. |
| `python run.py --leak-date-fallback` | Use the other dates on a leak when its revised date is empty. |
| `python scripts\maximo_leaks.py` | Relocate the enterprise (Maximo) leaks, read from SQL. |
| `python scripts\relocation_dashboard.py` | Write the relocation-distance dashboard as one HTML file. |
| `python scripts\apply_relocated_geometry.py` | Move the leaks on the live layer to their relocated positions. **A dry run unless `--apply` is given.** |
| `propy.bat scripts\update_leak_layer_arcpy.py` | In ArcGIS Pro's Python: copy layer 206 into a file geodatabase and move the copy's leaks to their relocated positions. Leaves the live layer alone. |

Every command answers `--help`.

Diagnostics, none of which change anything:

| | |
| --- | --- |
| `python scripts\arcgis_signin.py --test-query` | Sign in and prove the token works. `--status`, `--clear`, `--check`. |
| `python scripts\field_fill_report.py` | Which fields actually hold values, in the caches; `--service` asks the service too. |
| `python scripts\probe_leak_fields.py` | Why a leak field is empty: on our rows, or on every row. |
| `python scripts\describe_layer.py` | A layer's fields, dates and subtype domains; `--save` keeps a copy in `reference/`. |
| `python scripts\preflight_assettype_cache_check.py` | Whether the pipe caches carry a decoded material. |
| `python scripts\enrich_assettype_cache.py` | Repair an existing pipe cache. A normal run does not need it. |

## What it writes

Under `~/Downloads/LeakRelocation-GeoPandas/production_moved_leak_outputs/`
(set `LEAKRELOCATION_WORK_ROOT` to move it):

| File | From |
| --- | --- |
| `HistoricLeakRelocation.gpkg` | `python run.py` |
| `HistoricLeakRelocation_fuzzy_diameter.gpkg` | `python run.py --diameter fuzzy` |
| `MaximoLeakRelocation.gpkg` | `python scripts\maximo_leaks.py` |

Each GeoPackage has three layers:

| Layer | Contents |
| --- | --- |
| `relocated_leaks` | One point per relocated leak, on its pipe. |
| `relocated_leak_offset_lines` | A line from each leak's recorded position to its relocated one. |
| `leak_relocation_audit` | One row per leak, relocated or not: the pipe it matched, the distance, or the reason nothing matched. |

These files hold real asset data. They are written outside the repository and
`.gitignore` keeps them out of it.

## The rules, briefly

- **Material** comes from the pipe's decoded `ASSETGROUP` + `ASSETTYPE`, never
  from the DNV `material` field, which is a grade.
- **Diameter** must be the same nominal size, or one size either way under
  `--diameter fuzzy`.
- **Distance**: a leak relocates only onto a pipe within 100 ft. Otherwise it
  is reported as unmatched rather than paired with something far away.
- **Time**: a leak relocates only onto a pipe that existed when the leak was
  recorded.
- A leak's material and diameter come from `input/HL_SupplementalData.csv`,
  joined on the leak's GlobalID — never on its leak number, which is not
  unique.

The detail, and the reasoning, is in [docs/matching-rules.md](docs/matching-rules.md).

## Documentation

| | |
| --- | --- |
| [Setup](docs/setup.md) | Installing, the proxy, certificates. |
| [The matching rules](docs/matching-rules.md) | Material, the search radius, leak dates, exact and fuzzy diameter. |
| [The leak sources](docs/leak-sources.md) | Historic leaks and the supplemental CSV; Maximo leaks and the SQL query. |
| [The map and the dashboard](docs/map-and-dashboard.md) | Reading the map, the attribute table, the distance dashboard. |
| [Updating the leak layer](docs/geometry-write-back.md) | Writing relocated positions back to the live service, and undoing it. |
| [Configuration](docs/configuration.md) | Every environment variable; sign-in; how the layer cache works. |
| [Troubleshooting](docs/troubleshooting.md) | Empty fields, dropped connections, slow runs. |
| [Development](docs/development.md) | How the code is laid out, tests, lint, CI. |
| [Changelog](CHANGELOG.md) | What changed, and why. |

## Repository layout

| Path | Contents |
| --- | --- |
| `run.py` | The main entry point. |
| `bootstrap.py` | Environment setup. Standard library only. |
| `scripts/` | The other commands. |
| `src/leakrelocation/` | The package. See [Development](docs/development.md). |
| `input/` | The supplemental leak CSV and the Maximo query template. |
| `reference/` | A copy of the service metadata, which the tests check field names against. |
| `vendor/leaflet/` | Leaflet, committed so the map needs no internet. |
| `tests/` | The test suite. No network, no token, no asset data. |
| `docs/` | Everything above in detail. |
