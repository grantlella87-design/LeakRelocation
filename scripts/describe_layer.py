"""Print what an ArcGIS layer actually has: fields, dates, subtype domains.

    python scripts/describe_layer.py https://.../MapServer/62
    python scripts/describe_layer.py https://.../MapServer/62 --save

Every field name this project uses was read out of service metadata rather than
guessed, which is why reference/ exists. This is the tool for a layer that is not
in there yet - the retired pipe layer on the MA service, for instance, whose
schema no copy in this repository covers.

--save writes the layer JSON into reference/, where the tests can pin against it.
"""
import argparse
import json
import sys

from _bootstrap import config

from leakrelocation import auth
from leakrelocation.assettype import build_assettype_decoder
from leakrelocation.output import fail, log

# What the relocation workflow needs from a pipe layer, and where it comes from.
INTERESTING = {
    "material type": ["ASSETGROUP", "ASSETTYPE"],
    "diameter": ["nominaldiameter", "outsidediameter"],
    "pressure": ["operatingpressure", "maopdesign"],
    "identity": ["OBJECTID", "GLOBALID"],
    "life dates": ["CREATIONDATE", "dateretired", "installationdate",
                   "inservicedate"],
    "filtering": ["jurisdiction"],
    "delta cache": ["LASTUPDATE"],
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", nargs="?", default=config.RETIRED_PIPE_URL,
                        help="Layer URL. Default: the retired pipe layer.")
    parser.add_argument("--save", action="store_true",
                        help="Write the layer JSON into reference/ for the tests.")
    return parser.parse_args(argv)


def describe(layer_json, url):
    fields = layer_json.get("fields") or []
    names = [f.get("name") for f in fields if f.get("name")]
    lowered = {str(name).lower(): name for name in names}

    log(f"Layer: {layer_json.get('name')!r}  id={layer_json.get('id')}")
    log(f"Geometry: {layer_json.get('geometryType')}")
    log(f"Fields: {len(names)}")
    log(f"typeIdField: {layer_json.get('typeIdField')!r}")

    log("\nWhat the workflow looks for:")
    for purpose, wanted in INTERESTING.items():
        found = [lowered[name.lower()] for name in wanted if name.lower() in lowered]
        missing = [name for name in wanted if name.lower() not in lowered]
        mark = "ok  " if found else "MISS"
        log(f"  {mark} {purpose:14} found={found} missing={missing}")

    dates = [f["name"] for f in fields
             if str(f.get("type") or "") == "esriFieldTypeDate"]
    log(f"\nDate fields ({len(dates)}): {sorted(dates)}")

    _, decoder = build_assettype_decoder(layer_json)
    log(f"\nASSETTYPE subtype pairs: {len(decoder)}")
    for (group, code), info in sorted(decoder.items())[:12]:
        log(f"  ({group}, {code}) -> {info['ASSETTYPE_DECODED']!r} "
            f"[{info['PipeMaterialFamily']}]")
    if len(decoder) > 12:
        log(f"  ... {len(decoder) - 12} more")

    log(f"\nAll field names:\n{sorted(names)}")
    log(f"\nSource: {url}")


def save(layer_json, url):
    layer_id = layer_json.get("id")
    name = str(layer_json.get("name") or "layer").replace(" ", "_")
    target = config.REFERENCE_DIR
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"layer_{int(layer_id):03d}_{name}.json"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(layer_json, handle, indent=1)
    log(f"Wrote {path}")
    log(f"  from {url}")


def main(argv=None):
    args = parse_args(argv)
    session = auth.make_session()
    layer_json = auth.request_json(session, args.url.rstrip("/"), {"f": "json"})
    if not layer_json or "fields" not in layer_json:
        fail(f"{args.url} returned no field list. Response keys: "
             f"{sorted(layer_json or {})}")
    describe(layer_json, args.url)
    if args.save:
        save(layer_json, args.url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
