"""Tests for bootstrap.py - Zscaler detection, venv layout, pip command.

The detection half is checked against a copy of the page ip.zscaler.com actually
served, not against invented wording. The positive case is that same page with the
"not going through" sentence removed, because the decision is keyed on the marker
that was observable from outside the corporate network - what the page says when
you *are* behind Zscaler could not be verified, so nothing depends on it.
"""
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import bootstrap

# Trimmed from the live response, keeping the structure and the sentence that
# decides it. Fetched from http://ip.zscaler.com/ while writing this.
REAL_NOT_BEHIND = """<!DOCTYPE html>
<html><head><title>Zscaler Cloud Security: My IP Address</title></head>
<body class="site"><main class="site-content">
<div class="headline">The request received from you didn&#39;t come from a Zscaler
IP therefore you are not going through the Zscaler proxy service.</div>
<div class="ip">Your request is arriving at this server from the IP address
34.136.29.35</div>
<div>Your Gateway IP Address is most likely 34.136.29.35</div>
</main></body></html>"""

BEHIND_PAGE = """<!DOCTYPE html>
<html><head><title>Zscaler Cloud Security: My IP Address</title></head>
<body class="site"><main class="site-content">
<div class="headline">You are accessing the Internet via Zscaler Cloud:
zscalerthree.net</div>
<div class="ip">Your request is arriving at this server from the IP address
165.225.34.7</div>
</main></body></html>"""


class TestReadingTheCheckPage:
    def test_the_real_page_reads_as_not_behind(self):
        status, detail = bootstrap.interpret(REAL_NOT_BEHIND)
        assert status == bootstrap.NOT_BEHIND
        assert "34.136.29.35" in detail

    def test_a_zscaler_page_without_that_sentence_reads_as_behind(self):
        status, detail = bootstrap.interpret(BEHIND_PAGE)
        assert status == bootstrap.BEHIND
        assert "165.225.34.7" in detail

    def test_the_decision_survives_html_escaping(self):
        """The live page writes "didn&#39;t", and the marker sits just after it."""
        assert "&#39;" in REAL_NOT_BEHIND
        assert bootstrap.interpret(REAL_NOT_BEHIND)[0] == bootstrap.NOT_BEHIND

    def test_case_does_not_matter(self):
        assert bootstrap.interpret(REAL_NOT_BEHIND.upper())[0] == bootstrap.NOT_BEHIND

    @pytest.mark.parametrize("body", [
        "<html><body>Sign in to the guest network</body></html>",
        "<html><body>Access denied by corporate policy</body></html>",
        "",
        "not html at all",
    ])
    def test_something_that_is_not_the_zscaler_page_is_unknown(self, body):
        """A captive portal or a block page answering on that hostname must not be
        read as either answer - http is hijackable, which is the point."""
        assert bootstrap.interpret(body)[0] == bootstrap.UNKNOWN

    def test_an_unreachable_check_is_unknown(self):
        assert bootstrap.interpret(None)[0] == bootstrap.UNKNOWN

    def test_scripts_cannot_fake_the_marker(self):
        """The marker is looked for in the visible text, so a script or a comment
        mentioning it does not decide the answer."""
        page = BEHIND_PAGE.replace(
            "</main>",
            "<script>var s = 'not going through the Zscaler proxy service';</script></main>")
        assert bootstrap.interpret(page)[0] == bootstrap.BEHIND

    def test_the_check_is_plain_http(self):
        """https would fail exactly where Zscaler is present: it re-signs TLS, and
        this script runs before the venv exists, so there is no truststore yet."""
        assert bootstrap.ZSCALER_CHECK_URL.startswith("http://")

    def test_a_failed_fetch_returns_none_rather_than_raising(self, capsys):
        # Port 9 is discard; nothing listens on it.
        assert bootstrap.fetch("http://127.0.0.1:9/", timeout=2) is None
        assert "could not reach" in capsys.readouterr().out


class TestWhetherTheProxyIsUsed:
    def test_behind_zscaler_uses_it(self):
        proxy, why = bootstrap.proxy_for(
            bootstrap.BEHIND, False, False, bootstrap.ZSCALER_PROXY)
        assert proxy == "http://zscaler.nationalgrid.com:80"
        assert "Zscaler is active" in why

    def test_not_behind_does_not(self):
        proxy, _ = bootstrap.proxy_for(
            bootstrap.NOT_BEHIND, False, False, bootstrap.ZSCALER_PROXY)
        assert proxy is None

    def test_unknown_does_not_guess(self):
        """Off the corporate network that proxy is unreachable, so assuming it
        would break an install that would otherwise work. The message says how to
        force it."""
        proxy, why = bootstrap.proxy_for(
            bootstrap.UNKNOWN, False, False, bootstrap.ZSCALER_PROXY)
        assert proxy is None
        assert "--force-proxy" in why

    def test_force_and_no_proxy_win_over_the_check(self):
        forced, _ = bootstrap.proxy_for(
            bootstrap.NOT_BEHIND, True, False, bootstrap.ZSCALER_PROXY)
        assert forced == bootstrap.ZSCALER_PROXY
        refused, _ = bootstrap.proxy_for(
            bootstrap.BEHIND, False, True, bootstrap.ZSCALER_PROXY)
        assert refused is None

    def test_a_custom_proxy_is_honoured(self):
        proxy, _ = bootstrap.proxy_for(
            bootstrap.BEHIND, False, False, "http://other.example:3128")
        assert proxy == "http://other.example:3128"


