"""Find out why a leak field is empty here, by asking the service directly.

    python scripts/probe_leak_fields.py
    python scripts/probe_leak_fields.py --fields ADDRESS,NEARESTXSTREET,CITY

ADDRESS is a field on layer 206 and it is in the outFields this project sends,
yet every one of the 98,501 cached MA rows has no value for it. There are only
three ways that happens, and this asks the service a question that separates
them:

    1. the request loses it - it comes back under outFields=* and not under the
       list this project builds. That is a bug here;
    2. the service has it, but not for the rows this project downloads. The
       WHERE is jurisdiction = 'MA' and this is the NY service, so ADDRESS being
       populated on NY leaks and empty on MA ones looks identical from inside a
       MA-only cache;
    3. the field is empty for everyone.

So every field is counted four ways - with and without the MA filter, and null
against blank - and real values are pulled from rows that actually have one
rather than from the first five rows, which in case 2 are all empty.

Counts only, plus a handful of sample values. No cache is read or written and
nothing is downloaded.
"""
import argparse
import sys

from _bootstrap import config

from leakrelocation.output import fail, log, warn

DEFAULT_FIELDS = "ADDRESS,NEARESTXSTREET,CITY,REVISEDLEAKDATE,DISCOVEREDDATE"

# Only a text field can hold the empty string, and `<> ''` against a date field
# is a query the service rejects. Read off the layer metadata rather than
# guessed, so a field added later is handled without touching this.
TEXT_FIELD_TYPES = {"esriFieldTypeString"}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=config.HIST_LEAK_URL,
                        help="Layer URL. Default: the historic leak layer.")
    parser.add_argument("--fields", default=DEFAULT_FIELDS,
                        help=f"Comma separated. Default: {DEFAULT_FIELDS}")
    parser.add_argument("--where", default=config.WHERE_MA,
                        help="The rows this project downloads. Default: %(default)s")
    parser.add_argument("--samples", type=int, default=3,
                        help="Values to show per field. Default: 3")
    return parser.parse_args(argv)


def count(workflow, session, url, where):
    """Rows matching `where`, or None when the service would not answer."""
    params = {"f": "json", "where": where, "returnCountOnly": "true"}
    try:
        data = workflow.request_json(session, url.rstrip("/") + "/query", params)
    except Exception as ex:  # noqa: BLE001 - one bad field must not end the probe
        warn(f"   could not ask [{where}]: {ex}")
        return None
    if data.get("error"):
        warn(f"   the service rejected [{where}]: "
             f"{data['error'].get('message', data['error'])}")
        return None
    value = data.get("count")
    return None if value is None else int(value)


def values(workflow, session, url, where, out_fields, limit):
    """Attribute rows for `where`, empty when the service would not answer."""
    params = {
        "f": "json",
        "where": where,
        "outFields": out_fields,
        "returnGeometry": "false",
        "resultRecordCount": limit,
    }
    try:
        data = workflow.request_json(session, url.rstrip("/") + "/query", params)
    except Exception as ex:  # noqa: BLE001 - as above
        warn(f"   could not fetch [{where}]: {ex}")
        return []
    if data.get("error"):
        warn(f"   the service rejected [{where}]: "
             f"{data['error'].get('message', data['error'])}")
        return []
    return [feature.get("attributes", {}) or {}
            for feature in (data.get("features") or [])]


def attribute(attributes, field):
    """A field's value, matched however the service spells the name back."""
    match = next((key for key in attributes if key.lower() == field.lower()), None)
    return attributes.get(match) if match else None


def has_value(where, field, is_text):
    """The WHERE for rows that carry a real value, not a null or a blank."""
    clause = f"{field} IS NOT NULL"
    if is_text:
        clause += f" AND {field} <> ''"
    return f"({where}) AND {clause}" if where else clause


