"""Unit tests for the PVE driver's one request seam, `Pve._request` (br0kr/pve.py, review OPS-03).

Every PVE API call goes through `_request`, which times it and counts its outcome in `PVE_REQUEST_SECONDS`
(`br0kr_pve_request_seconds{op,outcome}`). `requests.Session.request` is stubbed, so no PVE is needed. Pinned: each outcome
class (ok, denied, http_error, timeout, unreachable) lands in its own series whether the call returns or raises,
the four verb helpers route through the seam with their method and form data, and the reply's `data` comes back
unchanged. The counter is process-wide, so each test reads deltas.
"""
from __future__ import annotations

import time
import types
from typing import Any

import pytest
import requests

from br0kr import pve

pytestmark = pytest.mark.unit


class _Reply:
    def __init__(self, status: int, data: Any = None) -> None:
        self.status_code = status
        self._data = data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            r = requests.Response()
            r.status_code = self.status_code
            raise requests.HTTPError(response=r)

    def json(self) -> dict[str, Any]:
        return {"data": self._data}


def _count(op: str, outcome: str) -> int:
    return pve.PVE_REQUEST_SECONDS.snapshot().get((op, outcome), (0.0, 0))[1]


def _driver() -> pve.Pve:
    return pve.Pve("pve.invalid", "node1", "id", "secret", timeout=3)


def test_every_verb_goes_through_the_seam_with_its_method_and_data(monkeypatch):
    """_get/_post/_put/_delete each send their HTTP method, the full API path, the token header and (for POST/PUT)
    the form data through ONE call on the driver's session, and return the reply's `data`. Guards a verb that bypasses the
    seam: its calls would vanish from br0kr_pve_request_seconds while still hitting PVE."""
    seen: list[tuple[str, str, Any]] = []

    def fake_request(session, method, url, *, data, verify, timeout):
        seen.append((method, url, data))
        assert session.headers["Authorization"] == "PVEAPIToken=id=secret" and verify is False
        assert timeout == 3
        return _Reply(200, {"method": method})

    monkeypatch.setattr(pve.requests.Session, "request", fake_request)
    d = _driver()
    assert d._get("/cluster/nextid") == {"method": "GET"}
    assert d._post("/nodes/node1/qemu", {"vmid": 9901}) == {"method": "POST"}
    assert d._put("/nodes/node1/qemu/9901/resize", {"size": "10G"}) == {"method": "PUT"}
    assert d._delete("/nodes/node1/qemu/9901") == {"method": "DELETE"}
    base = "https://pve.invalid:8006/api2/json"
    assert seen == [("GET", base + "/cluster/nextid", None),
                    ("POST", base + "/nodes/node1/qemu", {"vmid": 9901}),
                    ("PUT", base + "/nodes/node1/qemu/9901/resize", {"size": "10G"}),
                    ("DELETE", base + "/nodes/node1/qemu/9901", None)]


@pytest.mark.parametrize("status,outcome", [(200, "ok"), (401, "denied"), (403, "denied"),
                                            (404, "http_error"), (500, "http_error")])
def test_an_http_answer_is_counted_by_its_status_class(monkeypatch, status, outcome):
    """A 2xx counts `ok`; 401/403 count `denied` (an ACL gap, never transient: the one to alert on first); any
    other 4xx/5xx counts `http_error`, and the HTTPError still reaches the caller. Guards a refusal that is raised
    but not counted, or counted as ok."""
    monkeypatch.setattr(pve.requests.Session, "request", lambda *a, **k: _Reply(status, 1))
    before = _count("get", outcome)
    if status >= 400:
        with pytest.raises(requests.HTTPError):
            _driver()._get("/version")
    else:
        assert _driver()._get("/version") == 1
    assert _count("get", outcome) == before + 1


