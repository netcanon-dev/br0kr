"""mcp_operator_tools.py — the operator-only MCP tools (review UX-02; split out of mcp_server.py, review ARCH-03).

Served only by the operator gateway (BR0KR_MCP_OPERATOR_TOOLS): cross-tenant infrastructure actions an operator token
may call and a tenant token never can (cordon a node, pin any lease, verify and discover storage reachability,
migrate and replicate, force a stuck build or replication to fail). Each function authorizes through
`mcp_common._authorize`, delegates to br0kr/admin.py, pin.py, placement.py, gateway.py or replication.py, commits
before its best-effort audit row, and returns its plain answer. What an agent reads about each tool, its tier and its
verdict are its row in br0kr/verbs.py; the design notes that once crowded the descriptions are the comments above
each function.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from mcp.server.fastmcp import Context

from br0kr import (
    admin,
    gateway,
    mcp_common,
    nodes,
    pin,
    placement,
    reasons,
    replication,
)

log = logging.getLogger(__name__)


def node_cordon(ctx: Context, node_id: str, cordoned: bool) -> dict[str, Any]:
    """The node_cordon tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    gate = mcp_common._authorize(ctx, "node_cordon")
    if isinstance(gate, dict):
        return gate
    with mcp_common._conn() as c:
        r = admin.set_node_cordon(c, node_id, cordoned=cordoned)
        c.commit()   # admin.set_node_cordon does NOT self-commit (the caller owns the txn — UNLIKE every other
        #              audited delegate). Commit the cordon BEFORE the best-effort audit so a failing
        #              audit.record's rollback can only ever discard its OWN empty txn, never the durable
        #              cordon UPDATE. Without this, a pre-0023 ledger's audit failure would silently roll back
        #              the cordon while the verb still returned updated:true.
        if r.get("updated"):   # `gate` is the operator Principal here (project_id None → NULL in the row)
            mcp_common._audit_write(c, gate, "node_cordon", target=node_id,
                         params={"node_id": node_id, "cordoned": cordoned})
        return r


