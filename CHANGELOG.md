# Changelog

What changed, newest first, and why. Changes that alter which leaks relocate
are marked **output**.

## 1.1.0 — November 2026

Signing in.

- **The sign-in window closes when you click Allow.** The tab used to stay
  behind saying it could be closed: the page asked to close itself, and a
  browser refuses that for any tab that has navigated. The sign-in now opens in
  a small window of its own, in the default browser and your normal profile,
  and that window is closed from outside. Needs a Chromium browser (Edge,
  Chrome) as the default; otherwise it is an ordinary tab, closed with Ctrl+W
  only while it is the tab in front. `LEAKRELOCATION_SIGNIN_WINDOW=tab` forces
  the tab.
- **A sign-in is reused for two weeks.** The portal issues a refresh token
  with every sign-in and it was being thrown away, so the browser came back as
  soon as the access token lapsed — within the hour, and in the middle of a
  long download. It is now saved, and a lapsed or refused access token is
  renewed from it without a browser.
- A token refused mid-download is renewed once. Each of the eight download
  workers used to clear the cache and start a sign-in of its own.
- `arcgis_signin.py --status` reports both tokens and says whether the next
  run needs the browser. `--renew` fetches a new access token from the saved
  sign-in. `--clear` and `--force` now sign out completely.
- Fixed: with `LEAKRELOCATION_VERBOSE=1` the one-time authorization code was
  printed. Clicking **Deny** landed on the page that said "Signed in".
- Removed: the PowerShell script that sent Ctrl+W to whichever window was in
  front after a sign-in.

## 1.0.0 — November 2026

A restructuring for production use. No command changed and the relocation
output is byte-for-byte what it was: a 300-leak fixture run through all four
rule combinations was compared before and after every step.

### Structure

- The workflow and the map server moved into the package:
  `src/leak_relocation_geopandas.py` is `src/leakrelocation/workflow.py` and
  `src/leaflet_bbox_server.py` is `src/leakrelocation/map_server.py`. `run.py`
  and the scripts are unchanged to use.
- The 2,470-line workflow was split by responsibility into `arcgis`,
  `layer_cache`, `layer_fields`, `native_crs` and `dates`, leaving `workflow`
  as the relocation itself. See [Development](docs/development.md).
- One copy where there were several: the audit-layer reader, the output layer
  names, the list of diameter rules, `log`, `escape`, and the ArcGIS request
  helper.
- Code nothing called was removed, including a projection helper that assumed
  EPSG:3857 for any frame without a coordinate system.
- The README was split into `docs/`, one topic per file.

### Fixed

- **`--refresh` did nothing.** `python run.py --refresh` and
  `python scripts\maximo_leaks.py --refresh` printed that the caches would be
  ignored and then read them: the flag was written to the environment after the
  settings had already been read from it. It now takes effect.
- **Maximo leaks were read in the wrong coordinate system.** The step that puts
  every layer in one CRS relabelled whatever leaks it was given with layer
  206's CRS, discarding `LEAKRELOCATION_MAXIMO_CRS`. A longitude became an
  easting in feet and nothing matched. Leaks that did not come from layer 206
  are now projected from the CRS they carry, and refused if they carry none.
  **output** — for Maximo runs only.
- `preflight_assettype_cache_check.py --help` ran the check instead of
  describing it.
- On the map, the **Distance dashboard** link was clipped to a button-sized box
  with its text running off the edge. The browser console no longer shows a 404
  for the favicon.

### Tooling

- Python 3.11 is the declared minimum. It always was the real one — the code
  uses `datetime.UTC` — but the package metadata said 3.9. `bootstrap.py` now
  turns an older Python away before building an environment that would fail.
- The lint rule set is written down in `pyproject.toml`.
- CI runs the tests and the linter on every push, on Windows and Linux.
- `pip install -e .` now installs `truststore` too; `pyodbc` is the `maximo`
  extra.

## November 2026 — the 100 ft search radius

- **output** A leak relocates only onto a pipe within 100 ft. The search used
  to widen in 100 ft steps to 3,000 ft, so an output could contain a leak moved
  most of a mile onto the only pipe in reach. Those leaks are now reported as
  unmatched. `LEAKRELOCATION_MAX_RADIUS_FT` widens it again.
- The geometry write-back refuses moves over 100 ft by default, to match.

## October 2026 — writing the result back, and a second leak source

- `scripts/apply_relocated_geometry.py` moves the leaks on layer 206 to their
  relocated positions. A dry run unless `--apply` is given, with a typed
  confirmation, a rollback file written before any edit, and a check that every
  feature kept its GlobalID. It sends geometry and nothing else.
- `scripts/maximo_leaks.py` relocates the enterprise (Maximo) leaks, read from
  a SQL query that returns their material and diameter with them. It shares the
  whole matching pipeline with the historic run and writes its own GeoPackage.
- `--leak-date-fallback` uses `DISCOVEREDDATE`, `REPAIREDDATE` or
  `COMPLETEDDATE` when a leak has no `REVISEDLEAKDATE`. Off by default because
  it changes which leaks relocate; every run counts how many it would affect.
- Fixed: a date given as text, such as `2019-04-02`, was read as the number
  2019. A run that matched nothing crashed while writing its empty output.

## September 2026 — measuring the result

- The relocation-distance dashboard: how far each leak moved, with a slider for
  the share over and under any distance, by pipe layer, facility and material,
  and a review queue of the furthest moves.
- **output** `--diameter fuzzy` also accepts a pipe one nominal size up or
  down, and writes its own GeoPackage so the two can be compared. An exact
  diameter still wins at any distance, so the strict run's relocations are
  unchanged within it.
- Transient network failures are retried instead of ending the run. Download
  workers reuse one connection each.
- `bootstrap.py` sets up the environment, using the corporate proxy only when
  it is in the path.
- `field_fill_report.py` and `probe_leak_fields.py` say which fields the
  service actually populates. An empty string is no longer counted as a value.
- A leak with no street address is described by its nearest cross street and
  town.

## August 2026 — making the inputs facts

- **output** The supplemental CSV is joined on the leak's GlobalID, not its
  leak number. The number is not unique in the file: joining on it gave 8,425
  leaks another leak's material, diameter or facility type.
- **output** A leak relocates only onto a pipe that existed when the leak was
  recorded.
- **output** Pipe material comes from the decoded `ASSETGROUP` + `ASSETTYPE`
  subtype domain. The DNV `material` field is a grade and is no longer
  requested. Fixed material families collapsing to PLASTIC.
- Field names are the ones the service has, checked by tests against a
  committed copy of its metadata, instead of lists of guesses.
- The supplemental CSV is committed under `input/`; nothing reads a network
  share. Outputs are written locally.
- `run.py` is the single entry point: sign in, download, decode, match, write,
  serve the map. The material decode happens inside the run rather than as a
  separate step.
- One map, grouped into mains and services, with an attribute table, served
  from the local caches with Leaflet vendored so it needs no internet.
- Sign-in moved into its own module, using a loopback redirect so no
  authorization code is displayed.
