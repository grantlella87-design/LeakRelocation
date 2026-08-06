"""Console output helpers shared by the workflow and its supporting modules.

Kept in one place so a module split does not mean a second copy of log/warn.
"""
from . import config


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
