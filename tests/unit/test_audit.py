"""Unit tests for the audit trail (br0kr/audit.py) — the PURE half (redact / primary_target / row shaping)
and the BEST-EFFORT `record` contract, all without a database. The real SQL + append-only + both surfaces
writing exactly one row on success (and the pre-0023 swallow) are proven against real Postgres in
scripts/harness_audit.py.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from br0kr import audit

pytestmark = pytest.mark.unit


def test_redact_masks_secret_keys_keeps_ids_and_deep_copies():
    """redact masks any key whose name contains a secret-shaped substring (secret/password/ssh/pubkey/dsn),
    recursing into nested dicts, while KEEPING the useful ids/checksums (token_id/lease_id/sha256/idem_key).
    It NEVER mutates the caller's dict. Guards a secret (or bulky key material) landing in the audit table, or
    the id we DO want (token_id) being wrongly blanked (which the logging_setup denylist would do)."""
    src = {"token_id": 7, "lease_id": 42, "sha256": "abc", "idem_key": "k1",
           "token_secret_env": "PVE_TOKEN", "ssh_pubkey": "ssh-ed25519 AAAA...", "password": "hunter2",
           "nested": {"pgconn": "host=... password=x", "node_id": "n1"}}
    out = audit.redact(src)
    assert out["token_id"] == 7 and out["lease_id"] == 42 and out["sha256"] == "abc" and out["idem_key"] == "k1"
    assert out["token_secret_env"] == audit._REDACTED   # contains 'secret'
    assert out["ssh_pubkey"] == audit._REDACTED and out["password"] == audit._REDACTED
    assert out["nested"]["pgconn"] == audit._REDACTED and out["nested"]["node_id"] == "n1"   # recurses
    assert src["password"] == "hunter2" and src["ssh_pubkey"].startswith("ssh-")   # caller's dict untouched
    assert audit.redact("not a dict") == {} and audit.redact({}) == {}


def test_redact_never_stores_a_minted_secret():
    """The real leak risk: create_token's RESULT carries a plaintext `secret`. We audit PARAMS not results,
    but even a params dict that somehow held a 'secret' key is masked. Guards the AGENTS.md hard rule (never
    store/log a secret) AT the redaction boundary."""
    assert audit.redact({"secret": "PLAINTEXT-XYZ", "label": "ci"}) == {"secret": audit._REDACTED, "label": "ci"}


def test_primary_target_picks_the_first_present_id():
    """primary_target returns the first present of the known id keys (lease/node/project/…), stringified — a
    cheap who-touched-what handle — else None. Guards the target column being empty for a normal action."""
    assert audit.primary_target({"lease_id": 42, "node_id": "n1"}) == "42"   # lease_id has priority
    assert audit.primary_target({"node_id": "n1"}) == "n1"
    assert audit.primary_target({"cpu_limit": 4}) is None and audit.primary_target({}) is None
    assert audit.primary_target({"lease_id": ""}) is None   # a blank id is ignored


def test_audit_row_shapes_a_stored_row():
    """_audit_row shapes a DB row for the panel: ISO timestamp, int-coerced ids, the jsonb params passed
    through, a null-safe token_id, and the outcome (0025: None = applied write, 'denied' = rejected attempt).
    Guards a mis-shaped audit entry the dashboard renders."""
    row = (5, dt.datetime(2026, 7, 2, 14, 3, 0), "mcp", "release_lease", "agent", 7, "team-a", "consume",
           "42", {"lease_id": 42}, None, None)
    assert audit._audit_row(row) == {
        "id": 5, "at": "2026-07-02T14:03:00", "surface": "mcp", "action": "release_lease", "actor": "agent",
        "actor_token_id": 7, "project_id": "team-a", "scope": "consume", "target": "42",
        "params": {"lease_id": 42}, "source_ip": None, "outcome": None}
    dash = audit._audit_row((6, dt.datetime(2026, 7, 2, 14, 4), "dashboard", "/api/cancel",
                             "dashboard-operator", None, None, None, "7", {"lease_id": 7}, "192.168.1.20",
                             None))
    assert dash["actor_token_id"] is None and dash["source_ip"] == "192.168.1.20" and dash["outcome"] is None
    # a denial row (0025): actor 'anonymous', outcome 'denied', the reason + coalesced count in params.
    den = audit._audit_row((7, dt.datetime(2026, 7, 2, 14, 5), "mcp", "request_lease", "anonymous",
                            None, None, None, None, {"reason": "invalid_token", "attempts": 3}, "10.0.0.9",
                            "denied"))
    assert den["outcome"] == "denied" and den["actor"] == "anonymous" and den["source_ip"] == "10.0.0.9"
    assert den["params"] == {"reason": "invalid_token", "attempts": 3}


class _RecConn:
    """A fake conn recording (sql, params); execute returns self (or raises), commit/rollback count — to pin
    record()'s best-effort contract without a DB."""

    def __init__(self, raise_on_execute: bool = False) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.commits = 0
        self.rolledback = 0
        self._raise = raise_on_execute

    def execute(self, sql: str, params: Any = None) -> _RecConn:
        self.calls.append((sql, params))
        if self._raise:
            raise RuntimeError('relation "operator_audit" does not exist')
        return self

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rolledback += 1