def test_a_2xx_whose_body_is_not_the_api_reply_counts_as_bad_reply(monkeypatch):
    """A 200 whose body is not JSON, or has no `data`, raises to the caller as before but counts `bad_reply`, not
    `ok`. Guards a proxy or error page answering 200 reading as a healthy PVE on /metrics."""
    class _NotJson(_Reply):
        def json(self) -> dict[str, Any]:
            raise ValueError("Expecting value")

    monkeypatch.setattr(pve.requests.Session, "request", lambda *a, **k: _NotJson(200))
    before = (_count("get", "bad_reply"), _count("get", "ok"))
    with pytest.raises(ValueError):
        _driver()._get("/version")

    class _NoData(_Reply):
        def json(self) -> dict[str, Any]:
            return {"errors": {"vmid": "bad"}}

    monkeypatch.setattr(pve.requests.Session, "request", lambda *a, **k: _NoData(200))
    with pytest.raises(KeyError):
        _driver()._get("/version")
    assert (_count("get", "bad_reply"), _count("get", "ok")) == (before[0] + 2, before[1])


@pytest.mark.parametrize("exc,outcome", [(requests.Timeout("slow"), "timeout"),
                                         (requests.ConnectionError("refused"), "unreachable")])
def test_no_http_answer_is_counted_as_timeout_or_unreachable(monkeypatch, exc, outcome):
    """A call that gets no HTTP answer is still counted, `timeout` for a timeout and `unreachable` for any other
    transport failure, and the exception still reaches the caller. Guards a down PVE node reading as silence on
    /metrics rather than as a burst of failures."""
    def boom(*_a, **_k):
        raise exc

    monkeypatch.setattr(pve.requests.Session, "request", boom)
    before = _count("post", outcome)
    with pytest.raises(type(exc)):
        _driver()._post("/nodes/node1/qemu", {})
    assert _count("post", outcome) == before + 1


def test_the_seam_records_elapsed_time(monkeypatch):
    """The `_sum` series grows by the call's wall-clock time (a monotonic clock read around the request), so a
    slow PVE raises the mean latency. Guards a seam that counts calls but records no duration."""
    clock = iter([100.0, 102.5])
    # Replace pve's own `time` reference only; patching time.monotonic itself would reach every module.
    monkeypatch.setattr(pve, "time", types.SimpleNamespace(monotonic=lambda: next(clock), sleep=time.sleep))
    monkeypatch.setattr(pve.requests.Session, "request", lambda *a, **k: _Reply(200, None))
    before = pve.PVE_REQUEST_SECONDS.snapshot().get(("delete", "ok"), (0.0, 0))
    _driver()._delete("/x")
    total, count = pve.PVE_REQUEST_SECONDS.snapshot()[("delete", "ok")]
    assert count == before[1] + 1 and total == pytest.approx(before[0] + 2.5)


def test_a_driver_keeps_one_session_and_retries_only_a_refused_get():
    """Each driver holds one requests.Session (the token header, verify=False, an adapter with PVE_RETRY), so its
    GETs can reuse a kept-alive connection. PVE_RETRY retries only a GET answered 502/503/504, at most twice, and
    never a connect failure or a read timeout; raise_on_status is off. Guards a retry repeating a POST or DELETE
    (a second clone or destroy), a hung PVE failing a call after three timeouts instead of one, and an exhausted
    retry raising urllib3's RetryError past callers that handle requests.HTTPError (review PERF-04)."""
    d = _driver()
    s = d._session
    assert isinstance(s, requests.Session) and s.verify is False
    assert s.headers["Authorization"] == "PVEAPIToken=id=secret"
    retry = s.get_adapter("https://pve.invalid:8006/api2/json").max_retries
    assert retry is pve.PVE_RETRY and d._session is not _driver()._session   # one session per driver
    assert (retry.total, retry.connect, retry.read, retry.other, retry.status) == (2, 0, 0, 0, 2)
    assert set(retry.status_forcelist) == {502, 503, 504} and retry.allowed_methods == frozenset({"GET"})
    assert retry.raise_on_status is False