def pin_lease(ctx: Context, lease_id: int, pinned: bool) -> dict[str, Any]:
    """The pin_lease tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    gate = mcp_common._authorize(ctx, "pin_lease")
    if isinstance(gate, dict):
        return gate
    with mcp_common._conn() as c:
        r = pin.set_pinned(c, lease_id, pinned=pinned)
        c.commit()   # pin.set_pinned does NOT self-commit; commit (and free its row locks) BEFORE the best-effort
        #              audit, so an audit failure can only ever discard its own empty txn (as node_cordon does)
        if r.get("updated"):
            mcp_common._audit_write(c, gate, "pin_lease", target=str(lease_id),
                         params={"lease_id": lease_id, "pinned": pinned})
        return r


# Design notes for this tool (moved out of its MCP description, review UX-02; the description an
# agent sees is its row in br0kr/verbs.py):
#   OPERATOR only: reconcile br0kr's DECLARED storage reachability against what PVE actually reports, and
#   AUTO-QUARANTINE any (storage, node) pair on HARD drift evidence so every subsequent templated lease over
#   that pair is refused up front (fail-closed) until an operator clears it (clear_storage_node_quarantine).
#   Two kinds of hard evidence quarantine: PVE reports the pool EXPLICITLY DISABLED (enabled=0), or its live
#   `content` has DROPPED a CLONE-relevant DECLARED token ({images}) — the auto-narrow companion to the 0041
#   content gate (a pool declared images-capable but no longer reporting `images` would pass the clone gate at
#   admit then fail at PVE disk-create; narrowing closes that grant-then-kill). Keyed on the CLONE requirement
#   {images} because the reachability pair is the clone-placement gate (a build-only `import` drop leaves the pool
#   fully clone-capable, so it stays report-only + a legible build failure — never a fleet-wide clone outage).
#   Read-only against PVE (one GET per served node) and it NEVER WIDENS (never marks a pair reachable). Optionally
#   scope to one `cluster_id`. A pool PVE OMITS, reports merely INACTIVE (enabled but not mounted), drops only a
#   build-only/non-gate token, or whose `content` is empty/unreadable this poll, is REPORTED but NOT quarantined —
#   PVE ACL-filters its storage list per pool (so absence is ambiguous), inactive is often transient, and a
#   positive observed content set is required before taking a pool offline for the fleet; the post-clone
#   disk-location assertion stays the hard per-clone
#   tripwire. If the token lacks the `Datastore.Audit` ACL on /storage the verb degrades gracefully:
#   {ok:false, reason:'acl_insufficient', required:'Datastore.Audit on /storage'} and quarantines NOTHING (it
#   gathers every node's storage list BEFORE any write, so an ACL gap on ANY node aborts before a single
#   quarantine — true all-or-nothing). On success returns {ok:true, checked_nodes, unreachable_nodes,
#   quarantined, content_quarantined, absent, inactive, present}; content_quarantined entries carry the `missing`
#   gate tokens; unreachable_nodes are nodes whose PVE read transiently failed (skipped this run, not
#   quarantined); checked_nodes=[] means this process built no PVE drivers (e.g. its PVE token env is unset).
#   Requires an OPERATOR-tier token.
def verify_declarations(ctx: Context, cluster_id: str | None = None) -> dict[str, Any]:
    """The verify_declarations tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    gate = mcp_common._authorize(ctx, "verify_declarations")
    if isinstance(gate, dict):
        return gate
    with mcp_common._conn() as c:
        registry = nodes.from_ledger(c, env=os.environ)
        r = placement.verify_declarations(c, registry, cluster_id=cluster_id)
        c.commit()   # verify_declarations quarantines via quarantine_pair + audit.record (which self-commits
        #              each pair); this final commit is defensive, mirroring node_cordon's commit-before-audit
        #              discipline for a delegate whose writes don't ride a single owned txn.
        mcp_common._audit_write(c, gate, "verify_declarations", target=cluster_id,
                     params={"cluster_id": cluster_id, "ok": r.get("ok"),
                             "quarantined": len(r.get("quarantined", [])),
                             "content_quarantined": len(r.get("content_quarantined", []))})
        return r


