from pathlib import Path
import datetime
import re
import py_compile
import traceback

from _bootstrap import config

script = config.NETWORK_MAIN_SCRIPT
backup = script.with_name(script.name + "." + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".before_assetgroup_assettype_patch.bak")
backup.write_bytes(script.read_bytes())

text = script.read_text(encoding="utf-8", errors="ignore")
original = text

# Exact outFields string seen in your run log.
text = text.replace(
    "OBJECTID,LASTUPDATE,nominaldiameter,material,operatingpressure,GLOBALID,jurisdiction",
    "OBJECTID,LASTUPDATE,ASSETGROUP,ASSETTYPE,nominaldiameter,material,operatingpressure,GLOBALID,jurisdiction"
)

# Safer variants if spacing or list construction differs.
text = text.replace(
    '"OBJECTID", "LASTUPDATE", "nominaldiameter", "material", "operatingpressure", "GLOBALID", "jurisdiction"',
    '"OBJECTID", "LASTUPDATE", "ASSETGROUP", "ASSETTYPE", "nominaldiameter", "material", "operatingpressure", "GLOBALID", "jurisdiction"'
)

text = text.replace(
    "'OBJECTID', 'LASTUPDATE', 'nominaldiameter', 'material', 'operatingpressure', 'GLOBALID', 'jurisdiction'",
    "'OBJECTID', 'LASTUPDATE', 'ASSETGROUP', 'ASSETTYPE', 'nominaldiameter', 'material', 'operatingpressure', 'GLOBALID', 'jurisdiction'"
)

# If there are field arrays containing nominaldiameter/material but not ASSETTYPE,
# inject ASSETGROUP/ASSETTYPE immediately after LASTUPDATE.
lines = text.splitlines()
new_lines = []
for line in lines:
    new_line = line
    low = line.lower()
    if (
        "nominaldiameter" in low
        and "material" in low
        and "assettype" not in low
        and "lastupdate" in low
    ):
        new_line = re.sub(
            r"(LASTUPDATE['\"]?\s*,)",
            r"\1 'ASSETGROUP', 'ASSETTYPE',",
            line,
            count=1
        )
    new_lines.append(new_line)

text = "\n".join(new_lines)

if text == original:
    print("WARNING: No text replacement was made. The script may build outFields dynamically elsewhere.")
else:
    script.write_text(text, encoding="utf-8")

try:
    py_compile.compile(str(script), doraise=True)
    print("COMPILE: PASS")
except Exception:
    print("COMPILE: FAIL")
    print(traceback.format_exc())
    raise SystemExit(1)

print("SCRIPT:", script)
print("BACKUP:", backup)
print("Contains ASSETGROUP:", "ASSETGROUP" in text)
print("Contains ASSETTYPE:", "ASSETTYPE" in text)
