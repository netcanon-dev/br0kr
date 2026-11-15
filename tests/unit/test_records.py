"""Unit tests for capacity-only records (fleet onboarding, phase 2): admission.validate_record and
admission.record_existing, the writer that holds capacity for a guest br0kr does not manage. The real-Postgres
races (a record against an admit on the same project, two records of one guest) are in scripts/harness_records.py.
"""
from __future__ import annotations

import logging
import types

import psycopg
import pytest

from br0kr import admission, lease, reasons

pytestmark = pytest.mark.unit

_OK = {"project_id": "infra", "req_cpu": 4.0, "req_mem_mb": 8192, "guest_kind": "qemu", "observed_vmid": 9950,
       "label": "ledger VM"}


def _violation(cls: type[psycopg.Error], name: str | None) -> psycopg.Error:
    """A psycopg constraint error whose diag names `name` (a real one reads it from the server's answer)."""
    class _Named(cls):  # type: ignore[misc, valid-type]
        @property
        def diag(self):  # type: ignore[override]
            return types.SimpleNamespace(constraint_name=name)
    return _Named("violation")


@pytest.mark.parametrize("kw,reason", [
    ({"project_id": None}, "missing_project_id"),
    ({"project_id": ""}, "missing_project_id"),
    ({"req_cpu": True}, "boolean_resources"),
    ({"req_mem_mb": True}, "boolean_resources"),
    ({"req_cpu": float("nan")}, "non_finite_resources"),
    ({"req_mem_mb": float("inf")}, "non_finite_resources"),
    ({"req_mem_mb": 10 ** 400}, "non_finite_resources"),
    ({"req_cpu": "4"}, "non_finite_resources"),
    ({"req_cpu": -1.0}, "non_positive_resources"),
    ({"req_mem_mb": 0}, "non_positive_resources"),
    ({"req_mem_mb": 2 ** 63}, "resource_too_large"),
    ({"guest_kind": "docker"}, "invalid_guest_kind"),
    ({"guest_kind": "QEMU"}, "invalid_guest_kind"),
    ({"observed_vmid": 99}, "invalid_observed_vmid"),
    ({"observed_vmid": 1_000_000_000}, "invalid_observed_vmid"),
    ({"observed_vmid": True}, "invalid_observed_vmid"),
    ({"observed_vmid": "9950"}, "invalid_observed_vmid"),
    ({"observed_vmid": 9950.0}, "invalid_observed_vmid"),
    ({"label": ""}, "invalid_label"),
    ({"label": "   "}, "invalid_label"),
    ({"label": "x" * 201}, "invalid_label"),
    ({"label": "two\nlines"}, "invalid_label"),
    ({"label": None}, "invalid_label"),
])
def test_validate_record_refuses_a_malformed_record(kw, reason):
    """A record names a project, memory above zero, CPU of zero or more, a known guest kind, a vmid in Proxmox's
    range and a printable label of 1 to 200 characters once stripped. A JSON `true` is refused for either amount
    (not read as 1), and a non-finite, non-numeric or over-range amount is refused before the INSERT. Guards a
    record the 0055 CHECKs would reject surfacing as a crash, or a log line or dashboard cell broken by a newline."""
    assert admission.validate_record(**{**_OK, **kw}) == reason


@pytest.mark.parametrize("kw", [
    {},
    {"req_cpu": 0},                                 # a failover hold: memory only
    {"req_cpu": 0.5, "guest_kind": "lxc"},
    {"observed_vmid": 100}, {"observed_vmid": 999_999_999},
    {"label": "x" * 200}, {"label": "  padded  "},
])
def test_validate_record_accepts_a_well_formed_record(kw):
    """CPU 0 (a memory-only failover hold), a container, the ends of the vmid range, a 200-character label and a
    label with outer spaces (stripped when stored) are all well formed."""
    assert admission.validate_record(**{**_OK, **kw}) is None


