"""Makes the `leakrelocation` package importable from the scripts folder.

The diagnostic scripts are run directly (`python scripts/whatever.py`) rather
than as an installed package, so `src/` is not on sys.path. Importing this
module puts it there and re-exports the shared configuration:

    from _bootstrap import config

    df = pd.read_pickle(config.LAYER_CACHE_DIR / "service_pipes.pkl.gz")

If the project is installed (`pip install -e .`) this is unnecessary and
`from leakrelocation import config` works directly.
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from leakrelocation import config

__all__ = ["SRC", "config"]
