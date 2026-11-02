"""Hermetic tests for the node registry (br0kr/nodes.py).

The registry maps each ledger node_id to a `pve.Pve` driver, built from the node's
CLUSTER connection, so the daemon can route a lease to the node it was admitted against.
Validation (which joined rows are actuatable) is pure; `from_ledger` is tested with a fake
connection + env map (no DB, no PVE). Live cluster routing is proven in
scripts/harness_cluster.py.
"""
from __future__ import annotations

import pytest

from br0kr import nodes, pve

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "host,ok",
    [
        ("192.168.11.16", True),
        ("pvenas", True),
        ("pve-1.lan", True),
        ("http://evil.com", False),
        ("evil.com/path", False),
        ("user@evil.com", False),
        ("evil.com#x", False),
        ("host:9999", False),
        ("a b", False),
        ("", False),
    ],
)
def test_valid_pve_host_accepts_only_bare_hosts(host, ok):
    """valid_pve_host accepts a bare hostname/IPv4 and rejects anything carrying URL
    structure. Load-bearing security check: an unvalidated pve_host would let a typo'd or
    hostile cluster row redirect the daemon's TOKEN-BEARING request to another host."""
    assert nodes.valid_pve_host(host) is ok


@pytest.mark.parametrize(
    "name,ok",
    [
        ("FastStorage", True),
        ("local-zfs", True),
        ("local_lvm", True),          # '_' is permitted (real PVE pool ids use it) — unlike valid_pve_node
        ("ceph.rbd", True),
        ("OMV-nfs_01", True),
        ("pool/child", False),        # a path separator would inject a second qm-clone arg / API path segment
        ("pool a", False),            # whitespace
        ("pool;rm", False),
        ("pool:9", False),
        ("../etc", False),
        ("", False),
    ],
)
def test_valid_pve_storage_accepts_only_bare_pool_ids(name, ok):
    """valid_pve_storage accepts a bare PVE storage id (letters/digits/._- , INCLUDING '_' which real pool
    ids like local_lvm use) and rejects anything carrying a path separator, whitespace, or shell/URL
    structure. Load-bearing (0031): the value is interpolated into `qm clone --storage <name>` and a
    `/nodes/{n}/storage/{name}` API path, so an unvalidated pve_name could inject a second clone arg or a
    path segment into the daemon's TOKEN-BEARING request."""
    assert nodes.valid_pve_storage(name) is ok


def test_conn_of_accepts_a_fully_joined_row():
    """A node joined to its cluster with all fields present yields a NodeConn carrying the
    node's in-cluster name + the cluster's connection (no secret — token_secret_env names
    where it lives). Guards a wiring slip that transposes node/cluster fields."""
    cfg = nodes.conn_of(("n1", "pve1", "clA", "10.0.0.1", "root@pam!t", "S"))
    assert cfg == nodes.NodeConn("n1", "clA", "pve1", "10.0.0.1", "root@pam!t", "S")


@pytest.mark.parametrize(
    "row",
    [
        ("n1", None, "clA", "h", "tid", "S"),   # no in-cluster pve_node name
        ("n1", "pve1", None, "h", "tid", "S"),  # no cluster_id (unassigned → capacity-only)
        ("n1", "pve1", "clA", None, "tid", "S"),  # cluster has no host
        ("n1", "pve1", "clA", "h", None, "S"),  # cluster has no token id
        ("n1", "pve1", "clA", "h", "tid", None),  # cluster has no secret-env name
    ],
)
def test_conn_of_treats_an_incomplete_row_as_not_actuatable(row):
    """A joined row missing ANY required field is not actuatable (None) — admission can
    still reserve the node, but the daemon must not try to provision it. Guards building a
    half-configured driver that would fail every create."""
    assert nodes.conn_of(row) is None


class _FakeConn:
    """Returns canned joined node/cluster rows for the JOIN query, and canned unassigned
    node ids for the NULL-cluster query; commit/fetchall only."""

    def __init__(self, rows, unassigned=()):
        self._rows = rows
        self._unassigned = [(u,) for u in unassigned]
        self._last: list = []

    def execute(self, sql, params=None):
        if "JOIN cluster c" in sql:
            self._last = self._rows
        elif "cluster_id IS NULL" in sql:
            self._last = self._unassigned
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected SQL: {sql}")
        return self

    def fetchall(self):
        return self._last

    def commit(self):
        pass


