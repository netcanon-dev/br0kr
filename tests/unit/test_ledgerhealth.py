"""Unit tests for br0kr/ledgerhealth.py — the ledger's own replication posture.

The module exists because a dead hot standby degraded NOTHING for 45 hours (the daemon was leader, the
ledger was writable through the surviving multi-host PGCONN host, every health signal stayed green).
These tests pin the part that makes such a signal usable rather than ignorable: the THREE distinct
states, plus the two SCOPING rules a review showed the first cut got wrong. "cannot read" and "no
replication configured" must never look like "broken", and a healthy standby must never look like a
dead one — either way the operator learns to tune the signal out, which is the same class of mistake as
logging the failure at debug.

A fake connection is used deliberately: the derivation logic is pure given rows, and the real catalog
behaviour (a slot going inactive when its standby dies, WAL accumulating behind it) is proven against
REAL streaming replication in scripts/harness_ledger_replication.py. Unit tests own the branches; the
harness owns the Postgres semantics — including the row shapes that would be a lie to fake without
having seen them on a live cluster."""
from __future__ import annotations

import pytest

from br0kr import ledgerhealth

pytestmark = pytest.mark.unit


class _FakeCursor:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return list(self._rows)


class _FakeConn:
    """A psycopg-shaped stand-in: `execute(sql)` returns rows chosen by which catalog the SQL names.

    `standbys` rows are `(state, sync_state, replay_lag)` and `slots` rows are
    `(slot_name, slot_type, active, wal_status, retained)` — the shapes the real queries select.
    `sync_names` is whether `synchronous_standby_names` is set (the sync-required read).
    `autocommit` defaults True (the daemon's connection) so the rollback path is opt-in per test."""

    def __init__(self, *, in_recovery: bool = False, slots: list[tuple] | None = None,
                 standbys: list[tuple] | None = None, boom: bool = False,
                 autocommit: bool = True, sync_names: bool = False) -> None:
        self.in_recovery = in_recovery
        self.sync_names = sync_names
        self.slots = slots or []
        self.standbys = standbys or []
        self.boom = boom
        self.autocommit = autocommit
        self.seen: list[str] = []
        self.rollbacks = 0

    def execute(self, sql: str, *_args: object) -> _FakeCursor:
        self.seen.append(sql)
        if self.boom:
            raise RuntimeError("password=hunter2 host=secret-ledger.internal could not connect")
        if "pg_replication_slots" in sql:
            return _FakeCursor(self.slots)
        if "pg_stat_replication" in sql:
            return _FakeCursor(self.standbys)
        if "pg_is_in_recovery" in sql:
            return _FakeCursor([(self.in_recovery,)])
        if "synchronous_standby_names" in sql:
            return _FakeCursor([(self.sync_names,)])
        raise AssertionError(f"unexpected SQL: {sql}")

    def rollback(self) -> None:
        self.rollbacks += 1


def _slot(name: str = "br0kr_standby", *, kind: str = "physical", active: bool = True,
          wal_status: str = "reserved", retained: int | None = 0) -> tuple:
    return (name, kind, active, wal_status, retained)


# ── the three states ────────────────────────────────────────────────────────────────────────────────
def test_a_dead_standby_reads_as_configured_with_an_inactive_slot_and_growing_wal():
    """The exact 45-hour signature: a slot that still exists, with no consumer, pinning WAL.

    This is THE case the module was written for. A slot outlives the standby that used it, so
    `configured` stays True (replication IS meant to be happening), `standbys_streaming` drops to 0 and
    `slots_inactive` goes to 1 while `retained_wal_bytes` climbs. Guards any change that would let a
    dead standby read as healthy or as "not configured"."""
    conn = _FakeConn(slots=[_slot(active=False, retained=37748736)], standbys=[])
    result = ledgerhealth.read(conn)
    assert result.readable and result.reason == "ok"
    assert result.configured is True
    assert result.standbys_streaming == 0 and result.standbys_total == 0
    assert result.slots_inactive == 1 and result.slots_lost == 0
    assert result.max_retained_wal_bytes == 37748736


def test_a_healthy_streaming_standby_reads_clean():
    """A connected, streaming standby with an active slot reads configured + streaming + 0 inactive."""
    conn = _FakeConn(slots=[_slot()], standbys=[("streaming", "async", 0)])
    result = ledgerhealth.read(conn)
    assert result.configured and result.standbys_streaming == 1 and result.slots_inactive == 0
    assert result.max_replay_lag_bytes == 0
    assert result.standbys_sync == 0


def test_no_replication_configured_is_not_a_fault():
    """A single-node ledger (no slots, no walsenders) reads readable + NOT configured.

    Guards the failure mode that makes monitoring useless: if a deliberate single-node deployment
    rendered as broken, the operator would mute the signal, and it would then be muted on the day a
    real standby died. `reason` says 'not_configured' so the surface can render it neutrally."""
    result = ledgerhealth.read(_FakeConn())
    assert result.readable is True
    assert result.configured is False
    assert result.reason == "not_configured"
    assert result.slots_inactive == 0 and result.max_retained_wal_bytes is None


