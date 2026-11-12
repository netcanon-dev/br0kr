"""oauth.py — the OAuth 2.1 SHIM: a minimal Authorization Server so OAuth-only MCP hosts can connect.

WHY THIS EXISTS. br0kr authenticates agents with a static `Authorization: Bearer <project api_token>`
header (br0kr/auth.py). That works for the `claude` CLI and any client that lets you paste a header. But
some MCP hosts — notably the Claude Desktop / claude.ai "custom connector" UI — will ONLY authenticate to a
remote MCP server via OAuth 2.1 (the MCP authorization spec, revision 2025-11-25): they refuse a pasted
bearer and instead run OAuth discovery → dynamic client registration → an in-browser authorization-code +
PKCE flow. This module is exactly enough OAuth 2.1 Authorization Server to satisfy that flow.

THE DESIGN (deliberately tiny + gateway-preserving). The shim runs BESIDE the gateway, behind the same Caddy
TLS front. Caddy routes the OAuth paths here and sends `/mcp` straight to the gateway; Caddy itself returns
the `401 + WWW-Authenticate: Bearer resource_metadata="…"` challenge that kicks off discovery when `/mcp` is
hit with no bearer. The shim NEVER sits on the MCP data path and never proxies a stream. Its whole job:

  1. serve the two discovery documents (RFC 9728 protected-resource + RFC 8414 authorization-server),
  2. dynamic client registration (RFC 7591) — public clients only (`token_endpoint_auth_method=none`),
  3. `/authorize`: a server-rendered CONSENT page whose single input is "paste a br0kr project token";
     the token is validated with the SAME `auth.resolve` the gateway uses, proving the human already holds
     live access to that project. On submit we issue a PKCE-bound, single-use authorization code.
  4. `/token`: verify PKCE (S256), then MINT a fresh project-scoped br0kr `api_token` (admin.create_token)
     and return it as the opaque OAuth `access_token`.

So the "OAuth access token" IS a real br0kr api_token — opaque, single-audience by construction (only valid
at THIS gateway), expiring, and revocable through the existing token machinery. The gateway and the whole
admission core are UNTOUCHED: they keep seeing a normal project bearer. OAuth here is literally "a browser
flow that mints a project token, gated by proving you already hold one." The only state this module stores
(migration 0030) is public client registrations and short-lived, HASHED, single-use authorization codes —
never a long-term secret. See docs/runbooks/oauth-connector.md and SECURITY.md (control C16)."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import urllib.parse
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import psycopg
from psycopg.types.json import Json

from br0kr import admin, auth, db, logging_setup, migrate, ratelimit

log = logging.getLogger(__name__)

# Only the TENANT tiers can back an OAuth connector: an OPERATOR token (0020) is project-LESS (project_id
# NULL), so it cannot be the identity for a project-scoped connector — the consent step rejects it.
TENANT_SCOPES: tuple[str, ...] = ("read", "consume", "provision")

# PKCE verifiers are 43–128 chars of the unreserved set (RFC 7636 §4.1); anything else is a malformed grant.
_PKCE_MIN, _PKCE_MAX = 43, 128


# ── configuration (env) ──────────────────────────────────────────────────────────────────────────────
def issuer() -> str:
    """The canonical public HTTPS base URL clients reach this deployment at, e.g. `https://192.0.2.50`
    (direct Caddy TLS) or `https://br0kr.example.com` (behind a reverse-proxy / tunnel). LOAD-BEARING: every
    discovery document + endpoint URL is built from it, and it MUST match how the connector addresses the
    server (scheme + host) or the client rejects the flow. No trailing slash.

    Precedence — this is what makes br0kr TUNNEL-FRIENDLY: the PUBLIC hostname (issuer + discovery +
    WWW-Authenticate) is decoupled from the internal Caddy origin, so a tunnel whose public host != the
    origin still advertises URLs the client can actually fetch:
      1. BR0KR_OAUTH_ISSUER — an explicit override (advanced escape hatch);
      2. BR0KR_PUBLIC_URL   — the public https URL a reverse-proxy / tunnel serves (the tunnel-friendly knob);
      3. https://<BR0KR_TLS_SITE> — derived from the direct Caddy TLS site (the default LAN case).
    """
    for var in ("BR0KR_OAUTH_ISSUER", "BR0KR_PUBLIC_URL"):
        val = os.environ.get(var, "").strip().rstrip("/")
        if val:
            return val
    site = os.environ.get("BR0KR_TLS_SITE", "").strip().rstrip("/")
    return f"https://{site}" if site else ""


def resource() -> str:
    """The canonical MCP resource URI the access token is bound to (the gateway endpoint behind Caddy)."""
    return issuer() + "/mcp"


def code_ttl_seconds() -> int:
    """Authorization-code lifetime — short by spec intent (a code is a transient, single-use credential).
    Floored at 10s so a clock skew / slow browser can't make every exchange fail."""
    return max(10, int(os.environ.get("BR0KR_OAUTH_CODE_TTL", "120") or "120"))