def test_record_inserts_a_redacted_row_and_commits():
    """record inserts ONE row with the identity columns + the REDACTED params (a psycopg Json wrapper whose
    .obj is the masked dict), then commits its own row. Guards the wrong column order, or a secret param
    reaching the INSERT unredacted."""
    conn = _RecConn()
    audit.record(conn, surface="mcp", action="request_lease", actor="agent", actor_token_id=7,
                 project_id="team-a", scope="consume", target="42",
                 params={"node_id": "n1", "token_secret_env": "PVE"})
    assert len(conn.calls) == 1 and "INSERT INTO operator_audit" in conn.calls[0][0]
    p = conn.calls[0][1]
    assert p[:7] == ("mcp", "request_lease", "agent", 7, "team-a", "consume", "42")
    assert p[7].obj == {"node_id": "n1", "token_secret_env": audit._REDACTED}   # redacted before storage
    assert p[9] is None   # outcome NULL = an applied write (0025); denials write 'denied' via DenialLog
    assert conn.commits == 1


def test_record_is_best_effort_and_never_raises():
    """If the INSERT raises (a not-yet-0023 ledger, a blip), record SWALLOWS it, rolls back the aborted txn,
    and returns None — it NEVER propagates, so auditing can't break the caller's already-committed action.
    THE load-bearing safety property: a pre-migration deploy must not start failing every write."""
    conn = _RecConn(raise_on_execute=True)
    audit.record(conn, surface="dashboard", action="/api/cancel", actor="dashboard-operator")   # must not raise
    assert conn.rolledback == 1 and conn.commits == 0


class _RowsConn:
    def __init__(self, rows: list) -> None:
        self._rows, self.limit = rows, None

    def execute(self, sql: str, params: Any = None) -> _RowsConn:
        self.limit = params[0]
        return self

    def fetchall(self) -> list:
        return self._rows

    def commit(self) -> None:
        pass


def test_list_recent_clamps_the_limit():
    """list_recent clamps `limit` to [1, _MAX_RECENT] — a hostile/huge/negative limit can't pull the whole
    append-only table or error the read. Guards the clamp regressing. list_recent now delegates to query with
    no filters, so the LAST bound parameter is the clamped limit."""
    c = _RowsConn([])
    audit.list_recent(c, limit=99999)
    assert c.limit == audit._MAX_RECENT
    audit.list_recent(c, limit=-5)
    assert c.limit == 1
    audit.list_recent(c, limit=50)
    assert c.limit == 50


def test_like_escape_neutralizes_wildcards():
    """_like_escape escapes LIKE metacharacters so an operator's literal search text can't act as a wildcard:
    `%` → `\\%`, `_` → `\\_`, `\\` → `\\\\` (PG's default LIKE escape is backslash). Guards a search for
    `cancel_lease` matching `cancelXlease`, or `100%` matching everything."""
    assert audit._like_escape("100%") == "100\\%"
    assert audit._like_escape("a_b") == "a\\_b"
    assert audit._like_escape("x\\y") == "x\\\\y"
    assert audit._like_escape("plain") == "plain"


class _QueryConn:
    """Capture the (sql, params) audit.query builds + return canned rows — to prove the WHERE-builder, the
    escaped ILIKE, the clamp, and the ORDER BY without a database."""

    def __init__(self, rows: list | None = None) -> None:
        self.sql: str = ""
        self.params: tuple = ()
        self._rows = rows or []

    def execute(self, sql: str, params: Any = None) -> _QueryConn:
        self.sql, self.params = sql, params
        return self

    def fetchall(self) -> list:
        return self._rows

    def commit(self) -> None:
        pass


def test_query_no_filters_orders_by_id_desc_and_clamps():
    """With no filters, query emits no WHERE, orders by id DESC (the keyset key), and the ONLY bound param is
    the clamped limit. Guards the unfiltered/`list_recent` path regressing and the clamp being bypassed."""
    c = _QueryConn()
    audit.query(c, limit=99999)
    assert "WHERE" not in c.sql and c.sql.rstrip().endswith("ORDER BY id DESC LIMIT %s")
    assert c.params == (audit._MAX_RECENT,)
    audit.query(c, limit=25)
    assert c.params == (25,)
    audit.query(c, limit=9, max_rows=audit.EXPORT_CAP)   # export raises the ceiling
    assert c.params == (9,)


def test_list_recent_delegates_to_query_with_no_filter():
    """list_recent is the STABLE unfiltered wrapper over query — it must emit NO WHERE clause. A default filter
    creeping into query() (e.g. a defensive surface=… default) would silently narrow every simple caller + the
    harness; the clamp test alone (which only inspects the limit param) can't see that. Proven by the built SQL:
    no WHERE, ORDER BY id DESC, and the ONLY bound param is the clamped limit. Closes the CI gap where this
    contract was only exercised by the real-PG harness (check_round_trip), not the unit suite."""
    c = _QueryConn()
    audit.list_recent(c, limit=50)
    assert "WHERE" not in c.sql and c.sql.rstrip().endswith("ORDER BY id DESC LIMIT %s")
    assert c.params == (50,)


def test_query_builds_a_parameterized_and_where_with_escaped_ilike():
    """query AND-s every supplied filter as a BOUND clause in a stable order, with the clamped limit LAST;
    action/target use ILIKE with the search text escaped + %-wrapped (substring), the rest match exactly, and
    before_id is the `id < %s` keyset cursor. Guards a filter being dropped, mis-ordered, interpolated (an
    injection), or the wildcard search leaking a metacharacter."""
    c = _QueryConn()
    since, until = dt.datetime(2026, 7, 1), dt.datetime(2026, 7, 2, 23, 59, 59)
    audit.query(c, limit=50, surface="mcp", actor="agent", action="can%cel", actor_token_id=7,
                project_id="team-a", target="4_2", since=since, until=until, before_id=99)
    for clause in ("surface = %s", "actor = %s", "action ILIKE %s", "actor_token_id = %s", "project_id = %s",
                   "target ILIKE %s", "at >= %s", "at <= %s", "id < %s"):
        assert clause in c.sql, clause
    assert c.sql.count("%s") == 10   # 9 filters + the limit
    assert c.params == ("mcp", "agent", "%can\\%cel%", 7, "team-a", "%4\\_2%", since, until, 99, 50)


