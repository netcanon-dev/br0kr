"""Unit tests for the OAuth 2.1 shim's PURE seam (br0kr/oauth.py) — the security-critical decision
functions, tested without a socket or a DB. The DB seams (register/issue/take code) + the full flow are
proven against real Postgres in scripts/harness_oauth.py; here we pin the pure invariants that a regression
would otherwise slip past: PKCE must actually verify, the discovery documents must advertise S256, a redirect
URI must be an https/loopback absolute URI, and the /authorize validator must fail closed."""
from __future__ import annotations

import base64
import hashlib

import pytest

from br0kr import oauth


def _challenge_for(verifier: str) -> str:
    """The S256 challenge a compliant client computes for a verifier (BASE64URL(SHA256(verifier)), no pad)."""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def test_pkce_s256_accepts_a_correct_verifier() -> None:
    """A verifier whose SHA-256 matches the stored challenge must verify — else no legitimate token exchange
    could ever complete (the /token endpoint would reject every real client)."""
    verifier = "a" * 64
    assert oauth.pkce_s256_matches(verifier, _challenge_for(verifier)) is True


def test_pkce_s256_rejects_a_wrong_verifier() -> None:
    """A verifier that does NOT hash to the challenge must be refused — this is the whole point of PKCE: an
    attacker who intercepts the authorization code but lacks the original verifier cannot redeem it."""
    verifier = "a" * 64
    assert oauth.pkce_s256_matches("b" * 64, _challenge_for(verifier)) is False


def test_pkce_s256_rejects_out_of_range_verifier_length() -> None:
    """RFC 7636 bounds the verifier to 43–128 chars; a too-short/too-long verifier is malformed and must be
    rejected up front (guards against a degenerate/empty verifier being massaged into a match)."""
    assert oauth.pkce_s256_matches("a" * 42, _challenge_for("a" * 42)) is False
    assert oauth.pkce_s256_matches("a" * 129, _challenge_for("a" * 129)) is False


def test_pkce_s256_rejects_empty() -> None:
    """Empty verifier or challenge must never verify (a missing PKCE proof cannot be treated as valid)."""
    assert oauth.pkce_s256_matches("", "x") is False
    assert oauth.pkce_s256_matches("a" * 64, "") is False


def test_pkce_s256_does_not_raise_on_hostile_nonascii_verifier() -> None:
    """A hostile non-ASCII verifier must be a clean False, not a 500 — encode as UTF-8, never ascii-strict
    (the handler wraps this, but a crash here would still be a denial-of-service surface)."""
    assert oauth.pkce_s256_matches("é" * 64, _challenge_for("a" * 64)) is False


def test_hash_code_is_sha256_hex_and_stable() -> None:
    """Authorization codes are stored ONLY as their SHA-256 hash (like api_tokens) so a leaked-at-rest code
    can't be replayed; the hash must be the plain sha256 hex and deterministic (lookup depends on it)."""
    assert oauth.hash_code("abc") == hashlib.sha256(b"abc").hexdigest()
    assert oauth.hash_code("abc") == oauth.hash_code("abc")
    assert oauth.hash_code("abc") != oauth.hash_code("abd")


def test_issuer_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """issuer() decouples the PUBLIC URL (what discovery + WWW-Authenticate advertise) from the internal Caddy
    origin — this is what makes br0kr tunnel-friendly. Precedence: an explicit BR0KR_OAUTH_ISSUER override
    wins, else BR0KR_PUBLIC_URL (a tunnel's public https URL), else https://<BR0KR_TLS_SITE> (the direct-TLS
    LAN default). A regression that read the wrong source would make the connector fetch discovery URLs the
    cloud can't reach (the exact failure the tunnel exists to fix), or advertise the private origin. Also
    pins the trailing-slash trim (an issuer with a trailing '/' produces double-slashed endpoint URLs)."""
    for v in ("BR0KR_OAUTH_ISSUER", "BR0KR_PUBLIC_URL", "BR0KR_TLS_SITE"):
        monkeypatch.delenv(v, raising=False)
    assert oauth.issuer() == ""                                    # nothing set → empty (main() then refuses)
    monkeypatch.setenv("BR0KR_TLS_SITE", "192.168.11.232/")        # trailing slash must be trimmed (branch 3)
    assert oauth.issuer() == "https://192.168.11.232"             # derived from the direct-TLS site, slash trimmed
    monkeypatch.setenv("BR0KR_PUBLIC_URL", "https://br0kr.example.com/")
    assert oauth.issuer() == "https://br0kr.example.com"          # tunnel URL wins; trailing slash trimmed
    monkeypatch.setenv("BR0KR_OAUTH_ISSUER", "https://override.example")
    assert oauth.issuer() == "https://override.example"          # explicit override wins over everything


