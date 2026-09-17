"""Where a leak is, in one readable line.

Layer 206 keeps a leak's location in four places and no row is guaranteed to
have any particular one of them:

    ADDRESS         a street address - what anyone actually wants;
    NEARESTXSTREET  a cross street, which is a street without a number;
    CITY            a municipality;
    SuppTown        the supplemental CSV's yard-town code ("BOS-DORCHESTER"),
                    filled on all 98,464 rows and therefore the last resort.

Every cached MA row came back with ADDRESS empty, which is what
scripts/probe_leak_fields.py is for. Whatever that turns out to be, a leak with
no street address still has a cross street and a city, and "OAK ST / WATERTOWN"
places it where a blank does not.

So the rule is one street-level part and one municipality part, best available
of each:

    12 Elm St / WATERTOWN
    OAK ST / WATERTOWN
    WATERTOWN
    BOS-DORCHESTER

Taking the best of each rather than joining all four keeps the line short and
stops the same town appearing twice when CITY and SuppTown agree.

The workflow writes this into LeakAddress and the map server into LeakLocation.
They share this module so the two cannot drift into saying different things
about the same leak.
"""
import math

# Street level, best first: a numbered address beats a cross street.
STREET_FIELDS = ("ADDRESS", "NEARESTXSTREET")

# Municipality, best first: the service's own city beats the yard-town code.
AREA_FIELDS = ("CITY", "SuppTown")

# Everything this knows how to read, in the order it prefers them. The map
# server keeps these columns so a popup can show them beside the composed line.
LOCATION_FIELDS = STREET_FIELDS + AREA_FIELDS

# The ones layer 206 itself carries, and so the ones worth putting in outFields.
# SuppTown comes from the supplemental CSV; asking the service for a field it
# does not have makes it reject the whole query.
SERVICE_FIELDS = ("ADDRESS", "NEARESTXSTREET", "CITY")

SEPARATOR = " / "


def text(value):
    """A value as trimmed text, or "" for anything that is not one.

    A missing string in a pandas column reads back as a float NaN rather than
    None, and str() of one is "nan" - which would put the word "nan" in 98,501
    popups. Blanks go the same way as nulls: the cached ADDRESS column is full
    of them, and one would otherwise compose as "  / WATERTOWN".
    """
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def best(values):
    """The first of these that has a value."""
    for value in values:
        found = text(value)
        if found:
            return found
    return ""


def compose(street, area):
    """Join the two parts, skipping an empty one and never repeating a value."""
    parts = [part for part in (street, area) if part]
    if len(parts) == 2 and parts[0].casefold() == parts[1].casefold():
        return parts[0]
    return SEPARATOR.join(parts)


def from_values(values):
    """One location line from a field name -> value mapping.

    Names are matched case-insensitively, because the service answers with its
    own spelling and a cache carries whatever it was given.
    """
    lowered = {str(name).lower(): value for name, value in values.items()}
    street = best(lowered.get(name.lower()) for name in STREET_FIELDS)
    area = best(lowered.get(name.lower()) for name in AREA_FIELDS)
    return compose(street, area)
