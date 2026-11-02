"""pdm.py — the PdmDriver: br0kr's cross-CLUSTER inventory + live-migration layer, via the
Proxmox **Datacenter Manager** (PDM) REST API.

Where `br0kr/pve.py` is scoped to ONE node in ONE cluster (it creates/destroys guests, the
*create path*), PDM sits ABOVE clusters: a single appliance that registers many PVE clusters
as "remotes" and offers (a) one aggregated view of every guest across them and (b) **remote
migration** — moving a running guest from one cluster to another. This driver wraps exactly
those two capabilities; it is **NOT a second create path** (guests are still created per-cluster
by `pve.Pve`) and it is **NOT a capacity authority** (the ledger remains the sole admission
authority — PDM inventory is for reconciliation/observability, never to decide admission).

Two capabilities, mirroring pve.py's split of pure-decision vs thin-REST:

  inventory()                      → every qemu guest across all remotes, normalized
  fenced_inventory(items)          → (remote, node, vmid, lease_id) for br0kr-fenced guests —
                                     the cross-cluster analogue of pve.list_fenced
  fenced_migrate(src, vmid, lease, → migrate a guest to another cluster ONLY if its fence
                 dst, dst_node)      matches the lease — "migrated" / "fence_mismatch" /
                                     "same_cluster" / "gone" (+ "migrate_failed"/"_timeout")

**The fence travels with the guest.** A remote migration copies the guest's config (including
its PVE tags) to the destination, so the lease-id fence tag `br0kr-lease-<id>` survives the
move and a stale destroy on EITHER cluster stays self-fencing — no re-stamp needed. We reuse
`pve.fence_matches`/`pve.parse_fence_tag` so the tag format is one fact, shared with the create
path; if it ever drifts, both break together (a round-trip test pins it).

**fenced_migrate refuses to move a guest that isn't ours**, exactly as `fenced_destroy` refuses
to delete one: it reads the guest's fence from the live inventory and migrates only on a match,
so a wrong/forgotten lease id can never relocate another lease's VM. It RAISES only on a
transient API error (the caller retries); a mismatch / absent / same-cluster guest is a normal,
logged, non-exceptional outcome.

──────────────────────────────────────────────────────────────────────────────────────────────
ASSUMED API CONTRACT — NOT YET LIVE-PROVEN. PDM is young and its REST surface is not yet frozen,
and the homelab has no PDM instance yet. So every endpoint path, field name, auth scheme, and
port below is an *assumption*, gathered in the `_API_*` constants and the `_normalize_*` helpers
so the live fix-up is a handful of one-line edits in ONE file. The full assumed shape is written
down in docs/pdm-api-contract.md. The pure decision functions (fence + placement verdicts) are
contract-INDEPENDENT and exhaustively unit-tested; the REST methods are proven against a MOCK PDM
that serves the assumed shape (scripts/harness_pdm.py) and must be re-proven against a real PDM
before the migration path is ENABLED. The driver is now CONSTRUCTED at runtime from cluster config
(br0kr/remotes.py builds one Pdm per PDM-configured cluster), but the daemon's migration step is
DISABLED BY DEFAULT (BR0KR_MIGRATE_LIMIT=0) and inert unless a cluster carries PDM config — so no live
PDM call happens until an operator opts in AND has reconciled this contract against a real PDM.
──────────────────────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests

from br0kr import pve

log = logging.getLogger(__name__)

# ── The assumed PDM REST contract (see the module banner + docs/pdm-api-contract.md) ──
# Centralised so a live mismatch is fixed HERE, in one place, not scattered through methods.
_PDM_PORT = 8443                  # PDM's default HTTPS API port (PVE is 8006); verify on the box.
# PDM is built on the Proxmox stack and is ASSUMED to accept the same API-token auth header as
# PVE (`PVEAPIToken=<id>=<secret>`). Keeping this scheme has a real safety dividend: the C2 log
# redactor (br0kr/logging_setup.py) already scrubs `PVEAPIToken=`, so a leaked log line stays
# redacted. If a live PDM turns out to require a different scheme (e.g. `PDMAPIToken=`), change
# THIS constant AND add the scheme to the redactor's pattern in the same commit.
_AUTH_SCHEME = "PVEAPIToken"
_API_REMOTES = "/remotes"         # GET → the registered remotes (each remote == a br0kr cluster)
_API_RESOURCES = "/resources"     # GET → one aggregated resource list across every remote
# Per-remote task status (to await a migration) and the remote-migration verb. `{remote}`/`{vmid}`
# are filled in; `{upid}` is the task id PDM returns for the migration.
_API_TASK_STATUS = "/remotes/{remote}/tasks/{upid}/status"
_API_REMOTE_MIGRATE = "/remotes/{remote}/qemu/{vmid}/remote-migrate"
# Resource records we manage. PDM's aggregate (like PVE's /cluster/resources) mixes node/storage/
# sdn/qemu/lxc records under a `type`; the broker provisions QEMU VMs, so inventory keeps only those.
_GUEST_TYPE = "qemu"


# ── Normalized inventory (pure — no I/O; unit-tested) ──────────────────────────────
@dataclass(frozen=True)
class InventoryItem:
    """One guest as br0kr sees it across the fleet, normalized from a PDM resource record.

    `remote` is the PDM remote id, which br0kr treats as the **cluster id** (the operator names
    PDM remotes to match `cluster.id` — documented in docs/pdm-api-contract.md). `tags` carries
    the fence (may be None if the aggregate omits it — see the live-verification note on
    `Pdm.inventory`). `maxcpu`/`maxmem_mb` are the guest's CONFIGURED size (not live usage)."""

    remote: str
    node: str
    vmid: int
    name: str | None
    status: str | None
    tags: str | None
    maxcpu: float
    maxmem_mb: int