class _RecordConn:
    """A fake NON-autocommit connection answering record_existing's statements by SQL, recording each one with its
    parameters. `node_row` is the _NODE_AVAIL row (cpu avail, mem avail, cordoned, cluster); `leased` and
    `open_record` answer the two duplicate reads; `project_row` the budget read; `insert_raises` replaces the
    INSERT's answer with an error."""

    autocommit = False

    def __init__(self, *, node_row=(12.0, 39936, False, "c1"), leased=None, open_record=None,
                 project_row=(40.0, 100352, 10, None, None, None), insert_raises=None):
        self.node_row, self.leased, self.open_record = node_row, leased, open_record
        self.project_row, self.insert_raises = project_row, insert_raises
        self.executed: list[tuple[str, tuple]] = []
        self.commits = self.rollbacks = 0

    def cursor(self):
        return self

    def execute(self, sql, params=()):
        self.executed.append((sql, tuple(params)))
        self._sql = sql
        if sql == admission._RECORD_INSERT and self.insert_raises is not None:
            raise self.insert_raises
        return self

    def fetchone(self):
        sql = self._sql
        if "pg_advisory_xact_lock" in sql:
            return (None,)
        answers = {admission._NODE_AVAIL: self.node_row, admission._VMID_LEASED: self.leased,
                   admission._RECORD_OPEN: self.open_record, admission._PROJECT_AVAIL: self.project_row,
                   admission._RECORD_INSERT: (31,)}
        if sql in answers:
            return answers[sql]
        raise AssertionError(f"unexpected statement: {sql.strip()[:70]}")

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def reads(self) -> list[str]:
        named = {admission._NODE_AVAIL: "node", admission._VMID_LEASED: "leased", admission._RECORD_OPEN: "open",
                 admission._PROJECT_AVAIL: "project", admission._RECORD_INSERT: "insert"}
        return [("lock:" + p[0]) if "pg_advisory_xact_lock" in s else named[s] for s, p in self.executed]

    def params_of(self, sql: str) -> tuple:
        return next(p for s, p in self.executed if s == sql)


def _record(conn, **kw):
    args = {"node_id": "home", "project_id": "infra", "req_cpu": 4.0, "req_mem_mb": 8192,
            "observed_vmid": 9950, "label": "  ledger VM  "}
    return admission.record_existing(conn, **{**args, **kw})


def test_record_locks_node_then_project_and_inserts_a_pinned_capacity_only_lease():
    """record_existing takes admit()'s advisory locks for the two scopes it debits, node then project (the one
    canonical order, so it cannot deadlock with an admit), then under them reads the node, a live lease already
    counting the vmid, an open record of it, and the project's budget, and inserts one ADMITTED, pinned,
    capacity-only lease with no vmid and the label stripped, committing once. Guards a record taken without the
    project lock (two writers could both fit the last of a quota), or one a reaper could expire or a provisioner
    clone over."""
    conn = _RecordConn()
    res = _record(conn)
    assert res == admission.RecordResult(31, True, None, False)
    assert conn.reads() == ["lock:node:home", "lock:project:infra", "node", "leased", "open", "project",
                            "insert"]
    assert conn.params_of(admission._RECORD_INSERT) == ("home", 4.0, 8192, "infra", lease.CAPACITY_ONLY, "qemu",
                                                        9950, "ledger VM")
    assert conn.params_of(admission._VMID_LEASED) == ("c1", 9950)
    assert conn.params_of(admission._RECORD_OPEN) == ("home", 9950, lease.CAPACITY_ONLY)
    assert conn.commits == 1 and conn.rollbacks == 0
    sql = " ".join(admission._RECORD_INSERT.split())
    assert "'ADMITTED', 900, now(), true, %s" in sql and "vmid" not in sql.replace("observed_vmid", "")