# Design notes for this tool (moved out of its MCP description, review UX-02; the description an
# agent sees is its row in br0kr/verbs.py):
#   OPERATOR only: the WIDEN-half twin of verify_declarations (0041/PR-B) — read every served node's live PVE
#   storage listing and DIFF it against br0kr's declared reachability, returning a structured drift report the
#   operator acts on. Unlike verify_declarations, this WRITES NOTHING (discovery observes; the operator confirms
#   via the dashboard's add/remove reachability + set-content verbs — the operator-DECLARED trust boundary, so a
#   PVE listing never machine-widens the gate). Optionally scope to one `cluster_id`. Report buckets:
#   `known_pool_new_node` (a modeled pool PVE shows on a node with no declaration — a one-confirm widen),
#   `unknown_pool` (a PVE pool with no storage row — a create-storage recipe with observed type/content as
#   prefill), `declared_disabled` (PVE reports the declared pool enabled=0 — hard, but only REPORTED here),
#   `declared_absent` (PVE omits it — ambiguous: per-pool Datastore.Audit filtering makes absence never removal
#   evidence), `declared_inactive` (present+enabled but not mounted — soft), `quarantined_but_present` (a
#   quarantine PVE now contradicts — clear it; EXCLUDES a pair still dropping a clone token, whose quarantine is a
#   valid verify content auto-narrow surfaced in `content_report`, not clear-me), `content_report` (declared
#   content_types vs observed PVE content drift). Plus first-class `acl_denied_nodes` (a node whose WHOLE listing
#   was ACL-denied — never folded into
#   'absent'), `unreachable_nodes` (transient), and `unserved_nodes` (in-scope ledger nodes THIS daemon doesn't
#   serve — so a second daemon's blind spot can't make the report silently lie). checked_nodes=[] means this
#   process built no PVE drivers. The report ALSO carries the NETWORK read-side (0041/PR-C, the deliberately
#   PARTIAL vmbr parity): `bridge_menu` (the raw per-node bridge menu — enumeration only, NO bridge→logical-network
#   mapping is ever proposed), `declared_missing_bridge` (a declared `network_node` bridge PVE does not present —
#   ambiguous:false, report-only; the post-clone net0 assertion stays the sole quarantiner),
#   `network_quarantined_but_present` (a quarantined network pair whose bridge PVE now presents again — a possibly
#   stale quarantine the operator clears), `bridge_suggestions` (§6 — NAME-COINCIDENCE prefills for
#   `set_network_node`: for a node lacking a `network_node` row for a logical net declared elsewhere, a bridge
#   whose NAME matches one already mapped to that net, tagged `basis:"name_coincidence"` (+ `cross_cluster` when the
#   evidence is from another cluster). **A HINT, NEVER a mapping proposal — same-name ≠ same-L2 is unobservable
#   from PVE. The operator MUST VERIFY the L2 OUT OF BAND before confirming via `set_network_node`: that is the
#   ONLY defense — the post-clone net0 assertion does NOT catch a wrong-L2 name match (it fails only on bridge
#   ABSENCE, and a name-coincidence bridge is present by construction). Report-only, NOT a drift/dismissal
#   bucket.**), and the network node buckets
#   `network_checked_nodes`/`network_acl_denied_nodes`/`network_unreachable_nodes`. Requires an OPERATOR-tier
#   token.
def discover_reachability(ctx: Context, cluster_id: str | None = None) -> dict[str, Any]:
    """The discover_reachability tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    gate = mcp_common._authorize(ctx, "discover_reachability")
    if isinstance(gate, dict):
        return gate
    with mcp_common._conn() as c:
        registry = nodes.from_ledger(c, env=os.environ)
        r = placement.discover_reachability(c, registry, cluster_id=cluster_id)
        # Pure read — nothing to commit for the verb itself; audit the operator's discovery run (self-commits).
        mcp_common._audit_write(c, gate, "discover_reachability", target=cluster_id,
                     params={"cluster_id": cluster_id, "ok": r.get("ok"),
                             "new_node": len(r.get("known_pool_new_node", [])),
                             "unknown_pool": len(r.get("unknown_pool", [])),
                             "dismissed": len(r.get("dismissed", []))})
        return r


# Design notes for this tool (moved out of its MCP description, review UX-02; the description an
# agent sees is its row in br0kr/verbs.py):
#   OPERATOR only: DISMISS a known-benign `discover_reachability` finding (0044/PR-H) so it is FILTERED OUT of
#   future reports (moved to the `dismissed` roster) instead of re-surfacing every run and burying live drift —
#   e.g. a node-local `local`/`local-zfs`/`rpool` scratch pool that will never be declared shared. The finding
#   IDENTITY is (`kind`, `node_id`, `subject`) copied from the report row: `kind` is the drift bucket name
#   (`unknown_pool`, `known_pool_new_node`, `declared_disabled`, … — a non-drift or unknown kind → `invalid_kind`);
#   `subject` is that bucket's discriminator (a pve_name for `unknown_pool`, a storage_id for the declared_* /
#   known_pool_new_node / content_report buckets, a logical_network_id for the network buckets). PURELY a
#   reporting suppression — NO admission/placement path reads the dismissal, so it can never change the gate. `note`
#   is an optional free-text operator note (surfaced in the roster; distinct from the returned `reason` refusal
#   code). Idempotent (a re-dismiss is `dismissed=false`, not an error); `unknown_node` if the node is gone.
#   Returns {kind, node_id, subject, dismissed, reason}. Requires an OPERATOR-tier token.
def dismiss_discovery_finding(ctx: Context, kind: str, node_id: str, subject: str,
                              note: str | None = None) -> dict[str, Any]:
    """The dismiss_discovery_finding tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    gate = mcp_common._authorize(ctx, "dismiss_discovery_finding")
    if isinstance(gate, dict):
        return gate
    with mcp_common._conn() as c:
        r = admin.dismiss_discovery_finding(c, kind=kind, node_id=node_id, subject=subject, note=note,
                                            by=str(gate.token_id))
        c.commit()   # admin.dismiss_discovery_finding does NOT self-commit — commit the INSERT BEFORE the audit
        if r.get("dismissed"):   # audit only a real new dismissal (skip idempotent no-ops + validation refusals)
            mcp_common._audit_write(c, gate, "dismiss_discovery_finding", target=node_id,
                         params={"kind": kind, "node_id": node_id, "subject": subject, "note": note})
        return r


