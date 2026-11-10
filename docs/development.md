# Development

## How the code is laid out

`run.py` and the files in `scripts/` are entry points: they parse a command
line and call into the package. Everything else is in `src/leakrelocation/`.

| Module | What it holds |
| --- | --- |
| `config` | Every path, URL and tuning value, read from the environment once. |
| `auth` | ArcGIS sign-in and the cached token. Nothing else knows how a token is obtained. |
| `arcgis` | Requests to the REST layers: token attachment, retries, feature downloads. |
| `layer_fields` | Which fields are read from each layer and from the supplemental CSV. |
| `layer_cache` | The on-disk copy of each layer, and the full or delta download behind it. |
| `native_crs` | Each layer's own coordinate system, and the one matching is done in. |
| `dates` | Epoch-millisecond and SQL timestamp conversions. |
| `assettype` | Naming a pipe's material from its `ASSETGROUP` + `ASSETTYPE` subtype domain. |
| `matching` | The rules — material, diameter, pressure, pipe life. No I/O at all. |
| `workflow` | The relocation: prepare leaks and pipes, match, write the GeoPackage. |
| `maximo_sql` | The SQL boundary for the enterprise leaks: connection, query, column contract. |
| `leak_location` | Composing where a leak is, in words, from whichever fields are filled. |
| `schema` | The names of the layers and columns this project writes. |
| `distance_report`, `distance_dashboard` | How far the leaks moved, as numbers and as a page. |
| `apply_geometry` | Writing relocated positions back to the leak layer. |
| `map_server`, `viewer_pane` | The local map and its attribute table. |
| `output` | Console output and stage timings. |

Two conventions hold it together:

- **A module refers to another module's names through the module** —
  `arcgis.request_json(...)`, not `from ...arcgis import request_json`. A test
  that replaces `arcgis.request_json` then replaces it for every caller.
- **Every module sets `sys.path` before importing the package**, so each one
  works imported as `leakrelocation.x`, loaded by file path, or run directly.
  `tests/test_module_loading.py` holds every module to all three, and it is
  why the package uses absolute imports throughout.

`workflow.relocate()` is the one copy of the matching pipeline. The historic
run and the Maximo run both call it; a second copy would drift and the two leak
sets would stop being comparable.

Settings are read when `config` is first imported. A command-line flag is
applied with `config.set_run_options(...)`, which changes the setting and the
environment variable together — the matching workers are separate processes
and read the environment. `workflow` settles which GeoPackage it writes as it
is imported, so entry points apply their flags first and import it afterwards.

## Tests

```bat
python -m pytest
python -m ruff check .
```

Neither needs the network, an ArcGIS token or any asset data. Layers are
stubbed with a local HTTP server, the Maximo database with a fake connection,
and the field names are checked against the service metadata committed under
`reference/`. The suite takes under a minute.

CI (`.github/workflows/ci.yml`) runs both on every push, on Windows and Linux.

The lint rules are listed in `pyproject.toml` rather than left to the ruff
version's defaults, so the result is the same on every machine.

### Proving a refactor changed nothing

`tests/characterize.py` snapshots the behaviour of the pure functions — field
resolution, date conversion, the outgoing request parameters. Run it before and
after a change and compare:

```bat
python tests\characterize.py before.json
:: ...make changes...
python tests\characterize.py after.json
fc before.json after.json
```

## Making changes

Edit the source in this repository and run the tests. There is no deployed copy
to keep in step.

The repository holds everything a run reads. The only thing written outside it
is the working data, under `~/Downloads/LeakRelocation-GeoPandas` — caches and
the output GeoPackages — overridable with `LEAKRELOCATION_WORK_ROOT`. None of
it belongs in git; `.gitignore` covers it for the case where the work root is
pointed inside the clone.

What changed and why is in [CHANGELOG.md](../CHANGELOG.md).

## Service metadata reference

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
| `layer_206_Hist_GasLeak.json` | Historic leaks. Carries no material or diameter field, which is why both come from the supplemental CSV. `ADDRESS` and `REVISEDLEAKDATE` are read from it. |
| `manifest.json` | Every layer id on the service and its source URL. |
| `service.json` | Service-level metadata. |

The original copy held all 102 layer JSONs, 150 MB, of which these were the only
files anything read. Re-copy from the URL above if another layer is ever needed.