def test_record_ignores_a_cordon_and_skips_the_vmid_read_on_a_cluster_less_node():
    """A cordon closes a node to NEW guests; a record describes a guest already running there, so a cordoned node
    takes it. A node in no cluster has no vmid namespace to search, so the live-lease read is skipped. Guards the
    cordoned pve2-new and pve6 refusing the records phase 6 needs before they can be uncordoned."""
    conn = _RecordConn(node_row=(10.0, 1024, True, None))
    res = _record(conn, node_id="pve2-new", req_cpu=2, req_mem_mb=512)
    assert res.recorded and res.reason is None
    assert "leased" not in conn.reads()


def test_record_over_the_node_capacity_is_kept_and_reported(caplog):
    """The node's declared capacity is not a refusal: the record is kept, `node_overcommitted` is True and a
    WARNING says to raise the node (admits there wait until then). Guards a refusal that leaves the ledger
    counting less than the node carries, the unsafe direction."""
    conn = _RecordConn(node_row=(2.0, 4096, False, "c1"))
    with caplog.at_level(logging.WARNING, logger="br0kr.admission"):
        res = _record(conn)
    assert (res.recorded, res.node_overcommitted) == (True, True)
    assert any("over its declared capacity" in r.getMessage() for r in caplog.records)
    assert _record(_RecordConn(node_row=(4.0, 8192, False, "c1"))).node_overcommitted is False


@pytest.mark.parametrize("conn_kw,reason,lease_id,last_read", [
    ({"node_row": None}, "unknown_node", None, "node"),
    ({"leased": (25,)}, "vmid_leased", 25, "leased"),
    ({"open_record": (30,)}, "record_exists", 30, "open"),
    ({"project_row": None}, "unknown_project", None, "project"),
    ({"project_row": (40.0, 4096, 10, None, None, None)}, "insufficient_project_mem", None, "project"),
    ({"project_row": (2.0, 100352, 10, None, None, None)}, "insufficient_project_cpu", None, "project"),
    ({"project_row": (40.0, 100352, 0, None, None, None)}, "project_lease_limit", None, "project"),
])
def test_record_refusals_name_their_reason_and_insert_nothing(conn_kw, reason, lease_id, last_read):
    """Each refusal stops at the read that found it, inserts nothing, commits once (releasing the locks), and names
    its reason; `vmid_leased` and `record_exists` also name the lease that already counts the guest. The project's
    quota IS a refusal (the operator raises it first, Q4). Guards a guest counted twice, by a second record or by a
    record of a guest br0kr already leases."""
    conn = _RecordConn(**conn_kw)
    res = _record(conn)
    assert (res.lease_id, res.recorded, res.reason) == (lease_id, False, reason)
    assert conn.reads()[-1] == last_read and "insert" not in conn.reads()
    assert conn.commits == 1


def test_a_malformed_record_reads_nothing():
    """A malformed record is refused before any lock or read."""
    conn = _RecordConn()
    assert _record(conn, label="").reason == "invalid_label"
    assert conn.executed == [] and conn.commits == 0


def test_record_refuses_an_autocommit_connection():
    """On autocommit each advisory lock ends with its statement, so the budget read would be unserialized."""
    conn = _RecordConn()
    conn.autocommit = True
    with pytest.raises(RuntimeError, match="NON-autocommit"):
        _record(conn)
    assert conn.executed == []


def test_record_maps_its_constraint_races_and_rolls_back_anything_else():
    """The INSERT's own constraint errors become clean reasons after a rollback: the open-record unique index (by
    name) is record_exists, a node or project deleted under it maps through _fk_reason. Any other error rolls back
    and re-raises, so the connection is left clean. Guards an uncaught raise from a benign race, and a catch that
    would hide a different unique index."""
    named = _violation(psycopg.errors.UniqueViolation, admission.RECORD_UNIQUE)
    conn = _RecordConn(insert_raises=named)
    assert _record(conn) == admission.RecordResult(None, False, reasons.RECORD_EXISTS)
    assert conn.rollbacks == 1 and conn.commits == 0
    fk = _violation(psycopg.errors.ForeignKeyViolation, "lease_project_id_fkey")
    conn = _RecordConn(insert_raises=fk)
    assert _record(conn).reason == "unknown_project" and conn.rollbacks == 1
    for exc in (_violation(psycopg.errors.UniqueViolation, "lease_idem_key_key"), RuntimeError("boom")):
        conn = _RecordConn(insert_raises=exc)
        with pytest.raises(type(exc)):
            _record(conn)
        assert conn.rollbacks == 1 and conn.commits == 0