def clear_discovery_dismissal(ctx: Context, kind: str, node_id: str, subject: str) -> dict[str, Any]:
    """The clear_discovery_dismissal tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    gate = mcp_common._authorize(ctx, "clear_discovery_dismissal")
    if isinstance(gate, dict):
        return gate
    with mcp_common._conn() as c:
        r = admin.clear_discovery_dismissal(c, kind=kind, node_id=node_id, subject=subject)
        c.commit()   # admin.clear_discovery_dismissal does NOT self-commit — commit the DELETE BEFORE the audit
        if r.get("cleared"):   # audit only a real clear (skip unknown_dismissal no-ops)
            mcp_common._audit_write(c, gate, "clear_discovery_dismissal", target=node_id,
                         params={"kind": kind, "node_id": node_id, "subject": subject})
        return r


# Design notes for this tool (moved out of its MCP description, review UX-02; the description an
# agent sees is its row in br0kr/verbs.py):
#   OPERATOR only: REPORT the PVE NIC bridge a template's net0 is baked to (0038 → advisory in 0039) by reading
#   net0 from a live READY instance's PVE config — the diagnostic that tells an operator which physical bridge to
#   declare on each node for the template's LOGICAL NETWORK (`set_network_node`) and which identity to point the
#   template at (`set_template_logical_network`). READ-ONLY: under the logical-network model the identity a
#   template's net0 is on is an operator CHOICE (the same raw bridge could be 'lan' or 'dmz'), so this reports
#   rather than writes. Picks a READY instance of the template with a node + vmid and reads net0 via that node's
#   driver. Returns {ok:true, template_id, bridge, logical_network_id} (the template's CURRENT declared identity,
#   None if undeclared); else {ok:false, reason}: 'unknown_template', 'no_placed_instance' (no READY instance
#   bound to a node+vmid to read from), 'node_unroutable' (this process has no driver for that node), or
#   'no_bridge_on_net0' (net0 carries no bridge= to read). Requires an OPERATOR-tier token.
def detect_template_bridge(ctx: Context, template_id: str) -> dict[str, Any]:
    """The detect_template_bridge tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    gate = mcp_common._authorize(ctx, "detect_template_bridge")
    if isinstance(gate, dict):
        return gate
    with mcp_common._conn() as c:
        trow = c.execute("SELECT logical_network_id FROM template WHERE id = %s", (template_id,)).fetchone()
        if trow is None:
            return {"ok": False, "reason": reasons.UNKNOWN_TEMPLATE}
        irow = c.execute(
            "SELECT node_id, vmid FROM template_instance "
            "WHERE template_id = %s AND instance_state = 'READY' AND node_id IS NOT NULL AND vmid IS NOT NULL "
            "ORDER BY id LIMIT 1", (template_id,)).fetchone()
        if irow is None:
            return {"ok": False, "reason": reasons.NO_PLACED_INSTANCE}
        node_id, vmid = irow
        registry = nodes.from_ledger(c, env=os.environ)
        driver = registry.get(node_id)
        if driver is None:
            return {"ok": False, "reason": reasons.NODE_UNROUTABLE, "node_id": node_id}
        bridge = driver.read_guest_net_bridge(int(vmid))
        if bridge is None:
            return {"ok": False, "reason": reasons.NO_BRIDGE_ON_NET0, "vmid": int(vmid)}
        return {"ok": True, "template_id": template_id, "bridge": bridge, "logical_network_id": trow[0]}


