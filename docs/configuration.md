# Configuration

Every path, URL and tuning value lives in `src/leakrelocation/config.py`, and
every one has an environment-variable override. The defaults are a working
setup, so nothing needs to be set for a first run.

To see what a run has actually resolved:

```bat
python -c "import sys; sys.path.insert(0,'src'); from leakrelocation import config; print(config.describe())"
```

## Where things are read from and written to

| Variable | Default |
| --- | --- |
| `LEAKRELOCATION_WORK_ROOT` | `~/Downloads/LeakRelocation-GeoPandas` — caches and outputs live under it |
| `LEAKRELOCATION_CACHE_DIR` | `<work root>/layer_cache` |
| `LEAKRELOCATION_OUTPUT_GPKG` | `<work root>/production_moved_leak_outputs/HistoricLeakRelocation.gpkg` |
| `LEAKRELOCATION_MAXIMO_OUTPUT_GPKG` | `<work root>/production_moved_leak_outputs/MaximoLeakRelocation.gpkg` |
| `LEAKRELOCATION_SUPPLEMENTAL_CSV` | `input/HL_SupplementalData.csv`, in the repository |
| `LEAKRELOCATION_GIS_ROOT` | `https://gis.nationalgrid.com` |
| `LEAKRELOCATION_PORTAL_ROOT` | `<GIS root>/portal` |
| `LEAKRELOCATION_MA_MAP_SERVER` | the MA service that carries the retired-pipe layer |
| `LEAKRELOCATION_RETIRED_LAYER_ID` | `62` |
| `LEAKRELOCATION_RETIRED_PIPE_URL` | built from the two above; set it to point somewhere else entirely |
| `LEAKRELOCATION_USE_RETIRED_LAYER` | `1` — also relocate onto retired pipe; `0` leaves that layer out |

## The matching

Each of these is explained in [the matching rules](matching-rules.md).

| Variable | Default |
| --- | --- |
| `LEAKRELOCATION_MAX_RADIUS_FT` | `100` — how far the search reaches |
| `LEAKRELOCATION_INITIAL_RADIUS_FT` | `100` — the first pass |
| `LEAKRELOCATION_RADIUS_INCREMENT_FT` | `100` — how much each pass widens by |
| `LEAKRELOCATION_DIAMETER_MODE` | `exact` — or `fuzzy`. `--diameter` on the command line sets it for one run |
| `LEAKRELOCATION_PREFER_EXACT_DIAMETER` | `1` — under `fuzzy`, an exact diameter outranks an adjacent one at any distance |
| `LEAKRELOCATION_LEAK_DATE_FALLBACK` | `0` — `--leak-date-fallback` sets it for one run |

## Enterprise (Maximo) leaks

See [the leak sources](leak-sources.md#enterprise-maximo-leaks).

| Variable | Default |
| --- | --- |
| `LEAKRELOCATION_MAXIMO_DSN` | none. The connection string; read from the environment only and never written or logged |
| `LEAKRELOCATION_MAXIMO_QUERY` | `input/maximo_leaks.sql` |
| `LEAKRELOCATION_MAXIMO_CRS` | `EPSG:4326` — what the query's `X` and `Y` are in |

## Downloads and the cache

| Variable | Default |
| --- | --- |
| `LEAKRELOCATION_CACHE_FRESH_SECONDS` | `3600` — trust a cache younger than this without asking the server; `0` always checks |
| `FORCE_LAYER_REFRESH` | `0` — `--refresh` sets it for one run |
| `USE_LAYER_CACHE` | `1` — `0` downloads every layer in full, every run |
| `LEAKRELOCATION_PARALLEL_LOAD` | `1` — load the three layers concurrently |
| `LEAKRELOCATION_DOWNLOAD_WORKERS` | `8` — parallel batch downloads; lower it if the network keeps dropping them |
| `LEAKRELOCATION_BATCH_SIZE` | `2000` — object ids per download batch |
| `LEAKRELOCATION_PAGE_SIZE` | `2000` — rows per page where a layer is paged instead |
| `LEAKRELOCATION_TIMEOUT` | `120` — seconds before a request is given up on |
| `LEAKRELOCATION_RETRY_ATTEMPTS` | `4` — attempts per request before a transient failure ends the run |
| `LEAKRELOCATION_RETRY_BACKOFF` | `2` — seconds before the first retry, doubling with jitter |

## Signing in, and what is printed

| Variable | Default |
| --- | --- |
| `LEAKRELOCATION_LOOPBACK_OAUTH` | `1` — sign in via a loopback redirect; `0` uses the out-of-band page |
| `LEAKRELOCATION_LOOPBACK_PORT` | `8080` — must match a redirect URI on the portal app registration |
| `LEAKRELOCATION_LOOPBACK_HOST` | `localhost` |
| `LEAKRELOCATION_LOOPBACK_REDIRECT_URI` | built from the host and port; set it only if the registration lists something else |
| `LEAKRELOCATION_CLIENT_ID` | the portal app registration this project signs in through |
| `LEAKRELOCATION_VERBOSE` | `0` — `1` adds field resolution and setup detail |
| `LEAKRELOCATION_TIMINGS` | `0` — `1` prints elapsed time per stage |

## The map

| Variable | Default |
| --- | --- |
| `LEAKRELOCATION_MAP_MAX_ZOOM` | `28` |
| `LEAKRELOCATION_TILE_MAX_NATIVE_ZOOM` | `19` — the deepest level the tile providers have imagery for |

## Output location

The GeoPackage is written **locally**, under
`<work root>/production_moved_leak_outputs/`. Writing straight to a network
location made every run depend on network write throughput, and a partial write
left the shared copy broken. Set `LEAKRELOCATION_OUTPUT_GPKG` to publish
deliberately when a run looks good.

## How signing in works

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

## The layer cache and newly requested fields

A cache holds the columns that were requested when it was written, and the
refresh is a delta: only records whose `LASTUPDATE` moved are downloaded again.
So adding a field to the request lists does not bring it into an existing cache —
it would arrive for a handful of changed records and be blank for the rest, which
reads like a service that has stopped populating the field.

Each cache therefore stores a signature of the fields the code asked for. When
that signature does not match, the layer is downloaded in full instead, once, and
the run says so:

    historic leaks: the requested fields have changed since this cache was
    written. Refreshing the layer in full so the new fields are populated for
    every record.

The signature is per layer kind, and two things keep it from costing more than it
has to:

- a leak field and a pipe field have separate digests, so collecting one more
  leak column no longer re-downloads 1,274,797 service pipes that were never
  affected by it;
- when the signature does not match, the cache's own columns are checked before
  it is thrown away. A cache that already carries every requested field is kept:

      historic leaks: the requested fields have changed, but this cache already
      carries every one of them. Keeping it.

Adding `ADDRESS` changed the leak signature, so the first run after it
re-downloads the leak layer. Later runs use the cache as before.