# ── br0kr/records.py: the operator verbs the dashboard calls ─────────────────────────────────────────────────────
from br0kr import records  # noqa: E402


def test_record_verb_passes_the_ask_through_and_shapes_the_answer(monkeypatch):
    """records.record calls admission.record_existing with the operator's ask unchanged and answers {lease_id,
    recorded, reason, node_id, project_id, node_overcommitted}. Guards the verb adding a gate or a default of its
    own (the writer is the one authority) and the dashboard losing the overcommit flag it warns with."""
    seen = {}

    def fake(conn, **kw):
        seen.update(kw)
        return admission.RecordResult(31, True, None, True)
    monkeypatch.setattr(admission, "record_existing", fake)
    out = records.record(object(), node_id="peer", project_id="infra", req_cpu=0, req_mem_mb=6144,
                         observed_vmid=9950, label="failover hold")
    assert seen == {"node_id": "peer", "project_id": "infra", "req_cpu": 0, "req_mem_mb": 6144,
                    "observed_vmid": 9950, "label": "failover hold", "guest_kind": "qemu"}
    assert out == {"lease_id": 31, "recorded": True, "reason": None, "node_id": "peer", "project_id": "infra",
                   "node_overcommitted": True}


class _CloseConn:
    """Answers close_record's CAS with `cas_row`, then the ownership read with `owner_row`."""

    def __init__(self, cas_row, owner_row=None):
        self.answers = [cas_row, owner_row]
        self.sql: list[str] = []
        self.commits = 0

    def execute(self, sql, params=()):
        self.sql.append(sql)
        return self

    def fetchone(self):
        return self.answers.pop(0)

    def commit(self):
        self.commits += 1


@pytest.mark.parametrize("cas_row,owner_row,closed,reason", [
    ((31,), None, True, None),
    (None, None, False, "unknown_lease"),
    (None, ("provisioned",), False, "not_a_record"),
    (None, ("adopted",), False, "not_a_record"),
    (None, ("capacity_only",), False, "lease_not_live"),
])
def test_close_frees_only_an_open_record_and_names_why_not(cas_row, owner_row, closed, reason):
    """records.close is the guarded CAS (lease.close_record) and nothing else; when it matches no row, one read says
    why: no such lease, a lease that is not a record (a provisioned or adopted guest is never freed here: release
    it through its own verb), or a record already closed. Each statement's transaction is ended. Guards a close
    that frees the capacity of a guest that still runs, and a refusal that leaves the operator guessing."""
    conn = _CloseConn(cas_row, owner_row)
    assert records.close(conn, lease_id=31) == {"lease_id": 31, "closed": closed, "reason": reason}
    assert conn.sql[0].startswith("UPDATE lease SET state = 'RELEASED'")
    assert len(conn.sql) == (1 if closed else 2) and conn.commits == len(conn.sql)


def test_the_audit_predicates_record_what_changed_and_refuse_what_did_not():
    """A made record is audited under its new lease id and is not a refusal; a refused one is not audited and is a
    refusal with its reason; a close is audited only when it freed a record. Guards a refused record flooding the
    main trail, or a made one missing from it."""
    made = {"lease_id": 31, "recorded": True, "reason": None}
    refused = {"lease_id": 30, "recorded": False, "reason": "record_exists"}
    assert records.audit_record(made) == (True, "31") and records.refused_record(made) == (False, None)
    assert records.audit_record(refused) == (False, None)
    assert records.refused_record(refused) == (True, "record_exists")
    assert records.audit_close({"lease_id": 31, "closed": True, "reason": None}) == (True, "31")
    assert records.audit_close({"lease_id": 31, "closed": False, "reason": "not_a_record"})[0] is False