def force_fail_build(ctx: Context, build_id: int, reason: str | None = None) -> dict[str, Any]:
    """The force_fail_build tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    gate = mcp_common._authorize(ctx, "force_fail_build")
    if isinstance(gate, dict):
        return gate
    if reason is not None:
        reason = reason[:500]   # bound the operator note (no transport cap on the MCP arg) — matches the
        #                         `error` column's 500-char truncation, so the audited param can't bloat.
    with mcp_common._conn() as c:
        r = admin.force_fail_build(c, build_id, reason)
        c.commit()   # admin.force_fail_build does NOT self-commit — commit the CAS BEFORE the best-effort audit
        #              (same discipline as node_cordon) so a failing audit.record can only discard its own txn.
        if r.get("forced"):   # audit ONLY a real force (skip unknown/already-terminal no-ops); `gate` is operator
            mcp_common._audit_write(c, gate, "force_fail_build", target=str(build_id),
                         params={"build_id": build_id, "reason": reason})
        return r


# Design notes for this tool (moved out of its MCP description, review UX-02; the description an
# agent sees is its row in br0kr/verbs.py):
#   OPERATOR only: relocate a running lease's guest to a node in ANOTHER cluster, race-free — the broker
#   reserves the target through the proven admit() (so a concurrent migration/admission can't over-commit it)
#   then moves the guest fence-checked. Control plane: it RECORDS the intent; the leader daemon does the
#   reservation + the move (inert unless BR0KR_MIGRATE_LIMIT>0 and the source cluster has PDM config). Acts on
#   ANY lease (cross-tenant — an operator is not project-bound). Idempotent on idem_key (a retry returns the
#   same migration; the same key for a different ask is idem_key_conflict). At most one migration may be in
#   flight per lease: a re-request to the SAME target returns the existing saga, a retarget while one is active
#   is refused migration_in_progress (cancel the active saga to retarget). Returns {migration_id, lease_id,
#   target_node, state, accepted, reason}; a rejection's reason says why (lease_not_active, same_cluster,
#   storage_migration_unsupported, unknown_target_node, target_not_provisionable, source_cluster_unknown,
#   migration_in_progress). Poll it with migration_status. Requires an OPERATOR-tier token (read/consume/provision
#   get {"error":"forbidden_scope"}).
def migrate_lease(ctx: Context, idem_key: str, lease_id: int, target_node_id: str) -> dict[str, Any]:
    """The migrate_lease tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    gate = mcp_common._authorize(ctx, "migrate_lease")
    if isinstance(gate, dict):
        return gate
    with mcp_common._conn() as c:
        r = gateway.migrate_lease(c, idem_key=idem_key, lease_id=lease_id, target_node_id=target_node_id)
        if r.get("accepted"):
            mcp_common._audit_write(c, gate, "migrate_lease", target=str(lease_id),
                         params={"idem_key": idem_key, "lease_id": lease_id, "target_node_id": target_node_id})
        return r