def test_asm_metadata_advertises_mandatory_s256() -> None:
    """The authorization-server metadata MUST advertise code_challenge_methods_supported=['S256']: a spec-
    compliant client refuses to proceed without it, so dropping it would silently break every connection."""
    doc = oauth.authorization_server_metadata("https://h")
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert doc["issuer"] == "https://h"
    assert doc["authorization_endpoint"] == "https://h/authorize"
    assert doc["token_endpoint"] == "https://h/token"
    assert doc["registration_endpoint"] == "https://h/register"
    assert doc["token_endpoint_auth_methods_supported"] == ["none"]  # public client, PKCE-only


def test_prm_metadata_points_at_resource_and_as() -> None:
    """The protected-resource metadata must name the canonical /mcp resource and this host as its AS — the
    client verifies `resource` to prevent being pointed at an impostor server."""
    doc = oauth.protected_resource_metadata("https://h")
    assert doc["resource"] == "https://h/mcp"
    assert doc["authorization_servers"] == ["https://h"]
    assert doc["bearer_methods_supported"] == ["header"]


@pytest.mark.parametrize("uri,ok", [
    ("https://claude.ai/api/mcp/auth_callback", True),   # the Desktop/web callback
    ("https://example.com/cb", True),
    ("http://localhost:3334/oauth/callback", True),       # RFC 8252 native-app loopback
    ("http://127.0.0.1:52001/callback", True),
    ("http://[::1]:9000/cb", True),
    ("http://evil.com/cb", False),                        # plain http to a REMOTE host — refused
    ("ftp://host/cb", False),                             # non-http(s) scheme
    ("not-a-uri", False),                                 # scheme-less
    ("", False),
    ("https://ok/cb\r\nX-Injected: pwn", False),          # CRLF → response-header injection attempt (Fable MED)
    ("https://ok/cb\tX", False),                          # embedded control char (tab)
    ("https://ok/ cb", False),                            # embedded space
])
def test_valid_redirect_uri(uri: str, ok: bool) -> None:
    """Redirect URIs must be https, OR http to a loopback host (native-app pattern). A plain-http REMOTE URI
    or a scheme-less/foreign-scheme string is refused at registration so it can never be a redirect target
    (defence-in-depth beside the exact-match check at /authorize). CRITICALLY, a URI carrying a control char
    or whitespace (a CRLF) is refused — else it would ride the exact-match into the /authorize `Location`
    header (stdlib send_header does no CRLF sanitization) = response-header injection on the trusted origin."""
    assert oauth.valid_redirect_uri(uri) is ok


_GOOD_PARAMS = {"response_type": "code", "code_challenge": "x", "code_challenge_method": "S256"}
_CLIENT = {"client_id": "c", "client_name": "App", "redirect_uris": ["https://ok/cb"]}


def test_authorize_error_ok_when_wellformed() -> None:
    """A well-formed request (known client, registered redirect, response_type=code, S256 challenge present)
    passes validation → None, so the consent page renders / a code is issued."""
    assert oauth._authorize_error(_CLIENT, "https://ok/cb", _GOOD_PARAMS) is None


def test_authorize_error_unknown_client() -> None:
    """No client → we have no trusted redirect target, so we must NOT proceed (error page, never a redirect)."""
    assert oauth._authorize_error(None, "https://ok/cb", _GOOD_PARAMS) == "unknown_client"


def test_authorize_error_redirect_uri_must_exact_match() -> None:
    """A redirect_uri that isn't EXACTLY one the client registered is refused — the open-redirect guard: an
    attacker can't append/alter the callback to exfiltrate a code to a URI the client never declared."""
    assert oauth._authorize_error(_CLIENT, "https://ok/cb/extra", _GOOD_PARAMS) == "redirect_uri_mismatch"
    assert oauth._authorize_error(_CLIENT, "https://evil/cb", _GOOD_PARAMS) == "redirect_uri_mismatch"


def test_authorize_error_requires_code_response_type() -> None:
    """Only the authorization_code flow is supported; any other response_type is refused (no implicit flow)."""
    assert oauth._authorize_error(_CLIENT, "https://ok/cb", {**_GOOD_PARAMS, "response_type": "token"}) \
        == "unsupported_response_type"


def test_authorize_error_requires_s256_pkce() -> None:
    """PKCE with S256 is MANDATORY for these public clients: a missing challenge, or a method other than
    S256 (e.g. the downgradeable 'plain'), must be refused so an interception attack has no opening."""
    assert oauth._authorize_error(_CLIENT, "https://ok/cb", {"response_type": "code"}) == "pkce_required"
    assert oauth._authorize_error(
        _CLIENT, "https://ok/cb", {**_GOOD_PARAMS, "code_challenge_method": "plain"}) == "pkce_required"
    assert oauth._authorize_error(
        _CLIENT, "https://ok/cb", {**_GOOD_PARAMS, "code_challenge": ""}) == "pkce_required"