def normalize_inventory(records: list[dict[str, Any]]) -> list[InventoryItem]:
    """Turn PDM's raw aggregated resource list into typed `InventoryItem`s — qemu guests only,
    deterministically ordered by (remote, node, vmid). Pure, so inventory parsing is unit-testable
    without a PDM.

    Defensive by construction (the aggregate is heterogeneous and field presence varies by PDM
    version): a record that is not a qemu guest, is missing the identity keys (`remote`/`node`/
    `vmid`), or carries a non-numeric `vmid`/`maxcpu`/`maxmem` (e.g. "N/A", a dict), is SKIPPED
    rather than crashing the whole inventory — one malformed row must not blind the broker to every
    other guest. `maxmem` is reported in BYTES (PVE convention) and converted to MiB; absent size
    fields default to 0. `tags`/`name`/`status` are carried through as-is (possibly None)."""
    items: list[InventoryItem] = []
    for r in records:
        if r.get("type") != _GUEST_TYPE:
            continue
        remote, node, vmid = r.get("remote"), r.get("node"), r.get("vmid")
        if remote is None or node is None or vmid is None:
            log.debug("skipping PDM resource record missing remote/node/vmid: %r", r)
            continue
        try:
            item = InventoryItem(
                remote=str(remote), node=str(node), vmid=int(vmid),
                name=r.get("name"), status=r.get("status"), tags=r.get("tags"),
                maxcpu=float(r.get("maxcpu") or 0), maxmem_mb=int(r.get("maxmem") or 0) // (1024 * 1024),
            )
        except (ValueError, TypeError):
            # A present-but-non-numeric vmid/maxcpu/maxmem would otherwise abort the WHOLE inventory
            # (and so `inventory`/`find_guest`/`fenced_migrate`). Skip just this record — the
            # skip-don't-crash promise must hold for malformed VALUES, not only missing keys.
            log.debug("skipping PDM resource record with a malformed numeric field: %r", r)
            continue
        items.append(item)
    items.sort(key=lambda i: (i.remote, i.node, i.vmid))
    return items