def token_ttl_days() -> int:
    """Lifetime of the MINTED access token (a br0kr api_token). We do NOT issue refresh tokens (v1), so this
    is how long a connector stays connected before it must re-consent; floored at 1 day."""
    return max(1, int(os.environ.get("BR0KR_OAUTH_TOKEN_TTL_DAYS", "30") or "30"))


# ── pure helpers (unit-testable without a socket or a DB) ──────────────────────────────────────────────
def hash_code(code: str) -> str:
    """SHA-256 hex of an authorization code. The code rides in a redirect URL (a bearer credential), so like
    an api_token it is stored ONLY as its hash — a ledger read can't replay a leaked-at-rest code."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def pkce_s256_matches(verifier: str, challenge: str) -> bool:
    """True iff BASE64URL(SHA256(verifier)) == challenge (PKCE S256, RFC 7636 §4.6), compared constant-time.
    Rejects a verifier outside the 43–128 unreserved-char range up front. Encodes as UTF-8 (never raises on a
    hostile non-ASCII verifier — it simply won't match) rather than ascii-strict (which would 500)."""
    if not verifier or not challenge:
        return False
    if not (_PKCE_MIN <= len(verifier) <= _PKCE_MAX):
        return False
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(expected.encode("ascii"), challenge.encode("utf-8", "replace"))


def protected_resource_metadata(iss: str) -> dict[str, Any]:
    """RFC 9728 protected-resource metadata: what resource this is + which AS mints tokens for it."""
    return {
        "resource": iss + "/mcp",
        "authorization_servers": [iss],
        "scopes_supported": list(TENANT_SCOPES),
        "bearer_methods_supported": ["header"],
    }


def authorization_server_metadata(iss: str) -> dict[str, Any]:
    """RFC 8414 authorization-server metadata. code_challenge_methods_supported=['S256'] is MANDATORY — a
    client that doesn't see it MUST refuse to proceed (PKCE is required for public clients)."""
    return {
        "issuer": iss,
        "authorization_endpoint": iss + "/authorize",
        "token_endpoint": iss + "/token",
        "registration_endpoint": iss + "/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": list(TENANT_SCOPES),
    }


def valid_redirect_uri(uri: str) -> bool:
    """A syntactically valid absolute redirect URI: an https URL, OR an http URL whose host is loopback
    (localhost / 127.0.0.1 / [::1]) per RFC 8252 §7.3 (native-app loopback). Everything else — a scheme-less
    string, a custom scheme, a plain http remote — is refused at registration so it can never be a redirect
    target. (Exact-match against the registered set is still enforced at /authorize + /token.) ALSO rejects
    any control char or whitespace: a CRLF in a redirect URI would otherwise ride the exact-match into the
    /authorize `Location` response header (stdlib `send_header` does NO CRLF sanitization) = response-header
    injection on the trusted origin, so a control-bearing URI can never enter the registered set."""
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7f for c in uri):
        return False
    try:
        p = urllib.parse.urlparse(uri)
    except ValueError:
        return False
    if not p.scheme or not p.netloc:
        return False
    if p.scheme == "https":
        return True
    if p.scheme == "http":
        host = (p.hostname or "").lower()
        return host in ("localhost", "127.0.0.1", "::1")
    return False