def test_export_csv_has_a_header_json_params_and_flags_truncation():
    """export_csv writes a header row + one row per entry with `params` re-serialised as compact JSON in its
    cell (the csv module quotes the embedded quotes/commas, so it round-trips through csv.reader). A truncated
    export prepends a self-declaring `# …` comment naming EXPORT_CAP. Guards a mis-shaped download or a silent
    cap."""
    rows = [{"id": 2, "at": "2026-07-02T14:03:00", "surface": "mcp", "action": "release_lease",
             "actor": "agent", "actor_token_id": 7, "project_id": "team-a", "scope": "consume",
             "target": "42", "params": {"lease_id": 42}, "source_ip": None}]
    out = audit.export_csv(rows).decode("utf-8")
    assert not out.startswith("#")   # no truncation banner when the export is complete
    parsed = list(csv.reader(io.StringIO(out)))
    assert parsed[0] == list(audit._EXPORT_COLUMNS)
    assert parsed[1][9] == '{"lease_id":42}'   # the params column, un-escaped by the reader
    trunc = audit.export_csv(rows, truncated=True).decode("utf-8")
    banner = trunc.splitlines()[0]
    assert banner.startswith("# br0kr audit export truncated at") and str(audit.EXPORT_CAP) in banner


def test_export_csv_neutralizes_a_formula_injection_cell():
    """A provision-tenant supplies `target_template_id`, which becomes the audit `target` cell (validated only
    non-empty). export_csv prefix-quotes any string cell starting with a spreadsheet formula trigger
    (=/+/-/@/TAB/CR) so it can't EXECUTE when an operator opens the CSV (CWE-1236, review S13); a non-string
    column (the int `id`) is untouched, so no spurious quote appears. Guards a stored formula running as a macro
    on export."""
    rows = [{"id": 9, "at": "t", "surface": "mcp", "action": "request_template_download", "actor": "agent",
             "actor_token_id": 3, "project_id": "team-a", "scope": "provision", "target": "=1+1",
             "params": {"target_template_id": "=1+1"}, "source_ip": None, "outcome": None}]
    parsed = list(csv.reader(io.StringIO(audit.export_csv(rows).decode("utf-8"))))
    assert parsed[1][audit._EXPORT_COLUMNS.index("target")] == "'=1+1"     # prefix-quoted → inert text
    assert parsed[1][audit._EXPORT_COLUMNS.index("id")] == "9"             # int column untouched
    for trig in ("=", "+", "-", "@", "\t", "\r", "\n"):
        assert audit._csv_safe(trig + "x") == "'" + trig + "x"            # every trigger char (incl. LF) neutralized
    assert audit._csv_safe("normal") == "normal" and audit._csv_safe("") == "" and audit._csv_safe(7) == 7
    # A formula whose args carry a comma + quotes still round-trips inert: _csv_safe prefixes BEFORE csv
    # quoting, so the reader returns the apostrophe-led text (Excel strips the CSV quotes, then reads text).
    evil = '=HYPERLINK("http://evil","x")'
    rows2 = [{**rows[0], "target": evil}]
    got = list(csv.reader(io.StringIO(audit.export_csv(rows2).decode("utf-8"))))[1]
    assert got[audit._EXPORT_COLUMNS.index("target")] == "'" + evil


def test_redact_canonicalizes_a_signed_url_dropping_its_credential_query():
    """A URL-named param's value is reduced to scheme://host/path on storage, DROPPING the query where a
    presigned credential lives (X-Amz-Signature / a bare sig) — the key-NAME denylist can't see it because the
    whole URL sits under one benign key like `url` (review S14). Non-URL params are untouched and the host/path
    identity is kept (the audit still says WHICH image). Guards a time-limited signed-URL credential landing in
    operator_audit.params."""
    src = {"url": "https://s3.example.com/imgs/deb12.qcow2?X-Amz-Signature=DEADBEEFsecret&X-Amz-Expires=900",
           "download_url": "https://user:pw@host/a/b?sig=zzz", "sha256": "abc", "target_template_id": "deb12"}
    out = audit.redact(src)
    assert out["url"] == "https://s3.example.com/imgs/deb12.qcow2"          # signed query dropped
    assert out["download_url"] == "https://host/a/b"                        # userinfo + sig query both dropped
    blob = json.dumps(out)
    assert "Signature" not in blob and "DEADBEEF" not in blob and "sig=" not in blob and "pw@" not in blob
    assert out["sha256"] == "abc" and out["target_template_id"] == "deb12"  # non-url params untouched
    # A LIST value under a url-named key is canonicalized element-wise (redact's future-proof contract, L1).
    lst = audit.redact({"urls": ["https://h/a?X-Amz-Signature=Q", "https://h/b?sig=z", 7]})
    assert lst["urls"] == ["https://h/a", "https://h/b", 7]


