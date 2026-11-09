"""The OAuth shim's request handler, driven over a real local socket with the ledger faked (review SEC-01).

Under `--profile tunnel` the shim's `/register`, `/authorize` and `/token` are reachable from the internet
before anyone has authenticated. These tests pin the pre-authentication bounds at the HTTP layer: an oversized
or malformed body is refused before it is read, a request that cannot get a ledger slot is answered 503
without a ledger connection, and open registration is rate limited. The full OAuth flow against real
Postgres stays in scripts/harness_oauth.py.
"""
from __future__ import annotations

import http.client
import json
import logging
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from typing import Any

import pytest

from br0kr import oauth, ratelimit

pytestmark = pytest.mark.integration

_GOOD_REGISTRATION = json.dumps({"redirect_uris": ["https://client.example/callback"],
                                 "client_name": "test client"}).encode()


class _Conn:
    """A stand-in ledger connection for the handler paths that reach the ledger."""

    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def commit(self) -> None:
        return None


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, list[str]]]:
    """A real shim on 127.0.0.1:<random port> with fresh limits and a fake ledger; yields (port, connects),
    where `connects` records every ledger connection the handler opened."""
    connects: list[str] = []

    def fake_connect(*_a: Any, **_k: Any) -> _Conn:
        connects.append("connect")
        return _Conn()

    monkeypatch.setattr(oauth.db, "connect", fake_connect)
    monkeypatch.setattr(oauth, "register_client", lambda conn, name, uris: "client-1")
    monkeypatch.setattr(oauth, "_LEDGER", ratelimit.LedgerGate("test", slots=8, wait_s=0.05))
    monkeypatch.setattr(oauth, "_REGISTER_LIMIT", ratelimit.WindowLimiter("test", per_key=10, total=60))
    monkeypatch.setattr(oauth, "_CONSENT_LIMIT", ratelimit.WindowLimiter("test", per_key=20, total=240))
    monkeypatch.setattr(oauth, "get_client", lambda conn, client_id: None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), oauth._make_handler())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1], connects
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(port: int, path: str, body: bytes | None, content_length: str | None = None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.putrequest("POST", path)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", content_length if content_length is not None else str(len(body or b"")))
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        conn.endheaders()
        if body:
            conn.send(body)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")
    finally:
        conn.close()


def test_handler_has_a_socket_timeout():
    """The handler sets a per-connection socket timeout. Guards a slowloris (a stalled body, or a short one
    behind a lying Content-Length) pinning a request thread forever on the thread-per-connection server."""
    assert oauth._make_handler().timeout == 30


def test_oversized_body_is_refused_unread(server):
    """A /register claiming a 1 MiB body gets 413 at once, without the body being sent or read and without a
    ledger connection. Guards an attacker-chosen Content-Length being read into memory before any check."""
    port, connects = server
    status, body = _post(port, "/register", None, content_length=str(1 << 20))
    assert status == 413 and body["error"] == "invalid_request"
    assert connects == []


def test_malformed_content_length_is_refused(server):
    """A non-numeric, signed or underscored Content-Length gets 400 instead of a crash (500) or a read of the
    wrong size. Guards the old `int(...)` raising inside the handler on attacker input, and int()'s leniency
    ("+5", "1_0") reading a length no HTTP client would send."""
    port, connects = server
    for bad in ("lots", "-5", "+5", "1_0"):
        status, body = _post(port, "/register", None, content_length=bad)
        assert status == 400 and body["error"] == "invalid_request"
    assert connects == []


def test_ninth_concurrent_request_gets_503_without_touching_the_ledger(server, monkeypatch):
    """With all 8 ledger slots held, a valid registration is answered 503 temporarily_unavailable (with a
    Retry-After) and no ledger connection is opened. Guards the review's failure: a flood exhausting Postgres
    max_connections through the unauthenticated shim and starving the daemon and every legitimate agent."""
    port, connects = server
    gate = ratelimit.LedgerGate("held", slots=8, wait_s=0.05)
    monkeypatch.setattr(oauth, "_LEDGER", gate)
    with ExitStack() as held:
        for _ in range(8):
            held.enter_context(gate.slot())
        status, body = _post(port, "/register", _GOOD_REGISTRATION)
    assert status == 503 and body["error"] == "temporarily_unavailable"
    assert connects == []
    status, body = _post(port, "/register", _GOOD_REGISTRATION)   # slots free again: served normally
    assert status == 201 and body["client_id"] == "client-1" and connects == ["connect"]


def test_registration_is_rate_limited_per_client(server, monkeypatch):
    """Past the per-client limit /register answers 429 without a ledger connection, keyed on the client the
    proxy reports (Cf-Connecting-IP on the tunnel listener), so another client is still served. Guards one
    internet client filling the oauth_client table through open registration."""
    port, connects = server
    monkeypatch.setattr(oauth, "_REGISTER_LIMIT", ratelimit.WindowLimiter("test", per_key=2, total=60))
    tunnel = {"X-Forwarded-Proto": "http", "Cf-Connecting-IP": "203.0.113.7"}
    codes = [_post(port, "/register", _GOOD_REGISTRATION, headers=tunnel)[0] for _ in range(3)]
    assert codes == [201, 201, 429] and len(connects) == 2
    other = {"X-Forwarded-Proto": "http", "Cf-Connecting-IP": "203.0.113.8"}
    assert _post(port, "/register", _GOOD_REGISTRATION, headers=other)[0] == 201


def test_a_stalled_consent_body_does_not_hold_a_ledger_slot(server, monkeypatch):
    """A consent POST whose body is still trickling in holds only its own thread, not a ledger slot: with a
    single slot, a registration is still served while the consent waits for its body. Guards the round-1 review
    finding: the consent read its body INSIDE the slot, so eight slow connections (too few to trip any rate
    limit) answered every /register, /authorize and /token with 503."""
    port, _ = server
    monkeypatch.setattr(oauth, "_LEDGER", ratelimit.LedgerGate("one", slots=1, wait_s=0.05))
    slow = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        slow.sendall(b"POST /authorize HTTP/1.1\r\nHost: x\r\n"
                     b"Content-Type: application/x-www-form-urlencoded\r\n"
                     b"Content-Length: 100\r\n\r\nclient_id=")
        time.sleep(0.3)   # the handler is now blocked reading the rest of the body
        status, body = _post(port, "/register", _GOOD_REGISTRATION)
        assert status == 201 and body["client_id"] == "client-1"
    finally:
        slow.close()


def test_an_idle_client_is_dropped_quietly(monkeypatch, caplog):
    """A client that goes idle mid-body past the socket timeout is disconnected without a 500 and without an
    ERROR-level stack trace. Guards each stalled connection logging an exception and then failing to write a
    500 to a socket that already timed out."""
    monkeypatch.setattr(oauth, "_CONSENT_LIMIT", ratelimit.WindowLimiter("test", per_key=20, total=240))
    handler = oauth._make_handler()
    handler.timeout = 0.3
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with caplog.at_level(logging.INFO, logger="br0kr.oauth"):
            idle = socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=5)
            idle.sendall(b"POST /authorize HTTP/1.1\r\nHost: x\r\nContent-Length: 50\r\n\r\nabc")
            assert idle.recv(4096) == b""   # closed with no response written
            idle.close()
            time.sleep(0.1)
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("idle past" in r.getMessage() for r in caplog.records)
    finally:
        httpd.shutdown()
        httpd.server_close()
