"""audit.py — the APPEND-ONLY operator/agent audit trail (migration 0023).

A "who did what" trail across the two mutating surfaces: the dashboard operator POSTs (a shared operator
token + client IP) and the agent-facing MCP write verbs (the authenticated Principal: token_id/project/scope).

Like `bulk.py` and `settings.py`, this is a PURE LAYER BESIDE admission: `record` is a plain INSERT — it
debits NO capacity scope, takes NO advisory lock, and CANNOT weaken the `admit()` double-allocation floor.
Two hard rules shape the API:

  1. BEST-EFFORT, NEVER-BREAKS-THE-ACTION. `record` is called AFTER the caller's action has already durably
     committed, and it swallows EVERY error (a not-yet-0023 ledger, a blip) — so auditing can never roll back
     or fail a lease op / an operator write. It mirrors `auth.resolve`'s best-effort `last_used_at` bump and
     `daemon._apply_settings`'s tick fail-safe.
  2. NEVER STORE A SECRET. Only the action's PARAMS are recorded (never a verb RESULT — e.g. `create_token`'s
     result carries the one plaintext secret), and params pass through `redact` (a defensive name-denylist)
     first. The table is APPEND-ONLY (INSERT + SELECT) with EXACTLY ONE sanctioned mutation: `prune` — the
     opt-in retention DELETE (0024), daemon-driven + breadcrumbed. No module UPDATEs it, and `prune` is the
     only DELETE (a SECURITY.md control + the `test_audit.py` hygiene test that scans br0kr/*.py keep it honest).

`DenialLog` (0025) extends the trail from applied-writes-only to ALSO an intrusion signal: it records DENIED
authorization attempts (a wrong/absent bearer, a forbidden tier/scope) as `outcome='denied'` rows — still plain
best-effort INSERTs, still append-only, still aged out by the same retention. Because a denial is attacker-
triggered (a brute-forcer could flood the table), it is COALESCED per (surface, source_ip, action, reason) into
at most one row per window with a process-wide per-window ceiling, and it opens a connection ONLY when the
throttle admits a write (a fast-fail connector with a short connect_timeout) — so it can never turn a clean
401/403 into a 500, adds no latency to a suppressed denial, and even a network partition can't meaningfully
stall the reject. A write that fails (a ledger blip) is rolled back in the throttle, so it loses no coalesced
count and doesn't falsely charge the per-window ceiling.
"""
from __future__ import annotations

import contextlib
import csv
import datetime as dt
import io
import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

import psycopg
from psycopg.types.json import Json

from br0kr.logging_setup import canonicalize_url

log = logging.getLogger(__name__)

# Clamp for the panel read (`query`/`list_recent`) — a bounded slice of the (unbounded, append-only) table,
# like inventory's queue cap. EXPORT_CAP is the larger ceiling for a filtered download (offline analysis); a
# hostile/huge limit can exceed NEITHER. A cap HIT on export is never silent (SECURITY.md 'no silent caps').
_MAX_RECENT = 500
EXPORT_CAP = 5000

# Redaction denylist, by key SUBSTRING (case-insensitive). Our OWN narrow list — deliberately NOT
# logging_setup's, which masks bare 'token' / '*_token' and would wrongly blank the USEFUL token_id. We mask
# real-secret-shaped keys (a token/pdm secret-env NAME, an ssh/public key, a DSN/password) while KEEPING ids
# (token_id / lease_id / node_id), checksums (sha256), and idem keys. Denylist-by-name (not allowlist) so a
# FUTURE secret-carrying param is masked by default; over-redacting a harmless *_secret_env NAME is accepted,
# under-redacting is not.
_SECRET_KEY_SUBSTRINGS = ("secret", "password", "passwd", "authorization", "pubkey", "ssh", "pgconn", "dsn")
_REDACTED = "***redacted***"

# A URL param's value is canonicalized to scheme://host/path on storage (review S14): a presigned download
# URL hides its credential in the query (X-Amz-Signature / Signature / sig), which the key-NAME denylist above
# can't see because the whole URL sits under one benign key. Matched by key SUBSTRING so `url`, `image_url`,
# `download_url` are all covered.
_URL_KEY_SUBSTRING = "url"

# Leading chars that make a spreadsheet EXECUTE a CSV cell as a formula (CWE-1236). export_csv prefix-quotes any
# string cell starting with one of these so the agent-supplied `target` (a target_template_id, validated only
# non-empty) can't plant `=`/`+`/`-`/`@`/TAB/CR/LF that runs when an operator opens the export (review S13). Both
# CR and LF are included — a naive line-splitter / Text-to-Columns can start a fresh cell after either.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")