def test_export_json_envelopes_rows_with_a_truncation_flag():
    """export_json wraps the rows in {truncated, count, audit:[…]} — the truncated flag self-declares an
    EXPORT_CAP hit. Guards the envelope shape or a silent cap the operator can't see."""
    rows = [{"id": 1, "action": "x"}]
    assert json.loads(audit.export_json(rows).decode("utf-8")) == {"truncated": False, "count": 1, "audit": rows}
    d = json.loads(audit.export_json(rows, truncated=True).decode("utf-8"))
    assert d["truncated"] is True and d["count"] == 1


class _PruneConn:
    """A fake conn for prune: scripts a SEQUENCE of per-batch DELETE rowcounts, captures each batch's (sql,
    params) + counts commits — to prove prune's BATCHED loop shape, per-batch commit, and heartbeat WITHOUT a
    DB. A single int behaves like a one-batch prune (the loop ends after one short batch). When the script runs
    out, further batches return 0 (an empty trailing batch), so the loop can't over-run a scripted sequence."""

    def __init__(self, rowcount: int | list[int] = 3, events: list[tuple[str, Any]] | None = None) -> None:
        self._rcs = [rowcount] if isinstance(rowcount, int) else list(rowcount)
        self._i = 0
        self.sql: str = ""
        self.params: Any = None
        self.calls: list[tuple[str, Any]] = []
        self.commits = 0
        self._events = events   # optional shared ordering log ("exec"/"commit"); the beat lambda appends "beat"

    def execute(self, sql: str, params: Any = None) -> SimpleNamespace:
        self.sql, self.params = sql, params
        self.calls.append((sql, params))
        if self._events is not None:
            self._events.append(("exec", params))
        rc = self._rcs[self._i] if self._i < len(self._rcs) else 0
        self._i += 1
        return SimpleNamespace(rowcount=rc)   # psycopg cursor exposes rowcount after a DELETE

    def commit(self) -> None:
        self.commits += 1
        if self._events is not None:
            self._events.append(("commit",))


def test_prune_is_a_noop_when_disabled():
    """prune with older_than_days <= 0 is a NO-OP (retention OFF / keep-forever) — it issues NO DELETE and
    returns 0. Guards a 0/unset knob (the default) ever touching the append-only trail."""
    c = _PruneConn()
    assert audit.prune(c, 0) == 0 and audit.prune(c, -5) == 0
    assert c.sql == "" and c.commits == 0 and c.calls == []   # never executed / committed a delete


def test_prune_deletes_older_rows_in_one_bounded_batch_and_returns_the_count():
    """A trail smaller than one batch is pruned in a SINGLE bounded DELETE (rowcount < batch → the loop ends):
    the delete is the `ctid IN (SELECT ctid … LIMIT %s)` batch form (make_interval + bound params, never an
    UNBOUNDED delete), it commits once, and returns the cursor rowcount. Guards the sanctioned delete being
    mis-shaped or losing its LIMIT."""
    c = _PruneConn(rowcount=4)
    assert audit.prune(c, 90) == 4 and c.commits == 1 and len(c.calls) == 1
    # the TID-scan batch form: `ctid = ANY(ARRAY(SELECT ctid … LIMIT %s))`, NOT `ctid IN (subquery)` (which would
    # seq-scan the whole heap per batch — review S12 MED).
    assert "DELETE FROM operator_audit" in c.sql and "ctid = ANY" in c.sql and "ARRAY(" in c.sql
    assert "LIMIT %s" in c.sql and "make_interval(days => %s)" in c.sql and c.params == (90, audit._PRUNE_BATCH)


def test_prune_batches_a_large_trail_with_a_heartbeat_after_each_commit():
    """A trail LARGER than one batch is deleted in bounded batches: prune loops issuing a `ctid = ANY(ARRAY(…
    LIMIT batch))` DELETE, COMMIT, then on_progress() — IN THAT ORDER — per batch until a batch returns FEWER
    than `batch` rows (trail exhausted), returning the TOTAL. The exact (exec, commit, beat) event order matters:
    a heartbeat must stamp liveness only for DURABLE progress, never before the batch commits. Guards the S12
    regression — one unbounded DELETE of a huge first-enablement trail that holds a long lock and starves the
    daemon's liveness heartbeat — AND a beat-before-commit reordering."""
    events: list[tuple[str, Any]] = []
    c = _PruneConn(rowcount=[3, 3, 1], events=events)   # batch=3 → two full batches then a short (1 < 3) final
    total = audit.prune(c, 30, batch=3, on_progress=lambda: events.append(("beat",)))
    assert total == 7                          # 3 + 3 + 1
    assert len(c.calls) == 3 and c.commits == 3   # one bounded DELETE + one COMMIT per batch
    # strict per-batch ordering, three times: execute the bounded delete → commit it → THEN heartbeat.
    assert events == [("exec", (30, 3)), ("commit",), ("beat",)] * 3
    for sql, params in c.calls:                # every batch is the TID-scan form with the batch size bound
        assert "ctid = ANY" in sql and "ARRAY(" in sql and "LIMIT %s" in sql and params == (30, 3)


def test_prune_clamps_a_nonpositive_batch_so_it_cannot_spin():
    """A batch <= 0 is clamped to 1 (never a `LIMIT 0` that deletes nothing yet never satisfies `deleted <
    batch`, i.e. an infinite loop). Guards a mis-set internal batch knob turning the bounded prune into a hang."""
    c = _PruneConn(rowcount=[1, 0])   # clamped batch=1: one row, then an empty batch (0 < 1) ends it
    audit.prune(c, 30, batch=0)
    assert c.calls and c.calls[0][1] == (30, 1)   # the LIMIT bound param was clamped up to 1


