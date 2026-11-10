"""The documentation has to describe the project that is actually here.

The README once told people to run a file that had been renamed and gave the
map's address with the wrong port. Neither was caught, because nothing reads
the documentation except the person it misleads. These read it.
"""
import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def documents():
    found = [os.path.join(REPO_ROOT, name) for name in ("README.md", "CHANGELOG.md")]
    docs = os.path.join(REPO_ROOT, "docs")
    found += [os.path.join(docs, name) for name in sorted(os.listdir(docs))
              if name.endswith(".md")]
    return found


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def anchors(text):
    """The anchors GitHub generates for a document's headings."""
    found = set()
    for heading in re.findall(r"(?m)^#{1,6} +(.+?)\s*$", text):
        slug = re.sub(r"[`*_]", "", heading).lower()
        slug = re.sub(r"[^\w\- ]", "", slug).replace(" ", "-")
        found.add(slug)
    return found


DOCUMENTS = documents()
NAMES = [os.path.relpath(path, REPO_ROOT) for path in DOCUMENTS]


@pytest.mark.parametrize("path", DOCUMENTS, ids=NAMES)
def test_every_relative_link_leads_somewhere(path):
    text = read(path)
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if re.match(r"[a-z]+://", target) or target.startswith("mailto:"):
            continue
        file_part, _, anchor = target.partition("#")
        linked = (os.path.normpath(os.path.join(os.path.dirname(path), file_part))
                  if file_part else path)
        assert os.path.exists(linked), f"{target} does not exist"
        if anchor and linked.endswith(".md"):
            assert anchor in anchors(read(linked)), (
                f"{target}: no heading makes the anchor #{anchor}")


COMMAND = re.compile(
    r"python\s+((?:scripts[\\/]|src[\\/]leakrelocation[\\/]|tests[\\/])?[\w]+\.py)"
    r"([^\n`|]*)")


@pytest.mark.parametrize("path", DOCUMENTS, ids=NAMES)
def test_every_command_names_a_real_script_and_real_flags(path):
    for script, rest in COMMAND.findall(read(path)):
        script_path = os.path.join(REPO_ROOT, script.replace("\\", os.sep))
        assert os.path.isfile(script_path), f"python {script}: no such file"
        source = read(script_path)
        for flag in re.findall(r"(?<![\w-])(--[a-z][a-z-]+)", rest):
            assert f'"{flag}"' in source, f"python {script} has no {flag}"


def test_no_document_names_a_file_that_was_renamed():
    gone = ("leak_relocation_geopandas.py", "leaflet_bbox_server.py")
    for path in DOCUMENTS:
        if path.endswith("CHANGELOG.md"):
            continue   # the changelog is where the old names belong
        text = read(path)
        for name in gone:
            assert name not in text, f"{os.path.relpath(path, REPO_ROOT)}: {name}"


def test_the_map_address_is_the_port_the_server_uses():
    source = read(os.path.join(REPO_ROOT, "src", "leakrelocation", "map_server.py"))
    port = re.search(r"(?m)^PORT = (\d+)", source).group(1)
    for path in DOCUMENTS:
        for used in re.findall(r"127\.0\.0\.1:(\d+)", read(path)):
            assert used == port, (
                f"{os.path.relpath(path, REPO_ROOT)} says port {used}; "
                f"the map is served on {port}")


def test_every_environment_variable_documented_is_one_the_config_reads():
    config = read(os.path.join(REPO_ROOT, "src", "leakrelocation", "config.py"))
    known = set(re.findall(r'"((?:LEAKRELOCATION_[A-Z_]+|FORCE_LAYER_REFRESH|USE_LAYER_CACHE))"', config))
    for path in DOCUMENTS:
        for name in set(re.findall(r"\b(LEAKRELOCATION_[A-Z_]+)\b", read(path))):
            assert name in known, (
                f"{os.path.relpath(path, REPO_ROOT)} documents {name}, which "
                f"config.py does not read")


def test_every_script_is_listed_in_the_readme():
    readme = read(os.path.join(REPO_ROOT, "README.md"))
    for name in sorted(os.listdir(os.path.join(REPO_ROOT, "scripts"))):
        if name.endswith(".py") and not name.startswith("_"):
            assert f"scripts\\{name}" in readme, f"{name} is not in the README"
