# Updating the leak layer's geometry

> This is the one command in the project that changes something it did not
> create. Read this page before running it with `--apply`.

Everything else here is additive: it downloads, computes, and writes its own
files. This one reaches back and moves the leaks on layer 206 in place, in
production asset records. There is no undo on the far side.

```bat
python scripts\apply_relocated_geometry.py                  dry run
python scripts\apply_relocated_geometry.py --apply          do it
python scripts\apply_relocated_geometry.py --rollback FILE  put them back
```

**It is a dry run unless `--apply` is given**, and `--apply` still asks for the
phrase `MOVE THE LEAKS` to be typed next to the number of features. A flag is
easy to paste from somewhere; a phrase typed beside a count is the last point at
which someone notices the number is wrong.

## It updates geometry, and nothing else

Every feature keeps its GlobalID. An `updates` call is not a delete and an add
— the row is the same row — and a GlobalID is minted when a feature is
*created*, so a workflow that removed and re-inserted a leak would renumber it
and break every downstream reference, silently.

Three things hold that:

| | |
| --- | --- |
| the payload carries one attribute | the object id, and only to say *which* row. `check_geometry_only` refuses anything else on the way out, including a GlobalID — some services will accept one |
| nothing sends `adds` or `deletes` | `applyEdits` takes them in the same call as `updates`, so their absence is asserted rather than assumed |
| the GlobalIDs are read before and checked after | an update *should* not change them; reading them twice is how that stops being an assumption. If any moved, the run says which and exits non-zero |

The object id field is read off the layer rather than assumed to be
`OBJECTID` — a layer is free to call it something else, and the wrong key
identifies no row at all. The rollback file is geometry-only in the same way,
and records the GlobalIDs alongside (without sending them) so the features an
undo puts back can be confirmed to be the same features.

```
Object id field        : OBJECTID
GlobalID field         : GlobalID
This updates geometry only. Every feature keeps its GlobalID.
...
--- Checking the GlobalIDs survived ---
GlobalIDs checked: all 2 unchanged.
```

## What it does before it edits anything

1. pairs each relocated leak with its `OBJECTID` on the layer;
2. **re-reads the current geometry and GlobalIDs from the service** — so the
   rollback restores what is there now, a feature someone has already moved by
   hand is reported rather than overwritten, and there is something to check
   the ids against afterwards;
3. **writes the rollback file**, and stops if it cannot;
4. prints what it would do, and stops unless told otherwise.

The rollback file is an `applyEdits` payload, not a report, so undoing is
running it rather than reconstructing it under pressure:

```bat
python scripts\apply_relocated_geometry.py --rollback ...\leak_geometry_rollback_20261031T0113Z.json --apply
```

## What stops it

| Guard | Why |
| --- | --- |
| moves over `--max-distance` (default 100 ft) | matches the search radius, so a fresh output never trips it. It bites on an output written when the search reached further, where a leak that only matched at 2,900 ft is a record to check rather than an edit to push. `--allow-long-moves` overrides |
| more than `--max-features` (default 25,000) | catches a bad run, not a big one. Raise it deliberately |
| too many features missing from the layer | the output no longer describes this layer, so the edits were computed against something else |
| a leak already at the relocated position | a second run of the same output is a no-op, not a rewrite of every feature |
| a CRS that cannot be established | `applyEdits` takes geometry at face value. Sending analysis-CRS feet to a layer storing degrees writes every leak into the sea, and the service accepts it |

Each batch is one transaction (`rollbackOnFailure`), and every feature's result
is checked — `applyEdits` can return HTTP 200 with half the batch rejected, and
reporting that as success would claim leaks moved that did not. If a batch is
refused mid-run, the message says how many earlier batches went through and
where the rollback file is.

Start with the dry run and read what it says.

## The Maximo leaks

Their geometry lives in the SQL database, not on an ArcGIS layer, so writing it
back is a SQL `UPDATE` rather than `applyEdits`. That is not built: this
script moves the historic leaks only.
