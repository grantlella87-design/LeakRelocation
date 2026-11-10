# The leak sources


There are two sets of leaks and they arrive by completely different routes. Each
has its own entry point and its own GeoPackage; everything between the leaks
being read and the outputs being written is shared.

| | Historic (LMS) | Enterprise (Maximo) |
| --- | --- | --- |
| Command | `python run.py` | `python scripts\maximo_leaks.py` |
| Leaks from | ArcGIS layer 206 `Hist_GasLeak` | one SQL query |
| Material & diameter | `input/HL_SupplementalData.csv`, joined on GlobalID | the same query |
| Leak id | `GlobalID` | `LeakId` from the query |
| Output | `HistoricLeakRelocation.gpkg` | `MaximoLeakRelocation.gpkg` |

Every output row records `LeakSource` — `historic` or `maximo` — so a
GeoPackage says which leak set it holds, and the dashboard names it at the top
of the page. The map carries the Maximo relocations in their own group, off by
default until that output exists.

What they share is everything that matters: the pipe layers, the ASSETTYPE
material decode, the diameter rule, the date rule, the search radii, the three
output layers and the distance dashboard. `relocate()` in the workflow is the
one copy of it, and both entry points call it. A second copy of the matching
would drift from the first and the two leak sets would stop being comparable.

## Enterprise (Maximo) leaks

Because the query returns material and diameter with the leak, there is no
supplemental join here at all — and none of the wrong-attribute risk that join
carried on the historic side.

Three things to set before the first run:

```bat
set LEAKRELOCATION_MAXIMO_DSN=Driver={ODBC Driver 18 for SQL Server};Server=HOST;Database=DB;Trusted_Connection=yes
set LEAKRELOCATION_MAXIMO_QUERY=C:\path\to\your_query.sql
set LEAKRELOCATION_MAXIMO_CRS=EPSG:4326
```

The DSN is read from the environment and **never written to a file or a log**.
Anything that looks like a password is redacted before any message is printed,
including a driver's own connection error — the same discipline the GIS token
has, where only its length and expiry are ever shown.

Then work up to a full run:

```bat
python scripts\maximo_leaks.py --check      connection, query and columns only
python scripts\maximo_leaks.py --preview    read the leaks, relocate nothing
python scripts\maximo_leaks.py              the whole run
python scripts\maximo_leaks.py --diameter fuzzy
```

`--check` runs the query and stops. It is the cheap way to find out that a
column is misnamed, before any pipe layer is downloaded.

### The query is a file, and its column names are a contract

`input/maximo_leaks.sql` is a **template**: the tables in it are placeholders,
the aliases are not. Point `LEAKRELOCATION_MAXIMO_QUERY` at your own copy rather
than editing it in place if the real query should not be committed.

Required — a query missing any of these is refused by name:

| Alias | What it is |
| --- | --- |
| `LeakId` | unique per leak. A second row with the same value is skipped and counted, not merged |
| `X`, `Y` | position, in `LEAKRELOCATION_MAXIMO_CRS` |
| `Material` | the leaking pipe's material, as text |
| `Diameter` | nominal diameter in inches |

Optional — each one left out costs something the run names on screen:
`LeakNumber`, `FacilityType`, `LeakDate`, `Pressure`, `Address`, `City`,
`NearestXStreet`. The two worth watching are `FacilityType`, without which every
leak is searched against both mains and services, and `LeakDate`, without which
the date rule decides nothing.

Everything under the aliases can be anything — tables, joins, views, a stored
procedure. The project only reads the aliases.

### Set the CRS deliberately

`LEAKRELOCATION_MAXIMO_CRS` is what the `X` and `Y` columns are in. It defaults
to `EPSG:4326` — longitude then latitude. If they are MA state plane feet, set
it to `EPSG:2249`.

This is the one setting that fails quietly: coordinates read in the wrong CRS
put every leak somewhere there are no pipes, nothing matches, and a GeoPackage
is still written. A run that relocates nothing now says so and names this as the
likely cause, but it is easier to set it right.

