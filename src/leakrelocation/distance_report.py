"""How far each leak was relocated, reduced to numbers a dashboard can draw.

Reads the `leak_relocation_audit` layer written by write_outputs() and produces
one report dictionary. No I/O and no HTML: the caller reads the GeoPackage and
the caller renders. That keeps this testable against a frame built by hand,
which is how the thresholds below are checked.

What it is for. A relocation moves a leak from where it was recorded onto the
pipe it belongs to, and the distance it moved is the only measure of how much
the original record was trusted. A few feet is a snap onto the right main. Two
thousand feet is not a relocation, it is a guess that happened to find a pipe,
and it needs a person to look at it. The whole point of the report is to draw
that line wherever the reader wants it drawn, and to say how many rows fall on
each side.

Everything is in feet, because DistanceFt is.
"""
# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member. See leakrelocation/output.py for the full note.
import os as _os
import sys as _sys

_PACKAGE_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PACKAGE_PARENT not in _sys.path:
    _sys.path.insert(0, _PACKAGE_PARENT)

import datetime as dt
import math

from leakrelocation import schema

# A snap of less than this is a leak that was already sitting on its pipe. Worth
# counting separately: it is the difference between "the records agreed" and "we
# moved it".
ON_PIPE_FT = 1.0

# The thresholds always reported, whatever the slider is set to, so a written
# summary has fixed numbers to quote. 100 ft is the first search pass
# (config.INITIAL_RADIUS_FT), which makes it the most meaningful line on the
# scale: past it, the matcher had to widen its search to find anything at all.
THRESHOLDS_FT = (1.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0)

PERCENTILES = (50, 75, 90, 95, 99)

# Bin edges for the cumulative curve. The slider is an index into this list, so
# the count it reports is exact rather than interpolated - no "about 94%".
#
# Two resolutions because the data has two scales: most relocations are a few
# feet (0.036 ft and 16.1 ft are real values from a production run) while the
# tail runs to the 3,000 ft maximum radius. A flat 0.1 ft grid to 3,000 would be
# 30,000 numbers per series to serve a part of the range holding almost nothing.
FINE_LIMIT_FT = 100.0
FINE_STEP_FT = 0.1
COARSE_STEP_FT = 5.0

# How many of the furthest relocations to list by row. These are the review
# queue - the rows a person actually has to open - so it is a readable number
# rather than an export.
FURTHEST_ROWS = 50


def curve_edges(max_ft):
    """Right-hand bin edges, fine near zero and coarse out to `max_ft`."""
    limit = max(float(max_ft), FINE_STEP_FT)
    edges = []
    step_count = int(round(min(limit, FINE_LIMIT_FT) / FINE_STEP_FT))
    for index in range(step_count + 1):
        edges.append(round(index * FINE_STEP_FT, 1))
    edge = FINE_LIMIT_FT
    while edge < limit:
        edge = round(edge + COARSE_STEP_FT, 1)
        edges.append(edge)
    return edges


def cumulative(values, edges):
    """How many values are at or below each edge.

    Cumulative rather than per-bin because that is what the question needs: the
    slider asks "how many are within this far", and reading it off a cumulative
    array is one lookup instead of a running sum in the browser.
    """
    ordered = sorted(float(value) for value in values)
    counts = []
    position = 0
    for edge in edges:
        while position < len(ordered) and ordered[position] <= edge:
            position += 1
        counts.append(position)
    return counts


def histogram(values, edges):
    """Per-bin counts, for drawing the shape of the distribution."""
    running = cumulative(values, edges)
    return [running[0]] + [running[i] - running[i - 1]
                           for i in range(1, len(running))]


def percentile(ordered, fraction):
    """Linear-interpolated percentile of an already sorted list."""
    if not ordered:
        return None
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return float(ordered[low])
    return float(ordered[low] + (ordered[high] - ordered[low]) * (position - low))


def numbers(series):
    """The finite numeric values of a column, as a plain sorted list.

    Distances arrive from a GeoPackage read, so a column can be object dtype, can
    carry None for the unmatched rows, and can carry NaN. Anything that is not a
    finite number is not a distance and is dropped rather than counted as zero -
    counting it as zero would report an unmatched leak as a perfect snap.
    """
    if series is None:
        return []
    values = []
    for value in series.tolist():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            values.append(number)
    return sorted(values)


