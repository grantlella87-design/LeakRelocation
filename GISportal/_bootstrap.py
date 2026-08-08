"""Makes the shared `leakrelocation` package importable from GISportal.

GISportal is a separate project in this repository that talks to the same
ArcGIS Portal, so it reuses that package's authentication, configuration and
console helpers rather than carrying copies of them.

    from _bootstrap import auth, config, output

The copies that used to live here as GISportal/auth.py and GISportal/output.py
were byte-identical to the package versions, and they did not work: both kept a
sys.path setup written for a module inside src/leakrelocation/, so from
GISportal/ they pointed at the repository root and `from leakrelocation import
config` failed with ModuleNotFoundError.
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from leakrelocation import auth, config, output

__all__ = ["SRC", "auth", "config", "output"]