The leaks are projected from this CRS into the pipes' own. Only leaks that were
downloaded from layer 206 are taken to be in layer 206's coordinate system.

### Rows that cannot be used

Counted and reported, never silently dropped and never placed at 0, 0:

| Skipped | Why it cannot be relocated |
| --- | --- |
| no `LeakId` | nothing to key it on |
| a `LeakId` already seen | the first wins; merging would take an arbitrary row's attributes, which is the bug the historic join had |
| no usable `X`/`Y` | nowhere to put it |
| no `Material` or no `Diameter` | nothing to match a pipe on |

### Reporting on it

The distance dashboard is source-agnostic:

```bat
python scripts\relocation_dashboard.py --gpkg ...\MaximoLeakRelocation.gpkg
```

## Historic leaks: the supplemental CSV

`input/HL_SupplementalData.csv` is the leak attribute file, committed so a run does
not depend on a mapped drive, a VPN, or a shared folder that has been reorganised.
It carries **98,464 MA rows** across 33 columns.

It is the only source of leak **material** and **diameter** — layer 206 has
neither, which is why the match cannot be made from the service alone.

Because the file travels with the repository, the header names in the workflow are
now facts rather than guesses, and `tests/test_supplemental_csv.py` checks them
against it offline. What that turned up:

| Field | Column | Notes |
| --- | --- | --- |
| **join key** | `HistoricalLeaksID` | Layer 206's `GlobalID`. Unique on all 98,464 rows. |
| leak number | `LeakNumber` | Carried for labelling and the audit, **not** joined on — see below. `Name` holds the same value on all 98,464 rows. |
| diameter | `Diameter` | Filled on 94.7%. `Abdn_Diameter_Main` and `Abdn_Diameter_Service` are different measurements, not fallbacks. |
| material | `LeakMaterialType` | Filled on 95.8%. The `Abdn_Material*` columns are far sparser and sat behind a name that always resolves. |
| facility | `FacilityType` | `Distribution Main` or `Service`, which is what decides the pipe layers a leak may relocate onto. |
| pressure | — | **There is no pressure column of any kind.** Five names used to be guessed for one. |

### The join is on the GlobalID, not the leak number

**The leak number is not unique in this file.** 25,733 rows (26.1%) share a number
with another row, across 10,990 numbers, and 5,364 of those numbers have rows that
disagree about the material or the diameter. Leak 1000001 is three rows: a Cast
Iron 8" main, a Bare Steel 1.25" service, and a second copy of the main.

Joining on the number therefore gave a leak whichever of its namesakes' rows came
last, and discarded the rest. Measured against the committed file, that was:

| | |
| --- | --- |
| leaks given the wrong material | 4,476 |
| leaks given the wrong diameter | 6,247 |
| leaks given the wrong facility type | 6,285 |
| **leaks wrong in at least one of the three** | **8,425** |
| rows discarded outright | 14,743 |

The facility type is not cosmetic: it decides which pipe layers a leak may
relocate onto, so those 6,285 were being routed by another leak's data.

The join is on `HistoricalLeaksID` — layer 206's `GlobalID` — which is unique on
every row, so all 98,464 rows are kept and each leak gets its own. Both sides are
normalised through `normalize_key`, which strips the braces and upper-cases, so
`{8D0318B7-…}` and `8d0318b7-…` join.

A leak with no `GlobalID`, or a supplemental file without the column, is **not**
quietly joined on the number instead: the run counts it as unmatched, or stops.
Falling back is what produced plausible, wrong output.

The eight material values are `Cast Iron`, `Bare Steel`, `Plastic - MD`, `Coated
Steel`, `Copper`, `Plastic - HD`, `Wrought Iron` and blank. Note that
`Plastic - MD` and `Plastic - HD` are **not** DNV ASSETTYPE domain labels: they
reach a pipe through the material family, so the 15,313 leaks carrying them depend
on `ALLOW_MATERIAL_FAMILY_FALLBACK` being on.
