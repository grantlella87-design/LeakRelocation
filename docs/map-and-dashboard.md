# The map and the distance dashboard


## The map

`run.py` serves the map at <http://127.0.0.1:8765/> (`--port` moves it). The
layers sit under two parents,
each carrying everything that belongs to one kind of leak:

| MAINS | SERVICES |
| --- | --- |
| Main lines — material coded | Service lines — material coded |
| Main leaks — original location | Service leaks — original location |
| Main leaks — relocated location | Service leaks — relocated location |
| Main leaks — connecting line | Service leaks — connecting line |

Plus **abandoned / retired pipe** under OTHER. Ticking a parent switches its four
children together; each child still toggles on its own, and the parent shows a
dash when only some are on. Mains and services are independent — turning one off
leaves the other exactly as it was.

The split follows the same routing the matcher used, so the map agrees with the
output: relocated points and connecting lines are one GeoPackage layer each, split
on `LinkedLayer`, and the original leaks are one cache split on the supplemental
facility type. A facility type that names neither is searched against both by the
workflow, so it appears under both here rather than being hidden from one.

Leaflet's own layer control is a flat list and cannot express a parent, so the
data layers use a small grouped control instead; the basemaps and the two
reference shapes stay in Leaflet's.

Each leak carries its street `ADDRESS` from layer 206, in the attribute table and
in the popup below the leak number. The relocated points and the audit table
carry the same value as `LeakAddress`. Nothing matches on it — it is there so a
leak can be identified by where it is and not only by its number.

`run.py` always runs it. **Every** layer degrades on its own: a layer whose cache
or GeoPackage is not there is empty and says why, in the terminal and in the info
box on the page — `Main lines - material coded: not available - Missing cache
file: … Download it with: python run.py --no-view`. So a fresh checkout, which has
no caches at all, still opens the viewer instead of a traceback.

The abandoned layer reads `retired_pipes.pkl.gz`, which the workflow writes when
it loads layer 62. Its materials need layer 62's subtype domains, so run
`python scripts\describe_layer.py --save` once to put them in `reference/`.

Clicking a row in the attribute table shows that feature's attributes on the map
and leaves the row highlighted, and both survive the layer reload that a pan or
zoom triggers. The selection is remembered by OBJECTID rather than by the table
row or the Leaflet layer, because the map throws both away every time it moves.

Clicking anywhere that is not a row and not a feature — the map background, or
the empty space in the pane below the last row — drops the highlight and closes
the popup. Sorting and clicking inside an open popup are not outside clicks and
keep the selection.

Material comes from the decoded `ASSETGROUP + ASSETTYPE` subtype domains, which
is what the matching uses too, so the map and the match agree. Colours live in
one place, `MATERIAL_COLORS` in `map_server.py`; the page's palette is generated from
it, so editing it changes the map.

Both pipe layers are **on when the page opens**, along with the leaks and the
relocated output. Every layer can be switched off from the control top right.

The pipe layers are read from the downloaded caches and served by bounding box, a
capped number of features at a time, re-fetched as the map moves. They are far
too large to write into a single GeoJSON and hand to a browser. With the whole
state in view the cap is reached and the info box says `TRUNCATED - zoom in`;
that is the cap, not missing data.

The map zooms to **level 28**, as deep as Leaflet goes without complaint — 22,
24, 25, 26 and 28 were each checked in a browser and all zoomed and drew cleanly.
The tile providers only have imagery to 19, so past that the last tile is upscaled
and looks soft while the pipes stay sharp, because they are vectors rather than
baked into the tiles. For scale a pixel is about 4 cm at zoom 22, 9 mm at 24 and
0.6 mm at 28 — well past survey accuracy, so the deep levels are for separating
overlapping lines, not for measuring. `LEAKRELOCATION_MAP_MAX_ZOOM` changes it.

Leaflet is committed under `vendor/leaflet/`, so the page has it with no internet
and nothing to build first. The map falls back to a CDN only if that copy is
missing, and warns when it does.

The map can also be served on its own, without `run.py`:

```bat
python run.py --view-only
python src\leakrelocation\map_server.py --port 8800 --open
```

## Attribute pane

The relocation viewer docks an attribute table under the map, one tab per
layer:

- Click a row to zoom to that feature and open its popup.
- Click a feature on the map to select and scroll to its row.
- Click the map background, or the pane below the rows, to clear the selection.
- Click a column header to sort; numeric columns sort numerically.
- Filter rows with the search box (matches any attribute value).
- **Hide** collapses the pane and gives the space back to the map.

Rendering is capped at 500 rows per view — a production layer holds tens of
thousands of features and an uncapped table would lock the browser. The row
count shows `showing N of M` when the cap applies; narrow the set with the
filter.

## The relocation-distance dashboard

How far each leak moved is the only measure of how much its original record was
trusted. A few feet is a snap onto the right main; two thousand feet is not a
relocation but a guess that happened to find a pipe. The dashboard draws that
line wherever the reader wants it.

It is served by the map server, so `python run.py` already has it — the
**Distance dashboard** link sits top-left on the map, at
<http://127.0.0.1:8765/dashboard>. For a copy to keep or send on:

```bat
python scripts\relocation_dashboard.py
python scripts\relocation_dashboard.py --open
```

That writes `relocation_distance_dashboard.html` beside the GeoPackage and
prints the headline numbers to the terminal.

**The sliding scale** is the centre of it: drag it and the page reports, exactly,
how many relocations fall on each side — overall and split by pipe layer. It
steps in 0.1 ft up to 100 ft and in 5 ft beyond, because production distances run
from 0.036 ft to whatever the search radius allows, and a flat grid would put a
third of the data in one bin. Every figure is a stored count, never an
interpolation, and the fixed-threshold table is computed separately from the
curve so the two disagreeing would fail a test rather than mislead a reader.

Alongside it:

| Panel | What it answers |
| --- | --- |
| Median, p90, p95, p99, furthest | the report-ready "95% of leaks moved less than X ft" |
| Where the relocations fall | the shape of the distribution, with the threshold drawn on it |
| Share within a distance | the cumulative curve, overall and per pipe layer |
| Which pass found the pipe | how many needed the search widened past its first 100 ft — a leak with nothing eligible nearby is a different result from a close snap |
| By pipe layer / facility / leak material | mains are sparser than services, so those rows differing is expected; a material whose median move is far above the rest is a sign its records are placed less reliably |
| Material agreement | what share sit on a pipe of exactly the recorded material, against the family fallback |
| Why a leak did not match | the leaks that were never relocated, and so appear in no distance figure |
| Date check | `no_leak_date` on 98% of a production run, marked so the panel is not read as a clean bill of health |
| The furthest 50 | the review queue, row by row |

Nothing is fetched from the internet — no CDN, no web font — for the same reason
Leaflet is vendored: the proxy would not serve it. The file embeds aggregate
counts, group statistics and those 50 rows, not the 90,987-row table, so it is
safe to send and small enough to mail (~130 KB).
