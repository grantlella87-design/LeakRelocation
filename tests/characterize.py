"""Characterization harness for the leak relocation module.

Captures the observable behaviour of the pure/near-pure functions so a
refactor can be proven behaviour-preserving. Run before and after a change
and diff the JSON output:

    python tests/characterize.py > before.json
    ...refactor...
    python tests/characterize.py > after.json
    diff before.json after.json

This exercises only functions that need no network, no ArcGIS token and no
filesystem access to the production share.
"""
import importlib.util
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _WithFallbacks:
    """Looks up names on the workflow module, then on the package modules.

    The workflow imports only what it calls, so helpers now living in
    leakrelocation.matching or leakrelocation.auth are reachable through the
    package instead. Probing all of them keeps the snapshot comparable to one
    taken before the modules were split out.
    """

    def __init__(self, module, *fallbacks):
        self._module = module
        self._fallbacks = fallbacks

    def __getattr__(self, name):
        for source in (self._module, *self._fallbacks):
            if hasattr(source, name):
                return getattr(source, name)
        raise AttributeError(name)


def load_module():
    """Load the module under test. Set LR_MODULE_PATH to characterise a
    different copy (e.g. the pre-refactor file) with the same probes."""
    path = os.environ.get("LR_MODULE_PATH") or os.path.join(
        REPO_ROOT, "src", "leak_relocation_geopandas.py")
    spec = importlib.util.spec_from_file_location("lr_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    try:
        sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
        from leakrelocation import auth, matching
    except ImportError:
        return module
    return _WithFallbacks(module, matching, auth)


def safe(fn, *args):
    """Call fn, recording either its result or the exception type/message."""
    try:
        value = fn(*args)
    except Exception as exc:  # noqa: BLE001 - recording the failure is the point
        return {"error": type(exc).__name__, "message": str(exc)}
    return {"value": to_jsonable(value)}


def to_jsonable(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if hasattr(value, "__next__"):
        # Materialise generators; their repr() embeds a memory address.
        return {"generator": [to_jsonable(item) for item in value]}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_jsonable(val) for key, val in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if hasattr(value, "wkt"):
        return {"wkt": value.wkt}
    return repr(value)


SCALARS = [
    None, "", "   ", "0", "2", "2.0", "4.5", "-3", "abc", "1,200", "12 in",
    "PLASTIC", "plastic", " Plastic ", "Cast Iron", "CAST IRON", "cast-iron",
    "STEEL", "Unprotected Steel", "Coated Steel", "PE", "Polyethylene",
    "Copper", "WROUGHT IRON", "Ductile Iron", "Unknown", "N/A", "<Null>",
    True, False, 0, 2, 4.5, -1,
]

MATERIAL_PAIRS = [
    ("PLASTIC", "PLASTIC"), ("plastic", "PLASTIC"), ("PE", "PLASTIC"),
    ("Polyethylene", "PE"), ("CAST IRON", "Cast Iron"), ("Cast Iron", "STEEL"),
    ("STEEL", "Unprotected Steel"), ("Coated Steel", "STEEL"),
    ("Copper", "STEEL"), (None, "STEEL"), ("STEEL", None), (None, None),
    ("", "STEEL"), ("Unknown", "Unknown"), ("Wrought Iron", "Cast Iron"),
    ("Ductile Iron", "Cast Iron"),
]

NUMERIC_PAIRS = [
    (2, 2), (2, 2.0), (2.0, 2.0001), (2, 4), (None, 2), (2, None),
    (None, None), ("2", "2"), ("2", 2), (0, 0), (-1, -1), (1.5, 1.5),
]

DISTANCES = [0, 1, 50, 99.9, 100, 100.1, 250, 999, 1000, 2999, 3000, 3001, 10000, -5]

FACILITIES = [None, "", "Main", "MAIN", "Service", "SERVICE", "service line", "Other", "Unknown"]

FIELD_COLUMN_SETS = [
    ["OBJECTID", "nominaldiameter", "material", "ASSETTYPE"],
    ["OBJECTID", "NOMINALDIAMETER", "MATERIAL"],
    ["oid", "size"],
    [],
]

CACHE_NAMES = ["distribution_pipes", "service pipes", "A/B\\C", "weird:name*?", ""]

EPOCH_VALUES = [None, 0, 1, 1700000000000, "1700000000000", "2023-11-14", "not a date", -1]


def collect(module):
    out = {}

    def record(name, fn, arg_lists):
        out[name] = [{"args": to_jsonable(list(args)), "result": safe(fn, *args)} for args in arg_lists]

    single = [(value,) for value in SCALARS]

    for name in ["clean", "upper", "normalize_key", "parse_number", "material_label", "material_family"]:
        if hasattr(module, name):
            record(name, getattr(module, name), single)

    if hasattr(module, "material_matches"):
        record("material_matches", module.material_matches, MATERIAL_PAIRS)
    if hasattr(module, "diameter_matches"):
        record("diameter_matches", module.diameter_matches, NUMERIC_PAIRS)
    if hasattr(module, "pressure_matches"):
        record("pressure_matches", module.pressure_matches, NUMERIC_PAIRS)
    if hasattr(module, "matched_radius_from_distance"):
        record("matched_radius_from_distance", module.matched_radius_from_distance, [(d,) for d in DISTANCES])
    if hasattr(module, "route_layers"):
        record("route_layers", module.route_layers, [(f,) for f in FACILITIES])
    if hasattr(module, "safe_cache_name"):
        record("safe_cache_name", module.safe_cache_name, [(n,) for n in CACHE_NAMES])
    if hasattr(module, "date_value_to_epoch_ms"):
        record("date_value_to_epoch_ms", module.date_value_to_epoch_ms, [(v,) for v in EPOCH_VALUES])
    if hasattr(module, "epoch_ms_to_sql_timestamp"):
        record("epoch_ms_to_sql_timestamp", module.epoch_ms_to_sql_timestamp, [(v,) for v in [0, 1700000000000, None]])
    if hasattr(module, "chunk_list"):
        record("chunk_list", module.chunk_list, [
            ([], 2), ([1], 2), ([1, 2, 3], 2), ([1, 2, 3, 4], 2), (list(range(7)), 3),
        ])
    if hasattr(module, "extract_oauth_code"):
        record("extract_oauth_code", module.extract_oauth_code, [
            (None,), ("",), ("abc123",), ("code=abc123",), ("?code=abc123&x=1",),
            ("https://example.com/?code=xyz",), ("no code here",),
        ])
    if hasattr(module, "chrome_time_from_epoch"):
        record("chrome_time_from_epoch", module.chrome_time_from_epoch, [(0,), (1700000000,), (-1,)])
    if hasattr(module, "material_family"):
        record("material_family_pairs", module.material_family, single)

    if hasattr(module, "resolved_field"):
        arg_lists = []
        for cols in FIELD_COLUMN_SETS:
            arg_lists.append((cols, ["nominaldiameter", "NOMINALDIAMETER"]))
            arg_lists.append((cols, ["missing_field"]))
        record("resolved_field", module.resolved_field, arg_lists)

    if hasattr(module, "resolve_from_names"):
        arg_lists = []
        for cols in FIELD_COLUMN_SETS:
            arg_lists.append((cols, ["material", "MATERIAL"]))
        record("resolve_from_names", module.resolve_from_names, arg_lists)

    if hasattr(module, "esri_point_to_geom"):
        record("esri_point_to_geom", module.esri_point_to_geom, [
            ({"x": 1, "y": 2},), ({"x": None, "y": 2},), ({},), (None,),
        ])
    if hasattr(module, "esri_polyline_to_geom"):
        record("esri_polyline_to_geom", module.esri_polyline_to_geom, [
            ({"paths": [[[0, 0], [1, 1]]]},),
            ({"paths": [[[0, 0], [1, 1]], [[2, 2], [3, 3]]]},),
            ({"paths": []},), ({},), (None,),
        ])

    if hasattr(module, "build_delta_where"):
        record("build_delta_where", module.build_delta_where, [
            ("1=1", "LASTUPDATE", 1700000000000),
            ("jurisdiction = 'MA'", "LASTUPDATE", 0),
            ("1=1", None, 1700000000000),
        ])

    return out


def collect_out_fields(module):
    """build_out_fields drives which columns get downloaded - characterize it."""
    metas = [
        {"fields": [{"name": n} for n in
                    ["OBJECTID", "nominaldiameter", "material", "ASSETGROUP", "ASSETTYPE", "LASTUPDATE"]]},
        {"fields": [{"name": n} for n in ["OBJECTID", "MATERIAL", "NOMINALDIAMETER"]]},
        {"fields": [{"name": n} for n in ["OBJECTID"]]},
        {"fields": []},
        {},
    ]
    results = []
    for meta in metas:
        for layer in ["distribution_pipes", "service_pipes", "historic_leaks"]:
            results.append({
                "layer": layer,
                "meta_fields": [f.get("name") for f in meta.get("fields", [])],
                "result": safe(module.build_out_fields, meta, layer),
            })
    return results


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"features": []}


class FakeSession:
    """Stands in for requests.Session and records outbound query params.

    Carries a pre-set access token so request_json never attempts interactive
    ArcGIS authentication.
    """

    def __init__(self, sink):
        self.sink = sink
        self._arcgis_access_token = "TEST_TOKEN"

    def get(self, url, params=None, **kwargs):
        self.sink.append({"url": url, "params": to_jsonable(params)})
        return FakeResponse()


def collect_request_json_params(module):
    """Capture the outFields that actually reach the HTTP layer.

    Observing the outbound params (rather than any particular internal
    function) keeps this comparable across refactors that move the
    ASSETGROUP/ASSETTYPE logic around.
    """
    cases = [
        ("https://x/MapServer/6/query", {"outFields": "nominaldiameter,material"}),
        ("https://x/MapServer/7/query", {"outFields": "OBJECTID,operatingpressure"}),
        ("https://x/MapServer/6/query", {"outFields": "nominaldiameter,ASSETTYPE"}),
        ("https://x/MapServer/206/query", {"outFields": "nominaldiameter,material"}),
        ("https://x/MapServer/6/query", {"outFields": "*"}),
        ("https://x/MapServer/6/query", {"OUTFIELDS": "material"}),
        ("https://x/MapServer/6/query", {"outFields": "material", "token": "PRESET"}),
        ("https://x/MapServer/6/query", {}),
        ("https://x/MapServer/6/query", None),
        ("https://x/MapServer/16/query", {"outFields": "material"}),
    ]

    captured = []
    for url, params in cases:
        session = FakeSession(captured)
        before = len(captured)
        try:
            module.request_json(session, url, params)
        except Exception as exc:  # noqa: BLE001 - recording the failure is the point
            captured.append({"url": url, "error": type(exc).__name__, "message": str(exc)})
        if len(captured) == before:
            captured.append({"url": url, "note": "no outbound request"})

    return captured


def main():
    """Write the snapshot to argv[1]. The module under test prints to stdout,
    so the snapshot never goes there."""
    if len(sys.argv) < 2:
        sys.stderr.write("usage: characterize.py OUTPUT.json\n")
        return 2

    import contextlib
    import io

    noise = io.StringIO()
    with contextlib.redirect_stdout(noise):
        module = load_module()
        payload = {
            "functions": collect(module),
            "build_out_fields": collect_out_fields(module),
            "request_json_params": collect_request_json_params(module),
        }

    with open(sys.argv[1], "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")

    sys.stderr.write(f"wrote {sys.argv[1]}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
