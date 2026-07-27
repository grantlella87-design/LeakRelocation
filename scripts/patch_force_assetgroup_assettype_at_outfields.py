from pathlib import Path
import datetime
import py_compile
import traceback

script = Path(r"\\ngusnasnwh001\gasne\GasNE Shared\Shared\ENG\Complex Team\GIS AutoPrint\Distribution Leak Relocation\Arcpy Code\leak reolcation - geopandas.py")
backup = script.with_name(script.name + "." + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".before_force_assettype_outfields_patch.bak")
backup.write_bytes(script.read_bytes())

text = script.read_text(encoding="utf-8", errors="ignore")

marker = "# LR_FORCE_ASSETGROUP_ASSETTYPE_OUTFIELDS"
if marker in text:
    print("Patch marker already present. No duplicate insertion made.")
else:
    lines = text.splitlines()
    new_lines = []
    inserted = 0

    for line in lines:
        if "using outFields=" in line:
            indent = line[:len(line) - len(line.lstrip())]

            block = [
                indent + marker,
                indent + "_lr_is_pipe_layer = False",
                indent + "for _lr_local_name, _lr_local_value in list(locals().items()):",
                indent + "    try:",
                indent + "        _lr_text = str(_lr_local_value).lower()",
                indent + "    except Exception:",
                indent + "        _lr_text = ''",
                indent + "    if (",
                indent + "        'distribution pipes' in _lr_text",
                indent + "        or 'service pipes' in _lr_text",
                indent + "        or 'distribution pipe' in _lr_text",
                indent + "        or 'service pipe' in _lr_text",
                indent + "        or '/mapserver/6' in _lr_text",
                indent + "        or '/mapserver/7' in _lr_text",
                indent + "    ):",
                indent + "        _lr_is_pipe_layer = True",
                indent + "        break",
                indent + "if _lr_is_pipe_layer:",
                indent + "    for _lr_local_name, _lr_local_value in list(locals().items()):",
                indent + "        if isinstance(_lr_local_value, list):",
                indent + "            _lr_lower_values = [str(_v).lower() for _v in _lr_local_value]",
                indent + "            if (",
                indent + "                'nominaldiameter' in _lr_lower_values",
                indent + "                or 'material' in _lr_lower_values",
                indent + "                or 'operatingpressure' in _lr_lower_values",
                indent + "            ):",
                indent + "                for _lr_required_field in ['ASSETGROUP', 'ASSETTYPE']:",
                indent + "                    if _lr_required_field not in _lr_local_value:",
                indent + "                        _lr_local_value.append(_lr_required_field)",
                indent + "                        info('Added required DNV pipe domain field to outFields: {0}'.format(_lr_required_field))",
                indent + "        elif isinstance(_lr_local_value, str):",
                indent + "            _lr_lower_text = _lr_local_value.lower()",
                indent + "            if (",
                indent + "                'nominaldiameter' in _lr_lower_text",
                indent + "                and 'material' in _lr_lower_text",
                indent + "                and 'assettype' not in _lr_lower_text",
                indent + "            ):",
                indent + "                # Best effort for string-based outFields variables. In CPython, locals() assignment",
                indent + "                # is not always reliable for fast locals, so list-based modification above is preferred.",
                indent + "                locals()[_lr_local_name] = _lr_local_value + ',ASSETGROUP,ASSETTYPE'",
            ]

            new_lines.extend(block)
            inserted += 1

        new_lines.append(line)

    text = "\n".join(new_lines)
    script.write_text(text, encoding="utf-8")
    print("Inserted force-ASSETGROUP/ASSETTYPE block count:", inserted)

try:
    py_compile.compile(str(script), doraise=True)
    print("COMPILE: PASS")
except Exception:
    print("COMPILE: FAIL")
    print(traceback.format_exc())
    raise SystemExit(1)

print("SCRIPT:", script)
print("BACKUP:", backup)
print("Marker present:", marker in text)
