"""nodes.py — the node registry: which Proxmox node a lease routes to, and how to reach it.

Admission is multi-node (each lease carries a `node_id`; capacity is scoped per node). This
module is the actuation half: it maps each ledger `node_id` to a `pve.Pve` driver so the
daemon routes a lease to the node it was admitted against.

**Clusters own the connection (config-as-data, secret-in-env).** A Proxmox cluster shares
one API endpoint + token, and `/cluster/nextid` is unique only within a cluster — so the
connection (`pve_host`, `token_id`, `token_secret_env`) is a property of the `cluster` row,
and a node carries its `cluster_id` + its in-cluster `pve_node` name. The driver for a node
is built from ITS cluster's connection, targeting that node. The token SECRET is never in
the ledger: `token_secret_env` names the env var the daemon resolves it from, holding it
only in the driver's in-memory auth header (redacted in logs).

A node with no `cluster_id` (unassigned), or whose cluster's host is invalid / secret env is
unset, is **not actuatable here**: admission can still reserve against it, but THIS daemon
can't provision it, so it is skipped (capacity-only INFO, or a misconfig ERROR) rather than
failing the whole registry. Nodes in different clusters have independent vmid namespaces;
the per-VMID fence (global by lease id) keeps a cross-cluster destroy refused regardless.

**Hot-reloadable in place.** The registry is built from the ledger via `from_ledger`, but it
is not frozen for the daemon's life: `NodeRegistry.adopt` swaps its contents atomically, so
the reconciler can rebuild from the ledger on SIGHUP or a periodic cadence and pick up an
added node / a repointed cluster / a re-resolved token **without a restart** (br0kr/daemon.py;
docs/runbooks/docker-deploy.md §7).
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import psycopg

from br0kr import pve

log = logging.getLogger(__name__)

# A bare hostname or IPv4 — what `pve.Pve` safely interpolates into `https://{host}:8006`.
# Rejects anything carrying URL structure (scheme, '/', '@', '#', ':', whitespace) so a
# typo'd or hostile `pve_host` can't redirect the daemon's TOKEN-BEARING request to another
# host (the "config-as-data, VALIDATED" doctrine — host is the validated half).
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+$")


def valid_pve_host(host: str) -> bool:
    """Whether `host` is a bare hostname/IPv4 safe to place in the PVE base URL. Pure."""
    return bool(host) and _HOST_RE.match(host) is not None


def valid_pve_node(name: str) -> bool:
    """Whether `name` is a bare PVE node NAME (the in-cluster hostname the driver puts in the API path
    `/nodes/{name}/...`). Same charset guard as `valid_pve_host` so a hostile value can't inject a path
    segment / traversal into the token-bearing request. Pure."""
    return bool(name) and _HOST_RE.match(name) is not None


# A bare PVE STORAGE name — what the daemon interpolates into `qm clone --storage <name>` / a
# `/nodes/{n}/storage/{name}` path (0031's storage.pve_name). Mirrors `valid_pve_node`'s intent
# (no scheme/'/'/'@'/':'/'#'/whitespace, so a hostile value can't inject a path segment or a second
# clone arg) but ALSO permits '_' , which real PVE pool ids use (`local_lvm`, `local-zfs`, …) and
# `_HOST_RE` would wrongly reject.
_STORAGE_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


def valid_pve_storage(name: str) -> bool:
    """Whether `name` is a bare PVE storage id safe to place in `qm clone --storage`/a storage API path.
    Charset guard only (existence is a DB pre-check, not here); pure, so it is unit-testable without PVE."""
    return bool(name) and _STORAGE_RE.match(name) is not None


def valid_pve_bridge(name: str) -> bool:
    """Whether `name` is a bare PVE bridge / SDN vnet name (0038) safe to store as a node's declared bridge for a
    logical net (`network_node.bridge`) and to compare against a node's live bridge list. Same charset as
    `valid_pve_storage` (alnum + . _ -, no scheme / path segment / whitespace); pure, unit-testable without PVE."""
    return bool(name) and _STORAGE_RE.match(name) is not None


def valid_logical_network(name: str) -> bool:
    """Whether `name` is a bare LOGICAL NETWORK identity (0039) safe to store as a `logical_network` id and a
    `template.logical_network_id`. Same charset as a bridge/storage id (alnum + . _ -, no scheme / path segment /
    whitespace) — the identity is an operator-chosen label, not a PVE object, so the guard is charset-only
    (existence is a DB pre-check). Pure, so it is unit-testable without PVE."""
    return bool(name) and _STORAGE_RE.match(name) is not None


_CONTENT_RE = re.compile(r"^[a-z0-9]+\Z")   # \Z, NOT $ — `$` also matches BEFORE a trailing newline, so "import\n"
#                                              would slip past and silently declare a pool capable-of-nothing.


def valid_content_type(token: str) -> bool:
    """Whether `token` is a single PVE storage CONTENT type safe to store in `storage.content_types` (0041) and
    compare against a pool's declared capability (import / images / rootdir / iso / vztmpl / backup / snippets).
    Charset-only lowercase-word (a-z0-9) — deliberately NOT a fixed membership allowlist, so a FUTURE PVE content
    type is never rejected by a too-narrow guard (the 0031/0039 charset-at-the-verb discipline, mirrored for
    content). Lowercase-PINNED because PVE content tokens are lowercase: a mis-cased 'Import' is caught here rather
    than silently declaring a pool capable-of-nothing. Pure, so it is unit-testable without PVE."""
    return bool(token) and _CONTENT_RE.match(token) is not None


@dataclass(frozen=True)
class NodeConn:
    """A node's resolved actuation target: its in-cluster name + its cluster's connection
    (no secret — `token_secret_env` names where it lives)."""

    node_id: str
    cluster_id: str
    pve_node: str
    pve_host: str
    token_id: str
    token_secret_env: str


def conn_of(row: tuple[Any, ...]) -> NodeConn | None:
    """Pure: turn a joined `(node_id, pve_node, cluster_id, pve_host, token_id,
    token_secret_env)` row into a NodeConn, or None if a required field is missing (an
    unassigned/half-configured node — not actuatable). Validation is unit-testable here;
    the host-safety + secret resolution happen in `from_ledger`."""
    node_id, pve_node, cluster_id, pve_host, token_id, secret_env = row
    if not (pve_node and cluster_id and pve_host and token_id and secret_env):
        return None
    return NodeConn(node_id=node_id, cluster_id=cluster_id, pve_node=pve_node,
                    pve_host=pve_host, token_id=token_id, token_secret_env=secret_env)


@dataclass(frozen=True)
class _Snapshot:
    """The registry's two maps bundled so a hot-reload swaps BOTH in one atomic reference
    assignment. A concurrent reader (the reconcile tick, the routing destroyer) sees the
    old pair or the new pair in full — never a torn mix of new drivers with a stale cluster
    grouping. The dicts are never mutated after construction; a reload builds fresh ones."""

    drivers: dict[str, pve.Pve]
    node_clusters: dict[str, str]


class NodeRegistry:
    """A map of node_id → `pve.Pve` driver, plus each node's cluster. Construct directly from
    a dict in tests/harnesses, or from the ledger via `from_ledger`. ``node_clusters`` (node_id
    → cluster_id) powers per-cluster grouping/inventory; omit it for a flat single-cluster
    registry.

    **Hot-reloadable:** the contents live behind a single immutable `_Snapshot` reference that
    `adopt` swaps atomically, so the daemon can rebuild from the ledger (SIGHUP / periodic) and
    every holder of THIS object — notably the routing destroyer closure — sees the new fleet
    with no re-wiring, never a restart. The reads load the snapshot reference once, so they are
    consistent against a concurrent swap."""

    def __init__(self, drivers: Mapping[str, pve.Pve],
                 node_clusters: Mapping[str, str] | None = None) -> None:
        self._snap = _Snapshot(dict(drivers), dict(node_clusters or {}))

    def get(self, node_id: str) -> pve.Pve | None:
        """The driver for a node, or None if this daemon doesn't serve it (unassigned /
        unknown) — the caller skips/defers rather than provisioning on a wrong node."""
        return self._snap.drivers.get(node_id)

    def node_ids(self) -> list[str]:
        return sorted(self._snap.drivers)

    def items(self) -> list[tuple[str, pve.Pve]]:
        """(node_id, driver) pairs — what the daemon iterates to sweep every served node."""
        return sorted(self._snap.drivers.items())

    def cluster_of(self, node_id: str) -> str | None:
        """The cluster a served node belongs to (None if unknown/ungrouped)."""
        return self._snap.node_clusters.get(node_id)

    def clusters(self) -> list[str]:
        """The distinct clusters this registry serves nodes in."""
        return sorted(set(self._snap.node_clusters.values()))

    def nodes_in(self, cluster_id: str) -> list[str]:
        """The served node ids in a cluster."""
        return sorted(n for n, c in self._snap.node_clusters.items() if c == cluster_id)

    def adopt(self, other: NodeRegistry) -> None:
        """Atomically take on another registry's drivers + cluster groupings — a hot-reload of
        the fleet with no restart. The swap is a SINGLE reference assignment (atomic under the
        GIL), so the reconcile tick and the routing-destroyer closure that captured THIS object
        observe either the whole old fleet or the whole new one, never a half-applied mix.
        Object identity is preserved (only the contents change), so nothing that holds this
        registry needs re-wiring. The displaced `pve.Pve` drivers hold no connection (each REST
        call is stateless), so they need no close."""
        self._snap = other._snap

    def __len__(self) -> int:
        return len(self._snap.drivers)


def from_ledger(conn: psycopg.Connection[Any], *, env: Mapping[str, str] | None = None) -> NodeRegistry:
    """Build the registry by joining each node to its cluster. For every node whose cluster
    has a valid host and a resolvable token secret, construct a `pve.Pve` from the cluster's
    connection targeting that node; skip the rest (unassigned → capacity-only INFO; bad
    host / missing secret → misconfig ERROR) so one bad credential can't blank the fleet.
    Commits to end the read transaction."""
    resolve = os.environ if env is None else env
    # INNER JOIN: a node with NULL cluster_id (unassigned) isn't returned here → capacity-only;
    # surfaced separately below so an operator can tell capacity-only-by-design from a typo.
    rows = conn.execute(
        "SELECT n.id, n.pve_node, c.id, c.pve_host, c.token_id, c.token_secret_env "
        "FROM node n JOIN cluster c ON n.cluster_id = c.id ORDER BY n.id"
    ).fetchall()
    unassigned = [r[0] for r in conn.execute(
        "SELECT id FROM node WHERE cluster_id IS NULL ORDER BY id").fetchall()]
    conn.commit()

    drivers: dict[str, pve.Pve] = {}
    node_clusters: dict[str, str] = {}
    misconfigured: list[str] = []   # nodes whose cluster is broken — a likely operator mistake
    bad_clusters: set[str] = set()  # log each broken cluster ONCE, not once per node it owns
    for row in rows:
        cfg = conn_of(tuple(row))
        if cfg is None:
            log.info("node %s is missing its in-cluster name (pve_node) — capacity-only", row[0],
                     extra={"node_id": row[0]})
            continue
        if not valid_pve_host(cfg.pve_host):
            misconfigured.append(cfg.node_id)
            if cfg.cluster_id not in bad_clusters:
                bad_clusters.add(cfg.cluster_id)
                log.error("cluster %s: pve_host %r is not a bare hostname/IP — refusing to build "
                          "drivers for its nodes (won't send the API token there)",
                          cfg.cluster_id, cfg.pve_host, extra={"cluster_id": cfg.cluster_id})
            continue
        secret = resolve.get(cfg.token_secret_env)
        if not secret:
            misconfigured.append(cfg.node_id)
            if cfg.cluster_id not in bad_clusters:
                bad_clusters.add(cfg.cluster_id)
                log.error("cluster %s: token secret env %r is unset — cannot provision its nodes "
                          "(check this daemon's environment)", cfg.cluster_id, cfg.token_secret_env,
                          extra={"cluster_id": cfg.cluster_id})
            continue
        drivers[cfg.node_id] = pve.Pve(cfg.pve_host, cfg.pve_node, cfg.token_id, secret)
        node_clusters[cfg.node_id] = cfg.cluster_id
    reg = NodeRegistry(drivers, node_clusters)
    log.info("node registry built: serving %d node(s) %s across %d cluster(s) %s",
             len(drivers), sorted(drivers), len(reg.clusters()), reg.clusters())
    if unassigned:
        log.info("%d node(s) have no cluster (capacity-only — the daemon can't provision them): %s",
                 len(unassigned), unassigned, extra={"unassigned_nodes": unassigned})
    if misconfigured:
        log.error("%d node(s) UNPROVISIONABLE %s in %d misconfigured cluster(s) %s — their leases "
                  "will accrue ADMITTED; fix the cluster host/secret, then SIGHUP the daemon (or wait "
                  "a reload cadence) to pick it up",
                  len(misconfigured), misconfigured, len(bad_clusters), sorted(bad_clusters),
                  extra={"misconfigured": misconfigured})
    return reg