def _authorize_error(client: dict[str, Any] | None, redirect_uri: str, params: dict[str, str]) -> str | None:
    """Validate an /authorize request (GET render OR POST submit) IDENTICALLY. Returns a short error slug for
    a safe error PAGE, or None when the request is well-formed enough to show the consent page / issue a code.
    We render an error page (never redirect an error) for EVERY failure, because a bad client_id/redirect_uri
    means we have no trusted URI to redirect to — and treating all failures the same avoids leaking which
    check failed. Order: client must exist → redirect_uri must EXACTLY match a registered one → response_type
    must be `code` → a PKCE S256 challenge must be present."""
    if client is None:
        return "unknown_client"
    registered = client.get("redirect_uris") or []
    if redirect_uri not in registered:
        return "redirect_uri_mismatch"
    if params.get("response_type") != "code":
        return "unsupported_response_type"
    if params.get("code_challenge_method") != "S256" or not params.get("code_challenge"):
        return "pkce_required"
    return None


# ── DB seams (small; the SQL lives here, not smeared across the handler) ───────────────────────────────
def register_client(conn: psycopg.Connection[Any], client_name: str | None, redirect_uris: list[str]) -> str:
    """Insert a DCR registration and return the issued client_id. Public client — no secret is stored."""
    client_id = "br0kr-" + secrets.token_urlsafe(24)
    conn.execute(
        "INSERT INTO oauth_client (client_id, client_name, redirect_uris) VALUES (%s, %s, %s)",
        (client_id, client_name, Json(redirect_uris)),
    )
    return client_id


def get_client(conn: psycopg.Connection[Any], client_id: str) -> dict[str, Any] | None:
    """Fetch a registered client (client_name + redirect_uris) by id, or None."""
    row = conn.execute(
        "SELECT client_name, redirect_uris FROM oauth_client WHERE client_id = %s", (client_id,)
    ).fetchone()
    if row is None:
        return None
    return {"client_id": client_id, "client_name": row[0], "redirect_uris": list(row[1] or [])}


def issue_code(conn: psycopg.Connection[Any], *, client_id: str, project_id: str, scope: str,
               code_challenge: str, redirect_uri: str, resource_uri: str | None) -> str:
    """Mint a fresh authorization code, store ONLY its hash bound to the consent decision + PKCE challenge,
    and return the plaintext code (handed to the browser in the redirect). Seconds-lived + single-use."""
    code = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(seconds=code_ttl_seconds())
    conn.execute(
        "INSERT INTO oauth_code (code_hash, client_id, project_id, scope, code_challenge, redirect_uri, "
        "resource, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (hash_code(code), client_id, project_id, scope, code_challenge, redirect_uri, resource_uri, expires_at),
    )
    return code


def take_code(conn: psycopg.Connection[Any], code: str) -> dict[str, Any] | None:
    """ATOMICALLY consume an authorization code: DELETE … RETURNING enforces single-use (a replay finds no
    row) AND the `expires_at > now()` predicate enforces expiry in the same statement (an expired code is
    never redeemable). Returns the bound consent/PKCE fields, or None if absent/expired/already-used."""
    row = conn.execute(
        "DELETE FROM oauth_code WHERE code_hash = %s AND expires_at > now() "
        "RETURNING client_id, project_id, scope, code_challenge, redirect_uri, resource",
        (hash_code(code),),
    ).fetchone()
    if row is None:
        return None
    return {"client_id": row[0], "project_id": row[1], "scope": row[2], "code_challenge": row[3],
            "redirect_uri": row[4], "resource": row[5]}


def sweep_expired_codes(conn: psycopg.Connection[Any]) -> None:
    """Opportunistic GC of expired codes (they are useless past expires_at). Best-effort; never fatal."""
    with contextlib.suppress(Exception):   # GC must never break a request
        conn.execute("DELETE FROM oauth_code WHERE expires_at <= now() - interval '1 hour'")