def fenced_inventory(items: list[InventoryItem]) -> list[tuple[str, str, int, int]]:
    """`(remote, node, vmid, lease_id)` for every guest carrying a br0kr fence tag — the
    cross-cluster analogue of `pve.list_fenced`, and the input a future cross-cluster orphan
    sweep would cross-check against the ledger. The lease id is read FROM each guest's own tag
    (via the shared `pve.parse_fence_tag`), so any teardown that follows is self-fencing. A guest
    with no br0kr tag (foreign / hand-made) is skipped — the fence is an allowlist, exactly as in
    the per-node sweep."""
    fenced: list[tuple[str, str, int, int]] = []
    for it in items:
        lease_id = pve.parse_fence_tag(it.tags)
        if lease_id is not None:
            fenced.append((it.remote, it.node, it.vmid, lease_id))
    return fenced


def migrate_decision(item: InventoryItem | None, lease_id: int, target_remote: str) -> str:
    """The fenced cross-cluster-migration verdict — pure, so it's exhaustively testable:
      • 'gone'           — no such guest in the inventory; nothing to migrate.
      • 'fence_mismatch' — the guest's fence isn't this lease's; REFUSE — moving it would relocate
                           another lease's VM (the migration analogue of fenced_destroy's refusal).
      • 'same_cluster'   — the guest already lives on `target_remote`; a cross-cluster migration is
                           a no-op here (intra-cluster moves are a PVE-native path, out of scope).
      • 'migrate'        — fence matches and the target is a different cluster; safe to migrate.
    Note this gates on SAFETY only (fence + is-it-actually-cross-cluster); whether to migrate
    online vs offline is the caller's choice from `item.status`, not a safety property."""
    if item is None:
        return "gone"
    if not pve.fence_matches(item.tags, lease_id):
        return "fence_mismatch"
    if item.remote == target_remote:
        return "same_cluster"
    return "migrate"


