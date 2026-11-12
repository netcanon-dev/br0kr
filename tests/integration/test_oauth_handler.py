"""The OAuth shim's request handler, driven over a real local socket with the ledger faked (review SEC-01).

Under `--profile tunnel` the shim's `/register`, `/authorize` and `/token` are reachable from the internet
before anyone has authenticated. These tests pin the pre-authentication bounds at the HTTP layer: an oversized
or malformed body is refused before it is read, a request that cannot get a ledger slot is answered 503
without a ledger connection, and open registration is rate limited. They also drive the `/token` exchange
(code, PKCE check, token mint) against a fake, transactional ledger holding issued codes (review TEST-04). The
full OAuth flow against real Postgres stays in scripts/harness_oauth.py.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import logging
import socket
import threading
import time
import urllib.parse
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


# ── /token: authorization_code + PKCE → a minted project token ─────────────────────────────────────────
_VERIFIER = "v" * 43 + "-._~"           # a valid PKCE verifier (43..128 unreserved characters)
_CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(_VERIFIER.encode()).digest()).rstrip(b"=").decode()
_REDIRECT = "https://client.example/callback"


class _CodeLedger:
    """The ledger as /token sees it: issued authorization codes keyed by hash, consumed the way take_code's
    `DELETE … WHERE code_hash = %s AND expires_at > now() RETURNING …` consumes them (once, never when expired),
    and transactional like Postgres: a consumed code is gone for good only when its connection commits; a rollback,
    or an exception leaving the `with` block, puts it back. Counts connections, commits and rollbacks, and records
    every statement so a test can see one the fake does not model."""

    def __init__(self) -> None:
        self.codes: dict[str, tuple[tuple, bool]] = {}   # hash -> (row, expired)
        self.connects = self.commits = self.rollbacks = 0
        self.statements: list[str] = []

    def issue(self, code: str, *, expired: bool = False, scope: str = "consume", client_id: str = "client-1") -> None:
        row = (client_id, "team-a", scope, _CHALLENGE, _REDIRECT, "https://br0kr.example.com/mcp")
        self.codes[oauth.hash_code(code)] = (row, expired)

    def connect(self, *_a: Any, **_k: Any) -> _CodeConn:
        self.connects += 1
        return _CodeConn(self)


class _CodeConn:
    """One connection to a _CodeLedger, with its own uncommitted deletes."""

    def __init__(self, ledger: _CodeLedger) -> None:
        self.ledger, self._row = ledger, None
        self._taken: dict[str, tuple[tuple, bool]] = {}   # deleted in this transaction, not yet committed

    def __enter__(self) -> _CodeConn:
        return self

    def __exit__(self, exc_type: object, *_exc: object) -> None:
        if exc_type is not None:                          # psycopg rolls back a block left by an exception
            self.ledger.codes.update(self._taken)
        self._taken.clear()

    def execute(self, sql: str, params: tuple) -> _CodeConn:
        self.ledger.statements.append(sql)
        self._row = None
        if sql.startswith("DELETE FROM oauth_code WHERE code_hash = %s AND expires_at > now()"):
            entry = self.ledger.codes.get(params[0])
            if entry is not None and not entry[1]:
                self._taken[params[0]] = self.ledger.codes.pop(params[0])
                self._row = entry[0]
        return self

    def fetchone(self) -> tuple | None:
        return self._row

    def commit(self) -> None:
        self._taken.clear()
        self.ledger.commits += 1

    def rollback(self) -> None:
        self.ledger.codes.update(self._taken)
        self._taken.clear()
        self.ledger.rollbacks += 1


@pytest.fixture
def token_server(server, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, _CodeLedger, list[dict]]]:
    """The shim with its ledger replaced by a _CodeLedger and admin.create_token recorded; yields (port, ledger,
    mints), where `mints` holds the keyword arguments of every token mint."""
    port, _connects = server
    ledger, mints = _CodeLedger(), []

    def fake_create_token(_conn: Any, **kw: Any) -> dict[str, Any]:
        mints.append(kw)
        return {"created": True, "secret": "br0kr_test_secret", "token_id": len(mints), "reason": None}

    monkeypatch.setattr(oauth.db, "connect", ledger.connect)
    monkeypatch.setattr(oauth.admin, "create_token", fake_create_token)
    monkeypatch.setenv("BR0KR_OAUTH_TOKEN_TTL_DAYS", "7")
    yield port, ledger, mints
    # every statement the exchange ran is one the fake models (the unit test owns its exact shape)
    unmodelled = [q for q in ledger.statements if not q.startswith("DELETE FROM oauth_code WHERE code_hash = %s")]
    assert unmodelled == []


def _redeem(port: int, code: str, *, omit: tuple[str, ...] = (), **over: str
            ) -> tuple[int, dict[str, Any], dict[str, str]]:
    """POST /token as a form; `over` replaces or adds fields, `omit` drops them. Returns (status, body, headers)."""
    form = {"grant_type": "authorization_code", "code": code, "code_verifier": _VERIFIER, "client_id": "client-1",
            "redirect_uri": _REDIRECT, **over}
    for key in omit:
        del form[key]
    body = urllib.parse.urlencode(form).encode()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("POST", "/token", body=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}"), {k.lower(): v for k, v in resp.getheaders()}
    finally:
        conn.close()


def test_token_exchange_mints_a_bearer_for_the_bound_project(token_server):
    """A redemption with the right code, PKCE verifier, client and redirect URI answers 200, uncacheable, with a
    Bearer token minted for the project and scope the consent bound to the code, whatever project, scope or
    resource the request itself names, labelled by client and lasting the configured lifetime; then commits.
    Guards the exchange minting for a scope or project the client asked for instead of the one the operator
    consented to (a token that spends someone else's quota), and a token response a cache could keep."""
    port, ledger, mints = token_server
    ledger.issue("code-1", scope="provision")
    status, body, headers = _redeem(port, "code-1", scope="operator", project_id="team-b",
                                    resource="https://other.example/mcp")
    assert status == 200, body
    assert body == {"access_token": "br0kr_test_secret", "token_type": "Bearer", "expires_in": 7 * 86400,
                    "scope": "provision"}
    assert headers.get("cache-control") == "no-store"
    assert mints == [{"project_id": "team-a", "scope": "provision", "label": "oauth:client-1", "expires_in_days": 7}]
    assert ledger.commits == 1 and ledger.codes == {}


def test_the_token_label_names_the_client_within_its_bound(token_server):
    """The minted token's label is `oauth:` plus the first 16 characters of the client id. Guards the label
    outgrowing what admin.validate_token accepts for a long, client-chosen id, which would fail every mint."""
    port, ledger, mints = token_server
    long_id = "client-with-a-very-long-id"
    ledger.issue("code-1", client_id=long_id)
    assert _redeem(port, "code-1", client_id=long_id)[0] == 200
    assert mints[0]["label"] == "oauth:" + long_id[:16]


def test_a_code_redeems_once(token_server):
    """A second redemption of the same code is invalid_grant and mints nothing. Guards single use: an
    authorization code leaked from a redirect URL must not buy a second token."""
    port, ledger, mints = token_server
    ledger.issue("code-1")
    assert _redeem(port, "code-1")[0] == 200
    status, body, _ = _redeem(port, "code-1")
    assert (status, body["error"]) == (400, "invalid_grant")
    assert len(mints) == 1


def test_a_wrong_verifier_is_refused_and_spends_the_code(token_server):
    """A wrong PKCE verifier is invalid_grant, mints nothing, and the consumed code is COMMITTED away, not rolled
    back: a later redemption with the right verifier fails too. Guards PKCE being checked at all, and an attacker
    who intercepted the code getting unlimited verifier guesses because each refusal restored it."""
    port, ledger, mints = token_server
    ledger.issue("code-1")
    status, body, _ = _redeem(port, "code-1", code_verifier="w" * 43)
    assert (status, body["error"]) == (400, "invalid_grant")
    assert ledger.codes == {} and ledger.rollbacks == 0
    assert _redeem(port, "code-1")[0] == 400
    assert mints == []


@pytest.mark.parametrize(("over", "omit"), [({"client_id": "client-2"}, ()),
                                            ({"redirect_uri": "https://evil.example/cb"}, ()),
                                            ({}, ("client_id",)), ({}, ("redirect_uri",))],
                         ids=["other-client", "other-redirect", "no-client", "no-redirect"])
def test_a_code_redeems_only_for_its_client_and_redirect(token_server, over, omit):
    """A code presented by another client_id or with another redirect_uri, or with either left out, is
    invalid_grant, mints nothing, and is spent. Guards a code stolen from one client's redirect being redeemed by
    another client (RFC 6749 §4.1.3), including by simply omitting the field that would not match."""
    port, ledger, mints = token_server
    ledger.issue("code-1")
    status, body, _ = _redeem(port, "code-1", omit=omit, **over)
    assert (status, body["error"]) == (400, "invalid_grant")
    assert mints == [] and ledger.codes == {} and ledger.rollbacks == 0


def test_an_expired_or_unknown_code_is_refused(token_server):
    """An expired code and a code never issued are both invalid_grant and mint nothing. Guards expiry: a code
    sitting in a browser history past its lifetime must not redeem."""
    port, ledger, mints = token_server
    ledger.issue("old-code", expired=True)
    for code in ("old-code", "never-issued"):
        status, body, _ = _redeem(port, code)
        assert (status, body["error"]) == (400, "invalid_grant"), code
    assert mints == []


def test_token_refuses_other_grants_and_missing_fields_before_the_ledger(token_server):
    """A grant_type other than authorization_code is unsupported_grant_type, and a missing code or verifier is
    invalid_request, all without opening a ledger connection. Guards the refresh or password grants being
    accepted by accident, and an unauthenticated request costing a ledger connection."""
    port, ledger, mints = token_server
    for code, over, err in (("c", {"grant_type": "password"}, "unsupported_grant_type"),
                            ("c", {"grant_type": "refresh_token"}, "unsupported_grant_type"),
                            ("c", {"code_verifier": ""}, "invalid_request"), ("", {}, "invalid_request")):
        status, body, _ = _redeem(port, code, **over)
        assert (status, body["error"]) == (400, err), (code, over)
    assert ledger.connects == 0 and mints == []


def test_a_failed_mint_rolls_back_and_is_refused(token_server, monkeypatch: pytest.MonkeyPatch):
    """When the token mint reports it did not create a token, the exchange rolls back, which leaves the code
    redeemable, and answers invalid_grant with the mint's reason. Guards a 200 with no usable token, and a
    half-written mint being committed."""
    port, ledger, _mints = token_server
    monkeypatch.setattr(oauth.admin, "create_token",
                        lambda _c, **_k: {"created": False, "secret": None, "reason": "unknown_project"})
    ledger.issue("code-1")
    status, body, _ = _redeem(port, "code-1")
    assert (status, body["error"], body.get("error_description")) == (400, "invalid_grant", "unknown_project")
    assert ledger.rollbacks == 1 and ledger.commits == 0 and oauth.hash_code("code-1") in ledger.codes


def test_an_error_inside_the_exchange_answers_a_bare_server_error(token_server, monkeypatch: pytest.MonkeyPatch):
    """An exception inside the exchange (here the mint raising) answers exactly `{"error": "server_error"}` with a
    500, and the consumed code goes back. Guards an internal message (a SQL error, a secret-bearing repr)
    reaching an unauthenticated client in the error body."""
    port, ledger, _mints = token_server

    def boom(_c: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("internal detail that must not leak")

    monkeypatch.setattr(oauth.admin, "create_token", boom)
    ledger.issue("code-1")
    status, body, _ = _redeem(port, "code-1")
    assert (status, body) == (500, {"error": "server_error"})
    assert oauth.hash_code("code-1") in ledger.codes