# ── the consent page ───────────────────────────────────────────────────────────────────────────────────
def consent_html(*, client_name: str | None, redirect_uri: str, hidden: dict[str, str],
                 error: str | None = None) -> str:
    """Render the consent page. The ONLY input is a br0kr project token; the OAuth params carry through as
    hidden fields (re-validated server-side on POST — never trusted from the form). Displays the client name
    + redirect target PROMINENTLY (RFC-spec anti-spoofing: the human sees who they authorize). Every
    interpolated value is HTML-escaped. No JS (a static form POST) so the CSP forbids script entirely."""
    who = html.escape(client_name or "an application")
    dest = html.escape(redirect_uri)
    fields = "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">' for k, v in hidden.items()
    )
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return (
        "<!doctype html><html><head><meta charset=utf-8><title>Authorize br0kr connector</title>"
        "<meta name=viewport content='width=device-width,initial-scale=1'><style>"
        "body{font:15px system-ui,sans-serif;max-width:34rem;margin:3rem auto;padding:0 1rem;color:#1a1a1a}"
        ".card{border:1px solid #ddd;border-radius:12px;padding:1.5rem}"
        "h1{font-size:1.15rem} .who{font-weight:600} .dest{color:#666;font-size:.85rem;word-break:break-all}"
        "input[type=password]{width:100%;box-sizing:border-box;padding:.6rem;font:14px monospace;"
        "border:1px solid #bbb;border-radius:8px;margin:.5rem 0}"
        "button{background:#111;color:#fff;border:0;border-radius:8px;padding:.65rem 1.2rem;font-size:15px;"
        "cursor:pointer} .err{color:#b00020;font-size:.9rem} label{font-size:.9rem;color:#333}"
        "</style></head><body><div class=card>"
        "<h1>Authorize <span class=who>" + who + "</span> to use br0kr</h1>"
        "<p class=dest>Redirects to: " + dest + "</p>" + err +
        "<form method=post action='/authorize'>" + fields +
        "<label>Paste a br0kr <b>project token</b> to authorize this connector as that project:</label>"
        "<input type=password name=project_token autocomplete=off autofocus "
        "placeholder='project api token (~43 chars)'>"
        "<button type=submit>Authorize</button></form>"
        "<p class=dest>The connector will act as that project, with its quota. Only paste a token you own.</p>"
        "</div></body></html>"
    )


# Pre-authentication bounds (review SEC-01, br0kr/ratelimit.py). Under --profile tunnel every route below is
# reachable from the internet before any credential is checked, so: a body larger than any real registration or
# consent form is refused unread; at most 8 requests hold a ledger connection at once (the rest get 503, never
# touching the ledger); and open registration and consent submission are rate limited per client and overall,
# so one client cannot fill the oauth_client table and a flood that forges a new key each time still hits the
# ceiling. The /token exchange needs a live single-use code, so the gate alone bounds it.
_MAX_BODY = 16 * 1024
_LEDGER = ratelimit.LedgerGate("oauth shim", slots=8)
_REGISTER_LIMIT = ratelimit.WindowLimiter("oauth /register", per_key=10, total=60)
_CONSENT_LIMIT = ratelimit.WindowLimiter("oauth /authorize", per_key=20, total=240)


class _BadBody(Exception):
    """A request body refused before it is read: an oversized or unparseable Content-Length."""

    def __init__(self, status: int, desc: str) -> None:
        super().__init__(desc)
        self.status, self.desc = status, desc


def client_key(headers: Any, peer: str) -> str:
    """The client identity to rate-limit on (never used for auth). The shim is reachable only through Caddy, so
    the immediate peer is always the proxy: on the tunnel listener (`http://:8081`, plain HTTP, reachable only
    by cloudflared inside the compose network) the internet client is in cloudflared's `Cf-Connecting-IP`; on the
    direct TLS site Caddy sets `X-Forwarded-For` to the peer it saw (it trusts no client-sent value, since no
    trusted proxies are configured). Falls back to the socket peer when neither is present (a direct run)."""
    if (headers.get("X-Forwarded-Proto") or "").lower() == "http" and headers.get("Cf-Connecting-IP"):
        return str(headers.get("Cf-Connecting-IP")).strip()[:64]
    forwarded = headers.get("X-Forwarded-For")
    if forwarded:
        return str(forwarded).split(",")[0].strip()[:64]
    return peer


_SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # No script anywhere; the consent form posts to our own origin (form-action 'self'). Nothing else loads.
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'",
}