# ── The REST driver ────────────────────────────────────────────────────────────
class Pdm:
    """A thin PDM REST client. Auth is a Proxmox-style API token (see `_AUTH_SCHEME`); `verify=False`
    tolerates the appliance's self-signed cert on the LAN, matching `pve.Pve`. The HTTP verbs are
    split out so tests/the mock harness can stub them. `scheme`/`port` exist ONLY so the mock PDM
    (scripts/harness_pdm.py) can be reached over plain HTTP on a high port; production is always
    https on `_PDM_PORT`."""

    def __init__(self, endpoint: str, token_id: str, token_secret: str, *,
                 scheme: str = "https", port: int = _PDM_PORT, timeout: int = 30) -> None:
        self.base = f"{scheme}://{endpoint}:{port}/api2/json"
        self.timeout = timeout
        self._h = {"Authorization": f"{_AUTH_SCHEME}={token_id}={token_secret}"}

    def _get(self, path: str) -> Any:
        r = requests.get(self.base + path, headers=self._h, verify=False, timeout=self.timeout)
        r.raise_for_status()
        return r.json()["data"]

    def _post(self, path: str, data: dict[str, Any]) -> Any:
        r = requests.post(self.base + path, headers=self._h, data=data, verify=False, timeout=self.timeout)
        r.raise_for_status()
        return r.json()["data"]

    def list_remotes(self) -> list[str]:
        """The remote ids PDM has registered (each is a br0kr cluster). Used to confirm a target
        cluster is actually reachable through this PDM before attempting a migration."""
        return [str(r["id"]) for r in self._get(_API_REMOTES)]

    def resources(self) -> list[dict[str, Any]]:
        """The raw aggregated resource list across every remote — one call, PDM does the fan-out.
        Callers normally want `inventory()`; this is the unparsed form for diagnostics."""
        return list(self._get(_API_RESOURCES))

    def inventory(self) -> list[InventoryItem]:
        """Every qemu guest across the whole fleet, normalized (see `normalize_inventory`).

        LIVE-VERIFICATION NOTE: this trusts the `tags` field on each aggregate record. PVE's
        `/cluster/resources` does not always include tags (they live in per-guest config), and
        whether PDM's aggregate surfaces them is UNVERIFIED. If a live PDM omits tags here, the
        fence-dependent paths (`fenced_inventory`, `fenced_migrate`) need a per-guest config-read
        fallback — exactly as `pve.list_fenced` falls back today. That fallback is deliberately
        NOT built blind: it is the first thing to wire once a real PDM is available."""
        return normalize_inventory(self.resources())

    def find_guest(self, remote: str, vmid: int) -> InventoryItem | None:
        """The one guest at (remote, vmid) in the current inventory, or None if absent. The
        fence read that `fenced_migrate` checks before moving anything."""
        for it in self.inventory():
            if it.remote == remote and it.vmid == vmid:
                return it
        return None

    def wait(self, remote: str, upid: str, secs: int = 300,
             on_progress: Callable[[], None] | None = None) -> str:
        """Poll a PDM-tracked remote task to completion; returns its exitstatus ('OK' on success),
        'TIMEOUT' if it has not reached 'stopped' within the budget (distinct from a non-OK
        exitstatus — a still-running migration must NOT be treated as failed), or 'no_exitstatus'
        if a stopped task reports no exitstatus at all (the caller treats it as a failure — a
        stopped task with no success signal is never optimistically 'OK'). A transient status-GET
        error is retried, not fatal, so one network blip during a long migration doesn't abort the
        flow. Mirrors `pve.Pve.wait`; a cross-cluster migration is slow, so the default budget is
        larger.

        `on_progress` (optional) is called once per poll iteration — the migration caller passes the
        daemon's liveness heartbeat here so a multi-minute cross-cluster copy (which blocks this loop
        far longer than the daemon's stall floor) keeps the reconciler thread looking alive instead of
        being killed mid-migration. It runs BEFORE each status GET so even a stalled-network poll (which
        retries on the 1s sleep) still stamps progress."""
        path = _API_TASK_STATUS.format(remote=remote, upid=upid)
        for _ in range(secs):
            if on_progress is not None:
                on_progress()   # heartbeat: still awaiting the migration task (see the docstring)
            try:
                status = self._get(path)
            except requests.RequestException:
                time.sleep(1)
                continue
            if status.get("status") == "stopped":
                # Report the absent-exitstatus case as an explicit sentinel (the contract leaves the
                # exitstatus vocabulary unverified — see docs/pdm-api-contract.md) so fenced_migrate
                # maps it to a failure deliberately, not as an accident of str(None).
                exitstatus = status.get("exitstatus")
                return str(exitstatus) if exitstatus is not None else "no_exitstatus"
            time.sleep(1)
        return "TIMEOUT"

    def remote_migrate(
        self,
        source_remote: str,
        vmid: int,
        target_remote: str,
        target_node: str,
        *,
        target_vmid: int | None = None,
        online: bool = True,
        target_storage: str | None = None,
        target_bridge: str | None = None,
    ) -> str:
        """Kick off a cross-cluster (remote) migration via PDM and return the task UPID (on the
        SOURCE remote, which is where PDM runs/tracks the migration task). Thin — no fence check
        (that's `fenced_migrate`'s job); call this directly only when the fence is already known
        good. `target_vmid` lets the destination keep or reassign the id (the destination cluster
        has its own vmid namespace); `online` migrates a running guest live. Body field names are
        the assumed contract — see the module banner."""
        body: dict[str, Any] = {"target": target_remote, "target-node": target_node,
                                "online": 1 if online else 0}
        if target_vmid is not None:
            body["target-vmid"] = target_vmid
        if target_storage is not None:
            body["target-storage"] = target_storage
        if target_bridge is not None:
            body["target-bridge"] = target_bridge
        path = _API_REMOTE_MIGRATE.format(remote=source_remote, vmid=vmid)
        return str(self._post(path, body))

    def fenced_migrate_start(
        self,
        source_remote: str,
        vmid: int,
        lease_id: int,
        target_remote: str,
        target_node: str,
        **kwargs: Any,
    ) -> tuple[str, str | None]:
        """Fence-check the source guest and, if it is THIS lease's, START (POST) the cross-cluster migration
        — returning ('started', upid) WITHOUT waiting, so the caller can PERSIST the upid and, on a resume,
        RE-ATTACH via `wait()` instead of re-POSTing the copy or mis-reading an ambiguous source (review S07).
        Returns ('gone', None) / ('fence_mismatch', None) / ('same_cluster', None) when no task is started —
        the same fence verdicts `fenced_migrate` returns for those. `kwargs` pass through to `remote_migrate`
        (target_vmid / online / target_storage / target_bridge).

        The fence read + the verdict are computed BEFORE any state change, exactly as `pve.fenced_destroy`
        reads the tag before deleting — so a wrong lease id can never relocate another lease's VM. Raises
        only on a transient API error, which the caller may retry."""
        item = self.find_guest(source_remote, vmid)
        decision = migrate_decision(item, lease_id, target_remote)
        if decision == "gone":
            log.info("fenced_migrate_start: guest %s@%s already gone — nothing to migrate", vmid, source_remote,
                     extra={"lease_id": lease_id, "vmid": vmid})
            return "gone", None
        if decision == "fence_mismatch":
            log.warning(
                "fenced_migrate_start REFUSED: guest %s@%s fence != lease %s — not migrating someone else's VM",
                vmid, source_remote, lease_id, extra={"lease_id": lease_id, "vmid": vmid})
            return "fence_mismatch", None
        if decision == "same_cluster":
            log.info("fenced_migrate_start: guest %s already on %s — no cross-cluster move needed",
                     vmid, target_remote, extra={"lease_id": lease_id, "vmid": vmid})
            return "same_cluster", None
        upid = self.remote_migrate(source_remote, vmid, target_remote, target_node, **kwargs)
        log.info("fenced_migrate_start: POSTed migration of %s %s→%s (lease %s, task=%s)",
                 vmid, source_remote, target_remote, lease_id, upid, extra={"lease_id": lease_id, "vmid": vmid})
        return "started", upid

    def fenced_migrate(
        self,
        source_remote: str,
        vmid: int,
        lease_id: int,
        target_remote: str,
        target_node: str,
        *,
        on_progress: Callable[[], None] | None = None,
        **kwargs: Any,
    ) -> str:
        """Migrate a guest to another cluster ONLY if its fence matches `lease_id`, POSTing the task AND
        waiting for it — the one-shot convenience over `fenced_migrate_start` + `wait`. Returns
        'migrated' (fence matched, migration task succeeded), 'fence_mismatch' (the guest is
        another lease's — REFUSED, untouched), 'same_cluster' (already on the target — no-op),
        'gone' (no such guest), 'migrate_failed' (the task returned a failure exitstatus), or
        'migrate_timeout' (still running past the poll budget — the caller reconciles later, it is
        NOT a failure). Raises only on a transient API error, which the caller may retry. `kwargs`
        pass through to `remote_migrate` (target_vmid / online / target_storage / target_bridge);
        `on_progress` (keyword-only, NOT forwarded to remote_migrate) is the liveness heartbeat threaded
        into the migration-task `wait` so a long copy keeps the daemon alive.

        The resumable saga (br0kr/migration.py) uses the two-step `fenced_migrate_start` + `wait` instead, so
        it can persist the task UPID and re-attach on a resume (review S07); this composed one-shot is kept
        for callers that don't need resume (harnesses / a direct fence-and-move)."""
        verdict, upid = self.fenced_migrate_start(
            source_remote, vmid, lease_id, target_remote, target_node, **kwargs)
        if verdict != "started":
            return verdict   # gone / fence_mismatch / same_cluster — no task was started
        assert upid is not None
        rc = self.wait(source_remote, upid, on_progress=on_progress)
        if rc == "TIMEOUT":
            log.warning("fenced_migrate: migration of %s %s→%s still running past poll budget (task=%s)",
                        vmid, source_remote, target_remote, upid, extra={"lease_id": lease_id, "vmid": vmid})
            return "migrate_timeout"
        if rc != "OK":
            log.error("fenced_migrate: migration of %s %s→%s FAILED (task=%s rc=%s)",
                      vmid, source_remote, target_remote, upid, rc, extra={"lease_id": lease_id, "vmid": vmid})
            return "migrate_failed"
        log.info("fenced_migrate: guest %s migrated %s→%s (lease %s, task=%s)",
                 vmid, source_remote, target_remote, lease_id, upid, extra={"lease_id": lease_id, "vmid": vmid})
        return "migrated"
