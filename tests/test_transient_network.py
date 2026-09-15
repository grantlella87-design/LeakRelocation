"""Transient network failures must not end a run.

A download of the pipe layers is hundreds of requests over many minutes through a
corporate proxy. One being dropped is ordinary; it used to be fatal:

    RuntimeError: distribution pipes delta: objectId POST batch 20/322 failed:
    ('Connection aborted.', ConnectionResetError(10054, 'An existing connection
    was forcibly closed by the remote host', None, 10054, None))

with another layer 584 batches of 588 through, all of it thrown away.

The retry is exercised against a server that really resets the connection - closed
with SO_LINGER at zero, which sends RST - rather than against a mock that raises
the exception directly.
"""
import importlib.util
import io
import os
import socket
import struct
import sys
import threading
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

pytest.importorskip("geopandas")
pytest.importorskip("keyring")

import requests

from leakrelocation import config


@pytest.fixture(scope="module")
def lr():
    path = os.path.join(REPO_ROOT, "src", "leak_relocation_geopandas.py")
    spec = importlib.util.spec_from_file_location("lr_retry", path)
    module = importlib.util.module_from_spec(spec)
    with redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)
    # Keep the tests quick; the behaviour under test is the retrying, not the wait.
    module.REQUEST_RETRY_BACKOFF_SECONDS = 0
    return module