def _make_handler() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "br0kr-oauth"
        # A per-read socket timeout: a connection idle for 30 s (a stalled body, or a short one behind a lying
        # Content-Length) is dropped (review SEC-01; the dashboard does the same). It is not a total deadline, so a
        # client trickling a byte every 29 s still holds its thread; the number of connections is bounded at the
        # edge (the tunnel's rate-limiting rule), not here. No ledger slot is ever held while a body is read.
        timeout = 30

        # ---- low-level response helpers (mirror dashboard.py) ----
        def log_message(self, *args: Any) -> None:  # route access logs through logging, not stderr
            return

        def end_headers(self) -> None:
            for hk, hv in _SECURITY_HEADERS.items():
                self.send_header(hk, hv)
            super().end_headers()

        def _write(self, code: int, content_type: str, data: bytes,
                   extra: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for hk, hv in (extra or {}).items():
                self.send_header(hk, hv)
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code: int, obj: dict[str, Any], extra: dict[str, str] | None = None) -> None:
            self._write(code, "application/json", json.dumps(obj).encode(), extra)

        def _oauth_error(self, code: int, error: str, desc: str | None = None) -> None:
            body: dict[str, Any] = {"error": error}
            if desc:
                body["error_description"] = desc
            self._json(code, body)

        def _html(self, code: int, markup: str) -> None:
            self._write(code, "text/html; charset=utf-8", markup.encode())

        def _redirect(self, location: str) -> None:
            self._write(302, "text/plain", b"", {"Location": location})

        def _read_body(self) -> bytes:
            """The request body, at most `_MAX_BODY` bytes. A larger or unparseable Content-Length raises
            `_BadBody` BEFORE anything is read, and the connection is closed so the unread bytes are never
            parsed as a next request."""
            raw_len = (self.headers.get("Content-Length") or "").strip()
            if not raw_len:
                return b""
            # Digits only: int() would also accept "+5" or "1_0", which no HTTP client sends.
            length = int(raw_len) if raw_len.isascii() and raw_len.isdigit() else -1
            if length < 0 or length > _MAX_BODY:
                self.close_connection = True
                if length < 0:
                    raise _BadBody(400, "Content-Length is not a valid length")
                raise _BadBody(413, "request body too large")
            return self.rfile.read(length) if length else b""

        def _busy(self) -> None:
            self._json(503, {"error": "temporarily_unavailable",
                             "error_description": "the broker is busy; retry shortly"}, {"Retry-After": "1"})

        def _too_many(self) -> None:
            self._json(429, {"error": "too_many_requests",
                             "error_description": "too many requests from this client; retry later"},
                       {"Retry-After": "60"})

        def _client(self) -> str:
            return client_key(self.headers, self.client_address[0])

        def _body_form(self) -> dict[str, str]:
            raw = self._read_body()
            parsed = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
            return {k: v[0] for k, v in parsed.items()}

        def _body_json(self) -> dict[str, Any]:
            raw = self._read_body()
            try:
                obj = json.loads(raw or b"{}")
            except (ValueError, TypeError):
                return {}
            return obj if isinstance(obj, dict) else {}

        # ---- routing ----
        def do_GET(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            try:
                if path == "/healthz":
                    self._json(200, {"ok": True})
                elif path.startswith("/.well-known/oauth-protected-resource"):
                    self._json(200, protected_resource_metadata(issuer()))
                elif path.startswith("/.well-known/oauth-authorization-server"):
                    self._json(200, authorization_server_metadata(issuer()))
                elif path == "/authorize":
                    self._authorize_get()
                else:
                    self._json(404, {"error": "not_found"})
            except ratelimit.Busy:
                self._busy()
            except TimeoutError:   # the client stopped reading our response past `timeout`: drop it quietly
                self.close_connection = True
                log.info("oauth GET %s: client idle past %ss, connection dropped", path, self.timeout)
            except Exception:   # never leak a stack; log + generic 500
                log.exception("oauth GET %s failed", path)
                self._oauth_error(500, "server_error")

        def do_POST(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            try:
                if path == "/register":
                    self._register()
                elif path == "/authorize":
                    self._authorize_post()
                elif path == "/token":
                    self._token()
                else:
                    self._json(404, {"error": "not_found"})
            except _BadBody as e:
                self._oauth_error(e.status, "invalid_request", e.desc)
            except ratelimit.Busy:
                self._busy()
            except TimeoutError:   # the client went idle past `timeout` (mid-body, or not reading): drop it
                self.close_connection = True
                log.info("oauth POST %s: client idle past %ss, connection dropped", path, self.timeout)
            except Exception:   # never leak a stack; log + generic 500
                log.exception("oauth POST %s failed", path)
                self._oauth_error(500, "server_error")

        # ---- /register (RFC 7591 dynamic client registration) ----
        def _register(self) -> None:
            if not _REGISTER_LIMIT.allow(self._client()):
                self._too_many()
                return
            body = self._body_json()
            uris = body.get("redirect_uris")
            if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
                self._oauth_error(400, "invalid_redirect_uri", "redirect_uris must be a non-empty array")
                return
            # Bound a SINGLE registration's storage (open DCR is unauthenticated by spec): a
            # caller can't stuff an unbounded blob into one oauth_client row. The count of registrations is
            # itself unbounded (accepted risk — opt-in + TLS-gated; a minted token is revocable). SECURITY C16.
            if len(uris) > 16 or any(len(u) > 2048 for u in uris):
                self._oauth_error(400, "invalid_redirect_uri", "too many or too long redirect_uris")
                return
            if not all(valid_redirect_uri(u) for u in uris):
                self._oauth_error(400, "invalid_redirect_uri", "each redirect_uri must be https or http-loopback")
                return
            name = body.get("client_name")
            client_name = name.strip()[:128] if isinstance(name, str) and name.strip() else None
            with _LEDGER.slot(), db.connect() as conn:
                client_id = register_client(conn, client_name, list(uris))
                conn.commit()
            log.info("oauth: registered client %s (%d redirect uri(s))", client_id, len(uris))
            self._json(201, {
                "client_id": client_id,
                "client_name": client_name,
                "redirect_uris": list(uris),
                "grant_types": ["authorization_code"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            })

        # ---- /authorize GET (render consent) ----
        def _authorize_get(self) -> None:
            q = {k: v[0] for k, v in urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query, keep_blank_values=True).items()}
            client_id = q.get("client_id", "")
            redirect_uri = q.get("redirect_uri", "")
            with _LEDGER.slot(), db.connect() as conn:
                client = get_client(conn, client_id) if client_id else None
            err = _authorize_error(client, redirect_uri, q)
            if err is not None:
                self._html(400, f"<!doctype html><meta charset=utf-8><p>Authorization error: "
                                f"{html.escape(err)}. Nothing was authorized.</p>")
                return
            hidden = {k: q.get(k, "") for k in
                      ("client_id", "redirect_uri", "response_type", "code_challenge",
                       "code_challenge_method", "scope", "state", "resource")}
            self._html(200, consent_html(client_name=client["client_name"] if client else None,
                                         redirect_uri=redirect_uri, hidden=hidden))

        # ---- /authorize POST (consent submit → issue code) ----
        def _authorize_post(self) -> None:
            if not _CONSENT_LIMIT.allow(self._client()):
                self._too_many()
                return
            # Read the body BEFORE taking a ledger slot: a client trickling its body must hold only its own
            # thread, never one of the slots every other request needs (review SEC-01, round 1).
            f = self._body_form()
            with _LEDGER.slot():   # one slot for the whole consent, both of its ledger connections
                self._authorize_post_locked(f)

        def _authorize_post_locked(self, f: dict[str, str]) -> None:
            client_id = f.get("client_id", "")
            redirect_uri = f.get("redirect_uri", "")
            hidden = {k: f.get(k, "") for k in
                      ("client_id", "redirect_uri", "response_type", "code_challenge",
                       "code_challenge_method", "scope", "state", "resource")}
            # Validate the pasted project token with the SAME resolver the gateway uses, on a DEDICATED
            # autocommit connection: auth.resolve's contract is that its best-effort last_used bump must not
            # hold the api_token row lock — here that would otherwise last until our write txn commits.
            token = f.get("project_token", "").strip()
            principal = None
            if token:
                with db.connect(autocommit=True) as rconn:
                    principal = auth.resolve(rconn, token)
            with db.connect() as conn:
                client = get_client(conn, client_id) if client_id else None
                err = _authorize_error(client, redirect_uri, f)
                if err is not None:
                    self._html(400, f"<!doctype html><meta charset=utf-8><p>Authorization error: "
                                    f"{html.escape(err)}. Nothing was authorized.</p>")
                    return
                if principal is None or principal.project_id is None or principal.scope not in TENANT_SCOPES:
                    self._html(200, consent_html(
                        client_name=client["client_name"] if client else None, redirect_uri=redirect_uri,
                        hidden=hidden, error="That is not a valid project token. Paste a live read/consume/"
                                             "provision token for the project you want this connector to act as."))
                    return
                code = issue_code(conn, client_id=client_id, project_id=principal.project_id,
                                  scope=principal.scope, code_challenge=f.get("code_challenge", ""),
                                  redirect_uri=redirect_uri, resource_uri=f.get("resource") or None)
                sweep_expired_codes(conn)
                conn.commit()
            log.info("oauth: authorized client %s for project (scope=%s)", client_id, principal.scope)
            sep = "&" if "?" in redirect_uri else "?"
            loc = redirect_uri + sep + urllib.parse.urlencode(
                {"code": code, **({"state": f["state"]} if f.get("state") else {})})
            self._redirect(loc)

        # ---- /token (authorization_code + PKCE → mint api_token) ----
        def _token(self) -> None:
            f = self._body_form()
            if f.get("grant_type") != "authorization_code":
                self._oauth_error(400, "unsupported_grant_type")
                return
            code = f.get("code", "")
            verifier = f.get("code_verifier", "")
            if not code or not verifier:
                self._oauth_error(400, "invalid_request", "code and code_verifier are required")
                return
            with _LEDGER.slot(), db.connect() as conn:
                bound = take_code(conn, code)          # single-use + expiry enforced in the DELETE…RETURNING
                if bound is None:
                    conn.commit()
                    self._oauth_error(400, "invalid_grant", "code is unknown, used, or expired")
                    return
                # The redemption must present the SAME client_id + redirect_uri the code was bound to, and a
                # PKCE verifier that matches the stored S256 challenge. Any mismatch → invalid_grant.
                if (f.get("client_id", "") != bound["client_id"]
                        or f.get("redirect_uri", "") != bound["redirect_uri"]
                        or not pkce_s256_matches(verifier, bound["code_challenge"])):
                    conn.commit()   # the code is already consumed (single-use); do not re-issue it
                    self._oauth_error(400, "invalid_grant", "client/redirect/PKCE mismatch")
                    return
                label = f"oauth:{bound['client_id'][:16]}"   # ONE label — validated AND stored
                reason = admin.validate_token(bound["scope"], label, token_ttl_days())
                if reason is not None:                 # defensive: scope came from a CHECK-constrained column
                    conn.commit()
                    self._oauth_error(500, "server_error")
                    return
                minted = admin.create_token(conn, project_id=bound["project_id"], scope=bound["scope"],
                                            label=label, expires_in_days=token_ttl_days())
                if not minted.get("created"):
                    conn.rollback()
                    self._oauth_error(400, "invalid_grant", minted.get("reason") or "mint_failed")
                    return
                conn.commit()
            log.info("oauth: issued access token for project via client %s (scope=%s)",
                     bound["client_id"], bound["scope"])
            self._json(200, {
                "access_token": minted["secret"],
                "token_type": "Bearer",
                "expires_in": token_ttl_days() * 86400,
                "scope": bound["scope"],
            })

    return Handler


def main() -> None:
    """Entrypoint: `python -m br0kr.oauth`. Binds BR0KR_OAUTH_HOST:BR0KR_OAUTH_PORT (default 0.0.0.0:9100 —
    reachable on the compose network for Caddy). Refuses to start without BR0KR_OAUTH_ISSUER, since every
    discovery URL is built from it and a wrong/empty issuer silently breaks the whole flow."""
    logging_setup.apply_settings(log_file=os.environ.get("BR0KR_LOG_FILE"))
    if not issuer().startswith("https://"):
        raise SystemExit(
            "OAuth issuer must be an https URL — set BR0KR_PUBLIC_URL (the public tunnel/proxy URL) or "
            "BR0KR_TLS_SITE (the direct Caddy TLS host), or BR0KR_OAUTH_ISSUER to override. See issuer().")
    # Refuse to serve on a ledger older than this build (review ARCH-02); a boot-time blip is ridden through.
    with contextlib.closing(db.connect_with_retry()) as c:
        migrate.assert_schema(c, "oauth")
    host = os.environ.get("BR0KR_OAUTH_HOST", "0.0.0.0")   # in-cluster bind, fronted by Caddy TLS
    port = int(os.environ.get("BR0KR_OAUTH_PORT", "9100") or "9100")
    httpd = ThreadingHTTPServer((host, port), _make_handler())
    log.info("oauth shim serving on %s:%d (issuer=%s, resource=%s)", host, port, issuer(), resource())
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
