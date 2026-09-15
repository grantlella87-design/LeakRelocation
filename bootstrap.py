"""Set up the local environment: detect Zscaler, make a venv, install into it.

    python bootstrap.py                 detect, create .venv, install
    python bootstrap.py --dry-run       print what it would do, change nothing
    python bootstrap.py --force-proxy   install through the proxy regardless
    python bootstrap.py --no-proxy      never use the proxy

Why this exists: on the corporate network the outbound path is Zscaler, so pip
cannot reach PyPI without being told about the proxy, and off the network the same
proxy is unreachable and would break an install that would otherwise work. Which
of the two you are on is not something to remember - it is something to check.

Standard library only, and it must stay that way: this is the script that runs
before anything is installed.
"""
import argparse
import html
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# Zscaler's own "My IP Address" page. Fetched over http rather than https on
# purpose: when Zscaler is active it re-signs TLS with an internal root, and this
# script runs before the venv exists, so there is no truststore yet to verify
# against the Windows certificate store. An https probe would fail exactly where
# Zscaler is present - reporting "no Zscaler" in the one case that needs it.
ZSCALER_CHECK_URL = "http://ip.zscaler.com/"
ZSCALER_PROXY = "http://zscaler.nationalgrid.com:80"

# Read off the live page. The full sentence is "The request received from you
# didn't come from a Zscaler IP therefore you are not going through the Zscaler
# proxy service." The decision keys on this because it is the half that was
# verified against the real service; the wording when you *are* behind Zscaler
# names the cloud and was not observable from outside the network.
NOT_BEHIND_MARKER = "not going through the zscaler"

# Something that only Zscaler's page carries, so a captive portal or a corporate
# block page answering on this hostname is reported as unknown rather than taken
# for a positive.
PAGE_MARKERS = ("zscaler cloud security", "ip.zscaler.com", "zscaler proxy")

BEHIND = "behind"
NOT_BEHIND = "not-behind"
UNKNOWN = "unknown"


def log(text=""):
    print(text, flush=True)


def visible_text(page):
    """The page's text with markup, scripts and styles removed."""
    without = re.sub(r"<script.*?</script>", " ", page, flags=re.DOTALL | re.IGNORECASE)
    without = re.sub(r"<style.*?</style>", " ", without, flags=re.DOTALL | re.IGNORECASE)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", without)))


def fetch(url, timeout):
    """Return the page body, or None with the reason logged."""
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "LeakRelocation-bootstrap/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError) as ex:
        log(f"  could not reach {url}: {ex}")
        return None


def interpret(page):
    """(status, detail) for a fetched check page."""
    if page is None:
        return UNKNOWN, "the check page could not be fetched"
    text = visible_text(page)
    lowered = text.lower()
    if not any(marker in lowered for marker in PAGE_MARKERS):
        # Something answered, but not Zscaler. A captive portal or a block page
        # must not be read as either answer.
        return UNKNOWN, "the reply did not look like Zscaler's check page"
    address = re.search(r"from the IP address ([0-9a-fA-F:.]+)", text)
    seen_from = f", seen from {address.group(1)}" if address else ""
    if NOT_BEHIND_MARKER in lowered:
        return NOT_BEHIND, f"Zscaler says the request did not come from its network{seen_from}"
    return BEHIND, f"Zscaler answered without its 'not going through' notice{seen_from}"


def detect_zscaler(url=ZSCALER_CHECK_URL, timeout=10):
    log(f"Checking Zscaler: {url}")
    status, detail = interpret(fetch(url, timeout))
    log(f"  {status}: {detail}")
    return status


def venv_python(venv_dir, windows=None):
    """The interpreter inside a venv, on this platform.

    `windows` is a parameter rather than a read of os.name inside the body so the
    other layout can be exercised without patching os.name globally - pathlib
    picks WindowsPath from that same flag, so patching it makes Path() raise
    NotImplementedError on Linux, which took pytest's error formatting with it.
    """
    if windows is None:
        windows = os.name == "nt"
    if windows:
        return Path(venv_dir) / "Scripts" / "python.exe"
    return Path(venv_dir) / "bin" / "python"


