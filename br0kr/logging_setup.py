"""logging_setup.py - structured logging primitives for br0kr.

Drop-in module that encodes the org logging standard (docs/standards/logging.md):

  * one idempotent configure point (configure_logging)
  * dual-shape output: plain text by default, JSON lines on LOG_FORMAT=json,
    both to stderr AND a rotating file
  * correlation IDs threaded through contextvars (push_context / set_context /
    generate_correlation_id) so nested + worker logs inherit a run_id
  * a RedactionFilter on every handler - secret/PII keys -> [REDACTED],
    secret value-patterns scrubbed from message text + tracebacks
  * level + format resolved by precedence: env > config/profile > default

Usage (see examples/logging_usage.py):

    from br0kr.logging_setup import (
        configure_logging, push_context, generate_correlation_id,
    )
    configure_logging(log_file="app.log")          # at process start, once
    log = logging.getLogger(__name__)              # per module

    with push_context(run_id=generate_correlation_id(), op="reconcile"):
        log.info("start")

TUNE PER PROJECT: the redaction denylists (_SENSITIVE_KEY_SUBSTRINGS /
_SENSITIVE_KEY_EXACT) and value patterns (_SECRET_VALUE_PATTERNS) below - add
your domain's secret + PII field names. Over-redaction is preferred to under.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import sys
import traceback
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# ---------------------------------------------------------------------------
# Standard LogRecord fields we never copy into JSON extras. Anything NOT here
# and NOT starting with "_" is treated as a structured extra (log.info(msg,
# extra={...})) and serialized / redacted.
# ---------------------------------------------------------------------------
_STD_LOG_FIELDS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
})

# Ambient context auto-merged into every record on the current task/thread.
# The default {} is never mutated in place (set/push always store a fresh dict),
# so there is no cross-context sharing hazard.
_log_context: ContextVar[dict[str, Any]] = ContextVar("log_context", default={})  # noqa: B039


# ===========================================================================
# Redaction - logs are a secret surface (docs/standards/logging.md sec 4)
# ===========================================================================
REDACTED = "[REDACTED]"

# Substring-matched, case-insensitive. Specific enough that a substring hit is
# almost always sensitive ("first_name" not bare "name", so "filename" /
# "username" are spared). TUNE: add your project's secret + PII key names.
_SENSITIVE_KEY_SUBSTRINGS: tuple[str, ...] = (
    # secrets / credentials
    "password", "passwd", "secret", "api_key", "apikey", "authorization",
    "cookie", "credential", "session_key", "private_key", "access_key",
    "dsn", "conn_str", "connection_string",
    # personal identity
    "email", "phone", "address", "dob", "date_of_birth", "birth",
    "ssn", "social_security", "first_name", "firstname", "last_name",
    "lastname", "full_name", "fullname", "postal",
)
# Exact-key matches (lowercased) - short words that would over-redact as
# substrings. TUNE per project.
_SENSITIVE_KEY_EXACT: frozenset[str] = frozenset({
    "state", "province", "region",
    "city", "zip", "zipcode", "zip_code", "postcode", "postal_code",
})

# Value patterns scrubbed from free text wherever they appear, as
# (pattern, replacement). The replacement may reference capture groups so a
# param NAME is kept while only its value is blanked. TUNE per project.
_SECRET_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"), REDACTED),     # Authorization: Bearer ...
    (re.compile(r"(?i)PVEAPIToken=\S+"), REDACTED),                    # Proxmox API token header (id=secret)
    (re.compile(r"\b[A-Za-z0-9_\-]*sk-[A-Za-z0-9_\-]{12,}"), REDACTED),  # sk-... style API keys
    (re.compile(r"(?i)://[^:@/\s]+:[^@/\s]+@"), "://" + REDACTED + "@"),  # creds in a URL/DSN
    # Sensitive query-string params in logged URLs: blank only the value,
    # keep the param name. Covers token/secret/password/api_key/auth params, plus
    # signature-style presigned-URL params (X-Amz-Signature / Signature / a bare
    # sig) — the credential half of a presigned download URL, which the name list
    # above misses (review S14). canonicalize_url() drops the query wholesale at
    # the source; this is the free-text defense-in-depth for any raw URL still logged.
    (re.compile(r"(?i)([?&][\w.+-]*(?:token|secret|password|api[_-]?key|auth|signature|sig)[\w.+-]*=)"
                r"[^&\s\"'#]+"), r"\1" + REDACTED),
    # libpq KEYWORD/value DSN form (br0kr's PGCONN shape: "host=… password=…"): blank the
    # value, keep the keyword. The URL-form patterns above don't reach this; covers
    # password= / pgpassword= / sslpassword= (the password env var and the key passphrase).
    (re.compile(r"(?i)\b((?:pg|ssl)?password=)[^&\s\"'#]+"), r"\1" + REDACTED),
)

_MAX_REDACT_DEPTH = 6


def _key_is_sensitive(key: str) -> bool:
    k = str(key).lower()
    if k in _SENSITIVE_KEY_EXACT:
        return True
    if any(sub in k for sub in _SENSITIVE_KEY_SUBSTRINGS):
        return True
    # Auth tokens: bare "token" or "*_token" (csrf_token, access_token) - but
    # NOT the plural counters max_tokens / total_tokens, which are non-secret
    # analytics the structured logging exists to carry.
    return k == "token" or k.endswith("_token")


def redact_text(value: Any) -> Any:
    """Scrub secret value-patterns from a string; pass non-strings through."""
    if not isinstance(value, str):
        return value
    for pat, repl in _SECRET_VALUE_PATTERNS:
        value = pat.sub(repl, value)
    return value


def canonicalize_url(value: Any) -> Any:
    """Reduce a URL to its non-secret identity — ``scheme://host[:port]/path`` — DROPPING the query, fragment,
    and any ``user:pass@`` userinfo. A presigned download URL carries its credential in the query
    (``X-Amz-Signature``, ``Signature``, a bare ``sig``), which a key-NAME denylist can't see (the whole URL
    sits under one benign key like ``url``) and which the query-param value scrubber only catches for
    token/secret/password/api_key/auth/signature-NAMED params — so dropping the query wholesale is the robust,
    name-independent scrub for a value stored/logged as an audit or build breadcrumb (review S14). The host+path
    is the useful identity (WHICH image); the sha256 is stored separately. A non-str / non-URL / unparseable
    input passes through, but with its query/fragment stripped (the credential floor below). Pure — no I/O."""
    if not isinstance(value, str):
        return value
    # THE FLOOR: whatever the URL's shape, a presigned credential lives in the query — so no query (nor
    # fragment) may EVER survive, even for a value urlsplit can't cleanly parse: a `https:/host` one-slash
    # typo, a scheme-relative `//host`, or an empty-netloc `http:///` (review S14/M1 — these reach the audit
    # via a dashboard action that is refused by the allowlist yet still audited with its raw body). Truncate
    # at the first `?`/`#` so the credential is gone regardless of parseability.
    trimmed = value
    for sep in ("?", "#"):
        idx = trimmed.find(sep)
        if idx != -1:
            trimmed = trimmed[:idx]
    if "://" not in value:
        return trimmed
    try:
        parts = urlsplit(value)
    except ValueError:
        return trimmed
    if not parts.scheme or not parts.netloc:
        return trimmed
    # Well-formed: keep host[:port] verbatim (IPv6 literal brackets and the port survive) and drop the query,
    # fragment, AND a user:pass@ userinfo — the host has no literal '@', so the LAST '@' bounds any userinfo.
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def redact_value(key: str, value: Any, _depth: int = 0) -> Any:
    """Return a redacted copy of ``value``.

    A sensitive KEY blanks the whole value. dict/list values recurse (so PII
    nested under a benign key like "profile" is still caught); strings are
    scrubbed for embedded secret patterns.
    """
    if _depth > _MAX_REDACT_DEPTH:
        return REDACTED
    if isinstance(value, dict):
        return {k: redact_value(k, v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(key, v, _depth + 1) for v in value]
    if _key_is_sensitive(key) and value not in (None, "", 0, False):
        return REDACTED
    return redact_text(value)


class RedactionFilter(logging.Filter):
    """Scrub PII/secrets from every record before it is formatted.

    Attached to the HANDLERS (not a logger) so it applies to records from all
    loggers regardless of formatter. Mutates the record in place and flags it,
    so a record shared by multiple handlers is scrubbed once. Ambient
    push_context()/set_context() values are not on the record - the
    JSONFormatter redacts those at serialization time (defense in depth).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "_redacted", False):
            return True
        # Free-text message: interpolate now, scrub, clear args so downstream
        # getMessage() returns the already-redacted string.
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = redact_text(message)
        record.args = ()
        for key in list(record.__dict__.keys()):
            if key in _STD_LOG_FIELDS or key.startswith("_"):
                continue
            record.__dict__[key] = redact_value(key, record.__dict__[key])
        record._redacted = True
        return True