def test_from_ledger_builds_per_cluster_drivers_and_skips_misconfigured():
    """from_ledger builds a driver (from the CLUSTER's connection, targeting the node) for
    each node whose cluster has a valid host and a resolvable secret, and skips the rest:
    a bad host, an unset secret env, or a node with no in-cluster name. Guards sending the
    token to a bad host, and provisioning a node this daemon has no credential for."""
    rows = [
        ("n1", "pve1", "clA", "10.0.0.1", "tid", "S"),       # ok → driver in cluster clA
        ("n2", "pve2", "clB", "http://evil.com", "tid", "S"),  # bad host → skipped
        ("n3", "pve3", "clC", "10.0.0.3", "tid", "MISSING"),   # secret env unset → skipped
        ("n4", None, "clD", "10.0.0.4", "tid", "S"),          # no pve_node → skipped
    ]
    reg = nodes.from_ledger(_FakeConn(rows), env={"S": "x"})
    assert reg.node_ids() == ["n1"]
    driver = reg.get("n1")
    assert isinstance(driver, pve.Pve) and driver.node == "pve1"
    assert reg.cluster_of("n1") == "clA" and reg.clusters() == ["clA"]


def test_from_ledger_distinct_drivers_for_nodes_sharing_a_cluster():
    """from_ledger builds DISTINCT driver objects for two nodes that share one cluster's
    connection (so the daemon still routes per node), targeting each node's own pve_node,
    and groups them under the right clusters. Guards a regression that returns one shared
    driver for a cluster's nodes (breaking per-node routing) or mis-groups them."""
    rows = [
        ("a1", "pveA1", "clA", "10.0.0.1", "tid", "S"),
        ("a2", "pveA2", "clA", "10.0.0.1", "tid", "S"),   # same cluster as a1 (shared connection)
        ("b1", "pveB1", "clB", "10.0.0.2", "tid", "S"),
    ]
    reg = nodes.from_ledger(_FakeConn(rows), env={"S": "x"})
    assert reg.node_ids() == ["a1", "a2", "b1"]
    assert reg.nodes_in("clA") == ["a1", "a2"] and reg.cluster_of("b1") == "clB"
    assert reg.get("a1") is not reg.get("a2")                       # distinct driver objects
    assert reg.get("a1").node == "pveA1" and reg.get("a2").node == "pveA2"
    assert "10.0.0.1" in reg.get("a1").base and "10.0.0.2" in reg.get("b1").base


def test_registry_groups_nodes_by_cluster():
    """The registry reports each node's cluster, the distinct clusters served, and the nodes
    in a cluster. Guards the daemon iterating the wrong set, and the per-cluster grouping the
    capacity/inventory views rely on."""
    a, b, c = object(), object(), object()
    reg = nodes.NodeRegistry({"n1": a, "n2": b, "n3": c},
                             {"n1": "clA", "n2": "clA", "n3": "clB"})
    assert reg.get("n2") is b and reg.get("nope") is None
    assert reg.node_ids() == ["n1", "n2", "n3"]
    assert reg.cluster_of("n3") == "clB" and reg.cluster_of("nope") is None
    assert reg.clusters() == ["clA", "clB"]
    assert reg.nodes_in("clA") == ["n1", "n2"] and reg.nodes_in("clB") == ["n3"]
    assert len(reg) == 3


def test_adopt_swaps_the_fleet_in_place_for_a_holder_of_the_registry():
    """`adopt` replaces a registry's drivers + cluster groupings with another's IN PLACE (same
    object), so the daemon's hot-reload reconfigures the served fleet — a node added is now
    served, a node removed is no longer served, groupings track — without rebuilding every
    holder of the registry. The captured reference (`held`, standing in for the routing
    destroyer closure) sees the NEW fleet with no re-wiring. Guards the restart-free
    reconfiguration, and a swap that mixed new drivers with a stale cluster grouping."""
    a, b, c = object(), object(), object()
    reg = nodes.NodeRegistry({"n1": a, "n2": b}, {"n1": "clA", "n2": "clA"})
    held = reg   # a holder that captured the registry object (like make_routing_destroyer)
    fresh = nodes.NodeRegistry({"n2": b, "n3": c}, {"n2": "clA", "n3": "clB"})

    reg.adopt(fresh)

    assert reg.node_ids() == ["n2", "n3"]            # n1 dropped, n3 added
    assert reg.get("n1") is None                     # the removed node is no longer served
    assert held.get("n3") is c                       # the captured holder sees the new fleet
    assert reg.cluster_of("n3") == "clB" and reg.clusters() == ["clA", "clB"]
    assert reg.nodes_in("clA") == ["n2"] and reg.nodes_in("clB") == ["n3"]
    assert len(reg) == 2