def pip_command(python, requirements, proxy=None):
    """The pip install to run. The proxy is appended, not substituted."""
    command = [str(python), "-m", "pip", "install", "-r", str(requirements)]
    if proxy:
        command += ["--proxy", proxy]
    return command


def run(command, dry_run):
    log("  " + " ".join(command))
    if dry_run:
        return 0
    return subprocess.call(command)


def ensure_venv(venv_dir, dry_run):
    """Create the venv if it is not already there. Returns its interpreter."""
    python = venv_python(venv_dir)
    if python.exists():
        log(f"Virtual environment already present: {venv_dir}")
        return python, 0
    log(f"Creating virtual environment: {venv_dir}")
    code = run([sys.executable, "-m", "venv", str(venv_dir)], dry_run)
    if code != 0:
        log(f"  venv creation failed with exit code {code}")
        return python, code
    if not dry_run and not python.exists():
        log(f"  venv reported success but {python} is not there")
        return python, 1
    return python, 0


def proxy_for(status, force_proxy, no_proxy, proxy_url):
    """Whether to install through the proxy, and the message that explains it."""
    if no_proxy:
        return None, "--no-proxy: installing directly"
    if force_proxy:
        return proxy_url, f"--force-proxy: installing through {proxy_url}"
    if status == BEHIND:
        return proxy_url, f"Zscaler is active, so pip goes through {proxy_url}"
    if status == NOT_BEHIND:
        return None, "Zscaler is not in the path, so pip goes direct"
    return None, ("Zscaler could not be determined, so pip goes direct. "
                  "If the install cannot reach PyPI, re-run with --force-proxy")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--venv", default=str(REPO_ROOT / ".venv"),
                        help="Virtual environment directory. Default: .venv")
    parser.add_argument("--requirements", default=str(REPO_ROOT / "requirements.txt"),
                        help="Requirements file to install.")
    parser.add_argument("--proxy", default=ZSCALER_PROXY,
                        help=f"Proxy to use when Zscaler is active. Default: {ZSCALER_PROXY}")
    parser.add_argument("--force-proxy", action="store_true",
                        help="Use the proxy without checking.")
    parser.add_argument("--no-proxy", action="store_true",
                        help="Never use the proxy.")
    parser.add_argument("--skip-check", action="store_true",
                        help="Do not contact Zscaler at all.")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="Seconds to wait for the Zscaler check. Default: 10")
    parser.add_argument("--check-url", default=ZSCALER_CHECK_URL,
                        help="Where to check. Default: %(default)s")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the commands without running them.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.force_proxy and args.no_proxy:
        log("--force-proxy and --no-proxy ask for opposite things.")
        return 2

    log("=== LeakRelocation bootstrap ===")
    log(f"Python: {sys.version.split()[0]} ({sys.executable})")

    requirements = Path(args.requirements)
    if not requirements.is_file():
        log(f"No requirements file at {requirements}")
        return 1

    if args.skip_check or args.no_proxy or args.force_proxy:
        status = UNKNOWN
        log("Skipping the Zscaler check.")
    else:
        status = detect_zscaler(args.check_url, args.timeout)

    proxy, why = proxy_for(status, args.force_proxy, args.no_proxy, args.proxy)
    log(why)

    python, code = ensure_venv(args.venv, args.dry_run)
    if code != 0:
        return code

    log(f"Installing {requirements}")
    code = run(pip_command(python, requirements, proxy), args.dry_run)
    if code != 0:
        log(f"  pip failed with exit code {code}")
        if proxy is None and status != NOT_BEHIND:
            log("  If this machine is on the corporate network, try: "
                "python bootstrap.py --force-proxy")
        return code

    if args.dry_run:
        log("Dry run: nothing was created or installed.")
        return 0

    log("")
    log("Done. Next:")
    log(f"  {python} run.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