# ===========================================================================
# Formatters
# ===========================================================================
class JSONFormatter(logging.Formatter):
    """One JSON object per record, newline-delimited.

    Always-present keys: ts (ISO-8601 UTC, ms), level, logger, msg, module,
    func, line, thread. Optional: exc (when exc_info=True) + any ambient
    context or per-call extras.
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%Y-%m-%dT%H:%M:%S")
        payload: dict[str, Any] = {
            "ts": f"{ts}.{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
            "thread": record.threadName,
        }

        if record.exc_info:
            etype, evalue, etb = record.exc_info
            payload["exc"] = {
                "type": etype.__name__ if etype else None,
                # Exception text / tracebacks are free text the key-redactor
                # can't reach - scrub secret value-patterns here.
                "message": redact_text(str(evalue)) if evalue else None,
                "trace": [redact_text(line) for line
                          in traceback.format_exception(etype, evalue, etb)],
            }

        # Ambient context (lowest precedence). Redact here - context lives in a
        # contextvar, not on the record, so the handler filter can't reach it.
        ctx = _log_context.get()
        if ctx:
            for key, value in ctx.items():
                if key not in payload:
                    payload[key] = _safe_value(redact_value(key, value))

        # Per-call extras (highest precedence; never clobber standard keys).
        # Already scrubbed on the record by RedactionFilter; re-running is
        # idempotent and makes the formatter self-protecting.
        for key, value in record.__dict__.items():
            if key in _STD_LOG_FIELDS or key.startswith("_"):
                continue
            if key in payload and key not in ctx:
                continue
            payload[key] = _safe_value(redact_value(key, value))

        return json.dumps(payload, default=str, ensure_ascii=False)


def _safe_value(value: Any) -> Any:
    """Coerce unusual types to something JSON can serialize."""
    try:
        json.dumps(value, default=str, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        return str(value)


class _RedactingTextFormatter(logging.Formatter):
    """Plain-text formatter that also scrubs the appended traceback / stack
    (the message itself is already redacted on the record)."""

    def formatException(self, ei: Any) -> str:
        out: str = redact_text(super().formatException(ei))   # a str in, a str out
        return out

    def formatStack(self, stack_info: str) -> str:
        out: str = redact_text(super().formatStack(stack_info))
        return out


# ===========================================================================
# Context helpers - correlation IDs (docs/standards/logging.md sec 2)
# ===========================================================================
@contextmanager
def push_context(**fields: Any) -> Iterator[None]:
    """Tag every record emitted inside the block (including nested calls).

    Nests: inner values override outer on collision, only for the inner block.
    Unwinds cleanly on exception.
    """
    current = _log_context.get()
    token = _log_context.set({**current, **fields})
    try:
        yield
    finally:
        _log_context.reset(token)


def set_context(**fields: Any) -> None:
    """One-way merge into ambient context for this task/thread (no pop).

    Use at the top of a background worker - contextvars do NOT copy across a
    threading.Thread spawn, so the worker must re-establish its own context.
    """
    current = _log_context.get()
    _log_context.set({**current, **fields})


def clear_context() -> None:
    """Reset ambient context to empty. Primarily for test isolation."""
    _log_context.set({})


def current_context() -> dict[str, Any]:
    """Snapshot the current ambient context (debugging / tests)."""
    return dict(_log_context.get())


def generate_correlation_id() -> str:
    """Short (12-char hex) id for correlating a sequence of related logs."""
    return uuid.uuid4().hex[:12]


# ===========================================================================
# Configuration (docs/standards/logging.md sec 1, 5, 7)
# ===========================================================================
_PLAIN_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Third-party loggers pinned to WARNING so INFO stays signal (sec 3).
# TUNE: add your noisy dependencies (br0kr e.g.: "urllib3", "requests").
_NOISY_LOGGERS = ("urllib3", "requests", "asyncio")


def configure_logging(
    level: int = logging.INFO,
    json_output: bool | None = None,
    log_file: str | None = None,
    file_max_bytes: int = 5_000_000,
    file_backup_count: int = 3,
    stream: Any = None,
) -> None:
    """Install handlers on the root logger. Idempotent.

    Args:
        level             root log level (logging.DEBUG / INFO / ...).
        json_output       True -> JSON on stream + file. None (default) reads
                          LOG_FORMAT env ("json" -> True, else False).
        log_file          if given, also attach a RotatingFileHandler here. A path
                          that cannot be created or opened logs a WARNING and is
                          skipped (stderr still gets everything).
        file_max_bytes    per-file ceiling before rotation (default 5 MB).
        file_backup_count number of rotated files kept (default 3).
        stream            where the StreamHandler writes (default sys.stderr).

    Re-calling replaces existing root handlers, so tests + supervised restarts
    stay clean.
    """
    if json_output is None:
        json_output = os.environ.get("LOG_FORMAT", "").strip().lower() == "json"

    formatter: logging.Formatter = (
        JSONFormatter() if json_output else _RedactingTextFormatter(_PLAIN_FORMAT)
    )

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.setLevel(level)

    # One shared, stateless redactor on every handler; the per-record flag
    # makes the work happen once even with multiple handlers.
    redactor = RedactionFilter()

    stream_handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(redactor)
    root.addHandler(stream_handler)

    if log_file:
        # A log file is a convenience on top of stderr, so an unwritable path is a WARNING, not a crash: the
        # containers' root filesystem is read-only (review SEC-02), and a BR0KR_LOG_FILE left pointing into it
        # would otherwise stop every service at start, crash-looping under `restart: unless-stopped`.
        try:
            # RotatingFileHandler does not create the dir; make it (CI/clean checkout).
            log_dir = os.path.dirname(log_file)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=file_max_bytes,
                backupCount=file_backup_count,
                encoding="utf-8",
            )
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "log file %s is not writable (%s); logging to stderr only", log_file, exc,
                extra={"log_file": log_file})
        else:
            file_handler.setFormatter(formatter)
            file_handler.addFilter(redactor)
            root.addHandler(file_handler)

    # Pin noisy third-party loggers so INFO stays signal (sec 3).
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def using_json_format() -> bool:
    """True if any root handler currently uses JSONFormatter (tests / gating)."""
    return any(isinstance(h.formatter, JSONFormatter)
               for h in logging.getLogger().handlers)


# ===========================================================================
# Precedence resolution: env > config/profile > default (sec 7)
# ===========================================================================
_VALID_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def resolve_log_settings(
    profile: dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve effective level + format from env, then config/profile, then default.

    Precedence (highest first):
      1. env var (LOG_LEVEL / LOG_FORMAT) - always wins (field debugging).
      2. config/profile (profile["logging"]["level"|"format"]).
      3. default: level=INFO, format=text.

    Returns: level (int), level_name, json_output (bool), level_source,
    format_source. The *_source fields let a UI surface "env var is overriding
    your setting" so a saved value that doesn't take effect isn't confusing.
    An unknown value falls back to INFO/text - nothing runs silent.

    NOTE: ``profile`` is shown as a plain dict; wire it to your own config
    object (br0kr: read from the app config / a settings table).
    """
    if env is None:
        env = dict(os.environ)
    profile_logging = (profile or {}).get("logging", {}) or {}

    # level
    level_source = "default"
    env_level = env.get("LOG_LEVEL", "").strip().upper()
    if env_level:
        level_name, level_source = env_level, "env"
    elif profile_logging.get("level"):
        level_name, level_source = str(profile_logging["level"]).upper(), "profile"
    else:
        level_name = "INFO"

    resolved_level = getattr(logging, level_name, None)
    if not isinstance(resolved_level, int):
        level_name, resolved_level, level_source = "INFO", logging.INFO, "default"

    # format
    fmt_source = "default"
    env_format = env.get("LOG_FORMAT", "").strip().lower()
    if env_format:
        json_output, fmt_source = env_format == "json", "env"
    elif profile_logging.get("format"):
        json_output = str(profile_logging["format"]).lower() == "json"
        fmt_source = "profile"
    else:
        json_output = False

    return {
        "level": resolved_level,
        "level_name": level_name,
        "json_output": json_output,
        "level_source": level_source,
        "format_source": fmt_source,
    }


def apply_settings(
    profile: dict[str, Any] | None = None,
    log_file: str | None = None,
    file_max_bytes: int = 5_000_000,
    file_backup_count: int = 3,
) -> dict[str, Any]:
    """resolve_log_settings + configure_logging in one call.

    Call after a config change so a new level/format take effect immediately
    without a restart. Returns the resolved-settings dict so a caller can
    surface source info (e.g. "LOG_LEVEL env var is overriding your choice").
    """
    resolved = resolve_log_settings(profile)
    configure_logging(
        level=resolved["level"],
        json_output=resolved["json_output"],
        log_file=log_file,
        file_max_bytes=file_max_bytes,
        file_backup_count=file_backup_count,
    )
    return resolved
