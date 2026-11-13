"""remotes.py — the PDM registry: which Proxmox **Datacenter Manager** appliance fronts each cluster,
and how to reach it. The cross-cluster analogue of `br0kr/nodes.py` (which maps node → `pve.Pve`).

Where `nodes.py` builds a per-NODE `pve.Pve` (the create/destroy path, scoped to one cluster), this
module builds a per-CLUSTER `pdm.Pdm` (the cross-cluster inventory + remote-migration path). A migration
of a lease's guest OFF a cluster is actuated through the PDM that fronts that cluster as a "remote", so
the driver is looked up by the migration's SOURCE cluster (br0kr/migration.py `run_pending_routed`).

**Clusters own the PDM connection (config-as-data, secret-in-env)** — identical to the PVE token pattern
in `nodes.py`. Three columns on the `cluster` row (0002 + 0018): `pdm_endpoint` (the appliance host),
`pdm_token_id`, and `pdm_token_secret_env` (the ENV VAR NAME the daemon resolves the secret from — the
secret itself is NEVER in the ledger, held only in the driver's in-memory auth header, redacted in logs).

**PDM is OPTIONAL per cluster.** A cluster with none of the three set is simply not a migration source (no
noise). A cluster with SOME but not all set, an invalid endpoint host, or an unset secret env is a likely
operator mistake and is logged ERROR once — but it never raises: one misconfigured cluster can't blank the
registry, exactly as one bad PVE credential can't blank the node registry. An empty registry is NORMAL (no
PDM configured anywhere → cross-cluster migration is simply inactive), so — unlike the node registry — there
is no empty-guard: nothing is stranded by having no PDM.

**Hot-reloadable in place.** Like `NodeRegistry`, the contents live behind a single dict reference that
`adopt` swaps atomically, so the daemon can rebuild from the ledger on SIGHUP / a cadence and pick up an
added PDM endpoint or a re-resolved token WITHOUT a restart (br0kr/tick_config.py `maybe_reload`).
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import psycopg

from br0kr import nodes, pdm

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PdmConn:
    """A cluster's resolved PDM actuation target: the appliance endpoint + token id (no secret —
    `token_secret_env` names where it lives). Produced only for a cluster with ALL THREE PDM fields set."""

    cluster_id: str
    pdm_endpoint: str
    token_id: str
    token_secret_env: str


def pdm_conn_of(row: tuple[Any, ...]) -> PdmConn | None:
    """Pure: turn a `(cluster_id, pdm_endpoint, pdm_token_id, pdm_token_secret_env)` row into a `PdmConn`,
    or None if ANY of the three PDM fields is missing/empty (not migration-capable). The host-safety check
    and secret resolution happen in `from_ledger`; this is the presence gate, unit-testable without a DB."""
    cluster_id, endpoint, token_id, secret_env = row
    if not (endpoint and token_id and secret_env):
        return None
    return PdmConn(cluster_id=cluster_id, pdm_endpoint=endpoint,
                   token_id=token_id, token_secret_env=secret_env)


class PdmRegistry:
    """A map of cluster_id → `pdm.Pdm` driver — the PDM appliance that fronts each migration-capable
    cluster. Construct directly from a dict (tests/harnesses point it at a mock PDM) or from the ledger via
    `from_ledger`. Hot-reloadable: the contents live behind ONE dict reference that `adopt` swaps atomically,
    so a concurrent reader (the daemon's migration step) sees the whole old fleet or the whole new one."""

    def __init__(self, drivers: Mapping[str, pdm.Pdm]) -> None:
        self._drivers: dict[str, pdm.Pdm] = dict(drivers)

    def get(self, cluster_id: str | None) -> pdm.Pdm | None:
        """The PDM driver fronting `cluster_id`, or None if this cluster has no (valid) PDM config — the
        migration for a guest on such a cluster is left pending rather than actuated. A None cluster_id
        (an unrouted source node) is a clean miss."""
        if cluster_id is None:
            return None
        return self._drivers.get(cluster_id)

    def cluster_ids(self) -> list[str]:
        """The clusters that have a usable PDM driver (i.e. can be a migration source)."""
        return sorted(self._drivers)

    def adopt(self, other: PdmRegistry) -> None:
        """Atomically take on another registry's drivers — a hot-reload with no restart. The swap is a
        SINGLE reference assignment (atomic under the GIL), so a concurrent migration step observes either
        the whole old fleet or the whole new one. The displaced `pdm.Pdm` drivers hold no connection (each
        REST call is stateless), so they need no close."""
        self._drivers = other._drivers

    def __len__(self) -> int:
        return len(self._drivers)


def from_ledger(conn: psycopg.Connection[Any], *, env: Mapping[str, str] | None = None) -> PdmRegistry:
    """Build the PDM registry from the `cluster` rows. For every cluster with a COMPLETE PDM config
    (endpoint + token id + secret env), a valid endpoint host, and a resolvable secret, construct a
    `pdm.Pdm`; skip the rest. A cluster with NO PDM fields is skipped silently (it's just PVE-only); a
    cluster with PARTIAL config, a bad endpoint host, or an unset secret env is logged ERROR once (a likely
    operator mistake) — but never raises, so one misconfigured cluster can't blank the registry. An empty
    result is normal (PDM simply not configured). Commits to end the read transaction. `env` overrides
    `os.environ` for testing."""
    resolve = os.environ if env is None else env
    rows = conn.execute(
        "SELECT id, pdm_endpoint, pdm_token_id, pdm_token_secret_env FROM cluster ORDER BY id"
    ).fetchall()
    conn.commit()

    drivers: dict[str, pdm.Pdm] = {}
    misconfigured: list[str] = []
    for row in rows:
        cluster_id = row[0]
        cfg = pdm_conn_of(tuple(row))
        if cfg is None:
            # Distinguish "no PDM at all" (normal — a PVE-only cluster, no noise) from PARTIAL config
            # (endpoint/token/secret — some set, not all — a likely operator mistake worth flagging).
            if any((row[1], row[2], row[3])):
                misconfigured.append(cluster_id)
                log.error("cluster %s has PARTIAL PDM config (need all of pdm_endpoint + pdm_token_id + "
                          "pdm_token_secret_env) — not migration-capable", cluster_id,
                          extra={"cluster_id": cluster_id})
            continue
        if not nodes.valid_pve_host(cfg.pdm_endpoint):
            # Reuse the PVE host-safety guard: a bare host/IP only, so a typo'd/hostile pdm_endpoint can't
            # redirect the daemon's PDM-TOKEN-bearing request elsewhere (the "config-as-data, VALIDATED"
            # doctrine — mirrors nodes.from_ledger's pve_host check and set_cluster's write-time guard).
            misconfigured.append(cluster_id)
            log.error("cluster %s: pdm_endpoint %r is not a bare hostname/IP — refusing to build a PDM "
                      "driver (won't send the PDM token there)", cluster_id, cfg.pdm_endpoint,
                      extra={"cluster_id": cluster_id})
            continue
        secret = resolve.get(cfg.token_secret_env)
        if not secret:
            misconfigured.append(cluster_id)
            log.error("cluster %s: PDM token secret env %r is unset — cannot build its PDM driver "
                      "(check this daemon's environment)", cluster_id, cfg.token_secret_env,
                      extra={"cluster_id": cluster_id})
            continue
        drivers[cfg.cluster_id] = pdm.Pdm(cfg.pdm_endpoint, cfg.token_id, secret)
    reg = PdmRegistry(drivers)
    if drivers:
        log.info("PDM registry built: %d migration-capable cluster(s) %s", len(drivers), sorted(drivers))
    if misconfigured:
        log.error("%d cluster(s) have a broken PDM config %s — cross-cluster migration FROM them is "
                  "inactive; fix the pdm_endpoint/token/secret, then SIGHUP (or wait a reload cadence)",
                  len(misconfigured), sorted(misconfigured), extra={"misconfigured": misconfigured})
    return reg
