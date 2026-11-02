"""Unit tests for the PDM registry (br0kr/remotes.py).

The registry decides which clusters can be a cross-cluster MIGRATION source: it builds a `pdm.Pdm`
only for a cluster with a COMPLETE, valid, secret-resolvable PDM config, and SKIPS the rest (never
raising) so one misconfigured cluster can't blank the registry. A wrong build would either hand the
daemon a driver pointed at the wrong host (a token-bearing request to the wrong place) or drop a
correctly-configured cluster (silently disabling its migrations). These pin the presence gate, the
host/secret validation, and the atomic hot-reload — all without a DB or a network.
"""
from __future__ import annotations

import pytest

from br0kr import pdm, remotes

pytestmark = pytest.mark.unit


class _Cur:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple]:
        return self._rows


class _Conn:
    """A stand-in whose `execute(...).fetchall()` returns the seeded cluster rows; commit is a no-op."""

    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def execute(self, _sql: str, _params: object = None) -> _Cur:
        return _Cur(self._rows)

    def commit(self) -> None:
        pass


def test_pdm_conn_of_requires_all_three_pdm_fields():
    """The presence gate: a `PdmConn` is produced ONLY when endpoint + token id + secret-env are all set;
    any missing/empty one → None (not migration-capable). Guards a half-configured cluster (a likely
    operator mistake) being treated as ready and building a broken driver."""
    assert remotes.pdm_conn_of(("cl1", "pdm.host", "root@pam!m", "PDM_SECRET")) is not None
    for row in [("cl1", None, "t", "E"), ("cl1", "h", None, "E"), ("cl1", "h", "t", None),
                ("cl1", "", "t", "E"), ("cl1", "h", "t", "")]:
        assert remotes.pdm_conn_of(row) is None


def test_from_ledger_builds_only_fully_configured_valid_secret_resolvable_clusters():
    """from_ledger builds a driver for exactly the cluster that is COMPLETE + has a bare-host endpoint +
    whose secret env RESOLVES, and skips: a complete-but-secret-unset cluster, a partial one, a PVE-only
    one (no PDM at all), and one with a non-bare (SSRF-shaped) endpoint. Guards one bad cluster blanking
    the registry, and a migration being driven with an absent/misdirected token."""
    rows = [
        ("clA", "pdm.a", "root@pam!m", "PDM_A"),   # complete + secret present → BUILT
        ("clB", "pdm.b", "root@pam!m", "PDM_B"),   # complete but PDM_B unset → skipped
        ("clC", "pdm.c", None, "PDM_C"),           # partial (no token id) → skipped
        ("clD", None, None, None),                 # no PDM at all (PVE-only cluster) → skipped silently
        ("clE", "http://evil/", "t", "PDM_E"),     # non-bare endpoint (URL structure) → skipped
    ]
    reg = remotes.from_ledger(_Conn(rows), env={"PDM_A": "s3cret", "PDM_E": "x"})
    assert reg.cluster_ids() == ["clA"]
    assert reg.get("clA") is not None
    assert reg.get("clB") is None and reg.get("clC") is None
    assert reg.get("clD") is None and reg.get("clE") is None
    assert len(reg) == 1


def test_from_ledger_empty_is_normal_not_an_error():
    """No PDM-configured cluster → an EMPTY registry (cross-cluster migration simply inactive), returned
    cleanly with no exception. Guards an empty result being treated as a failure (it is the default, and
    the daemon's migration step must be a no-op, not a crash, when nothing has PDM)."""
    reg = remotes.from_ledger(_Conn([("cl1", None, None, None)]), env={})
    assert reg.cluster_ids() == [] and len(reg) == 0


def test_get_of_none_or_unknown_cluster_is_a_clean_miss():
    """`get(None)` (an unrouted source node) and `get(<unknown>)` both return None — the migration for a
    guest on such a cluster is left pending, never actuated with a wrong driver. Guards a None cluster id
    raising instead of missing cleanly."""
    reg = remotes.from_ledger(_Conn([("clA", "pdm.a", "t", "E")]), env={"E": "s"})
    assert reg.get(None) is None
    assert reg.get("nope") is None
    assert reg.get("clA") is not None


def test_from_ledger_constructs_a_real_pdm_with_the_resolved_secret():
    """A built entry is a `pdm.Pdm` carrying the endpoint + token id + the RESOLVED secret in its auth
    header (the secret came from the env, never the ledger). Guards the wrong secret/endpoint being wired
    into the driver — and pins that the PVEAPIToken scheme (which the log redactor scrubs) is used."""
    reg = remotes.from_ledger(_Conn([("clA", "pdm.a", "root@pam!brk", "E")]), env={"E": "topsecret"})
    driver = reg.get("clA")
    assert isinstance(driver, pdm.Pdm)
    assert driver._h["Authorization"] == "PVEAPIToken=root@pam!brk=topsecret"


def test_adopt_swaps_the_whole_driver_set_atomically():
    """`adopt` replaces the registry's drivers with another's in one reference assignment — a hot-reload
    with no restart, seen whole by a concurrent reader. Guards a torn half-applied swap and a stale driver
    surviving a re-point."""
    a = remotes.from_ledger(_Conn([("clA", "pdm.a", "t", "E")]), env={"E": "s"})
    b = remotes.from_ledger(_Conn([("clZ", "pdm.z", "t", "E")]), env={"E": "s"})
    a.adopt(b)
    assert a.cluster_ids() == ["clZ"]
    assert a.get("clA") is None and a.get("clZ") is not None
