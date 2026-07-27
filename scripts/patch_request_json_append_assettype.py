from pathlib import Path
import datetime
import py_compile
import traceback

script = Path(r"\\ngusnasnwh001\gasne\GasNE Shared\Shared\ENG\Complex Team\GIS AutoPrint\Distribution Leak Relocation\Arcpy Code\leak reolcation - geopandas.py")
backup = script.with_name(script.name + "." + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".before_request_json_assettype_wrapper.bak")
backup.write_bytes(script.read_bytes())

text = script.read_text(encoding="utf-8", errors="ignore")
marker = "# LR_REQUEST_JSON_APPEND_ASSETGROUP_ASSETTYPE"

wrapper = r'''
# LR_REQUEST_JSON_APPEND_ASSETGROUP_ASSETTYPE
_lr_original_request_json_for_assettype = request_json

def request_json(session, url, params=None, *args, **kwargs):
    try:
        _lr_url_text = str(url).lower()
        _lr_is_pipe_query = (
            "/mapserver/6" in _lr_url_text
            or "/mapserver/7" in _lr_url_text
        )
        if _lr_is_pipe_query and isinstance(params, dict):
            _lr_outfield_key = None
            for _lr_key in list(params.keys()):
                if str(_lr_key).lower() == "outfields":
                    _lr_outfield_key = _lr_key
                    break
            if _lr_outfield_key is not None:
                _lr_outfields = params.get(_lr_outfield_key)
                if isinstance(_lr_outfields, str):
                    _lr_lower = _lr_outfields.lower()
                    if (
                        ("nominaldiameter" in _lr_lower or "material" in _lr_lower or "operatingpressure" in _lr_lower)
                        and "assettype" not in _lr_lower
                    ):
                        params = dict(params)
                        params[_lr_outfield_key] = _lr_outfields + ",ASSETGROUP,ASSETTYPE"
                        print("Request wrapper appended DNV pipe domain fields to outFields: ASSETGROUP,ASSETTYPE", flush=True)
    except Exception as _lr_ex:
        try:
            print("WARNING: request_json ASSETTYPE wrapper skipped due to: {0}".format(_lr_ex), flush=True)
        except Exception:
            pass
    return _lr_original_request_json_for_assettype(session, url, params, *args, **kwargs)

'''

if marker in text:
    print("Request wrapper marker already present. No duplicate insertion made.")
else:
    needle = 'if __name__ == "__main__":'
    if needle in text:
        text = text.replace(needle, wrapper + "\n" + needle, 1)
    else:
        text = text + "\n\n" + wrapper
    script.write_text(text, encoding="utf-8")
    print("Inserted request_json outFields wrapper.")

try:
    py_compile.compile(str(script), doraise=True)
    print("COMPILE: PASS")
except Exception:
    print("COMPILE: FAIL")
    print(traceback.format_exc())
    raise SystemExit(1)

print("SCRIPT:", script)
print("BACKUP:", backup)
print("Wrapper marker present:", marker in text)