def test_prune_normalizes_an_unknown_rowcount_to_zero():
    """A DELETE cursor rowcount of -1 (a driver's 'unknown') is normalized to 0 — prune never reports a bogus
    negative deletion count into the breadcrumb, and -1 < batch ends the loop (no spin). Guards a weird rowcount
    surfacing as deleted:-1 or as an over-run."""
    assert audit.prune(_PruneConn(rowcount=-1), 90) == 0


def test_operator_audit_is_append_only_except_the_sanctioned_prune():
    """operator_audit is append-only — INSERT + SELECT only — with EXACTLY ONE sanctioned mutation: the 0024
    retention DELETE in audit.prune. A conservative defense-in-depth HEURISTIC against an ACCIDENTAL regression:
    scan every br0kr/*.py for a SQL-shaped DELETE/UPDATE of operator_audit (requiring the FROM/SET keyword, and
    tolerating an inline /* … */ comment between tokens so a `DELETE /* x */ FROM operator_audit` can't slip by)
    and assert ONLY audit.py has one. It replaces the manual grep SECURITY.md named. NOT proof against
    deliberate evasion — a regex can't stop dynamically-built SQL; the real control is the single-seam design +
    review. This catches the realistic slip: a plain `conn.execute("DELETE FROM operator_audit …")` reaching
    another module. (A non-audit module writing that exact SQL phrase even in prose IS flagged, by design — a
    second look is warranted; requiring FROM/SET keeps ordinary references like 'the operator_audit table'
    clear.)"""
    pkg = Path(audit.__file__).parent
    cmt = r"(?:/\*.*?\*/\s*)?"   # an optional inline SQL block comment between tokens
    pat = re.compile(rf"DELETE\s+{cmt}FROM\s+{cmt}operator_audit|UPDATE\s+{cmt}operator_audit\s+{cmt}SET",
                     re.IGNORECASE | re.DOTALL)
    offenders = [py.name for py in sorted(pkg.glob("*.py"))
                 if py.name != "audit.py" and pat.search(py.read_text(encoding="utf-8"))]
    assert offenders == [], f"non-audit module mutates operator_audit (append-only): {offenders}"


# ── Denial (401/403) logging — the DenialLog coalescing throttle + the outcome filter (0025) ──────────
def test_query_outcome_filter_splits_denied_refused_and_applied():
    """query's `outcome` filter splits the views: 'denied' → a bound `outcome = %s` = 'denied' (rejected auth,
    0025), 'refused' → a bound `outcome = %s` = 'refused' (final admission refusals, 0033/PR-3), 'applied' →
    `outcome IS NULL` with NO bound param (successful writes, the pre-0025 rows), any other value → NO outcome
    clause (all). Guards the intrusion/refusal views leaking successes (or vice-versa), the refused view
    aliasing denied, and 'applied' wrongly binding a `= NULL` (which matches nothing)."""
    c = _QueryConn()
    audit.query(c, limit=50, outcome="denied")
    assert "outcome = %s" in c.sql and "denied" in c.params and "refused" not in c.params
    cr = _QueryConn()
    audit.query(cr, limit=50, outcome="refused")
    assert "outcome = %s" in cr.sql and "refused" in cr.params and "denied" not in cr.params
    c2 = _QueryConn()
    audit.query(c2, limit=50, outcome="applied")
    assert "outcome IS NULL" in c2.sql and "applied" not in c2.params   # IS NULL is not a bound param
    c3 = _QueryConn()
    audit.query(c3, limit=50, outcome="bogus")
    # 'outcome' is always in the SELECT column list; what must be ABSENT is an outcome WHERE clause.
    assert "outcome = %s" not in c3.sql and "outcome IS NULL" not in c3.sql   # unknown value → no filter