def summarise(values):
    """The one-line statistics for a set of distances."""
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "min": None, "max": None, "mean": None,
                "median": None, "p90": None, "p95": None, "on_pipe": 0}
    return {
        "count": len(ordered),
        "min": ordered[0],
        "max": ordered[-1],
        "mean": sum(ordered) / len(ordered),
        "median": percentile(ordered, 0.50),
        "p90": percentile(ordered, 0.90),
        "p95": percentile(ordered, 0.95),
        "on_pipe": sum(1 for value in ordered if value <= ON_PIPE_FT),
    }


def label(value, fallback="(blank)"):
    """A groupable label, with blanks and nulls collapsed into one bucket."""
    if value is None:
        return fallback
    try:
        if math.isnan(value):
            return fallback
    except TypeError:
        pass
    text = str(value).strip()
    return text if text else fallback


def grouped(frame, column, distance_column=schema.DISTANCE_FT, limit=None):
    """Distance statistics per distinct value of `column`, biggest group first.

    Returns [] when the column is absent, so a report built from an older
    GeoPackage is short rather than broken.
    """
    if column not in frame.columns:
        return []
    buckets = {}
    for value, distance in zip(frame[column].tolist(),
                               frame[distance_column].tolist(), strict=False):
        try:
            number = float(distance)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(number):
            continue
        buckets.setdefault(label(value), []).append(number)
    rows = [{"name": name, **summarise(values)} for name, values in buckets.items()]
    rows.sort(key=lambda row: (-row["count"], row["name"]))
    return rows[:limit] if limit else rows


def counted(frame, column, limit=None):
    """Row counts per distinct value, for the columns that are not distances."""
    if column not in frame.columns:
        return []
    buckets = {}
    for value in frame[column].tolist():
        name = label(value)
        buckets[name] = buckets.get(name, 0) + 1
    rows = [{"name": name, "count": count} for name, count in buckets.items()]
    rows.sort(key=lambda row: (-row["count"], row["name"]))
    return rows[:limit] if limit else rows


def matched_rows(frame):
    """The rows that were relocated, and so the rows that have a distance.

    Preferring MatchStatus to "DistanceFt is not null" because the two disagreeing
    is itself worth knowing, and the caller reports it as a warning.
    """
    if schema.MATCH_STATUS in frame.columns:
        status = frame[schema.MATCH_STATUS].astype(str).str.strip().str.casefold()
        return frame[status == "matched"]
    return frame[frame[schema.DISTANCE_FT].notna()]


def furthest(frame, rows=FURTHEST_ROWS):
    """The longest relocations, row by row - the queue a person has to review.

    Row level and deliberately short. The full table is the GeoPackage; what a
    reader needs here is the handful at the far end, because a leak that moved
    2,900 ft is a record to check rather than a statistic.
    """
    wanted = [
        schema.LEAK_KEY, "LeakAddress", schema.DISTANCE_FT, schema.LINKED_LAYER,
        schema.LEAK_MATERIAL, schema.PIPE_MATERIAL, schema.LEAK_DIAMETER,
        schema.PIPE_DIAMETER, "SearchRadiusFt", "LeakDate",
    ]
    present = [name for name in wanted if name in frame.columns]
    if schema.DISTANCE_FT not in present:
        return []
    ordered = frame[present].copy()
    ordered["__d"] = [
        float(value) if _is_number(value) else float("-inf")
        for value in ordered[schema.DISTANCE_FT].tolist()
    ]
    ordered = ordered[ordered["__d"] > float("-inf")]
    ordered = ordered.sort_values("__d", ascending=False).head(rows)
    out = []
    for record in ordered.drop(columns="__d").to_dict("records"):
        out.append({key: _plain(value) for key, value in record.items()})
    return out


