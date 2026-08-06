"""Every package module must load however it is reached.

The package used relative imports, which work only when a module is imported as
a package member. Two things this project actually does broke on that:

* loading a module by file path with spec_from_file_location, which is how the
  workflow module itself is loaded by scripts/enrich_assettype_cache.py
* running a module directly, e.g. python src/leakrelocation/auth.py

Both give the module no parent package, so `from . import config` fails with
"attempted relative import with no known parent package".
"""
import importlib
import importlib.util
import io
import os
import subprocess
import sys
from contextlib import redirect_stdout

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO_ROOT, "src")
PACKAGE = os.path.join(SRC, "leakrelocation")
sys.path.insert(0, SRC)

MODULES = sorted(
    name[:-3] for name in os.listdir(PACKAGE)
    if name.endswith(".py") and name != "__init__.py"
)


def test_modules_were_found():
    assert "auth" in MODULES
    assert "matching" in MODULES
    assert len(MODULES) >= 6


@pytest.mark.parametrize("module_name", MODULES)
def test_imports_as_a_package_member(module_name):
    assert importlib.import_module(f"leakrelocation.{module_name}")


@pytest.mark.parametrize("module_name", MODULES)
def test_loads_by_file_path(module_name):
    """spec_from_file_location gives the module no parent package."""
    path = os.path.join(PACKAGE, module_name + ".py")
    spec = importlib.util.spec_from_file_location(f"byfile_{module_name}", path)
    module = importlib.util.module_from_spec(spec)
    with redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)
    assert module is not None


@pytest.mark.parametrize("module_name", MODULES)
def test_runs_directly(module_name):
    """--help so auth.py, which has a runnable main, prints usage and exits
    instead of attempting a real sign-in. Modules without argparse ignore it.
    The point is that the imports resolve when __package__ is unset."""
    path = os.path.join(PACKAGE, module_name + ".py")
    result = subprocess.run([sys.executable, path, "--help"], capture_output=True,
                            text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-400:]
    assert "attempted relative import" not in result.stderr


def test_auth_is_runnable_as_a_script():
    """python src/leakrelocation/auth.py runs a sign-in session."""
    path = os.path.join(PACKAGE, "auth.py")
    result = subprocess.run([sys.executable, path, "--help"], capture_output=True,
                            text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-400:]
    assert "--force" in result.stdout
    assert "--no-verify" in result.stdout


def test_no_relative_imports_remain():
    """A relative import here is what breaks the two cases above."""
    offenders = []
    for module_name in MODULES + ["__init__"]:
        path = os.path.join(PACKAGE, module_name + ".py")
        with open(path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if line.startswith(("from .", "import .")):
                    offenders.append(f"{module_name}.py:{number}: {line.strip()}")
    assert offenders == []


@pytest.mark.parametrize("script", [
    "leak_relocation_geopandas.py",
    "leaflet_bbox_server.py",
])
def test_top_level_scripts_load_by_file_path(script):
    path = os.path.join(SRC, script)
    spec = importlib.util.spec_from_file_location(f"byfile_{script}", path)
    module = importlib.util.module_from_spec(spec)
    with redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)
    assert module is not None
