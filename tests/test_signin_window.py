"""The sign-in window has to close when the sign-in is done - and nothing else may.

The tab used to stay behind saying "this tab can be closed". The page asked to
close itself and the browser said no: a script may only close a window a script
opened, or one that has never navigated, and a tab that has been through
sign-in and "Allow" is neither. The keystroke that was sent to clean up after
it went to whatever window happened to be in front.

So the tests here are mostly about what must NOT be closed.
"""
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from leakrelocation import config
from leakrelocation import signin_window as sw

TITLE = "ArcGIS sign-in complete 3fa9c1"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
FIREFOX = r"C:\Program Files\Mozilla Firefox\firefox.exe"


class Desktop:
    """A stand-in for the windows on screen, and a record of what was done."""

    def __init__(self, windows, foreground=0):
        self.open = dict(windows)          # handle -> title
        self.in_front = foreground
        self.closed = []
        self.keystrokes = 0
        self.looks = 0
        self.on_look = None

    def windows(self):
        self.looks += 1
        if self.on_look:
            self.on_look(self)
        return list(self.open.items())

    def foreground(self):
        return self.in_front

    def close(self, handle):
        self.closed.append(handle)
        self.open.pop(handle, None)

    def close_tab(self):
        self.keystrokes += 1


class Clock:
    """Time that only moves when something sleeps."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def close(desktop, title=TITLE, timeout=3.0):
    clock = Clock()
    return sw.close_signin_window(title, api=desktop, timeout=timeout,
                                  clock=clock, sleep=clock.sleep)


class TestAWindowOfItsOwn:
    """An app window holds one page, so its title is the page's and nothing
    more. It is asked to close, wherever it is on screen."""

    def test_it_is_closed(self):
        desktop = Desktop({10: TITLE})
        assert close(desktop) is True
        assert desktop.closed == [10]

    def test_it_is_closed_even_when_it_is_not_in_front(self):
        """The person may have clicked back to the terminal already. Asking a
        window to close does not need it to have the focus."""
        desktop = Desktop({10: TITLE, 20: "Windows PowerShell"}, foreground=20)
        assert close(desktop) is True
        assert desktop.closed == [10]

    def test_no_keystroke_is_sent(self):
        desktop = Desktop({10: TITLE}, foreground=10)
        close(desktop)
        assert desktop.keystrokes == 0

    def test_it_is_found_when_it_appears_late(self):
        desktop = Desktop({20: "Windows PowerShell"}, foreground=20)

        def appear(d):
            if d.looks == 5:
                d.open[10] = TITLE
        desktop.on_look = appear
        assert close(desktop) is True
        assert desktop.closed == [10]


class TestATabInAnOrdinaryWindow:
    """The page is the tab in view, so the window's title is the page's with
    the browser's name after it. Closing that window would take every other
    tab with it, so it gets the keystroke a person would use instead."""

    BROWSER = TITLE + " - Personal - Microsoft\u200b Edge"

    def test_the_tab_is_closed_with_a_keystroke(self):
        desktop = Desktop({10: self.BROWSER}, foreground=10)
        assert close(desktop) is True
        assert desktop.keystrokes == 1

    def test_the_window_itself_is_never_closed(self):
        desktop = Desktop({10: self.BROWSER}, foreground=10)
        close(desktop)
        assert desktop.closed == []

    def test_nothing_is_sent_when_another_window_is_in_front(self):
        """The whole danger of a keystroke: it goes to whatever is in front.
        Ctrl+W in an editor closes the file being worked on."""
        desktop = Desktop({10: self.BROWSER, 20: "workflow.py - Visual Studio Code"},
                          foreground=20)
        assert close(desktop) is False
        assert desktop.keystrokes == 0
        assert desktop.closed == []

    def test_nothing_is_sent_if_the_tab_closes_itself_first(self):
        """When the portal sends the browser straight back, the page is allowed
        to close itself, and does. The tab behind it then comes to the front of
        the same window - and must not get the keystroke meant for this one."""
        desktop = Desktop({10: self.BROWSER}, foreground=10)

        def self_close(d):
            if d.looks == 3:
                d.open[10] = "Inbox - Outlook - Personal - Microsoft\u200b Edge"
        desktop.on_look = self_close
        assert close(desktop) is False
        assert desktop.keystrokes == 0

    def test_it_waits_for_the_tab_to_settle_before_sending(self):
        desktop = Desktop({10: self.BROWSER}, foreground=10)
        clock = Clock()
        sw.close_signin_window(TITLE, api=desktop, timeout=3.0,
                               clock=clock, sleep=clock.sleep)
        assert clock.now >= sw.TAB_SETTLE_SECONDS

    def test_losing_the_front_restarts_the_wait(self):
        """In front, then not, then in front again: the settle time counts from
        the last time it came to the front, not the first."""
        desktop = Desktop({10: self.BROWSER, 20: "Terminal"}, foreground=10)
        sent_at = []

        def flicker(d):
            d.in_front = 20 if d.looks in (4, 5) else 10
        desktop.on_look = flicker
        clock = Clock()
        original = desktop.close_tab
        desktop.close_tab = lambda: (sent_at.append(clock.now), original())
        sw.close_signin_window(TITLE, api=desktop, timeout=3.0,
                               clock=clock, sleep=clock.sleep)
        came_back = 5 * sw._POLL_SECONDS
        assert sent_at and sent_at[0] >= came_back + sw.TAB_SETTLE_SECONDS


class TestWhatIsLeftAlone:
    def test_a_window_with_a_different_title_is_never_touched(self):
        desktop = Desktop({10: "ArcGIS sign-in complete 000000",
                           20: "Sign in - ArcGIS - Microsoft\u200b Edge",
                           30: "Inbox - Outlook"}, foreground=20)
        assert close(desktop) is False
        assert desktop.closed == []
        assert desktop.keystrokes == 0

    def test_a_title_that_merely_contains_it_is_not_a_match(self):
        """It has to be at the start. A page about this page is not this page."""
        desktop = Desktop({10: "Search: " + TITLE + " - Google Chrome"},
                          foreground=10)
        assert close(desktop) is False
        assert desktop.keystrokes == 0

    def test_it_gives_up_after_the_timeout(self):
        desktop = Desktop({20: "Terminal"}, foreground=20)
        clock = Clock()
        closed = sw.close_signin_window(TITLE, api=desktop, timeout=1.0,
                                        clock=clock, sleep=clock.sleep)
        assert closed is False
        assert 1.0 <= clock.now < 1.5

    def test_a_zero_timeout_looks_once_and_stops(self):
        desktop = Desktop({20: "Terminal"}, foreground=20)
        assert close(desktop, timeout=0) is False
        assert desktop.looks == 1

    def test_a_failure_to_look_is_not_an_error(self):
        """Closing the window is a courtesy. It must never be what stops a
        sign-in that has already succeeded."""
        class Broken(Desktop):
            def windows(self):
                raise OSError("access denied")
        assert close(Broken({})) is False

    def test_elsewhere_than_windows_it_does_nothing(self, monkeypatch):
        monkeypatch.setattr(sw, "on_windows", lambda: False)
        assert sw.close_signin_window(TITLE, timeout=0) is False


class TestFindingTheDefaultBrowser:
    def registry(self, prog_id, command):
        def read(root, key, value):
            if root == "HKCU" and key.endswith(r"https\UserChoice"):
                return prog_id
            if root == "HKCR" and key == prog_id + r"\shell\open\command":
                return command
            raise FileNotFoundError(key)
        return read

    def test_edge(self):
        read = self.registry("MSEdgeHTM", f'"{EDGE}" --single-argument %1')
        assert sw.default_browser_executable(read) == EDGE

    def test_a_path_with_no_quotes(self):
        read = self.registry("AppX", r"C:\Tools\chrome.exe -- %1")
        assert sw.default_browser_executable(read) == r"C:\Tools\chrome.exe"

    def test_no_default_recorded(self):
        def read(root, key, value):
            raise FileNotFoundError(key)
        assert sw.default_browser_executable(read) == ""

    def test_an_empty_command(self):
        assert sw.default_browser_executable(self.registry("X", "")) == ""

    def test_elsewhere_than_windows_there_is_none(self, monkeypatch):
        monkeypatch.setattr(sw, "on_windows", lambda: False)
        assert sw.default_browser_executable() == ""

    @pytest.mark.parametrize("path,expected", [
        (EDGE, True),
        (r"C:\Program Files\Google\Chrome\Application\chrome.exe", True),
        (r"C:\Program Files\BraveSoftware\Brave-Browser\Application\BRAVE.EXE", True),
        (FIREFOX, False),
        ("", False),
        (None, False),
    ])
    def test_which_browsers_have_a_window_of_their_own(self, path, expected):
        assert sw.has_app_window(path) is expected


class TestOpeningTheSignIn:
    URL = "https://portal.example/oauth2/authorize?client_id=x&redirect_uri=y"

    @pytest.fixture
    def tabs(self, monkeypatch):
        opened = []
        monkeypatch.setattr(sw.webbrowser, "open",
                            lambda url, *a, **k: opened.append(url) or True)
        return opened

    def test_the_default_browser_opens_it_in_its_own_window(self, tabs, monkeypatch):
        monkeypatch.setattr(config, "SIGNIN_WINDOW", "app")
        launched = []
        assert sw.open_signin(self.URL, executable=EDGE,
                              launch=launched.append) == sw.APP
        assert launched == [[EDGE, f"--app={self.URL}"]]
        assert tabs == []

    def test_the_address_is_one_argument_however_many_ampersands_it_has(
            self, tabs, monkeypatch):
        """Handed to the browser as a list, never through a shell, where the
        first & in the address would end the command."""
        monkeypatch.setattr(config, "SIGNIN_WINDOW", "app")
        launched = []
        sw.open_signin(self.URL, executable=EDGE, launch=launched.append)
        assert len(launched[0]) == 2
        assert launched[0][1].endswith("&redirect_uri=y")

    def test_no_profile_is_named(self, tabs, monkeypatch):
        """The window opens in the person's own profile, where they are already
        signed in to the portal. A separate profile would ask for their
        password and second factor every time."""
        monkeypatch.setattr(config, "SIGNIN_WINDOW", "app")
        launched = []
        sw.open_signin(self.URL, executable=EDGE, launch=launched.append)
        assert not any("user-data-dir" in part for part in launched[0])

    def test_a_browser_without_one_gets_a_tab(self, tabs, monkeypatch):
        monkeypatch.setattr(config, "SIGNIN_WINDOW", "app")
        launched = []
        assert sw.open_signin(self.URL, executable=FIREFOX,
                              launch=launched.append) == sw.TAB
        assert launched == []
        assert tabs == [self.URL]

    def test_a_browser_that_will_not_start_falls_back_to_a_tab(self, tabs, monkeypatch):
        monkeypatch.setattr(config, "SIGNIN_WINDOW", "app")

        def refuse(command):
            raise OSError("blocked by policy")
        assert sw.open_signin(self.URL, executable=EDGE, launch=refuse) == sw.TAB
        assert tabs == [self.URL]

    def test_tab_can_be_asked_for(self, tabs, monkeypatch):
        monkeypatch.setattr(config, "SIGNIN_WINDOW", "tab")
        launched = []
        assert sw.open_signin(self.URL, executable=EDGE,
                              launch=launched.append) == sw.TAB
        assert launched == []
        assert tabs == [self.URL]

    def test_tests_never_open_a_real_window(self):
        """The shared setup forces the stub. Without it, every sign-in test
        would open a browser on a Windows machine."""
        assert config.SIGNIN_WINDOW == "tab"
        assert sw.CLOSE_TIMEOUT_SECONDS == 0


@pytest.mark.skipif(sys.platform != "win32", reason="the real user32 calls")
class TestAgainstWindowsItself:
    """The stand-in above is only as good as its resemblance to Windows. These
    make the real calls, on the Windows CI runner and on any Windows machine
    that runs the suite."""

    def test_the_windows_on_screen_can_be_listed(self):
        api = sw.Win32Windows()
        listed = api.windows()
        assert isinstance(listed, list)
        for handle, title in listed:
            assert isinstance(handle, int) and handle
            assert isinstance(title, str) and title
        assert isinstance(api.foreground(), int)

    def test_the_registry_lookup_answers_without_raising(self):
        assert isinstance(sw.default_browser_executable(), str)

    def test_looking_for_a_window_that_is_not_there_closes_nothing(self):
        before = {handle for handle, _ in sw.Win32Windows().windows()}
        assert sw.close_signin_window(
            "No window has this title 7c1e55", timeout=0.3) is False
        after = {handle for handle, _ in sw.Win32Windows().windows()}
        assert before <= after

    def test_a_real_sign_in_window_is_opened_and_closed(self, tmp_path, monkeypatch):
        """The whole thing, with a real browser: open a page in a window of
        its own, find that window by its title, close it, and see it gone.

        Skipped, not failed, where there is no such browser or no desktop for
        it to draw on - that says nothing about this code. Once the window has
        been seen, failing to close it is a failure.
        """
        browser = next((path for path in (
            sw.default_browser_executable(),
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        ) if sw.has_app_window(path) and os.path.isfile(path)), "")
        if not browser:
            pytest.skip("no Chromium-family browser on this machine")

        title = f"ArcGIS sign-in complete {os.urandom(3).hex()}"
        page = (f"<!doctype html><title>{title}</title>"
                f"<p>sign-in window test</p>").encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        monkeypatch.setattr(config, "SIGNIN_WINDOW", "app")
        api = sw.Win32Windows()

        def showing():
            return [(h, t) for h, t in api.windows() if t.startswith(title)]

        try:
            # A profile of its own here, and only here: the runner's browser
            # has never been started, and its first-run screens would be what
            # opened instead of the page.
            opened = sw.open_signin(url, executable=browser, extra_args=(
                f"--user-data-dir={tmp_path}", "--no-first-run",
                "--no-default-browser-check"))
            assert opened == sw.APP

            deadline = time.monotonic() + 45
            while not showing() and time.monotonic() < deadline:
                time.sleep(0.25)
            seen = showing()
            if not seen:
                pytest.skip("the browser window never appeared - no desktop "
                            "to draw on here")
            # A window of its own carries the page's title and nothing after
            # it. Everything about closing it without a keystroke rests on
            # that, so it is checked against the real browser.
            assert [text for _, text in seen] == [title], (
                f"the sign-in window is titled {[text for _, text in seen]}")

            class Recording(sw.Win32Windows):
                keystrokes = 0

                def close_tab(self):
                    Recording.keystrokes += 1
                    super().close_tab()

            assert sw.close_signin_window(
                title, api=Recording(), timeout=10) is True
            assert Recording.keystrokes == 0, "it was closed with a keystroke"

            deadline = time.monotonic() + 15
            while showing() and time.monotonic() < deadline:
                time.sleep(0.25)
            assert showing() == [], "the sign-in window is still open"
        finally:
            server.shutdown()
            server.server_close()