def test_an_unreadable_catalog_is_unknown_never_raises_and_leaks_no_driver_text():
    """A raising query yields readable=False with a reason CODE, and never propagates.

    Two guarantees in one: (a) this read is called from a /metrics scrape and a dashboard request, so
    raising would turn a visibility feature into an outage of the page that reports it; (b) `reason`
    must carry only the exception CLASS — the fake's message deliberately contains a password and a
    hostname, and neither may reach an unauthenticated scrape body."""
    result = ledgerhealth.read(_FakeConn(boom=True))
    assert result.readable is False
    assert result.reason == "query_failed:RuntimeError"
    assert "hunter2" not in result.reason and "secret-ledger" not in result.reason
    assert result.configured is False and result.slots == ()


# ── scoping rule 1: privilege masking is not an error ───────────────────────────────────────────────
def test_privilege_masked_stats_read_as_UNKNOWN_not_as_a_dead_standby():
    """A `pg_stat_replication` row whose columns are NULL degrades to readable=False/'stats_masked'.

    Caught by review, and it is the nastiest possible failure for this feature. For a role without
    `pg_read_all_stats`/`pg_monitor`, Postgres does NOT raise — it returns the rows with every column
    but `pid` NULLed. Counting such a row would give standbys_total=1, standbys_streaming=0 and
    (because pg_replication_slots is NOT masked) configured=True: i.e. a PERFECTLY HEALTHY standby
    rendered as the documented dead-standby signature, permanently, on any least-privilege external
    Postgres — which .env.example explicitly invites. Chronic confident-wrong red is precisely what the
    three-state design exists to prevent, so the only honest answer is UNKNOWN."""
    conn = _FakeConn(slots=[_slot()], standbys=[(None, None, None)])
    result = ledgerhealth.read(conn)
    assert result.readable is False
    assert result.reason == "stats_masked"
    # And crucially it must NOT present the dead-standby shape while claiming to be readable.
    assert result.configured is False and result.slots_inactive == 0


# ── scoping rule 2: only physical slots / real standbys drive the DR aggregates ──────────────────────
def test_a_logical_cdc_slot_does_not_fabricate_or_mask_the_dr_posture():
    """A LOGICAL slot is excluded from the DR aggregates but still reported per-slot.

    A CDC consumer that connects nightly is `active=false` by design, so counting it would trip
    slots_inactive>0 permanently and page daily off the runbook's flagship alert — with no remedy but
    muting, which would refute the whole no-knob rationale. Conversely a logical slot alone must not
    read as 'this ledger has a replica'. It still appears in `slots` so its WAL retention stays
    visible; it just does not pretend to be DR."""
    conn = _FakeConn(slots=[_slot("cdc", kind="logical", active=False, retained=999)], standbys=[])
    result = ledgerhealth.read(conn)
    assert result.configured is False and result.reason == "not_configured"
    assert result.slots_inactive == 0 and result.max_retained_wal_bytes is None
    assert [s.name for s in result.slots] == ["cdc"]        # still visible in the detail
    # ...and a dead PHYSICAL slot alongside it still fires the signature.
    both = ledgerhealth.read(_FakeConn(slots=[_slot("cdc", kind="logical", active=False),
                                              _slot(active=False, retained=4096)]))
    assert both.configured is True and both.slots_inactive == 1


def test_a_pg_basebackup_session_is_not_counted_as_a_standby():
    """A walsender in state='backup' is skipped, so a backup window cannot flip a slotless single-node
    ledger to 'configured'. Guards a nightly base backup silently inventing a DR posture that then
    reads as streaming=0 — a red that resolves when the backup ends, i.e. pure alert noise."""
    result = ledgerhealth.read(_FakeConn(standbys=[("backup", "async", None)]))
    assert result.standbys_total == 0
    assert result.configured is False and result.reason == "not_configured"


def test_a_synchronous_standby_is_counted_separately():
    """sync/quorum standbys are counted in `standbys_sync`. Losing one blocks COMMITS, not merely DR,
    so it needs its own number rather than being averaged into the async count."""
    conn = _FakeConn(slots=[_slot()], standbys=[("streaming", "sync", 0), ("streaming", "async", 128)])
    result = ledgerhealth.read(conn)
    assert result.standbys_total == 2 and result.standbys_streaming == 2
    assert result.standbys_sync == 1
    assert result.max_replay_lag_bytes == 128        # the WORST lag, not the first