class _Clock:
    """A deterministic monotonic clock for DenialLog tests: advance() steps it; the throttle reads it via
    __call__. Injected so the coalescing window is testable without real time."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, dt_s: float) -> None:
        self.t += dt_s


class _DenialConn:
    """A fake conn for DenialLog.note: captures each INSERT (sql, params), counts commits/closes. `execute`
    can be made to raise to exercise the swallow-all path."""

    def __init__(self, raise_on_execute: bool = False) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.commits = 0
        self.closed = False
        self._raise = raise_on_execute

    def execute(self, sql: str, params: Any = None) -> _DenialConn:
        self.calls.append((sql, params))
        if self._raise:
            raise RuntimeError("boom")
        return self

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True


def test_denial_log_writes_first_denial_then_coalesces_within_the_window():
    """The FIRST denial for a key writes ONE row (outcome='denied', actor 'anonymous', target NULL, the reason
    + attempts=1 in redacted params, the source IP); further denials of the SAME key within the window are
    coalesced (no new row); crossing the window writes again, folding the SUPPRESSED count into `attempts`
    (this + the 2 suppressed = 3). Guards the coalescing miscounting or a per-request row flood."""
    clk = _Clock()
    conn = _DenialConn()
    dl = audit.DenialLog(window_s=60, now=clk)
    def connect() -> _DenialConn:
        return conn
    dl.note(connect, surface="mcp", action="request_lease", reason="invalid_token", source_ip="10.0.0.1")
    assert len(conn.calls) == 1 and conn.commits == 1 and conn.closed is True
    p = conn.calls[0][1]
    # tuple order: surface, action, actor, token_id, project, scope, target, Json(params), source_ip, outcome
    assert p[0] == "mcp" and p[1] == "request_lease" and p[2] == "anonymous" and p[3] is None
    assert p[6] is None and p[8] == "10.0.0.1" and p[9] == "denied"
    assert p[7].obj == {"reason": "invalid_token", "attempts": 1}
    clk.advance(10)
    dl.note(connect, surface="mcp", action="request_lease", reason="invalid_token", source_ip="10.0.0.1")
    clk.advance(10)
    dl.note(connect, surface="mcp", action="request_lease", reason="invalid_token", source_ip="10.0.0.1")
    assert len(conn.calls) == 1   # still one row — the two within-window denials were coalesced
    clk.advance(45)   # now 65s since the first write → past the 60s window
    dl.note(connect, surface="mcp", action="request_lease", reason="invalid_token", source_ip="10.0.0.1")
    assert len(conn.calls) == 2
    assert conn.calls[1][1][7].obj == {"reason": "invalid_token", "attempts": 3}   # this + 2 suppressed


def test_denial_log_coalesces_per_key_not_globally():
    """Coalescing is per (surface, source_ip, action, reason): a different IP, action, OR reason is a DISTINCT
    key that writes its own first-denial row. Guards one attacker's flood masking a different attack, or a
    single key swallowing unrelated denials."""
    clk = _Clock()
    conn = _DenialConn()
    dl = audit.DenialLog(window_s=60, now=clk)
    def connect() -> _DenialConn:
        return conn
    dl.note(connect, surface="mcp", action="a", reason="invalid_token", source_ip="1.1.1.1")
    dl.note(connect, surface="mcp", action="a", reason="invalid_token", source_ip="2.2.2.2")   # diff ip
    dl.note(connect, surface="mcp", action="b", reason="invalid_token", source_ip="1.1.1.1")   # diff action
    dl.note(connect, surface="mcp", action="a", reason="no_token", source_ip="1.1.1.1")        # diff reason
    assert len(conn.calls) == 4   # four distinct keys, four first-denial rows


def test_denial_log_attributes_a_forbidden_scope_token():
    """A forbidden_scope denial is ATTRIBUTED (a valid token over-reaching is the interesting signal): actor
    'agent' + the token_id/project/scope, still outcome='denied' with the reason/attempts in params. Guards a
    known over-reaching credential being logged as anonymous."""
    clk = _Clock()
    conn = _DenialConn()
    dl = audit.DenialLog(now=clk)
    dl.note(lambda: conn, surface="mcp", action="node_cordon", reason="forbidden_scope", source_ip="10.0.0.5",
            actor="agent", actor_token_id=42, project_id="team-a", scope="consume")
    p = conn.calls[0][1]
    assert p[2] == "agent" and p[3] == 42 and p[4] == "team-a" and p[5] == "consume" and p[9] == "denied"
    assert p[7].obj == {"reason": "forbidden_scope", "attempts": 1}


# ── note_refusal: the PLACEMENT/admission-refusal channel (outcome='refused', 0033/PR-3) ──────────────
def test_note_refusal_writes_a_refused_row_with_placement_context():
    """note_refusal writes ONE outcome='refused' row (on an outcome='refused' instance) carrying the placement
    context: actor 'placement', target = the primary id (lease over template), and params with reason +
    lease/template/node ids + attempts. Guards a refusal landing as the wrong outcome or losing the context an
    operator needs to see WHAT was refused."""
    conn = _DenialConn()
    dl = audit.DenialLog(window_s=60, now=_Clock(), outcome="refused")
    dl.note_refusal(lambda: conn, surface="daemon", action="placement_refused",
                    reason="cross_provision_refused", template_id="debian12", node_id="pve2", lease_id=42)
    assert len(conn.calls) == 1
    p = conn.calls[0][1]
    # tuple: surface, action, actor, token_id, project, scope, target, Json(params), source_ip, outcome
    assert p[0] == "daemon" and p[1] == "placement_refused" and p[2] == "placement" and p[9] == "refused"
    assert p[6] == "42"   # primary_target: lease_id wins over node/template
    assert p[7].obj == {"reason": "cross_provision_refused", "lease_id": 42,
                        "template_id": "debian12", "node_id": "pve2", "attempts": 1}


def test_note_refusal_coalesces_per_template_and_reason_not_globally():
    """The refusal key is (surface, action, reason, template_id, project_id): the SAME (reason, template) [same
    project None here] coalesces to one row/window (folding the count into attempts), while a different reason
    OR a different template is a DISTINCT key that writes its own row. Guards one template's flood masking
    another's, and a per-refusal row
    flood from a hammering agent."""
    clk = _Clock()
    conn = _DenialConn()
    dl = audit.DenialLog(window_s=60, now=clk, outcome="refused")
    def connect() -> _DenialConn:
        return conn
    dl.note_refusal(connect, surface="mcp", action="lease_refused", reason="no_reachable_instance", template_id="t1")
    dl.note_refusal(connect, surface="mcp", action="lease_refused", reason="no_reachable_instance", template_id="t1")
    assert len(conn.calls) == 1   # coalesced (same reason+template within window)
    dl.note_refusal(connect, surface="mcp", action="lease_refused", reason="no_reachable_instance", template_id="t2")
    dl.note_refusal(connect, surface="mcp", action="lease_refused", reason="no_capacity", template_id="t1")
    assert len(conn.calls) == 3   # +1 for a different template, +1 for a different reason
    clk.advance(65)
    dl.note_refusal(connect, surface="mcp", action="lease_refused", reason="no_reachable_instance", template_id="t1")
    assert conn.calls[3][1][7].obj["attempts"] == 2   # this + the 1 coalesced (t1,nri) in the first window


def test_note_refusal_keys_include_project_so_two_tenants_dont_coalesce():
    """The refusal key includes project_id, so two DIFFERENT tenants refused for the same (reason, template)
    write SEPARATE rows each carrying its OWN identity — never one coalesced row that mis-attributes one
    tenant's refusal/count to the other (Fable review). Guards the security-trail attribution across tenants,
    and (with _note_core using the caller's OWN params) a row whose identity and params come from one caller."""
    conn = _DenialConn()
    dl = audit.DenialLog(window_s=60, now=_Clock(), outcome="refused")

    def connect() -> _DenialConn:
        return conn

    dl.note_refusal(connect, surface="mcp", action="lease_refused", reason="no_capacity", template_id=None,
                    project_id="team-a", actor_token_id=1)
    dl.note_refusal(connect, surface="mcp", action="lease_refused", reason="no_capacity", template_id=None,
                    project_id="team-b", actor_token_id=2)
    assert len(conn.calls) == 2   # distinct project → distinct key → two rows, no cross-tenant coalescing
    # tuple: surface, action, actor, actor_token_id, project_id, scope, target, params, source_ip, outcome
    assert conn.calls[0][1][4] == "team-a" and conn.calls[0][1][3] == 1
    assert conn.calls[1][1][4] == "team-b" and conn.calls[1][1][3] == 2


def test_refused_instance_ceiling_is_independent_of_the_auth_denied_ceiling():
    """The placement-refusal DenialLog carries its OWN outcome + ceiling (constructed with outcome='refused',
    global_max=_REFUSAL_GLOBAL_MAX=100), leaving the auth _DENIAL_GLOBAL_MAX=200 untouched — the two channels
    can't starve each other's budget. Guards a shared ceiling letting a refusal flood suppress auth-denial
    intrusion signals (or vice-versa)."""
    assert audit._REFUSAL_GLOBAL_MAX == 100 and audit._DENIAL_GLOBAL_MAX == 200
    conn = _DenialConn()
    dl = audit.DenialLog(window_s=60, global_max=1, now=_Clock(), outcome="refused")
    dl.note_refusal(lambda: conn, surface="mcp", action="lease_refused", reason="no_capacity", template_id="t1")
    dl.note_refusal(lambda: conn, surface="mcp", action="lease_refused", reason="no_capacity", template_id="t2")
    assert len(conn.calls) == 1   # 2nd distinct key over the (test) global_max=1 → dropped, own budget


def test_denial_log_global_ceiling_caps_writes_and_preserves_the_count():
    """A process-wide per-window ceiling bounds total denial ROWS even under a rotating-IP flood (each spoofed
    IP is a fresh key = a fresh first-denial write that per-key coalescing can't stop). Over-ceiling denials
    are DROPPED (not written) but keep accruing their key's count, so the next window's write reports the true
    total. Guards a distributed flood ballooning the append-only table, and a dropped denial losing its count."""
    clk = _Clock()
    conn = _DenialConn()
    dl = audit.DenialLog(window_s=60, global_max=2, now=clk)
    def connect() -> _DenialConn:
        return conn
    dl.note(connect, surface="mcp", action="a", reason="invalid_token", source_ip="1")
    dl.note(connect, surface="mcp", action="a", reason="invalid_token", source_ip="2")
    dl.note(connect, surface="mcp", action="a", reason="invalid_token", source_ip="3")   # over the ceiling
    assert len(conn.calls) == 2   # only two rows this window despite three distinct keys
    clk.advance(61)   # new window resets the ceiling
    dl.note(connect, surface="mcp", action="a", reason="invalid_token", source_ip="3")
    assert len(conn.calls) == 3
    assert conn.calls[2][1][7].obj["attempts"] == 2   # the earlier dropped denial + this = 2


def test_denial_log_bounds_memory_via_lru_eviction():
    """Tracked keys are capped at max_keys with least-recently-seen eviction, so a rotating-IP flood can't grow
    the in-memory map without bound. Guards a memory-exhaustion vector on the throttle itself."""
    clk = _Clock()
    conn = _DenialConn()
    dl = audit.DenialLog(window_s=60, max_keys=2, now=clk)
    def connect() -> _DenialConn:
        return conn
    dl.note(connect, surface="mcp", action="a", reason="no_token", source_ip="1")
    dl.note(connect, surface="mcp", action="a", reason="no_token", source_ip="2")
    dl.note(connect, surface="mcp", action="a", reason="no_token", source_ip="3")   # evicts the oldest (ip=1)
    assert len(dl._keys) == 2   # never more than max_keys tracked


def test_denial_log_note_never_raises_and_swallows_a_bad_write():
    """note is BEST-EFFORT: a raising CONNECT (a down ledger) and a raising EXECUTE are both swallowed — a
    denial write can NEVER turn a clean 401/403 into a 500. Guards the reject path being broken by auditing."""
    clk = _Clock()
    dl = audit.DenialLog(now=clk)
    def bad_connect() -> _DenialConn:
        raise RuntimeError("ledger down")
    dl.note(bad_connect, surface="mcp", action="a", reason="no_token")   # connect raises → swallowed
    conn = _DenialConn(raise_on_execute=True)
    dl.note(lambda: conn, surface="mcp", action="b", reason="no_token")   # execute raises → swallowed
    assert conn.commits == 0   # a failed insert is never reported as committed


def test_denial_log_failed_write_preserves_count_and_refunds_the_window():
    """A FAILED admitted write (a ledger blip) must lose NOTHING (the review's MED finding): the reserve is
    ROLLED BACK — the coalesced count is preserved (the next successful write reports it, no under-count) and
    the per-window ceiling is NOT charged for the phantom row (so a PARTIAL outage can't fill the ceiling with
    never-written rows and start dropping OTHER keys' real denials). Guards the optimistic-mutation regression:
    _admit used to zero the count + advance last_write + bump _window_writes BEFORE the DB touch, so a failed
    write silently under-counted and mischarged the ceiling."""
    clk = _Clock()
    good = _DenialConn()
    bad = _DenialConn(raise_on_execute=True)
    dl = audit.DenialLog(window_s=60, global_max=2, now=clk)
    # key K's first denial FAILS to write — the reserve must roll back (count kept, window charge refunded).
    dl.note(lambda: bad, surface="mcp", action="a", reason="no_token", source_ip="K")
    assert bad.calls and good.calls == []   # it ATTEMPTED the write; nothing persisted
    # two OTHER keys each write within the SAME window: global_max=2. If the failed write had charged the
    # ceiling (the bug), _window_writes would be 1 and only ONE of these would get through.
    dl.note(lambda: good, surface="mcp", action="a", reason="no_token", source_ip="X")
    dl.note(lambda: good, surface="mcp", action="a", reason="no_token", source_ip="Y")
    assert len(good.calls) == 2   # both wrote → the failed write did NOT consume a ceiling slot
    # K's last_write was never advanced (the write failed) → still due; a new window resets the ceiling. K now
    # writes and reports attempts=2 (the earlier FAILED denial + this) → the count was PRESERVED, no under-count.
    clk.advance(61)
    dl.note(lambda: good, surface="mcp", action="a", reason="no_token", source_ip="K")
    assert len(good.calls) == 3 and good.calls[2][1][7].obj["attempts"] == 2


def test_denial_log_preserves_a_denial_that_coalesces_in_during_an_in_flight_write():
    """The reserve/finalize seam's raison d'être (a pass-2 coverage gap): a denial that arrives for a key WHILE
    that key's row is in flight must COALESCE into `count` (not race a 2nd write) and be reported by the NEXT
    window's write. _release(wrote=True) consumes only the `attempts` it persisted (`count - attempts`), NOT the
    whole count, and _reserve's `not st.writing` guard makes the mid-flight denial 'not due'. A deterministic
    re-entrant fake conn injects the mid-flight denial DURING execute (while writing=True; the lock is released
    during the DB write, so this models a concurrent caller). Guards two regressions that would BOTH stay green
    on every other test: `count = 0` on success (drops the mid-flight denial) or dropping the `not writing`
    guard (races a duplicate write)."""
    clk = _Clock()
    dl = audit.DenialLog(window_s=60, now=clk)
    state = {"reentered": False}

    class _ReentrantConn:
        def __init__(self) -> None:
            self.calls = 0
            self.commits = 0

        def execute(self, _sql: str, _params: Any = None) -> _ReentrantConn:
            self.calls += 1
            if not state["reentered"]:   # once, mid-write (K.writing=True) → this denial must coalesce, not write
                state["reentered"] = True
                dl.note(lambda: self, surface="mcp", action="a", reason="no_token", source_ip="K")
            return self

        def commit(self) -> None:
            self.commits += 1

        def close(self) -> None:
            pass

    conn = _ReentrantConn()
    dl.note(lambda: conn, surface="mcp", action="a", reason="no_token", source_ip="K")
    assert conn.calls == 1 and conn.commits == 1   # EXACTLY one write — the mid-flight denial coalesced
    clk.advance(61)
    good = _DenialConn()
    dl.note(lambda: good, surface="mcp", action="a", reason="no_token", source_ip="K")
    assert len(good.calls) == 1 and good.calls[0][1][7].obj["attempts"] == 2   # the mid-flight denial survived


def test_denial_log_eviction_skips_an_in_flight_key_and_refunds_a_failed_write():
    """The eviction-during-write defect (both pass-2 state-machine reviewers): while K's row is in flight, a
    rotating-IP flood must NOT LRU-evict K (its reservation would be orphaned), and if K's write then FAILS the
    per-window ceiling charge must STILL be refunded — on the reserved STATE OBJECT, not a stale key lookup that
    an evict/re-create would defeat. A re-entrant fake conn fills the 3-key map with fresh keys DURING K's
    write, then K's write raises. The buggy key-lookup release (evicted K → get(key)=None → early return) would
    leave `_window_writes` inflated by K's phantom charge, tightening the flood cap and dropping real denials."""
    clk = _Clock()
    dl = audit.DenialLog(window_s=60, global_max=10, max_keys=3, now=clk)

    class _FloodThenFailConn:
        def execute(self, _sql: str, _params: Any = None) -> _FloodThenFailConn:
            for ip in ("f1", "f2", "f3", "f4"):   # 4 fresh keys arrive mid-write → force LRU eviction (map cap 3)
                dl.note(lambda: _DenialConn(), surface="mcp", action="a", reason="no_token", source_ip=ip)
            raise RuntimeError("ledger blip mid-write")   # K's own write then FAILS

        def commit(self) -> None:
            pass

        def close(self) -> None:
            pass

    dl.note(lambda: _FloodThenFailConn(), surface="mcp", action="a", reason="no_token", source_ip="K")
    # K was writing throughout the flood → NOT evicted (eviction skips in-flight keys), reservation stays live.
    assert ("mcp", "K", "a", "no_token") in dl._keys
    # K's FAILED write refunded its charge, so _window_writes counts only the 4 real flood writes — not K's
    # phantom (the buggy release would have skipped the refund on the evicted key, leaving it at 5).
    assert dl._window_writes == 4