def test_a_refused_get_is_retried_and_a_refused_post_is_not(monkeypatch):
    """Over a real socket: a GET answered 503 then 200 succeeds (two requests reach the server); a POST answered
    503 is sent ONCE and raises requests.HTTPError; a GET answered 503 every time raises requests.HTTPError after
    three requests (not RetryError). Guards the retry policy as urllib3 actually applies it, not only its
    configuration."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    hits: list[tuple[str, str]] = []
    answers: dict[str, list[int]] = {}

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a) -> None:
            pass

        def _answer(self) -> None:
            if self.headers.get("Content-Length"):
                self.rfile.read(int(self.headers["Content-Length"]))
            hits.append((self.command, self.path))
            queue = answers.setdefault(self.path, [])
            code = queue.pop(0) if queue else 503
            body = json.dumps({"data": "ok"}).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _answer

    monkeypatch.setattr(pve.PVE_RETRY, "backoff_factor", 0)   # no sleeps between the retries in a test
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        d = _driver()
        d.base = f"http://127.0.0.1:{httpd.server_address[1]}/api2/json"
        d._session.mount("http://", d._session.get_adapter("https://pve.invalid"))   # the same adapter + retry
        answers["/api2/json/flaky"] = [503, 200]
        assert d._get("/flaky") == "ok" and hits.count(("GET", "/api2/json/flaky")) == 2
        with pytest.raises(requests.HTTPError):
            d._post("/refused", {"x": 1})
        assert hits.count(("POST", "/api2/json/refused")) == 1
        with pytest.raises(requests.HTTPError):
            d._get("/down")
        assert hits.count(("GET", "/api2/json/down")) == 3
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_self_signed_cert_is_accepted_even_with_a_ca_bundle_in_the_environment(monkeypatch):
    """With REQUESTS_CA_BUNDLE and CURL_CA_BUNDLE set, the transport still gets verify=False: the driver passes it on
    every call, because requests lets an environment bundle override a session's own verify setting. Guards a base
    image or an operator's shell exporting a bundle making every PVE call refuse the cluster's self-signed cert, so
    nothing could be provisioned or reaped (review PERF-04 round 1)."""
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/nonexistent/ca.pem")
    monkeypatch.setenv("CURL_CA_BUNDLE", "/nonexistent/ca.pem")
    seen: dict[str, Any] = {}

    def send(self, request, **kw):
        seen.update(kw)
        r = requests.Response()
        r.status_code, r._content, r.request, r.url = 200, b'{"data": 7}', request, request.url
        return r

    monkeypatch.setattr(pve.HTTPAdapter, "send", send)
    assert _driver()._get("/version") == 7 and seen["verify"] is False


def test_only_a_get_soon_after_the_last_reply_reuses_a_connection(monkeypatch):
    """Over a real keep-alive socket, on a fake clock: two GETs close together share one connection; a GET more
    than PVE_IDLE_REUSE_S after the last reply opens a fresh one; and a POST always opens a fresh one, even right
    after a GET. Guards a call going out on a socket pveproxy may be closing (it drops an idle connection at about
    5 s, the daemon's tick: review PERF-04 round 1, M1), and a create, change or destroy, which is never retried,
    failing on a reused socket where a fresh connection would not (round 2, L1)."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    peers: list[tuple[str, int]] = []

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"   # keep-alive

        def log_message(self, *_a) -> None:
            pass

        def _answer(self) -> None:
            if self.headers.get("Content-Length"):
                self.rfile.read(int(self.headers["Content-Length"]))
            peers.append((self.command, self.client_address[1]))
            body = b'{"data": 1}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _answer

    clock = [1000.0]

    class _Clock:
        def __getattr__(self, name):
            return getattr(time, name)

        @staticmethod
        def monotonic() -> float:
            return clock[0]

    monkeypatch.setattr(pve, "time", _Clock())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        d = _driver()
        d.base = f"http://127.0.0.1:{httpd.server_address[1]}/api2/json"
        d._session.mount("http://", d._session.get_adapter("https://pve.invalid"))
        d._get("/a")
        d._get("/b")
        clock[0] += pve.PVE_IDLE_REUSE_S + 1
        d._get("/c")
        d._post("/d", {"x": 1})
    finally:
        httpd.shutdown()
        httpd.server_close()
    (_, a), (_, b), (_, c), (_, post) = peers
    assert a == b and b != c and post not in (a, c)