class TestThePipCommand:
    def test_the_proxy_is_appended_not_substituted(self):
        command = bootstrap.pip_command("py", "requirements.txt",
                                        "http://zscaler.nationalgrid.com:80")
        assert command[:6] == ["py", "-m", "pip", "install", "-r", "requirements.txt"]
        assert command[-2:] == ["--proxy", "http://zscaler.nationalgrid.com:80"]

    def test_without_a_proxy_nothing_is_added(self):
        command = bootstrap.pip_command("py", "requirements.txt", None)
        assert "--proxy" not in command

    def test_it_installs_into_the_venv_not_the_running_python(self, tmp_path):
        """The whole point is a local environment; installing with sys.executable
        would put the packages wherever this script happens to be running."""
        python = bootstrap.venv_python(tmp_path / ".venv")
        command = bootstrap.pip_command(python, "requirements.txt", None)
        assert str(tmp_path) in command[0]
        assert command[0] != sys.executable


class TestVenvLayout:
    def test_windows_and_posix_paths(self):
        """The platform is passed in rather than patched onto os.name: pathlib
        reads that same flag, so patching it makes Path() raise
        NotImplementedError on Linux - and takes pytest's error reporting with
        it, which is how this was found."""
        assert bootstrap.venv_python("env", windows=True).parts[-2:] == \
            ("Scripts", "python.exe")
        assert bootstrap.venv_python("env", windows=False).parts[-2:] == \
            ("bin", "python")

    def test_it_follows_this_platform_by_default(self):
        expected = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
        assert bootstrap.venv_python("env").parts[-2:] == expected

    def test_an_existing_venv_is_reused(self, tmp_path, capsys):
        python = bootstrap.venv_python(tmp_path / ".venv")
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\n")
        found, code = bootstrap.ensure_venv(tmp_path / ".venv", dry_run=False)
        assert code == 0 and found == python
        assert "already present" in capsys.readouterr().out

    def test_a_missing_venv_is_created(self, tmp_path, capsys):
        with_dry_run = bootstrap.ensure_venv(tmp_path / ".venv", dry_run=True)
        assert with_dry_run[1] == 0
        out = capsys.readouterr().out
        assert "Creating virtual environment" in out
        assert "-m venv" in out
        # Dry run creates nothing.
        assert not (tmp_path / ".venv").exists()


class TestTheScriptRuns:
    """bootstrap.py is the one file that runs before anything is installed, so it
    has to work on a bare interpreter."""

    def test_it_imports_only_the_standard_library(self):
        with open(os.path.join(REPO_ROOT, "bootstrap.py"), encoding="utf-8") as handle:
            source = handle.read()
        third_party = ["requests", "geopandas", "pandas", "keyring", "truststore",
                       "leakrelocation"]
        for name in third_party:
            assert f"import {name}" not in source, name

    def test_dry_run_changes_nothing(self, tmp_path):
        requirements = tmp_path / "requirements.txt"
        requirements.write_text("wheel\n")
        code = subprocess.call(
            [sys.executable, os.path.join(REPO_ROOT, "bootstrap.py"), "--dry-run",
             "--skip-check", "--venv", str(tmp_path / ".venv"),
             "--requirements", str(requirements)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert code == 0
        assert not (tmp_path / ".venv").exists()

    def test_opposite_flags_are_refused(self, tmp_path):
        code = subprocess.call(
            [sys.executable, os.path.join(REPO_ROOT, "bootstrap.py"),
             "--force-proxy", "--no-proxy"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert code == 2

    def test_a_missing_requirements_file_stops_it(self, tmp_path):
        code = subprocess.call(
            [sys.executable, os.path.join(REPO_ROOT, "bootstrap.py"), "--skip-check",
             "--requirements", str(tmp_path / "nope.txt")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert code == 1

    def test_force_proxy_reaches_the_pip_command(self, tmp_path):
        requirements = tmp_path / "requirements.txt"
        requirements.write_text("wheel\n")
        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "bootstrap.py"), "--dry-run",
             "--force-proxy", "--venv", str(tmp_path / ".venv"),
             "--requirements", str(requirements)],
            capture_output=True, text=True, check=False)
        assert result.returncode == 0
        assert "--proxy http://zscaler.nationalgrid.com:80" in result.stdout

    def test_the_requirements_file_it_defaults_to_is_the_real_one(self):
        assert bootstrap.REPO_ROOT == bootstrap.Path(REPO_ROOT)
        assert (bootstrap.REPO_ROOT / "requirements.txt").is_file()
