"""Opening the browser for an ArcGIS sign-in, and closing it again afterwards.

Closing is the hard half. A page may only close a window that a script opened,
or one that has never navigated; a tab that has been through sign-in and
"Allow" is neither, so `window.close()` on the page it lands on is refused.
That is a rule of the browser, not a bug in the page, and it is why the tab
used to stay behind saying it could be closed.

So the sign-in is opened in a window of its own - the browser's "app" window,
which holds one page and no tabs, in the user's normal profile so they are
still signed in to whatever they were signed in to. A window like that can be
closed from outside, by asking Windows to close it, and closing it cannot take
anyone's other tabs with it.

Where that is not available - another operating system, a browser that has no
app window - the sign-in opens in an ordinary tab as before. On Windows that
tab is still closed, with the keystroke a person would use, and only while it
is demonstrably the tab in front.

Nothing here reads a token or an authorization code.
"""
import ntpath
import os
import shlex
import subprocess
import sys
import time
import webbrowser

# Absolute imports with this path setup, rather than relative imports, so the
# module also works when loaded by file path or run directly - not only when
# imported as a package member. See leakrelocation/output.py for the full note.
_PACKAGE_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PACKAGE_PARENT not in sys.path:
    sys.path.insert(0, _PACKAGE_PARENT)

from leakrelocation import config
from leakrelocation.output import detail

APP = "app"
TAB = "tab"

# Browsers built on Chromium, which all take --app=URL. Firefox has no
# equivalent, so it gets a tab.
CHROMIUM_BROWSERS = ("msedge.exe", "chrome.exe", "brave.exe", "vivaldi.exe",
                     "chromium.exe")

# How long to look for the sign-in window before leaving it for the person to
# close. It is on screen within a fraction of a second when it is there at all.
CLOSE_TIMEOUT_SECONDS = 3.0

# A tab is only closed once it has been the one in front for this long. If the
# page managed to close itself, it is gone well inside this, and the keystroke
# is never sent to whatever tab was behind it.
TAB_SETTLE_SECONDS = 0.4

_POLL_SECONDS = 0.05


def on_windows():
    return sys.platform == "win32"


def executable_from_command(command):
    """The program in a registry "open" command, or "" if there is none.

    The registry holds the whole command line - `"C:\\...\\msedge.exe"
    --single-argument %1` - and the program is its first token, quoted whenever
    its path has a space in it.
    """
    command = (command or "").strip()
    if not command:
        return ""
    if command.startswith('"'):
        end = command.find('"', 1)
        return command[1:end] if end > 0 else ""
    try:
        return shlex.split(command, posix=False)[0]
    except (ValueError, IndexError):
        return ""


def default_browser_executable(registry=None):
    """The program Windows opens an https link with, or "" when it cannot be told.

    Asked rather than assumed, because the default browser is the one the
    person is signed in to the portal with - opening a different one would
    make them sign in again.
    """
    if registry is None:
        if not on_windows():
            return ""
        registry = _read_registry
    try:
        prog_id = registry(
            "HKCU",
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations"
            r"\https\UserChoice",
            "ProgId")
        if not prog_id:
            return ""
        command = registry("HKCR", prog_id + r"\shell\open\command", "")
    except OSError as ex:
        detail(f"Could not read the default browser from the registry: {ex}")
        return ""
    return executable_from_command(command)


def _read_registry(root, key, value):
    import winreg
    hive = {"HKCU": winreg.HKEY_CURRENT_USER,
            "HKCR": winreg.HKEY_CLASSES_ROOT}[root]
    with winreg.OpenKey(hive, key) as handle:
        return str(winreg.QueryValueEx(handle, value)[0])


def has_app_window(executable):
    """Whether this browser can open a page in a window of its own."""
    # ntpath, so a Windows path is read the same way on any system.
    return ntpath.basename(executable or "").lower() in CHROMIUM_BROWSERS


