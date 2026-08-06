"""Enrich the local pipe caches with decoded ASSETGROUP + ASSETTYPE.

Replaces four near-identical scripts that differed only in how they fetched
pages (threaded, multiprocess-sharded, or serial) and which layers they
covered:

    RUN_enrich_assettype_cache_full_with_progress.py
    RUN_service_assettype_enrich_multiprocess_wifi.py
    RUN_service_assettype_enrich_stable.py
    enrich_pipe_cache_with_assettype.py

Those strategies are now the --workers option: use 1 over an unreliable
connection, the default otherwise.

    python scripts/enrich_assettype_cache.py                    # both layers
    python scripts/enrich_assettype_cache.py --layer service
    python scripts/enrich_assettype_cache.py --workers 1        # slow link
    python scripts/enrich_assettype_cache.py --dry-run          # no cache writes

Only local caches are modified; relocation is not run. The original cache is
backed up once before the first write.
"""
import argparse
import importlib.util
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from _bootstrap import config

from leakrelocation import schema
from leakrelocation.assettype import build_assettype_decoder, norm_code

LAYERS = {
    "distribution": {"cache_name": "distribution_pipes.pkl.gz", "url_attr": "DISTRIBUTION_PIPE_URL"},
    "service": {"cache_name": "service_pipes.pkl.gz", "url_attr": "SERVICE_PIPE_URL"},
}

DECODED_COLUMNS = [schema.ASSETGROUP_DECODED, schema.ASSETTYPE_DECODED,
                   schema.ASSETTYPE_DOMAIN, schema.PIPE_MATERIAL_FAMILY]
MANAGED_COLUMNS = ([schema.ASSETGROUP, schema.ASSETTYPE] + DECODED_COLUMNS
                   + [schema.PIPE_MATERIAL_RAW, schema.GRADE_MATERIAL])

RETRY_DELAYS = [2, 5, 15]


def log(message):
    print(message, flush=True)


def load_workflow_module():
    """Load the workflow module for its authenticated session and request
    helpers."""
    path = config.WORKFLOW_SCRIPT
    if not path.exists():
        raise RuntimeError(f"Workflow module not found: {path}")
    spec = importlib.util.spec_from_file_location("leak_relocation_workflow", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def request_with_retry(module, session, url, params, label):
    """Transient network failures are common over VPN and hotel wifi."""
    last_error = None
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            data = module.request_json(session, url, params)
        except Exception as exc:  # noqa: BLE001 - retried below, re-raised if final
            last_error = exc
        else:
            if "error" in data:
                raise RuntimeError(f"ArcGIS error for {label}: {data['error']}")
            return data
        if attempt < len(RETRY_DELAYS):
            delay = RETRY_DELAYS[attempt]
            log(f"  {label}: {last_error}; retrying in {delay}s")
            time.sleep(delay)
    raise RuntimeError(f"{label} failed after retries: {last_error}")


def fetch_assettype_rows(module, session, layer_url, wanted_objectids, page_size, workers):
    """Page through the layer, keeping only OBJECTIDs present in the cache."""
    query_url = layer_url.rstrip("/") + "/query"
    wanted = pd.Series(wanted_objectids).dropna().astype("int64").drop_duplicates()
    wanted_set = set(wanted.tolist())

    count_data = request_with_retry(
        module, session, query_url,
        {"f": "json", "where": "1=1", "returnCountOnly": "true"}, "count query")
    service_count = int(count_data.get("count") or 0)
    offsets = list(range(0, service_count, page_size))

    log(f"  service count: {service_count:,}")
    log(f"  cache OBJECTIDs to retain: {len(wanted_set):,}")
    log(f"  pages: {len(offsets):,} of {page_size:,} rows, workers: {workers}")

    def fetch_page(offset):
        data = request_with_retry(module, session, query_url, {
            "f": "json",
            "where": "1=1",
            "outFields": "OBJECTID,ASSETGROUP,ASSETTYPE",
            "returnGeometry": "false",
            "orderByFields": "OBJECTID",
            "resultOffset": offset,
            "resultRecordCount": page_size,
        }, f"page offset={offset}")

        rows = []
        for feature in data.get("features") or []:
            attrs = feature.get("attributes") or {}
            try:
                oid = int(attrs.get("OBJECTID"))
            except (TypeError, ValueError):
                continue
            if oid in wanted_set:
                rows.append({
                    "OBJECTID": oid,
                    "ASSETGROUP": attrs.get("ASSETGROUP"),
                    "ASSETTYPE": attrs.get("ASSETTYPE"),
                })
        return rows

    all_rows = []
    completed = 0
    if workers <= 1:
        for offset in offsets:
            all_rows.extend(fetch_page(offset))
            completed += 1
            if completed == 1 or completed % 10 == 0 or completed == len(offsets):
                log(f"  pages {completed:,}/{len(offsets):,}; rows matched {len(all_rows):,}")
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(fetch_page, offset) for offset in offsets]
            for future in as_completed(futures):
                all_rows.extend(future.result())
                completed += 1
                if completed == 1 or completed % 10 == 0 or completed == len(offsets):
                    log(f"  pages {completed:,}/{len(offsets):,}; rows matched {len(all_rows):,}")

    wanted_df = pd.DataFrame({"OBJECTID": wanted})
    if not all_rows:
        log("  WARNING: no ASSETGROUP/ASSETTYPE rows returned.")
        wanted_df["ASSETGROUP"] = None
        wanted_df["ASSETTYPE"] = None
        return wanted_df

    attr_df = pd.DataFrame(all_rows).drop_duplicates("OBJECTID", keep="first")
    attr_df["OBJECTID"] = attr_df["OBJECTID"].astype("int64")
    attr_df = wanted_df.merge(attr_df, how="left", on="OBJECTID")

    non_null = int(attr_df["ASSETTYPE"].notna().sum())
    log(f"  joined rows: {len(attr_df):,}; ASSETTYPE non-null: {non_null:,}")
    return attr_df


