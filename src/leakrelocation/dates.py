"""Date values as the ArcGIS layers carry them.

A layer hands dates back as epoch milliseconds, the caches store them that way,
and a delta query has to send one back as a SQL timestamp. These are the
conversions, and the rule for telling a date string from a number.
"""
import datetime as dt
import math
import os
import sys

import pandas as pd

# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member. See leakrelocation/output.py for the full note.
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PACKAGE_PARENT not in sys.path:
    sys.path.insert(0, _PACKAGE_PARENT)

from leakrelocation.matching import parse_number
from leakrelocation.output import warn

def looks_numeric(value):
    """Whether this value is a number, or a string that is nothing but one.

    The distinction matters because the two leak sources deliver dates
    differently. ArcGIS returns epoch milliseconds as a number; a SQL query
    returns a datetime or an ISO string. parse_number takes the *first* number
    out of anything, so "2019-04-02" came back as 2019 - which, read as epoch
    milliseconds, is two seconds past midnight on 1 January 1970. Every Maximo
    leak would then have failed the pipe-life check for being older than every
    pipe, and nothing would have said why.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    text = str(value).strip()
    if not text:
        return False
    try:
        float(text)
    except ValueError:
        return False
    return True


def date_value_to_epoch_ms(value):
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if looks_numeric(value):
        parsed_number = parse_number(value)
        if parsed_number is not None:
            return int(parsed_number)
    try:
        parsed = pd.to_datetime(value, utc=True)
        if pd.isna(parsed):
            return None
        return int(parsed.timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        # pandas raises DateParseError and OutOfBoundsDatetime for unparseable
        # or out-of-range input, and both descend from ValueError; a value of a
        # type it cannot read raises TypeError. OverflowError covers the int()
        # conversion of a non-finite timestamp.
        return None


def epoch_ms_to_iso(epoch_ms):
    """Epoch milliseconds as an ISO date, for the audit columns. "" when absent."""
    if epoch_ms is None:
        return ""
    try:
        return dt.datetime.fromtimestamp(
            float(epoch_ms) / 1000.0, dt.UTC).date().isoformat()
    except (OSError, OverflowError, TypeError, ValueError):
        return ""


def max_modified_epoch_ms(gdf, modified_field):
    if (
        gdf is None
        or len(gdf) == 0
        or not modified_field
        or modified_field not in gdf.columns
    ):
        return None
    values = []
    for value in gdf[modified_field].tolist():
        epoch_ms = date_value_to_epoch_ms(value)
        if epoch_ms is not None:
            values.append(epoch_ms)
    if not values:
        return None
    return max(values)


def epoch_ms_to_sql_timestamp(epoch_ms):
    safe_default = "timestamp '1970-01-01 00:00:00'"
    if epoch_ms is None:
        warn("Delta watermark was None. Falling back to full safe delta window.")
        return safe_default
    try:
        value = float(epoch_ms)
    except (TypeError, ValueError):
        warn(
            f"Delta watermark was not numeric [{epoch_ms}]. Falling back to full safe delta window."
        )
        return safe_default
    # No try/except around isfinite: value came from a float() that succeeded,
    # and math.isfinite cannot fail on a float. The except/pass that used to sit
    # here could never run.
    if not math.isfinite(value):
        warn(
            f"Delta watermark was not finite [{epoch_ms}]. Falling back to full safe delta window."
        )
        return safe_default
    if value <= 0:
        warn(
            f"Delta watermark was <= 0 [{epoch_ms}]. Falling back to full safe delta window."
        )
        return safe_default
    max_reasonable_epoch_ms = 32503680000000.0
    if value > max_reasonable_epoch_ms:
        warn(
            f"Delta watermark was outside valid millisecond range [{epoch_ms}]. Falling back to full safe delta window."
        )
        return safe_default
    try:
        when = dt.datetime.fromtimestamp(value / 1000.0, dt.UTC).replace(tzinfo=None)
    except (OSError, OverflowError, ValueError) as ex:
        # A watermark outside the platform's representable range raises
        # OverflowError, or OSError on Windows for pre-epoch values.
        warn(
            f"Could not convert delta watermark [{epoch_ms}] to timestamp: {ex}. Falling back to full safe delta window."
        )
        return safe_default
    return f"timestamp '{when.strftime('%Y-%m-%d %H:%M:%S')}'"


def build_delta_where(base_where, modified_field, last_epoch_ms):
    return f"({base_where}) AND {modified_field} > {epoch_ms_to_sql_timestamp(last_epoch_ms)}"
