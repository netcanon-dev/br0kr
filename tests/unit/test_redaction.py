"""Unit tests for log redaction (br0kr/logging_setup.py) — the covering check for
SECURITY.md C2 ("secrets never logged").

The whole secret story depends on this filter being the backstop: even if a code path
ever logged a PVE token, a DSN, or a bearer header, the redactor must scrub it before it
reaches a handler. These tests assert exactly that for the secret shapes br0kr handles —
so C2 is a claim with a check, not a placeholder.
"""
from __future__ import annotations

import logging

import pytest

from br0kr import logging_setup as ls

pytestmark = pytest.mark.unit


def test_pve_api_token_header_is_scrubbed():
    """A Proxmox `PVEAPIToken=<id>=<secret>` header value is redacted from free text. This
    is THE secret br0kr's drivers carry; guards it ever surfacing via an exception repr or
    a stray log of the auth header."""
    out = ls.redact_text("calling pve with PVEAPIToken=root@pam!svc=9c1f-deadbeef-secret now")
    assert "deadbeef-secret" not in out and ls.REDACTED in out


def test_bearer_token_is_scrubbed():
    """An `Authorization: Bearer <token>` value is redacted. Guards a logged HTTP header
    leaking a token."""
    assert ls.REDACTED in ls.redact_text("Authorization: Bearer abcdef0123456789")


def test_dsn_credentials_are_scrubbed():
    """User:password embedded in a DSN/URL is redacted (the password is the secret). Guards
    a logged PGCONN/connection string exposing the DB password."""
    out = ls.redact_text("connect postgresql://broker:hunter2pass@db:5432/broker")
    assert "hunter2pass" not in out and ls.REDACTED in out


def test_keyword_form_dsn_password_is_scrubbed():
    """The libpq KEYWORD-form DSN `password=…` (br0kr's actual PGCONN shape everywhere — see
    `br0kr/db.py`, the run_*.sh, the HA runbook) is redacted from free text, not just the URL
    form. Guards the real regression behind SECURITY.md C2: the redactor's only DSN pattern
    used to be URL-form (`://user:pass@`), so a logged keyword DSN leaked the password — the
    shape the connection seam leans on this backstop to catch."""
    out = ls.redact_text("host=127.0.0.1 port=5432 dbname=broker user=broker password=hunter2pass")
    assert "hunter2pass" not in out and ls.REDACTED in out
    # The non-secret keywords (host/user/dbname) are preserved — only the value is blanked.
    assert "user=broker" in out and "host=127.0.0.1" in out


def test_sensitive_key_value_is_blanked():
    """A value under a sensitive KEY (password/secret/token/…) is blanked wholesale, so a
    structured extra carrying a secret is scrubbed even without a recognizable value
    pattern. Guards `log.info(..., extra={'password': x})`."""
    assert ls.redact_value("password", "hunter2pass") == ls.REDACTED
    assert ls.redact_value("token", "abc123") == ls.REDACTED


def test_redaction_filter_scrubs_the_record_message():
    """End-to-end through the handler filter: a LogRecord whose message embeds a PVE token
    comes out with the secret gone. Guards the actual logging path, not just the helper —
    this is the C2 backstop firing on a real record."""
    rec = logging.LogRecord("br0kr", logging.INFO, __file__, 1,
                            "provision failed PVEAPIToken=root@pam!t=topsecretvalue oops", (), None)
    ls.RedactionFilter().filter(rec)
    assert "topsecretvalue" not in rec.getMessage() and ls.REDACTED in rec.getMessage()


def test_signature_style_query_param_is_scrubbed():
    """A presigned-URL SIGNATURE query param (X-Amz-Signature / a bare sig) is blanked in free text, keeping
    the param name — the credential half of a signed download URL, which the old token/secret/password/auth
    name list missed (review S14). Guards a raw signed URL logged anywhere the RedactionFilter is attached."""
    out = ls.redact_text("GET https://s3/img.qcow2?X-Amz-Signature=DEADBEEFsecret&X-Amz-Expires=900")
    assert "DEADBEEFsecret" not in out and ls.REDACTED in out
    assert "X-Amz-Expires=900" in out   # a non-secret param is kept


def test_canonicalize_url_drops_query_fragment_and_userinfo():
    """canonicalize_url reduces a URL to scheme://host[:port]/path, DROPPING the query (where a presigned
    credential lives), the fragment, and any user:pass@ userinfo — the name-independent scrub the audit trail
    and the operator build log both apply (review S14). Non-URL / unparseable / non-str input passes through
    unchanged. Guards a signed-URL credential surviving in a stored/logged breadcrumb."""
    assert ls.canonicalize_url(
        "https://s3.example.com/imgs/deb12.qcow2?X-Amz-Signature=abc&x=1") == "https://s3.example.com/imgs/deb12.qcow2"
    assert ls.canonicalize_url("https://u:p@host:8443/a/b#frag") == "https://host:8443/a/b"   # userinfo+port+frag
    assert ls.canonicalize_url("https://[2001:db8::1]:8443/p?sig=x") == "https://[2001:db8::1]:8443/p"  # IPv6 kept
    assert ls.canonicalize_url("not a url") == "not a url"        # passthrough
    assert ls.canonicalize_url("") == "" and ls.canonicalize_url(None) is None and ls.canonicalize_url(7) == 7


def test_canonicalize_url_truncates_a_malformed_url_at_the_query_so_no_signature_survives():
    """THE FLOOR: even a URL urlsplit can't cleanly parse must lose its query — a one-slash `https:/` typo, a
    scheme-relative `//host`, and an empty-netloc `http:///` are all truncated at the first `?`/`#`, so a
    presigned credential can never pass through raw (review S14/M1: these exact shapes reach the audit trail
    via a dashboard download refused by the allowlist yet still audited with its raw body)."""
    for bad in ("https:/s3.example.com/img.qcow2?X-Amz-Signature=SECRET",   # one-slash typo → no '://'
                "//s3.example.com/img.qcow2?X-Amz-Signature=SECRET",        # scheme-relative
                "http:///img.qcow2?X-Amz-Signature=SECRET"):                # empty netloc
        out = ls.canonicalize_url(bad)
        assert "SECRET" not in out and "?" not in out, out
    assert ls.canonicalize_url("scheme:noslash#frag=SECRET") == "scheme:noslash"   # fragment also dropped