def test_sync_required_follows_synchronous_standby_names():
    """`sync_required` is the primary's `synchronous_standby_names` being set, read separately from the standbys.

    An ASYNC ledger (the bundled compose pair) has standbys_sync 0 by design, so an alert on that count alone
    paged forever on every async deployment; the sync-lost alert needs to know commits actually wait. Pins
    both values, that a standby connection never reads the setting (it is the primary's), and that the flag
    survives the reason rewrite of a slotless ledger."""
    assert ledgerhealth.read(_FakeConn(slots=[_slot()], standbys=[("streaming", "async", 0)])).sync_required is False
    sync = ledgerhealth.read(_FakeConn(slots=[_slot()], standbys=[("streaming", "sync", 0)], sync_names=True))
    assert sync.sync_required is True and sync.standbys_sync == 1
    lost = ledgerhealth.read(_FakeConn(sync_names=True))   # sync configured, no standby left at all
    assert lost.sync_required is True and lost.reason == "not_configured"
    replica = _FakeConn(in_recovery=True, slots=[_slot()], sync_names=True)
    assert ledgerhealth.read(replica).sync_required is False
    assert not any("synchronous_standby_names" in sql for sql in replica.seen)


# ── recovery + slot states ──────────────────────────────────────────────────────────────────────────
def test_a_standby_connection_skips_the_primary_only_terms():
    """Read against a connection in recovery: slots are still read, primary-only terms are skipped.

    `pg_current_wal_lsn()` RAISES on a standby, so the retained-WAL and replay-lag terms must be
    omitted rather than computed. Guards someone pointing the dashboard at a replica and getting a
    hard error instead of a degraded-but-honest answer (`is_primary=False`)."""
    conn = _FakeConn(in_recovery=True, slots=[_slot(active=False, retained=None)])
    result = ledgerhealth.read(conn)
    assert result.readable and result.is_primary is False and result.reason == "in_recovery"
    assert result.max_replay_lag_bytes is None
    assert result.slots[0].retained_wal_bytes is None
    # pg_stat_replication must not have been queried at all on a standby.
    assert not any("pg_stat_replication" in sql for sql in conn.seen)
    # ...and neither must the primary-only WAL-position function.
    assert not any("pg_current_wal_lsn" in sql for sql in conn.seen)


def test_a_lost_slot_is_flagged_as_needing_a_re_clone():
    """`wal_status='lost'` sets `lost` on the slot and counts into `slots_lost`.

    This is the one slot state a restart cannot fix: the primary already recycled the WAL, so the
    standby must be re-cloned. It deserves its own signal rather than being lumped in with 'inactive',
    because the remedy is different."""
    conn = _FakeConn(slots=[_slot(active=False, wal_status="lost")])
    result = ledgerhealth.read(conn)
    assert result.slots[0].lost is True
    assert result.slots_lost == 1


# ── transaction neutrality ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("boom", [False, True], ids=["success", "failure"])
def test_read_is_transaction_neutral_on_a_non_autocommit_connection(boom):
    """On a non-autocommit connection the reads are rolled back, on BOTH the success and failure paths.

    psycopg opens an implicit transaction on the first execute, so without this a success would leave
    the caller idle-in-transaction holding a snapshot, and a mid-read failure would leave it ABORTED —
    every later statement raising InFailedSqlTransaction — while `read()` returned a clean UNKNOWN and
    gave the caller no cue that anything needed settling. Both shipped callers use a short-lived
    connection they close, so this is about not making the module a trap for the next caller."""
    conn = _FakeConn(slots=[_slot()], standbys=[], boom=boom, autocommit=False)
    ledgerhealth.read(conn)
    assert conn.rollbacks >= 1


def test_read_does_not_rollback_an_autocommit_connection():
    """An autocommit connection has no transaction to settle, so no rollback is issued — the daemon's
    scrape path stays exactly as cheap as it was."""
    conn = _FakeConn(slots=[_slot()], autocommit=True)
    ledgerhealth.read(conn)
    assert conn.rollbacks == 0


def test_as_dict_materialises_the_derived_fields_for_the_dashboard():
    """`as_dict` exposes the derived rollups explicitly rather than making the client re-derive them.

    The three states are separate keys (`readable`, `configured`) so the UI branches on data instead of
    inferring state from a pattern of nulls — which is how a "not configured" ledger would eventually
    get coloured like a broken one. `physical` rides along per slot so the UI can label a logical slot
    rather than imply it is a replica."""
    conn = _FakeConn(slots=[_slot(active=False, wal_status="lost", retained=1024)], standbys=[])
    payload = ledgerhealth.as_dict(ledgerhealth.read(conn))
    assert payload["readable"] is True and payload["configured"] is True
    assert payload["slots_inactive"] == 1 and payload["slots_lost"] == 1
    assert payload["max_retained_wal_bytes"] == 1024
    assert payload["standbys_sync"] == 0 and payload["sync_required"] is False
    assert payload["slots"] == [{
        "name": "br0kr_standby", "slot_type": "physical", "active": False,
        "wal_status": "lost", "retained_wal_bytes": 1024, "lost": True, "physical": True,
    }]