def show_samples(workflow, session, url, where, field, extra, limit):
    rows = values(workflow, session, url, where,
                  ",".join([field, *extra]), limit)
    if not rows:
        return False
    for attributes in rows:
        context = " ".join(
            f"{name}={attribute(attributes, name)!r}" for name in extra)
        log(f"      {attribute(attributes, field)!r}   {context}")
    return True


def probe(workflow, session, url, field, is_text, where, samples):
    """Count one field four ways and show values from rows that have one."""
    log(f"\n--- {field} ---")
    everywhere = count(workflow, session, url, has_value("", field, is_text))
    in_scope = count(workflow, session, url, has_value(where, field, is_text))
    null_anywhere = count(workflow, session, url, f"{field} IS NULL")
    blank = (count(workflow, session, url, f"{field} = ''") if is_text else None)

    width = max(len(where), 11)
    log(f"   with a value, {'whole layer':<{width}} : {fmt(everywhere)}")
    log(f"   with a value, {where:<{width}} : {fmt(in_scope)}")
    log(f"   null,         {'whole layer':<{width}} : {fmt(null_anywhere)}")
    if is_text:
        log(f"   empty string, {'whole layer':<{width}} : {fmt(blank)}")

    if in_scope:
        log("   values on the rows this project downloads:")
        show_samples(workflow, session, url, has_value(where, field, is_text),
                     field, ["LMSLEAKNUMBER"], samples)
    elif everywhere:
        log(f"   no value on any {where} row. Values elsewhere on the layer:")
        show_samples(workflow, session, url, has_value("", field, is_text),
                     field, ["jurisdiction", "STATE"], samples)
    return {"everywhere": everywhere, "in_scope": in_scope}


def fmt(value):
    return "could not ask" if value is None else f"{value:,}"


def verdict(result, cached_in_request):
    """What the counts mean, in one line, phrased as what to do about it."""
    everywhere, in_scope = result["everywhere"], result["in_scope"]
    if in_scope:
        if cached_in_request:
            return ("THE SERVICE HAS IT AND THIS PROJECT ASKS FOR IT - if the "
                    "cache is empty the field is lost between the two. A bug here.")
        return ("the service has it, but this project does not ask for it. Add "
                "it to the request.")
    if everywhere:
        return ("the field is populated on this layer but on none of the rows "
                "this project downloads. Nothing in the request can fill it in.")
    if everywhere == 0:
        return "the field is empty for every row on the layer."
    return "could not be determined - the service did not answer."


def main(argv=None):
    args = parse_args(argv)
    sys.path.insert(0, str(config.WORKFLOW_SCRIPT.parent))
    import leak_relocation_geopandas as workflow

    fields = [name.strip() for name in args.fields.split(",") if name.strip()]
    url = args.url.rstrip("/")
    session = workflow.make_session()

    log(f"Layer: {url}")
    log(f"The rows this project downloads: {args.where}")

    meta = workflow.layer_metadata(session, url)
    asked = workflow.build_out_fields(meta, "historic leaks")
    log(f"\nThe workflow asks the service for:\n   {asked}")
    requested = {name.strip().lower() for name in asked.split(",")}

    types = {str(field.get("name", "")).lower(): field.get("type")
             for field in (meta.get("fields") or [])}
    for name in fields:
        if name.lower() not in types:
            fail(f"The layer has no {name} field. It has: "
                 f"{', '.join(sorted(types))}")

    total = count(workflow, session, url, args.where)
    log(f"Rows matching that WHERE: {fmt(total)}")
    if total == 0:
        fail(f"The layer returned no rows at all for [{args.where}], so nothing "
             f"below would mean anything.")

    results = {}
    for name in fields:
        results[name] = probe(workflow, session, url, name,
                              types[name.lower()] in TEXT_FIELD_TYPES,
                              args.where, args.samples)

    log("\n=== verdict, per field ===")
    for name in fields:
        log(f"\n   {name}")
        log(f"      {verdict(results[name], name.lower() in requested)}")

    log("\nSend this whole output and the next step follows from it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