def already_enriched(df):
    return (all(col in df.columns for col in ["ASSETGROUP", "ASSETTYPE", "ASSETTYPE_DECODED", "PipeMaterialFamily"])
            and df["ASSETTYPE"].notna().sum() > 0)


def enrich_layer(module, session, layer_key, args):
    cfg = LAYERS[layer_key]
    cache_path = config.LAYER_CACHE_DIR / cfg["cache_name"]
    layer_url = getattr(module, cfg["url_attr"])

    log("")
    log("=" * 60)
    log(f"ENRICHING: {layer_key}")
    log(f"cache: {cache_path}")
    log(f"url:   {layer_url}")

    if not cache_path.exists():
        log("SKIP: cache not found.")
        return

    df = pd.read_pickle(cache_path, compression="gzip")
    log(f"cache rows: {len(df):,}")

    if already_enriched(df) and not args.force:
        log("SKIP: already decoded. Use --force to redo.")
        log(df["PipeMaterialFamily"].value_counts(dropna=False).head(20))
        return

    if "OBJECTID" not in df.columns:
        raise RuntimeError(f"OBJECTID missing from cache: {cache_path}")

    layer_json = request_with_retry(module, session, layer_url, {"f": "json"}, "layer metadata")
    type_id_field, decoder = build_assettype_decoder(layer_json)
    log(f"typeIdField: {type_id_field}; decoder entries: {len(decoder):,}")
    if not decoder:
        raise RuntimeError(f"No ASSETTYPE subtype domains found for {layer_key}")

    objectids = df["OBJECTID"].dropna().astype("int64").drop_duplicates().tolist()
    attr_df = fetch_assettype_rows(module, session, layer_url, objectids, args.page_size, args.workers)

    # DNV "material" is Grade data. Capture it once, then republish "material"
    # as the decoded ASSETTYPE, which is what the relocation logic expects.
    #
    # On a --force re-run "material" already holds the decoded ASSETTYPE from
    # the previous pass, so an existing GradeMaterial column is the only
    # surviving copy of the original grade and must be carried across the drop
    # rather than recomputed.
    grade = df["GradeMaterial"] if "GradeMaterial" in df.columns else df.get("material")

    df = df.drop(columns=[col for col in MANAGED_COLUMNS if col in df.columns])

    if grade is not None:
        df["GradeMaterial"] = grade

    df = df.merge(attr_df, how="left", on="OBJECTID")

    decoded = pd.DataFrame([
        decoder.get((norm_code(group), norm_code(atype)), {})
        for group, atype in zip(df["ASSETGROUP"], df["ASSETTYPE"])
    ])
    for col in DECODED_COLUMNS:
        df[col] = decoded[col] if col in decoded.columns else None

    # Pipe material comes from ASSETTYPE:
    #   PipeMaterialRaw  the raw ASSETTYPE as the service returns it (a subtype
    #                    code), kept so a decode can always be traced back
    #   material         its domain value, which is what the relocation logic
    #                    and the viewers read
    # Both were previously set to the decoded value, so nothing recorded the
    # raw code despite the column name saying otherwise.
    df[schema.PIPE_MATERIAL_RAW] = df[schema.ASSETTYPE]
    df[schema.MATERIAL] = df[schema.ASSETTYPE_DECODED]

    log("")
    log("PipeMaterialFamily counts:")
    log(df["PipeMaterialFamily"].value_counts(dropna=False).head(20))

    if args.dry_run:
        log("")
        log("DRY RUN: cache not written.")
        return

    backup = cache_path.with_name(cache_path.name + ".before_assettype_as_material.bak")
    if not backup.exists():
        backup.write_bytes(cache_path.read_bytes())
        log(f"backup written: {backup}")

    df.to_pickle(cache_path, compression="gzip")
    log(f"cache updated: {cache_path}")

    config.ENRICHMENT_DIR.mkdir(parents=True, exist_ok=True)
    counts_csv = config.ENRICHMENT_DIR / (layer_key + "_assettype_decode_counts.csv")
    (df.groupby(["ASSETGROUP", "ASSETGROUP_DECODED", "ASSETTYPE", "ASSETTYPE_DECODED", "PipeMaterialFamily"],
                dropna=False)
       .size().reset_index(name="count").sort_values("count", ascending=False)
       .to_csv(counts_csv, index=False))
    log(f"decode counts: {counts_csv}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--layer", choices=sorted(LAYERS) + ["both"], default="both")
    parser.add_argument("--workers", type=int, default=8,
                        help="Concurrent page fetches. Use 1 on an unreliable connection.")
    parser.add_argument("--page-size", type=int, default=2000)
    parser.add_argument("--force", action="store_true", help="Re-enrich an already decoded cache.")
    parser.add_argument("--dry-run", action="store_true", help="Report results without writing caches.")
    args = parser.parse_args(argv)

    layer_keys = sorted(LAYERS) if args.layer == "both" else [args.layer]

    log("ASSETTYPE cache enrichment")
    log(f"workflow module: {config.WORKFLOW_SCRIPT}")
    log(f"cache dir:       {config.LAYER_CACHE_DIR}")
    log(f"layers:          {', '.join(layer_keys)}")
    log("This modifies local pipe caches only. It does not run relocation.")

    module = load_workflow_module()
    session = module.make_session()

    for layer_key in layer_keys:
        enrich_layer(module, session, layer_key, args)

    log("")
    log("DONE: local pipe caches use decoded ASSETTYPE as material.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