def cancel_migration(ctx: Context, migration_id: int) -> dict[str, Any]:
    """The cancel_migration tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    gate = mcp_common._authorize(ctx, "cancel_migration")
    if isinstance(gate, dict):
        return gate
    with mcp_common._conn() as c:
        r = gateway.cancel_migration(c, migration_id)
        if r.get("cancelled"):   # audit only a REAL cancel; a refusal (copy_in_flight/terminal/unknown) is a no-op
            mcp_common._audit_write(c, gate, "cancel_migration", target=str(migration_id),
                         params={"migration_id": migration_id})
        return r


# Design notes for this tool (moved out of its MCP description, review UX-02; the description an
# agent sees is its row in br0kr/verbs.py):
#   OPERATOR only: request an async REPLICATION of a template onto another storage pool, so a node that
#   cannot reach the original can clone from a local copy. CONTROL PLANE: this RECORDS the intent (a REQUESTED
#   job); the leader daemon copies the guest once the engine is enabled (`replication_enabled` in the response;
#   a REQUESTED job simply waits until then). At most one live job per (template, target cluster, target pool):
#   a duplicate is refused `replication_in_progress`. `mechanism` is `clone_shared` (an intra-cluster full-clone
#   onto a pool reachable from the SOURCE node) or `rebuild_source` (a cross-cluster provenance rebuild — replay
#   the template's recorded build on a node that reaches the target pool; needs NO source instance but requires
#   the template to carry build provenance and to be added to the `replication_mechanisms` knob allowlist).
#   `force_source_dirty` (rebuild_source only) rebuilds a hand-modified (`source_dirty`) template anyway — the
#   audited operator override for the `source_hand_modified` refusal. `project_id` (optional) attributes the
#   copy to a tenant and applies that project's image governance; omit it for an operator (governance/quota-
#   exempt) copy. Returns {replication_id, requested, reason, job_state, mechanism, size_gb, replication_enabled,
#   ...}; a rejection's `reason` says why (mechanism_not_allowed / mechanism_not_implemented / unknown_template /
#   unknown_cluster / unknown_storage / storage_unbound / storage_wrong_cluster / already_present /
#   no_replicable_source / storage_unreachable / storage_capability_undeclared / storage_not_import_capable
#   (rebuild_source: the target pool lacks the declared {import, images} content, 0041) / source_not_replicable /
#   source_hand_modified / no_replication_path / source_unsized / unknown_project / template_forbidden /
#   replication_in_progress).
#   Poll it with replication_status; cancel a REQUESTED one with cancel_replication. Requires an OPERATOR-tier
#   token (read/consume/provision get {"error":"forbidden_scope"}).
def replicate_template(
    ctx: Context, template_id: str, target_cluster_id: str, target_storage_id: str,
    project_id: str | None = None, mechanism: str = "clone_shared", force_source_dirty: bool = False,
) -> dict[str, Any]:
    """The replicate_template tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    gate = mcp_common._authorize(ctx, "replicate_template")
    if isinstance(gate, dict):
        return gate
    with mcp_common._conn() as c:
        r = gateway.request_replication(
            c, template_id=template_id, target_cluster_id=target_cluster_id,
            target_storage_id=target_storage_id, requested_by=f"agent:{gate.token_id}",
            mechanism=mechanism, project_id=project_id, force_source_dirty=force_source_dirty)  # self-commits
        if r.get("requested"):   # audit ONLY a real request (a refusal is returned to the operator verbatim)
            mcp_common._audit_write(c, gate, "replicate_template", target=template_id,
                         params={"template_id": template_id, "target_cluster_id": target_cluster_id,
                                 "target_storage_id": target_storage_id, "mechanism": mechanism,
                                 "force_source_dirty": force_source_dirty, "project_id": project_id,
                                 "replication_id": r.get("replication_id")})
        return r