# Primary-id keys, in priority order — the `target` of an action (a quick who-touched-what handle).
_TARGET_KEYS = ("lease_id", "node_id", "project_id", "template_id", "target_template_id", "cluster_id",
                "storage_id", "token_id", "operator_id", "migration_id", "build_id", "url_prefix")

_INSERT = (
    "INSERT INTO operator_audit "
    "(surface, action, actor, actor_token_id, project_id, scope, target, params, source_ip, outcome) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
# The stored columns, in order — shared by the panel read (`query`) and the download (`export_*`). Ordered by
# `id DESC` (the monotonic insertion key), NOT `at`: id is a total order, so a `before_id` keyset cursor walks
# strictly older rows with no dup/skip — unlike `at DESC` (two rows can share an `at`), which would need a
# compound-column cursor to page correctly. `outcome` (0025, a CODE-convention domain — 0025 deliberately put
# NO CHECK constraint on the column so a new value never makes the best-effort writer fail): NULL = an applied
# write, 'denied' = a rejected auth attempt, 'refused' = a final admission refusal (0033/PR-3).
_COLS = ("id, at, surface, action, actor, actor_token_id, project_id, scope, target, params, source_ip, "
         "outcome")
_EXPORT_COLUMNS = ("id", "at", "surface", "action", "actor", "actor_token_id",
                   "project_id", "scope", "target", "params", "source_ip", "outcome")

# ── Denial (401/403) coalescing (0025) ──────────────
# A denial is recorded at the AUTHORIZE layer, BEFORE any action — unlike `record` (a post-commit success
# append), a denial has no committed action to ride, so `DenialLog` opens its OWN short-lived connection, and
# only when its in-memory throttle admits a write. The throttle exists because a denial is attacker-triggered:
# a brute-forcer could otherwise flood the (append-only) table. Two bounds:
#   * per-key COALESCING — at most one row per (surface, source_ip, action, reason) per window, folding the
#     suppressed count into the written row's `attempts` ("~40 attempts in the last minute" as ONE row);
#   * a process-wide per-window CEILING — bounds total denial rows even under a rotating-IP flood that defeats
#     per-key coalescing (each spoofed IP is a fresh key = a fresh "first denial" = a write); excess is dropped
#     with a single WARNING per window (never a silent cap).
# Memory is bounded to _DENIAL_MAX_KEYS tracked keys (least-recently-seen evicted). The clock is injectable so
# the throttle is deterministically unit-testable.
_DENIAL_WINDOW_S = 60.0      # per-key coalescing window AND the global-ceiling window
_DENIAL_GLOBAL_MAX = 200     # max denial ROWS written per window across ALL keys (rotating-IP flood backstop)
_DENIAL_MAX_KEYS = 4096      # cap on distinct (surface, ip, action, reason) keys held in memory

# PLACEMENT-refusal coalescing (0033/PR-3) — a SEPARATE DenialLog instance per admitting process (daemon hold
# path; the MCP + dashboard final-refusal paths), keyed on (surface, action, reason, template_id, project_id)
# (project_id IN the key so two tenants' refusals don't coalesce into one mis-attributed row) with
# outcome='refused'. Its own SMALLER ceiling (a refusal is agent-triggered, not attacker-triggered, and a
# hammering agent must not flood the trail) — the auth _DENIAL_GLOBAL_MAX=200 is UNTOUCHED so the two channels
# never contend for each other's budget.
_REFUSAL_WINDOW_S = 60.0     # per-key coalescing window for placement refusals
_REFUSAL_GLOBAL_MAX = 100    # max refusal ROWS written per window across ALL keys (own budget, not the auth 200)


def _like_escape(s: str) -> str:
    """Escape an operator's literal search text for a LIKE/ILIKE pattern so its `%`/`_` can't act as wildcards
    (a search for `cancel_lease` or `100%` means those literally). PG's default LIKE escape char is backslash,
    so `\\` → `\\\\`, `%` → `\\%`, `_` → `\\_`. Pure. The escaped text is still passed as a BOUND parameter —
    this only stops metacharacter surprise, injection is already impossible."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _key_is_secret(key: str) -> bool:
    k = key.lower()
    return any(s in k for s in _SECRET_KEY_SUBSTRINGS)


def _key_is_url(key: str) -> bool:
    return _URL_KEY_SUBSTRING in key.lower()


def _canon_url_value(value: Any) -> Any:
    """Canonicalize a URL-named param's value: a str is reduced to scheme://host/path, and a LIST is mapped
    element-wise (a future list-of-urls param must not smuggle a signed URL past redact's denylist-by-default
    contract, review S14/L1). Any other type is returned unchanged."""
    if isinstance(value, str):
        return canonicalize_url(value)
    if isinstance(value, list):
        return [canonicalize_url(x) if isinstance(x, str) else x for x in value]
    return value


def redact(params: dict[str, Any]) -> dict[str, Any]:
    """A PURE, deep-copied redaction of an action's params for storage: any key whose lowercased name contains
    a secret-shaped substring (secret/password/ssh/pubkey/dsn/…) has its value replaced by '***redacted***',
    recursing into nested dicts; a URL-named key's string (or list-of-strings) value is canonicalized to
    scheme://host/path (its query — where a presigned credential lives — dropped, review S14); every other
    value (ids, counts, checksums, idem keys) is kept. Denylist-by-NAME so a future secret param is masked.
    NEVER mutates the caller's dict. Guards a secret (or bulky key material like an ssh_pubkey, or a signed-URL
    credential) ever landing in the audit table. A non-dict input → {}."""
    if not isinstance(params, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in params.items():
        if _key_is_secret(str(k)):
            out[k] = _REDACTED
        elif isinstance(v, dict):
            out[k] = redact(v)
        elif _key_is_url(str(k)):
            out[k] = _canon_url_value(v)
        else:
            out[k] = v
    return out


def _csv_safe(value: Any) -> Any:
    """Neutralise a spreadsheet formula-injection cell (CWE-1236): a STRING value that would EXECUTE when the
    export is opened in Excel/Sheets — because it starts with a formula trigger (`=`/`+`/`-`/`@`) or a control
    char (TAB/CR) — is prefixed with a single quote so the spreadsheet treats it as literal text. Applied to
    every exported cell (the agent-supplied `target`/`target_template_id` is the known vector, but guarding all
    cells is cheap defense-in-depth). Non-str / empty values (ints like id, None) pass through unchanged. Pure."""
    if isinstance(value, str) and value and value[0] in _CSV_FORMULA_PREFIXES:
        return "'" + value
    return value


def primary_target(params: dict[str, Any]) -> str | None:
    """The primary id an action targeted — the first present of `_TARGET_KEYS`, stringified for the `target`
    column (a cheap who-touched-what handle). None when the params carry no recognized id. Pure."""
    if not isinstance(params, dict):
        return None
    for key in _TARGET_KEYS:
        v = params.get(key)
        if v is not None and v != "":
            return str(v)
    return None


def record(
    conn: psycopg.Connection[Any], *, surface: str, action: str, actor: str,
    params: dict[str, Any] | None = None, actor_token_id: int | None = None,
    project_id: str | None = None, scope: str | None = None, target: str | None = None,
    source_ip: str | None = None,
) -> None:
    """BEST-EFFORT append of one audit row on the caller's connection, then commit ITS OWN row. NEVER raises
    and NEVER rolls back the caller's action — so it MUST be called AFTER that action has already durably
    committed (the dashboard `_action` commit; the self-committing MCP gateway/admin delegate). On ANY error —
    a not-yet-0023 ledger (UndefinedTable), a ledger blip, a serialization error — it does a guarded rollback
    (to clear the aborted txn on the shared conn) + logs a warning; the caller's committed action is untouched.
    Params are redacted before storage (a secret never lands here). Debits no scope, takes no lock."""
    try:
        # outcome=None → an APPLIED write (the success trail); denials are written by DenialLog with 'denied'.
        conn.execute(_INSERT, (surface, action, actor, actor_token_id, project_id, scope, target,
                               Json(redact(params or {})), source_ip, None))
        conn.commit()
    except Exception:
        with contextlib.suppress(Exception):
            conn.rollback()
        log.warning("audit: record failed (swallowed) surface=%s action=%s", surface, action, exc_info=True)


class _DenialState:
    """Per-key throttle bookkeeping: `count` denials accrued since the last WRITTEN row, `last_write` (the
    monotonic time we last emitted a row for this key, or None if never), `last_seen` (for LRU eviction), and
    `writing` (a row for this key is in flight — so a concurrent denial coalesces into `count` instead of
    racing a second write, and a FAILED write can be rolled back without a lost count)."""

    __slots__ = ("count", "last_seen", "last_write", "writing")

    def __init__(self, now: float) -> None:
        self.count = 0
        self.last_write: float | None = None
        self.last_seen = now
        self.writing = False


class DenialLog:
    """In-memory coalescing recorder for a best-effort audit OUTCOME — ONE instance per serving process per
    channel. Two channels use it: DENIED auth attempts (`note`, `outcome='denied'`, 0025) and FINAL admission
    REFUSALS (`note_refusal`, `outcome='refused'`, 0033/PR-3). The `outcome` value + the coalescing key shape
    are the only differences; the throttle/ceiling/eviction core is shared. Thread-safe (the dashboard is
    thread-per-connection; FastMCP runs sync tools in a threadpool). Best-effort: it NEVER raises and opens a DB
    connection ONLY when the throttle admits a write, so it can't turn a clean 4xx/refusal into a 500 or add
    latency to a coalesced / over-cap event. It coalesces per opaque key into <=1 row per `window_s` (folding
    the suppressed count into the written row's `attempts`); a process-wide `global_max`-rows-per-window ceiling
    bounds writes under a flood; at most `max_keys` keys are tracked (least-recently-seen evicted). The clock is
    injectable for deterministic tests."""

    def __init__(self, *, window_s: float = _DENIAL_WINDOW_S, global_max: int = _DENIAL_GLOBAL_MAX,
                 max_keys: int = _DENIAL_MAX_KEYS, outcome: str = "denied",
                 now: Callable[[], float] = time.monotonic) -> None:
        self._window_s = float(window_s)
        self._global_max = int(global_max)
        self._max_keys = int(max_keys)
        # The outcome VALUE this instance stamps. Default 'denied' keeps every existing auth DenialLog()
        # byte-identical; the placement instances pass outcome='refused'. (0025 has no CHECK, so a new value is
        # a pure code convention — never a failing best-effort write.)
        self._outcome = str(outcome)
        self._now = now
        self._lock = threading.Lock()
        # The coalescing key is OPAQUE: the auth `note` path uses (surface, source_ip, action, reason); the
        # placement `note_refusal` path uses (surface, action, reason, template_id, project_id). Treated as
        # an arbitrary hashable tuple, so the two key shapes coexist in one instance's map without aliasing.
        self._keys: OrderedDict[tuple[Any, ...], _DenialState] = OrderedDict()
        self._window_start = now()
        self._window_writes = 0
        self._dropped = 0     # over-ceiling denials dropped in the CURRENT window (surfaced once at roll-over)

    def _reserve(self, key: tuple[Any, ...]) -> tuple[bool, int, _DenialState | None]:
        """Throttle core, phase 1 (holds the lock). Decides whether to write NOW and RESERVES the write —
        marking the key in-flight (`writing`) and charging the per-window ceiling — but WITHOUT yet consuming
        the key's count or advancing its window, so a FAILED write can be rolled back by `_release` with no lost
        count and no phantom ceiling charge (the bug the review caught: optimistic mutation before the DB
        touch). Returns (write, attempts, state): (True, the coalesced count to report, the reserved
        `_DenialState`) to write, else (False, 0, None). The reserved STATE OBJECT is returned (not the key) so
        `_release` finalizes the EXACT reservation even if that key is LRU-evicted / re-created mid-write — a
        stale key lookup would otherwise skip the failure refund or clobber a different state (a second review
        catch). Rolls the global window (a single WARNING if the prior window dropped over-ceiling denials —
        never a silent cap), evicts the least-recently-seen NON-writing key at capacity (an in-flight key is
        never evicted, so its reservation stays live), and counts EVERY denial (a coalesced / over-ceiling /
        rolled-back denial keeps accruing, so nothing is silently lost)."""
        with self._lock:
            now = self._now()
            if now - self._window_start >= self._window_s:
                if self._dropped:
                    log.warning("audit: denial-log dropped %d over-ceiling %s rows in the last ~%.0fs "
                                "(global cap %d/window)", self._dropped, self._outcome, self._window_s,
                                self._global_max)
                self._window_start = now
                self._window_writes = 0
                self._dropped = 0
            st = self._keys.get(key)
            if st is None:
                if len(self._keys) >= self._max_keys:
                    self._evict_one()   # drop the least-recently-seen key that is NOT mid-write
                st = _DenialState(now)
                self._keys[key] = st
            self._keys.move_to_end(key)   # mark most-recently-seen for LRU
            st.count += 1
            st.last_seen = now
            # not due if within the window OR a write for this key is already in flight — either way coalesce
            # into `count` (which is preserved), never a second racing/duplicate write.
            due = (st.last_write is None or (now - st.last_write) >= self._window_s) and not st.writing
            if not due:
                return (False, 0, None)
            if self._window_writes >= self._global_max:
                # over the per-window ceiling: DON'T write, but keep `count` accruing so the next admitted row
                # for this key still reports the true total. Counted for the roll-over WARNING.
                self._dropped += 1
                return (False, 0, None)
            st.writing = True
            self._window_writes += 1   # charged at reserve; `_release` REFUNDS it if the write fails
            return (True, st.count, st)

    def _evict_one(self) -> None:
        """LRU-evict ONE key at capacity, skipping any that is currently mid-write (`writing`) so an in-flight
        reservation is never orphaned (the eviction-during-write refund/clobber bug the review caught). Caller
        holds the lock. In-flight keys are bounded by the per-window ceiling (`global_max` << `max_keys`), so a
        non-writing victim effectively always exists; if EVERY tracked key were somehow writing, we skip the
        eviction (let the map briefly exceed max_keys) rather than evict a live reservation — bounded and safe."""
        for k, st in self._keys.items():   # least-recently-seen first (OrderedDict insertion/LRU order)
            if not st.writing:
                del self._keys[k]
                return

    def _release(self, st: _DenialState, attempts: int, *, wrote: bool) -> None:
        """Throttle core, phase 2 (holds the lock). Finalizes the reserved STATE (the exact object `_reserve`
        returned — NOT re-looked-up by key, so an evict/re-create of that key mid-write can't skip the refund or
        clobber a different state). On SUCCESS: consume the `attempts` denials we persisted (leaving any that
        coalesced in DURING the write to be reported next time) and stamp `last_write`. On FAILURE (a ledger
        blip): REFUND the window charge and leave `count` + `last_write` untouched, so the accrued denials are
        written by the NEXT attempt — no silent under-count, no phantom ceiling charge that would drop OTHER
        keys' denials. Clears the in-flight flag either way."""
        with self._lock:
            st.writing = False
            if wrote:
                st.count = max(0, st.count - attempts)
                st.last_write = self._now()
            else:
                self._window_writes = max(0, self._window_writes - 1)

    def _note_core(self, connect: Callable[[], psycopg.Connection[Any]], *, key: tuple[Any, ...], surface: str,
                   action: str, reason: str, actor: str, actor_token_id: int | None, project_id: str | None,
                   scope: str | None, target: str | None, source_ip: str | None,
                   params: dict[str, Any] | None) -> None:
        """The shared reserve→connect→INSERT→release body for BOTH `note` (denials) and `note_refusal`
        (placement refusals). Writes `outcome=self._outcome`. The stored params ALWAYS carry {reason, attempts}
        (the coalesced count), plus — for a refusal — the RESERVING caller's own `params` (lease/template/node
        ids), NOT a shared per-key buffer (which would race a concurrent same-key event, Fable review).
        `connect` is the surface's FAST-FAIL connector (short connect_timeout): a refused ledger
        raises immediately and is swallowed. The throttle RESERVES first (no DB touch), so a coalesced /
        over-ceiling event costs nothing; only a reserved write opens a connection, and `_release` rolls the
        reservation back on failure. NEVER raises — auditing can't break the caller's response."""
        try:
            write, attempts, st = self._reserve(key)
            if not write or st is None:   # not due / over-ceiling / coalesced → nothing reserved to release
                return
            wrote = False
            try:
                # Use the params PASSED to THIS (the reserving) call — NOT a shared per-key buffer: a concurrent
                # same-key event would otherwise swap the buffer between reserve and write, producing a row
                # whose identity (actor/token/project) and params come from DIFFERENT callers (Fable review).
                row = {**(params or {}), "reason": reason, "attempts": attempts}
                with contextlib.closing(connect()) as conn:
                    conn.execute(_INSERT, (surface, action, actor, actor_token_id, project_id, scope, target,
                                           Json(redact(row)), source_ip, self._outcome))
                    conn.commit()
                wrote = True
            finally:
                self._release(st, attempts, wrote=wrote)
        except Exception:
            log.warning("audit: %s record failed (swallowed) surface=%s action=%s reason=%s",
                        self._outcome, surface, action, reason, exc_info=True)

    def note(self, connect: Callable[[], psycopg.Connection[Any]], *, surface: str, action: str, reason: str,
             source_ip: str | None = None, actor: str = "anonymous", actor_token_id: int | None = None,
             project_id: str | None = None, scope: str | None = None) -> None:
        """Best-effort record of ONE denied attempt (an `outcome='denied'` row on an auth-outcome instance).
        `reason` (no_token / invalid_token / forbidden_scope / unauthorized / disabled) + the coalesced
        `attempts` count are stored in params (redacted); `target` is NULL (a denied call never parsed its
        args); the key is (surface, source_ip, action, reason). Behaviour is byte-identical to before PR-3 —
        the throttle, ceiling, and coalescing are unchanged; only the reserve/write body is now shared."""
        self._note_core(connect, key=(surface, source_ip, action, reason), surface=surface, action=action,
                        reason=reason, actor=actor, actor_token_id=actor_token_id, project_id=project_id,
                        scope=scope, target=None, source_ip=source_ip, params=None)

    def note_refusal(self, connect: Callable[[], psycopg.Connection[Any]], *, surface: str, action: str,
                     reason: str, template_id: str | None = None, node_id: str | None = None,
                     lease_id: int | None = None, actor: str = "placement", actor_token_id: int | None = None,
                     project_id: str | None = None, source_ip: str | None = None,
                     extra: dict[str, Any] | None = None) -> None:
        """Best-effort record of ONE FINAL admission refusal (an `outcome='refused'` row, on an
        outcome='refused' instance). Coalesced per (surface, action, reason, template_id, project_id) — a
        hammering agent gets ~1 row/window with a folded `attempts` count, never a flood. `project_id` is IN
        the key so two tenants refused for the same reason DON'T coalesce into one row that would then mis-
        attribute one tenant's identity/count to the other (Fable review); the daemon path passes project_id
        None. The stored params carry the placement context (lease/template/node ids + reason) so an operator
        sees WHAT was refused and WHY; `target` is the primary id (lease → node → template). Distinct from
        `note`'s key shape + outcome, so the auth and placement channels never alias. NEVER raises."""
        ctx: dict[str, Any] = {"reason": reason}
        if lease_id is not None:
            ctx["lease_id"] = lease_id
        if template_id is not None:
            ctx["template_id"] = template_id
        if node_id is not None:
            ctx["node_id"] = node_id
        if extra:
            ctx.update(extra)
        self._note_core(connect, key=(surface, action, reason, template_id, project_id), surface=surface,
                        action=action, reason=reason, actor=actor, actor_token_id=actor_token_id,
                        project_id=project_id, scope=None, target=primary_target(ctx), source_ip=source_ip,
                        params=ctx)


def _audit_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """Shape an operator_audit row for the dashboard panel: ISO timestamp, the surface/action/actor identity,
    the optional token/project/scope/target, the redacted params (already a JSON dict from jsonb), and the
    source IP. Pure, so the panel render is testable without a DB."""
    (aid, at, surface, action, actor, actor_token_id, project_id, scope, target,
     params, source_ip, outcome) = row
    return {
        "id": int(aid), "at": at.isoformat() if at is not None else None,
        "surface": surface, "action": action, "actor": actor,
        "actor_token_id": None if actor_token_id is None else int(actor_token_id),
        "project_id": project_id, "scope": scope, "target": target,
        "params": params if isinstance(params, dict) else {}, "source_ip": source_ip,
        "outcome": outcome,   # None = an applied write; 'denied' = a rejected auth attempt (0025)
    }


def query(
    conn: psycopg.Connection[Any], *, limit: int = 100, max_rows: int = _MAX_RECENT,
    surface: str | None = None, actor: str | None = None, action: str | None = None,
    actor_token_id: int | None = None, project_id: str | None = None, target: str | None = None,
    outcome: str | None = None, since: dt.datetime | None = None, until: dt.datetime | None = None,
    before_id: int | None = None,
) -> list[dict[str, Any]]:
    """Filtered, keyset-paged read of the append-only trail, NEWEST first (`ORDER BY id DESC`). Every filter is
    optional and AND-ed, and EVERY value is a BOUND parameter (never string-interpolated — no injection).
    `surface`/`actor`/`project_id`/`actor_token_id` match EXACTLY (they're enums/ids); `action`/`target` match
    by case-insensitive SUBSTRING (a human hunting `/api/cancel` or `lease 42`), with LIKE metacharacters
    escaped so the text is literal. `outcome` splits the views: 'denied' → only rejected auth attempts
    (`outcome = 'denied'`, 0025), 'refused' → only final admission refusals (`outcome = 'refused'`, 0033/PR-3),
    'applied' → only successful writes (`outcome IS NULL`, the pre-0025 rows), anything else → all. `since`/
    `until` bound `at`. `before_id` is the keyset cursor (`id < %s`) — because id is the
    monotonic insertion order, paging from the previous page's last id walks strictly older rows with NO
    dup/skip. `limit` is clamped to [1, max_rows]: the panel uses the default (_MAX_RECENT), an export raises
    max_rows to EXPORT_CAP. Operator-gated (rows carry source IPs + token_ids). Read-only; commits to end the
    read transaction."""
    where: list[str] = []
    params: list[Any] = []
    if surface:
        where.append("surface = %s")
        params.append(surface)
    if actor:
        where.append("actor = %s")
        params.append(actor)
    if action:
        where.append("action ILIKE %s")
        params.append(f"%{_like_escape(action)}%")
    if actor_token_id is not None:
        where.append("actor_token_id = %s")
        params.append(actor_token_id)
    if project_id:
        where.append("project_id = %s")
        params.append(project_id)
    if target:
        where.append("target ILIKE %s")
        params.append(f"%{_like_escape(target)}%")
    if outcome == "denied":
        where.append("outcome = %s")   # only rejected auth attempts (0025)
        params.append("denied")
    elif outcome == "refused":
        where.append("outcome = %s")   # only final admission refusals (0033/PR-3)
        params.append("refused")
    elif outcome == "applied":
        where.append("outcome IS NULL")   # only successful writes (no bound param — IS NULL, not = NULL)
    if since is not None:
        where.append("at >= %s")
        params.append(since)
    if until is not None:
        where.append("at <= %s")
        params.append(until)
    if before_id is not None:
        where.append("id < %s")
        params.append(before_id)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    lim = max(1, min(int(limit), int(max_rows)))
    sql = f"SELECT {_COLS} FROM operator_audit{clause} ORDER BY id DESC LIMIT %s"
    rows = conn.execute(sql, (*params, lim)).fetchall()
    conn.commit()
    return [_audit_row(r) for r in rows]


def list_recent(conn: psycopg.Connection[Any], limit: int = 100) -> list[dict[str, Any]]:
    """Recent audit rows, NEWEST first, clamped to [1, _MAX_RECENT] — the unfiltered convenience wrapper over
    `query`. The dashboard Audit panel calls `query` directly (with filters + a keyset cursor); this keeps a
    stable no-filter entry point for the harness + any simple caller. Read-only; commits to end its txn."""
    return query(conn, limit=limit)


def export_csv(rows: list[dict[str, Any]], *, truncated: bool = False) -> bytes:
    """Serialise audit rows (from `query`) as UTF-8 CSV for an operator download: a header row, then one row
    per entry with `params` re-serialised as compact JSON in its cell (the `csv` module quotes any embedded
    commas/quotes). When `truncated` (the query hit EXPORT_CAP), a leading `# …` comment line makes the file
    SELF-DECLARE the cap, so a CSV opened offline isn't silently partial ('no silent caps'; the GUI, an
    `X-Br0kr-Truncated` header and a server WARNING flag it too). Every non-`params` cell is passed through
    `_csv_safe` so an agent-planted formula cell can't execute on open (CWE-1236, review S13). Pure — testable
    without a DB."""
    buf = io.StringIO()
    if truncated:
        buf.write(f"# br0kr audit export truncated at {EXPORT_CAP} rows — narrow the filters for the full set\r\n")
    w = csv.writer(buf)
    w.writerow(_EXPORT_COLUMNS)
    for r in rows:
        w.writerow([json.dumps(r.get("params") or {}, separators=(",", ":")) if c == "params"
                    else _csv_safe(r.get(c))
                    for c in _EXPORT_COLUMNS])
    return buf.getvalue().encode("utf-8")


def export_json(rows: list[dict[str, Any]], *, truncated: bool = False) -> bytes:
    """Serialise audit rows as a JSON envelope for an operator download: `{truncated, count, audit:[…]}`. The
    `truncated` flag self-declares an EXPORT_CAP hit ('no silent caps'). Pure — testable without a DB."""
    return json.dumps({"truncated": truncated, "count": len(rows), "audit": rows},
                      separators=(",", ":")).encode("utf-8")


# The retention prune (0024) DELETEs in bounded batches of _PRUNE_BATCH rows, committing + heartbeating between
# batches (review S12): a FIRST enablement over a huge accumulated trail would otherwise be ONE unbounded DELETE
# holding a long lock + a large WAL burst that stalls the daemon's reconcile tick past its liveness stall check
# (the tick stops stamping `_beat` for the whole delete). Each batch is a small, immediately-durable delete that
# releases its lock and lets the caller's on_progress() (the daemon's `_beat`) keep the progress stamp fresh.
# Sized well above the reaper's per-guest batch (which paces SLOW PVE calls one guest at a time) because a prune
# batch is a pure in-DB row delete — a million-row trail is a few hundred quick batches, not millions of round-trips.
_PRUNE_BATCH = 5000

# The batch delete: `ctid = ANY(ARRAY(SELECT ctid … LIMIT N))`, NOT `ctid IN (SELECT …)` (review S12 MED). The
# `ARRAY(subquery)` is an UNCORRELATED sub-select — Postgres evaluates it ONCE as an InitPlan constant, so the
# outer DELETE is a TID SCAN that fetches exactly those N tuples by physical location. `ctid IN (subquery)`
# instead plans as a semi-join whose OUTER side SEQ-SCANS THE WHOLE HEAP once per batch (PG has no parameterized
# TID scan for that form) — which on a multi-million-row first enablement would be O(N²), far WORSE than the one
# unbounded DELETE we're replacing, and now health-masked because every batch heartbeats. The harness EXPLAINs
# this statement and asserts the Tid Scan, so a future rewrite that regresses the plan fails the gate.
_PRUNE_DELETE = (
    "DELETE FROM operator_audit WHERE ctid = ANY (ARRAY("
    "SELECT ctid FROM operator_audit WHERE at < now() - make_interval(days => %s) LIMIT %s))"
)


def prune(conn: psycopg.Connection[Any], older_than_days: int, *, batch: int = _PRUNE_BATCH,
          on_progress: Callable[[], None] | None = None) -> int:
    """The ONE sanctioned DELETE on the otherwise append-only trail (0024 retention): remove rows OLDER than
    `older_than_days` and return how many were deleted. OPT-IN — `older_than_days <= 0` is a NO-OP (returns 0,
    the default 'keep forever'), so a 0/unset knob never touches the table. BATCHED (review S12): each iteration
    deletes at most `batch` matching rows via a `ctid = ANY(ARRAY(SELECT ctid … LIMIT batch))` TID-scan delete
    (see `_PRUNE_DELETE`), COMMITS, then calls `on_progress` — so a FIRST-enablement prune of a huge trail is a
    series of small, immediately-durable deletes (each releasing its lock + bounding its WAL) with a heartbeat
    between them, instead of one unbounded DELETE that holds a long lock and starves the daemon's liveness
    `_beat`. Loops until a batch deletes FEWER than `batch` rows (the trail is exhausted); `batch` is clamped to
    >= 1 so a mis-set 0/negative can't spin. TERMINATION: rows are only ever deleted, and a row becomes eligible
    only `older_than_days` (>= 1 day) after it was inserted — so eligible rows 'age in' at (at most) the audit
    WRITE rate crossing a days-old boundary, orders of magnitude below a batch's delete rate; the loop drains far
    faster than it refills and always reaches a short batch. (A DELETE never blocks concurrent INSERTs, so writers
    aren't starved either.) Bound params + `make_interval`; the inner LIMIT scan uses the `at` index. Debits NO
    capacity scope, takes NO lock — maintenance beside admission. The caller (daemon) records a best-effort
    breadcrumb of the TOTAL of a COMPLETED prune; if a batch raises MID-loop (a ledger blip), the batches already
    committed stay deleted and the partial count is LOGGED here (never silent) before the error propagates to the
    caller's fail-safe, with the remainder pruned + breadcrumbed next window. This is the SOLE mutation of
    operator_audit — the hygiene test pins that no other module DELETEs/UPDATEs it."""
    if older_than_days <= 0:
        return 0
    n = max(1, int(batch))
    total = 0
    try:
        while True:
            cur = conn.execute(_PRUNE_DELETE, (older_than_days, n))
            conn.commit()
            deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            total += deleted
            if on_progress is not None:
                on_progress()   # heartbeat AFTER the batch commits — the daemon's _beat keeps liveness fresh
            if deleted < n:     # a short (or empty) batch means the matching set is exhausted
                break
    except Exception:
        # A mid-loop failure leaves the already-committed batches durably deleted. Surface the partial so the
        # deletion is never silent (the breadcrumb rides only a completed prune), then let the daemon fail-safe
        # swallow it; the remainder is pruned + breadcrumbed on the next retention window.
        if total:
            log.warning("audit prune interrupted after deleting %d row(s) — remainder retried next window", total)
        raise
    return total
