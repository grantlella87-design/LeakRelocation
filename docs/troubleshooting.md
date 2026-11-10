# Troubleshooting

Install and certificate problems are covered in [Setup](setup.md).


## If a field is empty on the map

An empty column has two causes that need opposite fixes, and they are told apart
with:

```bat
python scripts\field_fill_report.py
python scripts\field_fill_report.py --service
```

The first reads the local caches and the output GeoPackage and reports, per field,
how many rows hold a value. The second asks the service the same question - one
count query per field, no data returned - so:

| What the report shows | What it means |
| --- | --- |
| `not in this source` | the download predates the field. `python run.py --refresh` |
| `0` here, filled on the service | it is being lost after the download — a bug here |
| `0` in both | the service does not populate it. Nothing here can fill it in |

`--service` also asks about the fields this project does **not** request, so when
a configured field turns out to be empty the alternatives are right there. On the
MA leaks, `REVISEDLEAKDATE` and `ADDRESS` came back present but empty on all
98,501 rows, which is why the audit table reads `no_leak_date` for 98% of them —
the date rule has nothing to compare. Layer 206 carries `DISCOVEREDDATE`,
`REPAIREDDATE` and `COMPLETEDDATE` as well, and the report says which of those MA
fills in.

`ADDRESS` and `REVISEDLEAKDATE` were added to the leak query after the first
caches were written, so a cache from before then simply has no such column and the
map says so beside the layer's own name.

## Why a leak field is empty

`ADDRESS` **is** a field on layer 206, it **is** in the `outFields` this project
sends, and the column **is** in the cache — with no value on any of the 98,501 MA
rows. The fill report cannot say which of three things that is, because all three
look identical from inside a MA-only cache. This can:

```bat
python scripts\probe_leak_fields.py
```

It counts each field four ways — with and without the `jurisdiction = 'MA'`
filter, null against empty string — and pulls sample values from rows that
actually have one rather than from the first five rows, which in the middle case
below are all empty. It downloads nothing and writes nothing.

| Verdict | What to do |
| --- | --- |
| the service has it and this project asks for it | it is lost between the two — a bug here |
| populated on the layer, on no `MA` row | nothing in the request can fill it in; the fallback below is the answer |
| empty for every row on the layer | same, and stop asking |

`ADDRESS IS NOT NULL` is true of the empty string, so the probe asks for `<> ''`
as well on text fields — the fill report once counted 92,707 blanks as fully
populated for exactly that reason.

## Where a leak is, when it has no address

`LeakAddress` in the audit table and `LeakLocation` in the map popup are composed
by `src/leakrelocation/leak_location.py`, from one street-level part and one
municipality part, best available of each:

| | street | municipality |
| --- | --- | --- |
| first choice | `ADDRESS` | `CITY` |
| fallback | `NEARESTXSTREET` | `SuppTown` (from the supplemental CSV) |

So a leak reads `12 Elm St / WATERTOWN` when the service has an address and
`OAK ST / WATERTOWN` when it does not. Both callers share that module, so the
popup over a leak and its row in the GeoPackage cannot describe it differently.
This is a fallback for rows that lack a street address — not a replacement for
one.

## If the network drops a request

A layer download is hundreds of POSTs over many minutes through the proxy, so one
being dropped is ordinary. It used to be fatal:

    RuntimeError: distribution pipes delta: objectId POST batch 20/322 failed:
    ('Connection aborted.', ConnectionResetError(10054, 'An existing connection
    was forcibly closed by the remote host'))

with the other layer 584 batches of 588 through, and all of it thrown away.

Transient failures are now retried — a reset, a dropped or refused connection, a
timeout, and the statuses that mean *not now* (429, 502, 503, 504). Four attempts
by default, waiting 2s then 4s then 8s, each with jitter so eight parallel batches
do not all come back in the same instant. Each retry says so:

    WARNING: POST .../query: ConnectionError: ('Connection aborted.', ...).
    Attempt 1 of 4; retrying in 2.3s

HTTP 500 is **not** retried: ArcGIS returns a genuine query error that way, and
repeating a bad query only makes it fail more slowly.

Workers also reuse one connection each instead of opening a new TLS connection per
batch — 588 handshakes for one layer, eight at a time, was itself part of why the
proxy was resetting them.

If a batch still fails all four attempts, the run stops and names the two knobs
worth turning: `LEAKRELOCATION_DOWNLOAD_WORKERS=2` for a network that dislikes
parallelism, and `LEAKRELOCATION_RETRY_ATTEMPTS` for one that is merely flaky.

## If a run is slow

```bat
set LEAKRELOCATION_TIMINGS=1
python run.py --no-view
```

prints elapsed time per stage — supplemental CSV, layer loading, CRS
normalisation, index building, matching, writing — so the slow stage is
identified rather than guessed at.

`LEAKRELOCATION_VERBOSE=1` adds which field each name resolved to, and the
request each layer was sent. [Configuration](configuration.md) shows how to
print every setting a run has resolved.