class ResetHandler(BaseHTTPRequestHandler):
    # HTTP/1.0, so an abruptly closed connection cannot leave the server waiting
    # for another request on it.
    protocol_version = "HTTP/1.0"
    resets_left = 0
    status_failures_left = 0
    failure_status = 503
    seen = 0

    def do_POST(self):
        type(self).seen += 1
        if type(self).resets_left > 0:
            type(self).resets_left -= 1
            self.connection.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            self.connection.close()
            return
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if type(self).status_failures_left > 0:
            type(self).status_failures_left -= 1
            self.send_response(type(self).failure_status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b'{"features": [{"attributes": {"OBJECTID": 1}, "geometry": null}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    ResetHandler.resets_left = 0
    ResetHandler.status_failures_left = 0
    ResetHandler.seen = 0
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ResetHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/query"
    httpd.shutdown()


@pytest.fixture
def session():
    made = requests.Session()
    made._arcgis_access_token = "test-token"
    return made


class TestARealResetIsRetried:
    def test_a_post_recovers(self, lr, server, session):
        ResetHandler.resets_left = 2
        with redirect_stdout(io.StringIO()):
            data = lr.request_json_post(session, server, {"f": "json"})
        assert data["features"], "the request never produced data"
        assert ResetHandler.seen == 3, "expected two failures then a success"

    def test_a_get_recovers(self, lr, server, session):
        ResetHandler.resets_left = 1
        with redirect_stdout(io.StringIO()):
            data = lr.request_json(session, server, {"f": "json"})
        assert data["features"]
        assert ResetHandler.seen == 2

    def test_it_gives_up_eventually(self, lr, server, session):
        """A network that is genuinely gone must still end the run rather than
        retrying for ever."""
        ResetHandler.resets_left = 99
        with pytest.raises(requests.exceptions.ConnectionError), \
                redirect_stdout(io.StringIO()):
            lr.request_json_post(session, server, {"f": "json"})
        assert ResetHandler.seen == lr.REQUEST_RETRY_ATTEMPTS

    def test_each_attempt_is_reported(self, lr, server, session):
        """A run that pauses for tens of seconds should say why."""
        ResetHandler.resets_left = 1
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            lr.request_json_post(session, server, {"f": "json"})
        printed = buffer.getvalue()
        assert "ConnectionError" in printed
        assert "retrying in" in printed
        assert f"of {lr.REQUEST_RETRY_ATTEMPTS}" in printed

    def test_a_first_attempt_that_works_is_not_retried(self, lr, server, session):
        with redirect_stdout(io.StringIO()):
            lr.request_json_post(session, server, {"f": "json"})
        assert ResetHandler.seen == 1


class TestTransientStatuses:
    def test_a_gateway_error_is_retried(self, lr, server, session):
        ResetHandler.status_failures_left = 2
        ResetHandler.failure_status = 503
        with redirect_stdout(io.StringIO()):
            data = lr.request_json_post(session, server, {"f": "json"})
        assert data["features"]
        assert ResetHandler.seen == 3

    def test_five_hundred_is_not_retried(self, lr):
        """ArcGIS returns a genuine query error as a 500, and repeating a bad
        query only makes it fail more slowly."""
        assert 500 not in lr.TRANSIENT_STATUSES
        assert 429 in lr.TRANSIENT_STATUSES
        for status in (502, 503, 504):
            assert status in lr.TRANSIENT_STATUSES

    def test_a_four_hundred_is_passed_straight_through(self, lr, server, session):
        ResetHandler.status_failures_left = 5
        ResetHandler.failure_status = 400
        with pytest.raises(RuntimeError, match="HTTP 400"), \
                redirect_stdout(io.StringIO()):
            lr.request_json_post(session, server, {"f": "json"})
        assert ResetHandler.seen == 1, "a 400 must not be retried"


class TestWhatCountsAsTransient:
    def test_the_exceptions_cover_a_reset(self, lr):
        """requests wraps urllib3's ProtocolError and Python's
        ConnectionResetError - WinError 10054 - in ConnectionError."""
        assert requests.exceptions.ConnectionError in lr.TRANSIENT_EXCEPTIONS
        assert requests.exceptions.Timeout in lr.TRANSIENT_EXCEPTIONS
        assert requests.exceptions.ChunkedEncodingError in lr.TRANSIENT_EXCEPTIONS

    def test_an_http_error_is_not_swallowed_as_transient(self, lr):
        assert requests.exceptions.HTTPError not in lr.TRANSIENT_EXCEPTIONS

    def test_the_attempt_count_is_configurable(self):
        assert config.REQUEST_RETRY_ATTEMPTS >= 2
        assert config.REQUEST_RETRY_BACKOFF_SECONDS >= 1

    def test_backoff_grows_and_is_jittered(self, lr, monkeypatch):
        """Eight batches run at once, so a proxy that drops several together must
        not get them all back in the same instant."""
        delays = []
        monkeypatch.setattr(lr.time, "sleep", delays.append)
        monkeypatch.setattr(lr, "REQUEST_RETRY_BACKOFF_SECONDS", 2)
        monkeypatch.setattr(lr, "REQUEST_RETRY_ATTEMPTS", 4)
        calls = {"n": 0}

        def always_reset():
            calls["n"] += 1
            raise requests.exceptions.ConnectionError("reset")

        with pytest.raises(requests.exceptions.ConnectionError), \
                redirect_stdout(io.StringIO()):
            lr.send_with_retry(always_reset, "test")

        assert calls["n"] == 4
        assert len(delays) == 3
        # 2, 4, 8 before jitter, and jitter only ever adds.
        for index, base in enumerate([2, 4, 8]):
            assert base <= delays[index] <= base * 1.5, delays


class TestWorkerSessionsAreReused:
    """A fresh Session per batch meant a fresh TLS handshake per batch: 588 of
    them for one layer, eight at a time, through a proxy that re-signs each one."""

    def test_one_session_per_thread(self, lr):
        seen = {}

        def record(name):
            def go():
                seen[name] = [id(lr.worker_session("token")) for _ in range(3)]
            return go

        threads = [threading.Thread(target=record(f"w{index}")) for index in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        for ids in seen.values():
            assert len(set(ids)) == 1, "a thread built more than one session"
        assert len({ids[0] for ids in seen.values()}) == 3, "threads shared a session"

    def test_the_token_is_carried(self, lr):
        assert lr.worker_session("abc")._arcgis_access_token == "abc"

    def test_a_new_token_replaces_the_old_one_on_the_same_session(self, lr):
        first = lr.worker_session("one")
        second = lr.worker_session("two")
        assert first is second
        assert second._arcgis_access_token == "two"


class TestTheFailureMessageIsActionable:
    def test_it_names_the_knobs(self):
        path = os.path.join(REPO_ROOT, "src", "leak_relocation_geopandas.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        assert "LEAKRELOCATION_DOWNLOAD_WORKERS=2" in source
        assert "LEAKRELOCATION_RETRY_ATTEMPTS" in source
        assert "after {REQUEST_RETRY_ATTEMPTS} attempts" in source