def cancel_replication(ctx: Context, replication_id: int) -> dict[str, Any]:
    """The cancel_replication tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    gate = mcp_common._authorize(ctx, "cancel_replication")
    if isinstance(gate, dict):
        return gate
    with mcp_common._conn() as c:
        r = replication.cancel_replication(c, replication_id)
        c.commit()   # cancel_replication does NOT self-commit — commit the CAS BEFORE the best-effort audit
        if r.get("cancelled"):   # audit only a REAL cancel (skip unknown / not-cancellable no-ops)
            mcp_common._audit_write(c, gate, "cancel_replication", target=str(replication_id),
                         params={"replication_id": replication_id})
        return r


def prune_replications(ctx: Context, older_than_days: int, cluster_id: str | None = None) -> dict[str, Any]:
    """The prune_replications tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    gate = mcp_common._authorize(ctx, "prune_replications")
    if isinstance(gate, dict):
        return gate
    with mcp_common._conn() as c:
        r = replication.prune_replications(c, older_than_days, cluster_id=cluster_id)
        c.commit()   # prune_replications does NOT self-commit — commit the delete BEFORE the best-effort audit
        mcp_common._audit_write(c, gate, "prune_replications", target=cluster_id,
                     params={"older_than_days": older_than_days, "cluster_id": cluster_id,
                             "pruned": r.get("pruned")})
        return r


def force_fail_replication(ctx: Context, replication_id: int, reason: str | None = None) -> dict[str, Any]:
    """The force_fail_replication tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    gate = mcp_common._authorize(ctx, "force_fail_replication")
    if isinstance(gate, dict):
        return gate
    with mcp_common._conn() as c:
        r = replication.force_fail_replication(c, replication_id, reason)
        c.commit()   # force_fail_replication does NOT self-commit — commit the CAS BEFORE the settle + audit
        # Free the held byte reservation UNCONDITIONALLY after the commit — idempotent, and on the already_terminal
        # retry this is the REPAIR path for a crash between a PRIOR CAS commit and its settle (which would else
        # leave the reservation ADMITTED until TTL, refusing a re-request target_storage_insufficient).
        # A no-op on an unknown id / already-settled reservation.
        replication.settle_forced_reservation(c, replication_id)
        if r.get("forced"):   # audit only a REAL force-fail (skip unknown / already-terminal no-ops)
            mcp_common._audit_write(c, gate, "force_fail_replication", target=str(replication_id),
                         params={"replication_id": replication_id, "reason": reason,
                                 "prior_state": r.get("prior_state"), "vmid": r.get("vmid")})
        return r


# Design notes for this tool (moved out of its MCP description, review UX-02; the description an
# agent sees is its row in br0kr/verbs.py):
#   OPERATOR only: SET (or CLEAR) a template's `source_dirty` hand-modification attestation (PR-6, §5.2).
#   Set `dirty=true` after you HAND-EDIT a built template's guest in PVE (e.g. installing qemu-guest-agent per
#   the standing rule): `rebuild_source` then REFUSES that template `source_hand_modified`, because rebuilding
#   from its recorded image would silently reproduce the PRISTINE guest WITHOUT your fix. `dirty=false` clears
#   it (a fresh build also auto-clears it). br0kr cannot DETECT an in-guest edit — this flag is the operator's
#   attestation, so the standing rule is 'any hand modification of a built template's guest ⇒ mark it dirty in
#   the same sitting'; an audited `replicate_template(..., force_source_dirty=true)` rebuilds a dirty template
#   anyway when the divergence is known-acceptable. Idempotent. Returns {template_id, updated, source_dirty,
#   reason} (`unknown_template` if absent). Requires an OPERATOR-tier token (read/consume/provision get
#   {"error":"forbidden_scope"}).
def mark_template_source_dirty(ctx: Context, template_id: str, dirty: bool = True) -> dict[str, Any]:
    """The mark_template_source_dirty tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    gate = mcp_common._authorize(ctx, "mark_template_source_dirty")
    if isinstance(gate, dict):
        return gate
    with mcp_common._conn() as c:
        r = admin.set_template_source_dirty(c, template_id, dirty=dirty)
        c.commit()   # set_template_source_dirty does NOT self-commit — commit the UPDATE BEFORE the audit
        if r.get("updated"):   # audit only a real change (skip unknown_template no-ops)
            mcp_common._audit_write(c, gate, "mark_template_source_dirty", target=template_id,
                         params={"template_id": template_id, "dirty": dirty})
        return r
