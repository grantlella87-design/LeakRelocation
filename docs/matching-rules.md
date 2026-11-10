# The matching rules

What decides whether a leak relocates, and onto which pipe. Every rule here is
applied by `src/leakrelocation/matching.py` and recorded per leak in the audit
table, so an output can always say why a leak did or did not move.

## In short

**Material** comes from decoded `ASSETGROUP + ASSETTYPE`. Do not use the DNV
`material` Grade field as the material class for relocation assessment.

**A leak relocates only onto a pipe within 100 ft.** The matcher makes one
pass at `INITIAL_RADIUS_FT` and stops at `MAX_RADIUS_FT`; with both at 100 ft
there is no widening. A leak with no eligible pipe in reach is reported as
unmatched rather than paired with something far away. See
[The search radius](#the-search-radius).

**Diameter matching has two modes, and they write different files.** `exact` is
the default and the original rule. `fuzzy` also accepts a pipe one nominal size
up or down. See [Diameter matching](#diameter-matching).

**A leak only relocates onto a pipe that existed when it was recorded.** The date
is `REVISEDLEAKDATE` on layer 206, and which end of the pipe's life is checked
depends on the layer:

| Layer | Rule |
| --- | --- |
| 6 Distribution Pipe, 7 Service Pipe | `REVISEDLEAKDATE` **after** `CREATIONDATE` |
| 62 Pipeline Line (A), on the MA service | `REVISEDLEAKDATE` **before** `dateretired` |

Both comparisons are strict. A leak with no revised date, or a pipe missing the
date its rule needs, is matched anyway and the audit table records which — a data
gap does not silently remove a relocation. The `DateCheck` column reads `ok` when
the dates were actually compared.

`CREATIONDATE` is when the GIS record was created, not when the pipe was laid;
those layers also carry `installationdate` and `inservicedate`. A pipe installed
in 1960 and migrated into the system in 2015 has `CREATIONDATE` 2015, so a 2010
leak fails the check even though the pipe was in the ground.

## The search radius

```
Search radius: 100 ft, widening by 100 ft to 100 ft (one pass, no widening)
```

A leak relocates onto the nearest eligible pipe within **100 ft**. If there
isn't one, it is reported as unmatched — `No exact diameter/material/pressure
match found within 100 ft` in the audit's `NoMatchReason`.

This was **3,000 ft**, widening in 100 ft steps. That meant an output could
contain a leak "relocated" most of a mile onto the only pipe in reach — a
pairing that happened to find something rather than a relocation anyone would
stand behind. The dashboard's review queue existed largely to surface those.
Unmatched is the honest answer, and it leaves the leak for someone to look at.

Expect fewer relocations and more no-matches than the previous output had. The
`Why a leak did not match` panel accounts for them.

| | |
| --- | --- |
| `LEAKRELOCATION_MAX_RADIUS_FT` | how far the search reaches. Default 100 |
| `LEAKRELOCATION_INITIAL_RADIUS_FT` | the first pass. Default 100 |
| `LEAKRELOCATION_RADIUS_INCREMENT_FT` | how much each pass widens by. Default 100 |

Widening it again is a setting, not a commit. The audit records
`SearchRadiusFt` per row either way, so a wider run still says which pass found
each pipe, and the dashboard still groups them.

Reports made from an **older, wider output** are not truncated to the current
setting — the threshold table and the distance curve follow the data as well as
the configuration, so a 3,000 ft GeoPackage still reports its whole range.

## Leak dates, and why the rule was deciding nothing

`REVISEDLEAKDATE` came back empty on all 98,501 MA rows, so `DateCheck` reads
`no_leak_date` for 98.1% of the audit: a leak can be matched to a pipe recorded
years after it was reported, and the only sign is a column nobody reads.

`DISCOVEREDDATE`, `REPAIREDDATE` and `COMPLETEDDATE` sit on the same row.

```bat
python run.py --leak-date-fallback
```

They are **always collected and always counted**, so every run reports how many
leaks have no revised date but do carry another one — the size of the gap is
visible whether or not you act on it. They are only *used* when asked for,
because using them changes which leaks relocate: a leak that matched unchecked
can be refused once there is a date to check it against. That is the rule
working, not a regression.

Every output row records `LeakDateSource` — the column that supplied its date,
or blank when nothing did — and the dashboard has a panel for it.

## Diameter matching

A leak carries a diameter from the supplemental CSV and a pipe carries one from
the service, and by default they have to be the same nominal size. That is
right when both records are good and it is the reason some leaks never relocate:
there is a pipe of the right material a few feet away, and its diameter is one
size off.

`fuzzy` widens the rule to the next nominal size up or down.

```bat
python run.py                    the exact rule (unchanged)
python run.py --diameter fuzzy   one nominal size up or down
```

The nominal sizes, in inches:

    1   1¼   1½   2   4   6   8   12   16   20   24   30   36   42   48

### Sizes that are not on the list

The data carries diameters the list does not: 0.5 and 0.75 on service pipes, 3
and 10 on mains. The same rule applies to them, and the rule is stated so that
it can:

> two diameters match when there is **no nominal size strictly between them**.

For a listed size that is exactly "one size up or down" — 8″ reaches 6″ and 12″,
and not 16″, because 12″ sits between 8″ and 16″. For an unlisted size it falls
out of the same sentence: the 10″ pipe reaches 8″ and 12″, the 3″ reaches 2″ and
4″, and 0.75″ and 1″ reach each other.

It is written that way rather than as "look up my two neighbours" because the
neighbour form is **asymmetric** at the uncommon sizes. A 0.75″ leak would reach
1″ while a 1″ leak would not reach back down to 0.75″, since 1″ is the bottom of
the list. "Nothing listed in between" gives the same answer whichever side you
ask from.

### An exact diameter always wins

Within the widened rule, an exact diameter outranks an adjacent one **at any
distance** — a 900 ft exact match beats a 20 ft one-size-off match. So every
leak the strict run relocated keeps the same pipe, and the difference between
the two outputs is only the leaks the widening rescued. That is what makes the
comparison readable: `Lost` and `Moved to another pipe` should both be zero, and
the dashboard says so in red if they are not.

Set `LEAKRELOCATION_PREFER_EXACT_DIAMETER=0` to take the nearest pipe in the
size window instead. Existing relocations can change under that setting.

### Both outputs, side by side

Each rule writes its own GeoPackage, so running both leaves both on disk:

| Rule | GeoPackage |
| --- | --- |
| `exact` | `HistoricLeakRelocation.gpkg` |
| `fuzzy` | `HistoricLeakRelocation_fuzzy_diameter.gpkg` |

Every audit row records `DiameterMode` — which rule produced it — and
`DiameterMatch`, one of `exact`, `one_size_up` or `one_size_down`. So a
GeoPackage says which rule wrote it without anyone having to remember which file
is which, and a single relocation says whether its diameter was a fact or an
assumption. `DiameterMatch` is on the relocated points too, so the map popup
shows it.

### Switching between them

The map server serves both and links each to the other:

    http://127.0.0.1:8765/dashboard?mode=exact
    http://127.0.0.1:8765/dashboard?mode=fuzzy

The map's top-left control lists both; a rule that has not been run yet shows
the command in its tooltip rather than a dead link. On each page a bar names the
rule in words, and a panel gives the difference between the two outputs:

| Figure | Meaning |
| --- | --- |
| Gained | relocated under the widened rule and not under the strict one — the whole point |
| Lost | relocated under the strict rule and not the widened one. Should be zero |
| Moved to another pipe | matched in both, to different pipes. Should be zero |

The gained leaks are then broken down by how much slack they took, which pipe
layer found them, the size step (`8″ → 12″`), and how far they moved against the
median of the whole output. They are the rows where the diameter is an
assumption, so they are worth reviewing as a set.

The comparison is oriented strict → widened whichever page is open, so the same
pair of files cannot read as "+1,104 gained" from one side and "1,104 lost" from
the other.

For files to keep or send on:

```bat
python scripts\relocation_dashboard.py --mode fuzzy
python scripts\relocation_dashboard.py --both
```

Both are paired on the leak's own id, not its number: 25,733 rows of the
supplemental file share a leak number, so joining on the number would compare
different leaks.
