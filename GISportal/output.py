"""Console output helpers shared by the workflow and its supporting modules.

Kept in one place so a module split does not mean a second copy of log/warn.
"""
# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member. spec_from_file_location gives a module no parent
# package, and a relative import then fails with "attempted relative import with
# no known parent package".
import os as _os
import sys as _sys

_PACKAGE_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _PACKAGE_PARENT not in _sys.path:
    _sys.path.insert(0, _PACKAGE_PARENT)

from leakrelocation import config


def log(text):
    print(str(text), flush=True)


def step(text):
    log(f"\n--- {text} ---")


def warn(text):
    log(f"WARNING: {text}")


def fail(text):
    raise RuntimeError(str(text))


def detail(text):
    """Diagnostic output. Hidden unless LEAKRELOCATION_VERBOSE is set.

    Field resolution, TLS/proxy setup and outFields lists are useful when
    something is wrong and noise the rest of the time.
    """
    if config.VERBOSE:
        log(text)