def open_signin(url, executable=None, launch=None, extra_args=()):
    """Open the sign-in page. Returns APP or TAB, saying which it opened in.

    An app window when the default browser has one and
    LEAKRELOCATION_SIGNIN_WINDOW has not been set to "tab"; an ordinary tab
    otherwise, or if the app window could not be started.
    """
    if config.SIGNIN_WINDOW != TAB:
        if executable is None:
            executable = default_browser_executable()
        if has_app_window(executable):
            try:
                (launch or _launch)([executable, *extra_args, f"--app={url}"])
            except OSError as ex:
                detail(f"Could not open a sign-in window with {executable}: "
                       f"{ex}. Using a browser tab instead.")
            else:
                detail(f"Sign-in opened in its own window: {executable}")
                return APP
    webbrowser.open(url, new=1, autoraise=True)
    return TAB


def _launch(command):
    # The browser hands the page to the copy of itself that is already running
    # and exits, so there is nothing to wait for and nothing to keep hold of.
    subprocess.Popen(command, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                     close_fds=True)


def close_signin_window(title, api=None, timeout=None, clock=time.monotonic,
                        sleep=time.sleep):
    """Close the window or tab showing the page with this title.

    `title` is the title of the page the sign-in ends on, which carries a
    random marker so that no other window can have it. Two cases:

    * a window whose title is exactly that - one holding nothing but this
      page - is asked to close, wherever it is on screen;
    * a browser window whose title starts with it - the page is the tab in
      view, with the browser's name after it - gets Ctrl+W, and only while
      that window is the one in front and has been for a moment.

    Anything else is left alone. Returns whether something was closed; False
    costs nothing but a tab the person closes themselves.
    """
    if api is None:
        if not on_windows():
            return False
        api = Win32Windows()
    timeout = CLOSE_TIMEOUT_SECONDS if timeout is None else timeout
    deadline = clock() + timeout
    in_front_since = None
    while True:
        try:
            windows = api.windows()
            matching = [(handle, text) for handle, text in windows
                        if text.startswith(title)]
            for handle, text in matching:
                if text.strip() == title:
                    api.close(handle)
                    detail("Closed the sign-in window.")
                    return True
            in_front = api.foreground()
            if any(handle == in_front for handle, _ in matching):
                if in_front_since is None:
                    in_front_since = clock()
                elif clock() - in_front_since >= TAB_SETTLE_SECONDS:
                    api.close_tab()
                    detail("Closed the sign-in tab.")
                    return True
            else:
                in_front_since = None
        except OSError as ex:
            detail(f"Could not close the sign-in window: {ex}")
            return False
        if clock() >= deadline:
            return False
        sleep(_POLL_SECONDS)


class Win32Windows:
    """The few window operations closing a sign-in needs, through user32."""

    WM_CLOSE = 0x0010
    VK_CONTROL = 0x11
    VK_W = 0x57
    KEYEVENTF_KEYUP = 0x0002

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._enum_proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [self._enum_proc, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [
            wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.keybd_event.argtypes = [
            wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
        user32.keybd_event.restype = None
        self._user32 = user32

    def title(self, handle):
        length = self._user32.GetWindowTextLengthW(handle)
        if length <= 0:
            return ""
        buffer = self._ctypes.create_unicode_buffer(length + 1)
        self._user32.GetWindowTextW(handle, buffer, length + 1)
        return buffer.value

    def windows(self):
        """(handle, title) for every visible top-level window with a title."""
        found = []

        def visit(handle, _lparam):
            if self._user32.IsWindowVisible(handle):
                text = self.title(handle)
                if text:
                    found.append((int(handle or 0), text))
            return True

        self._user32.EnumWindows(self._enum_proc(visit), 0)
        return found

    def foreground(self):
        return int(self._user32.GetForegroundWindow() or 0)

    def close(self, handle):
        """Ask a window to close, the way its own close button does."""
        self._user32.PostMessageW(handle, self.WM_CLOSE, 0, 0)

    def close_tab(self):
        """Ctrl+W, to whichever window is in front."""
        press = self._user32.keybd_event
        press(self.VK_CONTROL, 0, 0, 0)
        press(self.VK_W, 0, 0, 0)
        press(self.VK_W, 0, self.KEYEVENTF_KEYUP, 0)
        press(self.VK_CONTROL, 0, self.KEYEVENTF_KEYUP, 0)