def _is_number(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _plain(value):
    """A JSON-safe value. NaN is not JSON, and str(nan) is the word "nan"."""
    if value is None:
        return None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (int, bool, str)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return number if math.isfinite(number) else None


def material_agreement(frame):
    """How many relocations put a leak on a pipe of a different material.

    The matcher may fall back from an exact material to the material family
    (config.ALLOW_MATERIAL_FAMILY_FALLBACK), so an exact/inexact split says how
    much of the output rests on that fallback. Inexact is not wrong - a family
    match is a deliberate rule - but it is a weaker claim than an exact one and
    should not be invisible.
    """
    if not {schema.LEAK_MATERIAL, schema.PIPE_MATERIAL} <= set(frame.columns):
        return None
    exact = different = unknown = 0
    for leak, pipe in zip(frame[schema.LEAK_MATERIAL].tolist(),
                          frame[schema.PIPE_MATERIAL].tolist(), strict=False):
        left, right = label(leak, ""), label(pipe, "")
        if not left or not right:
            unknown += 1
        elif left.casefold() == right.casefold():
            exact += 1
        else:
            different += 1
    return {"exact": exact, "family_or_other": different, "unknown": unknown}


# How the widening search passes are grouped for reporting. The matcher starts
# at INITIAL_RADIUS_FT and widens by RADIUS_INCREMENT_FT, so a 3,000 ft maximum
# is thirty distinct radii - thirty table rows, twenty-odd of them a fraction of
# a percent. The grouping keeps the first pass on its own, because that is the
# only one that means "there was a pipe right there", and buckets the rest by
# how far the search had to reach.
RADIUS_BUCKETS = (
    (100.0, "100 ft (first pass)"),
    (500.0, "200 to 500 ft"),
    (1000.0, "600 to 1,000 ft"),
    (2000.0, "1,100 to 2,000 ft"),
    (float("inf"), "Beyond 2,000 ft"),
)


def radius_passes(frame):
    """How many relocations were found on each widening pass of the search.

    Everything past the first pass is a leak with no eligible pipe nearby, which
    is a different kind of result from a close snap even when the final distance
    happens to be modest.
    """
    if "SearchRadiusFt" not in frame.columns:
        return []
    buckets = {name: [] for _, name in RADIUS_BUCKETS}
    unknown = []
    for radius, distance in zip(frame["SearchRadiusFt"].tolist(),
                                frame[schema.DISTANCE_FT].tolist(), strict=False):
        if not _is_number(distance):
            continue
        if not _is_number(radius):
            unknown.append(float(distance))
            continue
        value = float(radius)
        for limit, name in RADIUS_BUCKETS:
            if value <= limit:
                buckets[name].append(float(distance))
                break
    rows = [{"name": name, **summarise(buckets[name])}
            for _, name in RADIUS_BUCKETS if buckets[name]]
    if unknown:
        rows.append({"name": "Not recorded", **summarise(unknown)})
    return rows


def diameter_mode_of(audit):
    """Which diameter rule wrote this output, read off the rows themselves.

    Recorded per row by the workflow, so a GeoPackage says which rule produced
    it rather than the reader having to remember which file is which.
    """
    if schema.DIAMETER_MODE not in audit.columns or not len(audit):
        return None
    seen = {label(value, "") for value in audit[schema.DIAMETER_MODE].tolist()}
    seen.discard("")
    if len(seen) == 1:
        return seen.pop()
    return "/".join(sorted(seen)) if seen else None


def build_report(audit, source="", max_radius_ft=None, compare_audit=None,
                 compare_source=""):
    """The whole report, from the audit frame alone.

    Given `compare_audit` - the other diameter rule's output - it also carries
    the difference between the two, which is what makes switching between them
    reviewable rather than a matter of trust.
    """
    warnings = []
    total = len(audit)
    if schema.DISTANCE_FT not in audit.columns:
        raise KeyError(
            f"The audit layer has no {schema.DISTANCE_FT} column, so there is no "
            f"relocation distance to report. Present: {sorted(audit.columns)}")

    matched = matched_rows(audit)
    distances = numbers(matched[schema.DISTANCE_FT])
    unmatched_count = total - len(matched)

    if len(distances) != len(matched):
        warnings.append(
            f"{len(matched) - len(distances):,} rows are marked Matched but carry "
            f"no usable {schema.DISTANCE_FT}. They are counted as audited and "
            f"excluded from every distance figure.")

    stats = summarise(distances)
    ordered = distances
    top = max(max_radius_ft or 0.0, stats["max"] or 0.0)
    edges = curve_edges(top)

    series = {"All relocations": cumulative(ordered, edges)}
    shapes = {"All relocations": histogram(ordered, edges)}
    if schema.LINKED_LAYER in matched.columns:
        for row in grouped(matched, schema.LINKED_LAYER):
            subset = [
                float(distance)
                for value, distance in zip(matched[schema.LINKED_LAYER].tolist(),
                                           matched[schema.DISTANCE_FT].tolist(),
                                           strict=False)
                if label(value) == row["name"] and _is_number(distance)
            ]
            series[row["name"]] = cumulative(subset, edges)
            shapes[row["name"]] = histogram(subset, edges)

    thresholds = []
    for limit in THRESHOLDS_FT:
        under = sum(1 for value in ordered if value <= limit)
        thresholds.append({
            "ft": limit,
            "at_or_under": under,
            "over": len(ordered) - under,
            "pct_at_or_under": (100.0 * under / len(ordered)) if ordered else None,
        })

    if not ordered:
        warnings.append(
            "No row in the audit layer carries a relocation distance, so every "
            "figure below is empty. Run the workflow to write the outputs: "
            "python run.py")

    comparison = None
    if compare_audit is not None and len(compare_audit):
        mine = diameter_mode_of(audit) or "this run"
        theirs = diameter_mode_of(compare_audit) or "the other run"
        # Strict first, always. Which output is on screen must not change the
        # sign of the difference.
        if stricter_first(mine, theirs):
            comparison = compare_outputs(audit, compare_audit, mine, theirs)
        else:
            comparison = compare_outputs(compare_audit, audit, theirs, mine)
        if comparison.get("usable") and comparison["lost"]:
            warnings.append(
                f"{comparison['lost']:,} leaks relocated under {mine} and not "
                f"under {theirs}. Widening the diameter rule is meant to add "
                f"relocations, never remove them, so this is worth "
                f"investigating before either output is used.")
        if comparison.get("usable") and comparison["moved_to_another_pipe"]:
            comparison_note = (
                f"{comparison['moved_to_another_pipe']:,} leaks matched in both "
                f"runs but to a different pipe. With PREFER_EXACT_DIAMETER on "
                f"an exact diameter outranks an adjacent one at any distance, "
                f"so this should be zero.")
            warnings.append(comparison_note)

    return {
        "generated_utc": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "source": str(source),
        "run_utc": _run_stamp(audit),
        "unit": "ft",
        "totals": {
            "audited": total,
            "relocated": len(ordered),
            "unmatched": unmatched_count,
            "match_rate_pct": (100.0 * len(matched) / total) if total else None,
        },
        "distance": {
            **stats,
            "percentiles": {f"p{p}": percentile(ordered, p / 100.0)
                            for p in PERCENTILES},
            "on_pipe_ft": ON_PIPE_FT,
        },
        "thresholds": thresholds,
        "curve": {"edges": edges, "series": series},
        "histogram": {"edges": edges, "series": shapes},
        "by_layer": grouped(matched, schema.LINKED_LAYER),
        "by_leak_material": grouped(matched, schema.LEAK_MATERIAL),
        "by_facility": grouped(matched, "FacilityType"),
        "by_radius": radius_passes(matched),
        "material_agreement": material_agreement(matched),
        "diameter_mode": diameter_mode_of(audit),
        "diameter_match": counted(matched, schema.DIAMETER_MATCH),
        "by_diameter_match": grouped(matched, schema.DIAMETER_MATCH),
        "date_check": counted(matched, "DateCheck"),
        "no_match_reasons": counted(audit[audit.index.isin(
            audit.index.difference(matched.index))], "NoMatchReason"),
        "furthest": furthest(matched),
        "comparison": comparison,
        "compare_source": str(compare_source),
        "warnings": warnings,
    }


# The leak identity two outputs are compared on. LeakKey is the leak *number*,
# which is not unique - 25,733 rows of the supplemental file share a number with
# another row - so joining two runs on it would pair up different leaks. The
# OBJECTID is one row of layer 206, and the GlobalID is its stable id.
COMPARE_KEYS = ("LeakOID", "LeakGlobalID", schema.LEAK_KEY)

# How permissive each diameter rule is. The comparison is always oriented from
# the stricter rule to the wider one, whichever output the reader happens to
# have open - otherwise the same pair of files reads as "+1,104 gained" from one
# page and "1,104 lost" from the other, and the second of those looks like an
# alarm when nothing is wrong.
MODE_STRICTNESS = {"exact": 0, "fuzzy": 1}


def stricter_first(left_mode, right_mode):
    """True when `left_mode` is the stricter of the two, so it is the baseline."""
    return (MODE_STRICTNESS.get(left_mode, 99)
            <= MODE_STRICTNESS.get(right_mode, 99))


def compare_key(frame):
    """Which column to join two outputs on, most trustworthy first."""
    for name in COMPARE_KEYS:
        if name in frame.columns:
            return name
    return None


def _matched_by_key(audit, key):
    """key -> the matched row's figures, for one output."""
    rows = matched_rows(audit)
    out = {}
    for record in rows[[
            column for column in
            (key, schema.DISTANCE_FT, schema.LINKED_LAYER, "MatchedPipeOID",
             schema.DIAMETER_MATCH, schema.LEAK_DIAMETER, schema.PIPE_DIAMETER)
            if column in rows.columns]].to_dict("records"):
        identity = label(record.get(key), "")
        if identity:
            out[identity] = record
    return out


def same_identity(left, right):
    """Whether two id values name the same thing, across dtypes.

    A pipe OID read from an output whose column holds a null comes back as a
    float and one from a column of whole numbers comes back as an int, so 900.0
    and 900 are the same pipe arriving from two files. Comparing their text
    forms made every unchanged relocation look as though it had moved to
    another pipe - 90,987 false findings on a real pair of outputs.
    """
    if _is_number(left) and _is_number(right):
        return float(left) == float(right)
    return label(left, "") == label(right, "")


def compare_outputs(base_audit, other_audit, base_label="exact",
                    other_label="fuzzy"):
    """What changed between two runs of different diameter rules.

    This is the whole point of having both outputs: not two sets of numbers but
    the difference between them. Keyed per leak, so "3,412 more leaks relocated"
    is backed by which leaks, how far they moved, and how much diameter slack
    each one took.

    `lost` and `moved_to_another_pipe` should both be zero while
    PREFER_EXACT_DIAMETER is on, because an exact diameter outranks an adjacent
    one at any distance. They are counted rather than assumed: if either is not
    zero the widened run has taken a relocation away from the strict one, and
    that is the one outcome nobody would want to discover from a map.
    """
    base_key = compare_key(base_audit)
    other_key = compare_key(other_audit)
    if not base_key or base_key != other_key:
        return {"usable": False,
                "why": "The two outputs carry no common leak identity column, "
                       "so their rows cannot be paired."}

    base = _matched_by_key(base_audit, base_key)
    other = _matched_by_key(other_audit, other_key)

    gained_keys = sorted(set(other) - set(base))
    lost_keys = sorted(set(base) - set(other))
    both_keys = sorted(set(base) & set(other))

    gained = [other[key] for key in gained_keys]
    gained_distances = [float(row[schema.DISTANCE_FT]) for row in gained
                        if _is_number(row.get(schema.DISTANCE_FT))]

    changed_pipe = []
    for key in both_keys:
        left, right = base[key], other[key]
        if "MatchedPipeOID" not in left or "MatchedPipeOID" not in right:
            continue
        if not same_identity(left["MatchedPipeOID"], right["MatchedPipeOID"]):
            changed_pipe.append({
                "key": key,
                "from_pipe": _plain(left["MatchedPipeOID"]),
                "to_pipe": _plain(right["MatchedPipeOID"]),
                "from_ft": _plain(left.get(schema.DISTANCE_FT)),
                "to_ft": _plain(right.get(schema.DISTANCE_FT)),
            })

    tiers = {}
    for row in gained:
        tiers[label(row.get(schema.DIAMETER_MATCH))] = \
            tiers.get(label(row.get(schema.DIAMETER_MATCH)), 0) + 1

    sizes = {}
    for row in gained:
        leak = row.get(schema.LEAK_DIAMETER)
        pipe = row.get(schema.PIPE_DIAMETER)
        if not (_is_number(leak) and _is_number(pipe)):
            continue
        name = f"{float(leak):g}″ → {float(pipe):g}″"
        sizes[name] = sizes.get(name, 0) + 1
    size_rows = sorted(({"name": name, "count": count}
                        for name, count in sizes.items()),
                       key=lambda row: (-row["count"], row["name"]))

    return {
        "usable": True,
        "key": base_key,
        "base_label": base_label,
        "other_label": other_label,
        "base_audited": len(base_audit),
        "other_audited": len(other_audit),
        "base_relocated": len(base),
        "other_relocated": len(other),
        "gained": len(gained_keys),
        "lost": len(lost_keys),
        "unchanged": len(both_keys) - len(changed_pipe),
        "moved_to_another_pipe": len(changed_pipe),
        "moved_examples": changed_pipe[:20],
        "gained_distance": summarise(gained_distances),
        "gained_by_tier": sorted(
            ({"name": name, "count": count} for name, count in tiers.items()),
            key=lambda row: (-row["count"], row["name"])),
        "gained_by_size_step": size_rows[:25],
        "gained_by_layer": _counts_of(gained, schema.LINKED_LAYER),
    }


def _counts_of(records, column):
    buckets = {}
    for record in records:
        name = label(record.get(column))
        buckets[name] = buckets.get(name, 0) + 1
    return sorted(({"name": name, "count": count}
                   for name, count in buckets.items()),
                  key=lambda row: (-row["count"], row["name"]))


def _run_stamp(audit):
    """When the workflow wrote this audit table, if it recorded it."""
    if "RunUTC" not in audit.columns or not len(audit):
        return None
    return _plain(audit["RunUTC"].iloc[0])
