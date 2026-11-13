"""verbs.py — the MCP verb registry: one row per tool, holding everything about it (review ARCH-03).

Each `Verb` row says, for one tool an agent can call:
- `tier`: who may call it (auth.VERB_TIER is this column, so `auth.tier_allows` reads it);
- `verdict`: how its answer says it worked, which envelope.py turns into `ok` and a `reason` (a key whose truth is
  the verdict, a function of the answer, or None for a tool that worked whenever it was not refused); plus
  `not_found`, the reason an empty lookup gets, and `bulk_item`, the key that says each item of a bulk answer worked;
- `kind` and `call`: how it runs. LOOKUP, OWN_LEASE and OWN_LEASES rows are built by br0kr/mcp_kinds.py around the
  function `call` names ("module.function" in br0kr); a HANDLER row's `call` is its hand-written tool function, in
  br0kr/mcp_tools.py (tenant) or br0kr/mcp_operator_tools.py (operator);
- `doc`: its description, what an agent reads before calling it. So every word an agent sees about the tools is in
  this file. The descriptions are pinned by tests/fixtures/mcp_contract.json (scripts/gen_mcp_contract.py).

An operator-tier tool is served only by the operator gateway (BR0KR_MCP_OPERATOR_TOOLS, review UX-02); every other
row by both. The rows are in registration order, which is the order an agent's tools/list shows.

Adding a read-only tool that fits a shape: write the function it calls (gateway.py or inventory.py) and add one row
here; a LOOKUP row states whose rows it returns, `scope=TENANT` for anything a project owns, `SHARED` for
infrastructure. A tool that fits no shape is a HANDLER: its function in mcp_tools.py or mcp_operator_tools.py, and
its row here. Either way, rerun scripts/gen_api_reference.py and scripts/gen_mcp_contract.py, whose output is what
changed for agents.

Tiers. A tenant token's tier is a rank (read ⊂ consume ⊂ provision): it may call any verb at or below it. 'read' is
inventory, capacity and status; 'consume' the lease lifecycle, always against the token's own project; 'provision'
self-service config within an operator-set ceiling. The operator scope is parallel, not a rank: an operator token is
cross-tenant infrastructure (project-less), may call the operator verbs and observe through the read verbs, and never
a tenant lease or quota verb. A verb with no row is denied (fail closed).

This module imports nothing but br0kr/reasons.py, so auth.py and envelope.py can read it.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from br0kr import reasons

Verdict = str | Callable[[Mapping[str, Any]], bool] | None

# How a row runs (br0kr/mcp_kinds.py builds the first three).
LOOKUP = "lookup"          # authorize, call(conn, **params) and return it, optionally wrapped / with acting_as
OWN_LEASE = "own_lease"    # the same for one lease_id the token's project owns, audited when `audit` says so
OWN_LEASES = "own_leases"  # the bulk form over lease_ids (br0kr/bulk.py), each id gated and audited alone
HANDLER = "handler"        # a hand-written tool function, named by `call`

# Whose rows a LOOKUP returns, which every LOOKUP row must state (a row with neither is refused when its tool is
# built). TENANT passes the token's own project as project_id, so the function returns that project's rows only
# (None, every project, for an operator token). SHARED passes none: shared infrastructure any token may see (nodes,
# clusters, pools, fleet health). The inventory readers take project_id=None to mean every tenant, so a tenant-data
# row that forgot its scope would leak across tenants; tests/unit/test_mcp_contract.py also requires TENANT exactly
# for the rows whose function takes a project_id.
TENANT = "tenant"
SHARED = "shared"

REQUIRED: Any = object()   # a Param with no default


@dataclass(frozen=True)
class Param:
    """One argument a LOOKUP row takes beside the connection, with its type (the tool's schema) and default."""

    name: str
    type: Any
    default: Any = REQUIRED


@dataclass(frozen=True)
class Verb:
    """One tool. See the module docstring for each field."""

    name: str
    tier: str
    verdict: Verdict
    kind: str
    call: str
    doc: str
    params: tuple[Param, ...] = ()
    scope: str | None = None            # LOOKUP: TENANT or SHARED, which it must state (see the constants)
    wrap: str | None = None             # LOOKUP: return {wrap: answer}
    acting_as: str | None = None        # LOOKUP: add the token's project as acting_as, "first" or "last"
    audit: Callable[[Mapping[str, Any]], bool] | None = None   # OWN_LEASE(S): does this answer (item) get an audit row
    not_found: str | None = None
    bulk_item: str | None = None


def _admitted_or_queued(answer: Mapping[str, Any]) -> bool:
    return bool(answer.get("admitted") or answer.get("queued"))


def _no_reason(answer: Mapping[str, Any]) -> bool:
    return answer.get("reason") is None


# Which answers of a lease verb are a change worth an audit row: a repeat, a no-op or a refusal is not.
def _released(answer: Mapping[str, Any]) -> bool:
    return bool(answer.get("released"))


def _cancel_acted(answer: Mapping[str, Any]) -> bool:
    return answer.get("outcome") not in (None, "noop")   # cancelled or released; not an already-terminal no-op


def _cancel_item_acted(item: Mapping[str, Any]) -> bool:
    return not item.get("error") and item.get("outcome") not in (None, "noop")


VERBS: tuple[Verb, ...] = (
    Verb("request_lease", "consume", _admitted_or_queued, HANDLER, "mcp_tools.request_lease", doc="""\
Reserve capacity for a VM on a node (race-free admission control).

idem_key MUST be a stable id for THIS logical request — retry with the SAME key to
safely get the same lease back (reusing it for a different ask is refused). The lease is
always counted against YOUR token's project, so project_id may be omitted (passing a
DIFFERENT project is refused with {"error":"project_forbidden"}); the request is also
refused if your project is over its cpu/mem/lease-count quota. Returns {ok, lease_id, admitted,
queued, idempotent, reason, pinned}: ok is true when admitted or queued. If not, reason says why (e.g.
insufficient_node_cpu, insufficient_project_cpu, project_lease_limit, idem_key_conflict,
lease_not_live); docs/reference/reason-codes.md lists every code with what to do about it.
Heartbeat the returned lease_id before its ttl_seconds elapse or the broker reclaims it.

LIMITS: ttl_seconds >= 10 (else invalid_ttl); storage_id and req_disk_gb > 0 go together (else
storage_without_disk / disk_without_storage); a target pool not declared for VM images may be
refused storage_not_capable.

Set queue=True to WAIT IN LINE instead of being rejected when there's no room right now:
the request is parked (state PENDING, queued=true) and the broker admits it automatically,
in arrival order, the moment capacity/quota frees. A queued answer also carries
queue_position (how many queued requests across the whole queue are older; advisory, null when
not known or no longer waiting) and next (what to do).
Then poll lease_status (PENDING → ADMITTED → ACTIVE) and call renew_lease to hold your place
(a crashed agent's spot frees); max_wait_seconds caps the total wait, after which the spot
expires (REJECTED). A queue at its depth cap returns reason 'queue_full'. cancel_lease drops a
spot you no longer want.

VM ACCESS (optional): to actually log into the VM, pass ssh_pubkey = YOUR OWN ssh PUBLIC key (e.g.
"ssh-ed25519 AAAA... you@host"). The broker injects it via cloud-init; you keep the private key. ci_user
sets the login username (the operator's default, usually br0kr, if omitted: lease_status then reports
ci_user as null); ip_config sets a static Proxmox ipconfig0 (e.g.
"ip=192.0.2.5/24,gw=192.0.2.1") instead of the DHCP default. Once the guest boots, call lease_status to get
its ip_address and ci_user, then ssh in. A malformed key/user/ip is refused (reason ssh_pubkey_malformed,
cloud_init_requires_pubkey, …). Omit ssh_pubkey for a bare reserved/fenced VM with no injected login.

OS TEMPLATE (optional): pass template_id (a catalog id from list_templates) to clone THAT image instead
of the operator default; node_id's cluster must hold a usable copy of the template — its catalog home OR a
replicated copy (cross-cluster placement) — else reason no_reachable_instance (an unknown id is
unknown_template). Selecting a private image your project has not been granted is
refused {"error":"template_forbidden"} — list_templates shows only the images you may use. The template
is part of the idempotency ask — changing it on a retry of the same idem_key is idem_key_conflict.

DATA DISKS (optional): pass data_disks — a list of {"storage_id": <pool>, "req_disk_gb": <int>} objects — to
attach one-or-more ADDITIONAL empty data volumes, each on its own pool, alongside the boot disk (requires
template_id). Each disk is part of the idempotency ask and is debited per-pool, so a retry that changes the
disk layout is idem_key_conflict; a malformed entry is refused data_disk_malformed. The feature is gated by
the operator: while it is off, a data_disks ask is refused multi_disk_disabled. Omit it (the default) for a
single-disk VM.

PINNED (optional): pinned=True asks for a lease the broker never reclaims for a missed heartbeat, for a
VM that must outlive this session (a dev box, a long-running service). It needs no renew_lease; call
release_lease when done, which still destroys it. Your operator must allow pins for your project, else
reason pin_disabled; at the project's pin limit it is pinned_limit; it can't be combined with queue=True
(pin_not_queueable). The answer's `pinned` says whether you got one; lease_status shows it too. A retry
with the same idem_key never adds a pin: if that lease isn't pinned (a request that didn't ask, or one
interrupted between admission and the pin), the answer stays admitted with pinned false and reason
pin_refused_on_replay. Then renew it like any lease, or release it and ask again with a new idem_key.

Requires a consume-tier (or higher) token; an unauthorized call returns {"error": ...}.
"""),
    Verb("schedule_lease", "consume", _admitted_or_queued, HANDLER, "mcp_tools.schedule_lease", doc="""\
Reserve capacity and let the broker PICK the node for you (placement/scheduling).

Use this instead of request_lease when you don't care WHICH node — you just need room.
Set cluster_id to place on any node in that cluster; omit it to place on any cluster with
room. The lease is always counted against YOUR token's project, so project_id may be
omitted (passing a DIFFERENT project is refused {"error":"project_forbidden"}); placement
is refused outright if your project is over its (global) cpu/mem/lease-count quota. idem_key
MUST be a stable id for THIS request — retry with the SAME key to get the same lease back (it
returns the same lease wherever it was placed; reusing it for a different ask is refused).
Returns {ok, lease_id, node_id, admitted, queued, idempotent, reason, tried, pinned}; ok is true when
admitted or queued. `tried` is the
per-candidate trace — a list of {node_id, reason} recording each node the broker attempted and why it
did not take the lease (reason=None for the node that DID) — so you can see exactly which nodes were
considered. If admitted is false, reason says why (e.g. no_capacity, unknown_cluster,
insufficient_project_cpu, project_lease_limit, idem_key_conflict); docs/reference/reason-codes.md lists
every code with what to do about it. Heartbeat the returned lease_id before its ttl_seconds elapse or the
broker reclaims it. ttl_seconds and the storage_id / req_disk_gb pairing follow request_lease's rules
(invalid_ttl, storage_without_disk, disk_without_storage, storage_not_capable).

Set queue=True to WAIT IN LINE instead of being rejected when nothing has room right now:
the request is parked (state PENDING, queued=true) and the broker picks a node and admits it
automatically, in arrival order, when room frees. A queued answer also carries queue_position
(how many queued requests across the whole queue are older; advisory, null when not known or no
longer waiting) and next (what to do). Poll lease_status
and call renew_lease to hold your place; max_wait_seconds caps the wait; a full queue returns
reason 'queue_full'.

VM ACCESS (optional): pass ssh_pubkey = YOUR OWN ssh PUBLIC key to be injected via cloud-init (with
ci_user / ip_config as for request_lease); the broker places the lease and injects the key on whatever
node it picks. Poll lease_status for the guest ip_address once it boots, then ssh in with your private
key. A malformed key/user/ip is refused.

OS TEMPLATE (optional): pass template_id (a catalog id from list_templates) to clone THAT image; the
broker places it on ANY cluster holding a usable copy — the catalog home OR a replicated copy (cross-cluster
placement) — so a cluster_id scope with no copy yields no_reachable_instance (an unknown id is
unknown_template). A private image your project lacks a grant for is refused
{"error":"template_forbidden"}. The template is part of the idempotency ask (a changed template_id on a
retry is idem_key_conflict).

DATA DISKS (optional): pass data_disks — a list of {"storage_id": <pool>, "req_disk_gb": <int>} objects —
to attach one-or-more additional empty data volumes on their own pools alongside the boot disk (requires
template_id). The broker only picks a node every named pool can reach. Part of the idempotency ask (a
changed layout is idem_key_conflict); a malformed entry is refused data_disk_malformed; the feature is
operator-gated (multi_disk_disabled while off). Omit it (default) for a single-disk VM.

PINNED (optional): pinned=True asks for a lease the broker never reclaims for a missed heartbeat, with
request_lease's rules (pin_disabled, pinned_limit, pin_not_queueable, and pin_refused_on_replay for a retry
whose lease isn't pinned); release_lease still destroys it.

Requires a consume-tier (or higher) token; an unauthorized call returns {"error": ...}.
"""),
    Verb("renew_lease", "consume", 'renewed', OWN_LEASE, "gateway.renew_lease", doc="""\
Heartbeat a lease to extend its TTL. Returns {ok, lease_id, renewed, expires_at}.
If renewed is false (reason lease_not_live), the lease is no longer live (released or reaped,
or a queued spot past its max_wait_seconds) — STOP the work you were doing under it; the
capacity is no longer yours. You may only renew a
lease that belongs to YOUR project (else {"error":"lease_forbidden"}). Requires a
consume-tier token."""),
    Verb("release_lease", "consume", 'released', OWN_LEASE, "gateway.release_lease", audit=_released, doc="""\
Release a lease when you're done so its capacity is reclaimed. Idempotent —
released is false if it was already releasing/gone (reason lease_not_live), or if it is still
queued (reason lease_queued: use cancel_lease to drop a queued request). Returns
{ok, lease_id, released}; a repeat answers ok false with lease_not_live: already gone, nothing to do.
You may only release a lease that belongs to YOUR project (else
{"error":"lease_forbidden"}). Requires a consume-tier token."""),
    Verb("lease_status", "read", 'exists', OWN_LEASE, "gateway.lease_status", not_found=reasons.UNKNOWN_LEASE, doc="""\
Look up a lease's current state (PENDING=queued/waiting, ADMITTED=reserved,
ACTIVE=provisioned, RELEASING/RELEASED/REJECTED/RELEASE_FAILED=on the way out) AND its VM-access
connection facts. Returns {lease_id, state, exists, ip_address, ci_user, vmid, node_id, access_requested,
released_reason, hold_reason}. ``released_reason`` says WHY a terminal lease ended (e.g.
'cross_provision_detected', 'placement_blocked:<reason>', 'queue_timeout') and ``hold_reason`` why an
ADMITTED lease is being HELD by the placement re-check (e.g. 'instance_unavailable',
'cross_provision_refused') instead of provisioning — both NULL in the normal path (0033);
docs/reference/reason-codes.md says what each one means and what to do. If you
requested cloud-init access, once the lease is ACTIVE and the guest has booted, ip_address is its IP
and ci_user the login user — ssh ci_user@ip_address with your own private key (ip_address is null
until discovered, or always for a lease with no cloud-init). You may only inspect a lease that belongs
to YOUR project (else {"error":"lease_forbidden"}, returned the same for another tenant's lease or a
non-existent id, so ids can't be enumerated). Requires a read-tier (or higher) token."""),
    # lease_status, waited for server-side (at most 60 s)
    Verb("wait_for_lease", "read", 'reached', HANDLER, "mcp_tools.wait_for_lease",
         not_found=reasons.UNKNOWN_LEASE,
         doc="""\
Wait server-side (timeout_s, at most 60 s) until your lease reaches until_state (ADMITTED, PROVISIONING or
ACTIVE; default ACTIVE) or ends, instead of polling lease_status. For ACTIVE with want_ip, a lease that asked
for ssh access also waits for its ip_address. Returns lease_status's answer plus reached and waited_s; ok is
reached. Else reason wait_timeout: call again (keep renewing; read hold_reason; no IP ever? want_ip=false), or
lease_not_live: it ended (read released_reason). Too many waits: {"error":"busy"}, retry or poll
lease_status. Your project's leases only. Read tier."""),
    Verb("cancel_lease", "consume", 'cancelled', OWN_LEASE, "gateway.cancel_lease", audit=_cancel_acted, doc="""\
Give up a QUEUED request (or release a live lease you no longer want). If the lease is
still waiting in line (PENDING) it is dropped; if it was already promoted to a live lease it
is released instead. Idempotent. Returns {ok, lease_id, cancelled, outcome} (outcome:
cancelled/released/noop; a noop is ok false with lease_not_live: already gone, nothing to do).
You may only cancel a lease that belongs to YOUR project (else {"error":"lease_forbidden"}).
Requires a consume-tier token."""),
    Verb("queue_status", "read", 'exists', OWN_LEASE, "gateway.queue_status", not_found=reasons.UNKNOWN_LEASE, doc="""\
Check a QUEUED lease: its state and how many requests are AHEAD of it in line (its GLOBAL
position across the whole queue). Returns {lease_id, state, queue_position, blocked_reason, exists}.
queue_position is null once the lease is no longer PENDING (it has been promoted, cancelled,
or timed out). blocked_reason names why a PENDING lease can't promote yet — 'node_cordoned' (its pinned
node is closed to provisioning), else null (just waiting its turn). You may only check a lease that
belongs to YOUR project (else {"error":"lease_forbidden"}). Requires a read-tier (or higher) token."""),
    # The bulk forms fan a lease verb over many of the token's OWN leases in one call (br0kr/bulk.py): the same
    # tier and per-lease ownership gate as their single counterparts, no new authority.
    Verb("renew_leases", "consume", None, OWN_LEASES, "gateway.renew_lease", bulk_item='renewed', doc="""\
Heartbeat MANY leases in one call (keep a whole fleet alive) — the bulk form of renew_lease. Pass the list of
lease ids; each is renewed independently. Returns {ok, results, count} where each result carries its own ok (and
a reason when false) beside {lease_id, renewed, expires_at} or {lease_id, error}: a `renewed:false` means that
lease is no longer live (STOP work under it); an id NOT in your project (or unknown) comes back
{error:"lease_forbidden"} without affecting the others; a repeated id runs once. At most 100 ids per call (else
{error:"batch_too_large"} and nothing runs) — chunk beyond that. Requires a consume-tier token."""),
    Verb("release_leases", "consume", None, OWN_LEASES, "gateway.release_lease",
         audit=_released, bulk_item='released',
         doc="""\
Release MANY leases in one call (fleet shutdown) — the bulk form of release_lease. Each is released
independently and idempotently. Returns {ok, results, count} where each result carries its own ok (and a reason
when false) beside {lease_id, released} or {lease_id, error}: an id NOT in your project (or unknown) comes back
{error:"lease_forbidden"} without affecting the others; a repeated id runs once. At most 100 ids per call (else
{error:"batch_too_large"} and nothing runs) — chunk beyond that. Requires a consume-tier token."""),
    Verb("cancel_leases", "consume", None, OWN_LEASES, "gateway.cancel_lease",
         audit=_cancel_item_acted, bulk_item='cancelled',
         doc="""\
Give up MANY queued requests (or release the live ones) in one call — the bulk form of cancel_lease. Each is
dropped if still PENDING, else released if already promoted; idempotent. Returns {ok, results, count} where each
result carries its own ok (and a reason when false) beside {lease_id, cancelled, outcome} (outcome:
cancelled/released/noop) or {lease_id, error}: an id NOT in your project (or unknown) comes back
{error:"lease_forbidden"} without affecting the others; a repeated id runs once. At most 100 ids per call (else
{error:"batch_too_large"} and nothing runs) — chunk beyond that. Requires a consume-tier token."""),
    # bulk read: status of many of your leases in one call
    Verb("lease_statuses", "read", None, OWN_LEASES, "gateway.lease_status", bulk_item='exists', doc="""\
Look up MANY leases' state + VM-access connection facts in one call — the bulk form of lease_status
(so an agent that provisioned several VMs gets all their ip/vmid/state at once). Returns {results,
count} where each result is the same {lease_id, state, exists, ip_address, ci_user, vmid, node_id} as
lease_status, or {lease_id, error}: an id NOT in your project (or unknown) comes back
{error:"lease_forbidden"} without affecting the others; a repeated id runs once. At most 100 ids per
call (else {error:"batch_too_large"} and nothing runs) — chunk beyond that. Requires a read-tier (or
higher) token."""),
    Verb("migration_status", "read", 'exists', HANDLER, "mcp_tools.migration_status",
         not_found=reasons.UNKNOWN_MIGRATION,
         doc="""\
Watch a cross-cluster MIGRATION of one of YOUR leases progress (REQUESTED → RESERVED → MIGRATING →
DONE/ABORTED). Returns {migration_id, exists, state, lease_id, source_node, target_node, reservation_id,
reason}; an ABORTED migration's reason says why (the lease is left untouched). A migration is STARTED by
an operator (there is no agent verb to start one) — this is the read so you can watch your own lease move
and see its new node once state is DONE. You may only inspect a migration whose lease belongs to YOUR
project: an unknown id AND another tenant's migration both return {exists: false} (so ids can't be
enumerated). Requires a read-tier (or higher) token."""),
    Verb("node_capacity", "read", 'exists', LOOKUP, "gateway.node_capacity", scope=SHARED,
         params=(Param("node_id", str),), not_found=reasons.UNKNOWN_NODE,
         doc="""\
Current free capacity on a node (cpu + mem, derived live) so you can size a
request before making it. Returns {node_id, cpu_available, mem_mb_available,
cordoned, exists}. `cordoned` True means the operator has closed the node to new
provisioning — a pinned request_lease here is refused `node_cordoned` even with room,
so use schedule_lease to let the broker pick an open node. Nodes are shared
infrastructure, visible to any authenticated token. Requires a read-tier (or higher) token."""),
    Verb("cluster_capacity", "read", 'exists', LOOKUP, "gateway.cluster_capacity", scope=SHARED,
         params=(Param("cluster_id", str),), acting_as="last", not_found=reasons.UNKNOWN_CLUSTER,
         doc="""\
Current free capacity across a whole cluster (sum of its nodes, derived live) so you
can pick a cluster with room before choosing a node in it. Returns {acting_as, cluster_id,
cpu_available, mem_mb_available, exists} (`acting_as` = your bound project, null for an operator).
Clusters are shared infrastructure, visible to any authenticated token. Requires a read-tier (or
higher) token."""),
    Verb("project_capacity", "read", 'exists', HANDLER, "mcp_tools.project_capacity",
         not_found=reasons.UNKNOWN_PROJECT,
         doc="""\
Remaining tenant budget for YOUR project (cpu + mem + how many more leases it may hold,
derived live) so you can check your quota before requesting. project_id may be omitted (it
defaults to your token's project); naming a DIFFERENT project is refused
{"error":"project_forbidden"}. Returns {acting_as, project_id, cpu_available, mem_mb_available,
lease_slots_available, exists} (`acting_as` = your bound project — same as project_id here). Requires a
read-tier (or higher) token."""),
    # reflect the token's own project/scope/id (no ledger read)
    Verb("whoami", "read", None, HANDLER, "mcp_tools.whoami", doc="""\
Identify the tenant + privilege THIS token acts as — so you can confirm WHICH project the connector is
bound to before you request or release anything. (MCP connectors are selected per host/app, not per
project, so a globally-shared connector always acts as its ONE token's project regardless of which
workspace you opened — call this to see which.) Returns {project_id, scope, token_id}: `project_id` is your
tenant (null for a cross-tenant OPERATOR token), `scope` is your tier (read/consume/provision/operator),
and `token_id` identifies the authenticating token (its SECRET is never returned). The main capacity/lease
reads also echo `acting_as` = this project_id. Requires any authenticated (read-tier or higher) token."""),
    Verb("set_quota", "provision", 'updated', HANDLER, "mcp_tools.set_quota", doc="""\
Raise or lower YOUR project's quota within the envelope your operator set (the provision tier's
self-service). cpu/mem/lease limits must each be > 0 and at most your project's ceiling; a dimension
with no ceiling can't be raised above its current value, and if your operator hasn't set ANY ceiling
the request is refused (reason 'self_service_disabled'). The node/cluster floor still caps what
actually admits, so this can never overprovision hardware — it only widens your tenant budget within
the operator's bound (see your ceiling via project_capacity). Returns {project_id, updated, reason,
quota, ceiling} — your resulting quota and the ceiling envelope. Always applies to YOUR token's
project. Requires a provision-tier token (read/consume tokens get {"error":"forbidden_scope"})."""),
    Verb("request_template_download", "provision", 'requested', HANDLER, "mcp_tools.request_template_download", doc="""\
Download a net-new OS image and register it as a PRIVATE catalog template owned by YOUR project
(phase A4.2). The image is fetched + imported + templatized by the broker's leader daemon (asynchronous —
poll its status on the dashboard / via an operator), then becomes selectable by YOUR project (and only
yours) in request_lease's template_id.

`url` MUST be covered by an operator-curated source allowlist (else reason 'source_not_allowed') and
`sha256` (64 hex) MUST match the image — Proxmox verifies it server-side, so a wrong digest fails the
build. `storage_id` is the Proxmox storage to build on (it must support image import). `size_gb` is your
DECLARED disk budget: it is reserved against your project's template-disk quota up front, and the build
FAILS if the actual imported image is larger (so declare a true upper bound). `target_template_id` is the
new catalog id (must be free). `name`/`description` are optional labels.

Returns {build_id, requested, reason, used_gb, ceiling_gb}. If requested is false, reason says why:
source_not_allowed / invalid_sha256 / invalid_target_template_id (the id must be a dns-slug ≤40 chars — it
is interpolated into the replication guest name) / invalid_size_gb / unknown_cluster / storage_undeclared /
storage_capability_undeclared / storage_not_import_capable (the storage_id pool has no broker row, or its
operator-declared content lacks the {import, images} a build needs — 0041) / template_exists /
build_in_progress / template_downloads_disabled (your operator hasn't set a template-disk ceiling for
your project — ask them to enable it) / template_disk_quota_exceeded (used_gb + size_gb > ceiling_gb).
Always counts against YOUR token's project. Requires a PROVISION-tier token (read/consume tokens get
{"error":"forbidden_scope"})."""),
    Verb("list_nodes", "read", None, LOOKUP, "inventory.list_nodes", scope=SHARED, wrap="nodes", acting_as="first",
         doc="""\
Inventory EVERY node at once (not just one by id): each node's cpu/mem used-vs-total, utilization
%, live lease count, and its cluster (null = unassigned/capacity-only). Use this to survey the whole
fleet and pick where to place work. Nodes are shared infrastructure (no per-tenant detail is exposed).
Returns {acting_as, nodes: [...]} (`acting_as` = your bound project, null for an operator). Requires a
read-tier (or higher) token."""),
    Verb("list_clusters", "read", None, LOOKUP, "inventory.list_clusters", scope=SHARED, wrap="clusters", doc="""\
Inventory EVERY cluster at once: per-cluster cpu/mem used-vs-total (summed across its nodes), node
count, and live lease count. Use this to find a cluster with room before scheduling into it. Clusters
are shared infrastructure. Returns {clusters: [...]}. Requires a read-tier (or higher) token."""),
    Verb("list_storage", "read", None, LOOKUP, "inventory.list_storage", scope=SHARED, wrap="storage", doc="""\
Inventory EVERY shared-storage pool at once: disk used-vs-total and active lease count per pool.
Shared infrastructure. Returns {storage: [...]}. Requires a read-tier (or higher) token."""),
    Verb("list_projects", "read", None, LOOKUP, "inventory.list_projects",
         wrap="projects", scope=TENANT,
         doc="""\
Inventory YOUR project (tenant): cpu/mem/lease quota vs live usage, plus your current
admission-queue depth. Scoped to your token's project — a PROJECT-BOUND token never sees other tenants;
an OPERATOR token (not project-bound) gets the cross-tenant view (every project). Returns {projects: [...]}
(one entry for a tenant token, all for an operator). Requires a read-tier (or higher) token."""),
    Verb("list_templates", "read", None, LOOKUP, "inventory.list_templates",
         wrap="templates", scope=TENANT,
         doc="""\
Inventory the OS-template images YOUR project may clone: each public image PLUS any private image an
operator has granted to your project (id, cluster + vmid, optional name/description). Use it to see what
you can pass as request_lease/schedule_lease `template_id` — selecting one you can't see is refused
`template_forbidden`. Scoped to your token's project, so a private image granted to another tenant is
hidden (an OPERATOR token, not project-bound, sees the full catalog). Returns {templates: [...]}. Requires
a read-tier (or higher) token."""),
    Verb("queue_view", "read", None, LOOKUP, "inventory.queue_view",
         params=(Param("limit", int, 100),), scope=TENANT,
         doc="""\
View YOUR project's admission queue: depth plus the oldest-first head of your waiting requests (up
to `limit`), each with its position, how long it has waited, time left before its wait ceiling, the
ask (target node/cluster, resources), and `blocked_reason` — 'node_cordoned' when a pinned request's
target node has been cordoned (it won't promote until an operator uncordons it or you cancel and
re-request elsewhere), else null (a null with room still waiting is transient — it will promote).
Scoped to your token's project — depth and position are within your tenant (for a single lease's GLOBAL
position use queue_status); an OPERATOR token (not project-bound) gets the cross-tenant queue view.
Returns {depth, shown, truncated, waiting: [...]}. Requires a read-tier (or higher) token."""),
    # the caller's ACTIVE leases (operator token: all tenants')
    Verb("list_leases", "read", None, LOOKUP, "inventory.list_leases",
         params=(Param("limit", int, 100),), scope=TENANT, wrap="leases", acting_as="first",
         doc="""\
List YOUR project's ACTIVE leases — the ones currently HOLDING capacity (ADMITTED/PROVISIONING/
ACTIVE/RELEASING), oldest-first (up to `limit`). Each entry carries the lease id, where it runs (node/
cluster/vmid/ip), its state and ask (cpu/mem/storage/disk), how long it has run, and how long until its
heartbeat TTL expires — so you can see what you hold before renew_leases/release_leases (the bulk ops)
or a targeted release. PENDING (queued) leases are NOT here — use queue_view for those; a terminal lease
is gone. Scoped to your token's project — a PROJECT-BOUND token never sees another tenant's leases; an
OPERATOR token (not project-bound) sees ALL tenants' leases. Returns {acting_as, leases: [...]} (`acting_as`
= your bound project, null for an operator). Requires a read-tier (or higher) token."""),
    Verb("fleet_stats", "read", None, LOOKUP, "inventory.fleet_stats", scope=SHARED, doc="""\
Fleet-wide admission health — shared infrastructure (like list_nodes), not per-tenant: the current
admission-queue depth, the oldest waiting request's age, and admission-latency percentiles over the last
hour — queue-wait (created→admitted, time spent in line) and provision latency (admitted→active). A
percentile is null when the window had no samples (rather than a misleading 0). Use it to size requests,
decide whether to queue, or watch fleet pressure. Returns {queue_depth, oldest_pending_age_seconds,
window_minutes, admit_wait_p50_seconds, admit_wait_p95_seconds, provision_latency_p50_seconds,
provision_latency_p95_seconds}, plus placement/reachability fleet-health counts (each 0 in a healthy
fleet): held_leases (leases the placement re-check is holding), unverified_reachability_pairs /
quarantined_reachability_pairs (declared (storage,node) pairs never runtime-proven / proven-wrong),
unsized_instances (live instances the gate can't size for headroom), and
nodes_with_zero_reachable_storage (cluster nodes that can host no templated clone). Requires a read-tier
(or higher) token."""),
    Verb("replication_status", "read", 'exists', HANDLER, "mcp_tools.replication_status", doc="""\
Status of a replication job: its state (REQUESTED → COPYING → CONVERTING → READY | FAILED), mechanism,
attempts, any (pre-redacted) error, the target (cluster/storage), the declared size, `replication_enabled`
(whether the data plane is advancing jobs yet), and `age_seconds`. A TENANT token sees ONLY jobs attributed
to its OWN project (another project's id returns exists=false); an operator sees any. Returns the job dict
(with ok and exists true) or {ok:false, replication_id, exists:false, reason}; `found` is the older name of
`exists`, kept one release. Requires a read-tier (or higher) token."""),
    Verb("node_cordon", "operator", 'updated', HANDLER, "mcp_operator_tools.node_cordon", doc="""\
OPERATOR only: cordon (cordoned=true) or uncordon (false) a node — the "place NO new lease here"
toggle. A cordoned node accepts NO new provisioning (a pinned request_lease there is refused
node_cordoned; the scheduler drops it from placement) but existing leases keep running — it is NOT
eviction. A fleet-wide infra action across ALL tenants, which is why it needs an operator token, not a
per-project one. Returns {node_id, updated, reason} (reason unknown_node if no such node). Requires an
OPERATOR-tier token (read/consume/provision tokens get {"error":"forbidden_scope"})."""),
    # pin_lease: pin/unpin ANY tenant's live lease (0049) — a permanent capacity hold on someone's quota, bounded
    # by that project's max_pinned. A tenant asks for a pin only at request time (request_lease pinned=true).
    Verb("pin_lease", "operator", _no_reason, HANDLER, "mcp_operator_tools.pin_lease", doc="""\
OPERATOR only: pin (pinned=true) or unpin (false) any tenant's live lease. A pinned lease is never
reclaimed for a missed heartbeat; unpinning gives it a fresh TTL from now. The pin is bounded by the
lease's project: it needs max_pinned > 0 (else pin_disabled) and a free slot (else pinned_limit); an
ungoverned lease can't be pinned (pin_needs_project). Returns {lease_id, pinned, updated, reason} (reason
also unknown_lease, lease_not_live). Requires an OPERATOR-tier token (others get
{"error":"forbidden_scope"})."""),
    # verify_declarations: reconcile declared storage reachability vs PVE's live view (0033/PR-3). Read-only
    # against PVE but it MUTATES the ledger (auto-quarantines a proven-wrong (storage,node) pair) — a
    # fleet-wide reachability action, operator-only, never a tenant verb.
    Verb("verify_declarations", "operator", None, HANDLER, "mcp_operator_tools.verify_declarations", doc="""\
OPERATOR only: reconcile DECLARED storage reachability against PVE and quarantine a (storage, node)
pair on hard evidence (PVE reports the pool disabled, or its live content dropped `images`), so
templated leases over it are refused until clear_storage_node_quarantine. A pool PVE omits, reports
inactive, or whose content is unreadable is reported, not quarantined. Read-only against PVE; never
widens. Optional `cluster_id`. All-or-nothing: lacking Datastore.Audit on /storage on any node returns
{ok:false, reason:'acl_insufficient', required} and quarantines nothing. Returns {ok, checked_nodes,
unreachable_nodes (skipped this run), quarantined, content_quarantined (each with its `missing`
tokens), absent, inactive, present}; checked_nodes=[] means this gateway has no PVE drivers (its PVE
token env is unset). Requires an OPERATOR-tier token."""),
    # discover_reachability: the WIDEN-half twin of verify_declarations (0041/PR-B) — reads every served node's
    # live PVE storage listing and REPORTS drift (new-node widen candidates, unknown pools, disabled/absent
    # declarations, content drift) WITHOUT writing anything. Read-only (unlike verify, which quarantines), but it
    # reaches into fleet PVE infra a tenant can't see, so operator-only.
    Verb("discover_reachability", "operator", None, HANDLER, "mcp_operator_tools.discover_reachability", doc="""\
OPERATOR only: diff every served node's live PVE storage and bridges against br0kr's declarations.
Writes nothing (confirm changes in the dashboard). Optional `cluster_id`. Storage: known_pool_new_node
(a one-confirm widen), unknown_pool (no storage row), declared_disabled, declared_absent (ambiguous:
never evidence to remove), declared_inactive (often transient), quarantined_but_present (a stale
quarantine: clear it), content_report. Network: bridge_menu, declared_missing_bridge,
network_quarantined_but_present, bridge_suggestions (a NAME match only: verify the L2 out of band
before mapping; nothing else checks it). Nodes: checked_nodes ([] = no PVE drivers here),
acl_denied_nodes, unreachable_nodes, unserved_nodes (not checked here: the report is blind there), and
network_* twins. Hide a benign finding with dismiss_discovery_finding. Requires an OPERATOR-tier token."""),
    # dismiss_discovery_finding / clear_discovery_dismissal: an operator silences a KNOWN-benign discovery
    # finding so it stops re-surfacing in every report (0044/PR-H). Pure reporting suppression (no gate path
    # reads discovery_dismissal), fleet-scoped like the report they act on, so operator-only.
    Verb("dismiss_discovery_finding", "operator", _no_reason, HANDLER, "mcp_operator_tools.dismiss_discovery_finding",
         doc="""\
OPERATOR only: dismiss a known-benign discover_reachability finding (e.g. a node-local scratch pool
never meant to be shared), so later reports list it under `dismissed` instead of raising it again.
Identify it by (`kind`, `node_id`, `subject`) from the report row: `kind` is the bucket name; `subject`
is the pve_name for unknown_pool, the storage_id for declared_* / known_pool_new_node / content_report,
the logical_network_id for the network buckets. Report-only: no admission or placement path reads it.
`note` is optional operator text. Idempotent (a re-dismiss returns dismissed=false); otherwise
unknown_node or invalid_kind. Returns {kind, node_id, subject, dismissed, reason}. Undo with
clear_discovery_dismissal. Requires an OPERATOR-tier token."""),
    Verb("clear_discovery_dismissal", "operator", 'cleared', HANDLER, "mcp_operator_tools.clear_discovery_dismissal",
         doc="""\
OPERATOR only: CLEAR a discovery-finding dismissal (0044/PR-H) — un-suppress a finding previously dismissed
via `dismiss_discovery_finding`, so it re-surfaces in `discover_reachability` if still live. The (`kind`,
`node_id`, `subject`) identity must match the dismissal exactly. `unknown_dismissal` if no such dismissal.
Returns {kind, node_id, subject, cleared, reason}. Requires an OPERATOR-tier token."""),
    # detect_template_bridge: advisory READ of a template's net0 bridge from PVE (0038 → read-only in 0039), the
    # diagnostic that helps an operator map a template to a logical net — reads a node's guest config, so
    # operator-only, never a tenant verb (even read-only, it reaches into fleet infra a tenant can't see).
    Verb("detect_template_bridge", "operator", None, HANDLER, "mcp_operator_tools.detect_template_bridge", doc="""\
OPERATOR only: report which PVE bridge a template's net0 is baked to, read from a READY instance's live
config, to guide set_network_node and set_template_logical_network. Read-only. Returns {ok:true,
template_id, bridge, logical_network_id} (the template's declared logical network, or null), else
{ok:false, reason}: unknown_template, no_placed_instance, node_unroutable (no driver for that node here,
e.g. no PVE token env), no_bridge_on_net0. Requires an OPERATOR-tier token."""),
    # force_fail_build: force a STUCK template-build → FAILED (a wedged reclaim that keeps pinning a project's
    # disk-quota reserve). Fleet-wide config action across ANY project's build, so operator-only.
    Verb("force_fail_build", "operator", 'forced', HANDLER, "mcp_operator_tools.force_fail_build", doc="""\
OPERATOR only: force a STUCK template-download build → FAILED, the manual escape hatch for a build the
daemon's automated reclaim can't finalize (a wedged PVE / dead node whose guest DELETE keeps failing) — it
would otherwise retry forever and keep pinning the owning project's declared template-disk quota reserve.
This does NOT tear down the guest (the reason to force is that teardown is broken): if a `vmid` is returned
that guest may STILL be running and needs manual operator cleanup (its fence is `br0kr-build-<id>`). Marks
the build FAILED, freeing the reserve and stopping the retry loop. Idempotent — an unknown or already-
terminal build returns forced=false. Returns {build_id, forced, prior_state, vmid, reason}. A fleet-wide
config action across ANY project's build, so it requires an OPERATOR-tier token (read/consume/provision
tokens get {"error":"forbidden_scope"})."""),
    Verb("migrate_lease", "operator", 'accepted', HANDLER, "mcp_operator_tools.migrate_lease", doc="""\
OPERATOR only: relocate a running lease's guest to a node in ANOTHER cluster, race-free: the target is
reserved through admit(), then the guest moves fence-checked. Records the intent; the leader daemon acts
only when BR0KR_MIGRATE_LIMIT>0 and the source cluster has PDM config. Any tenant's lease. Idempotent on
`idem_key`; one migration in flight per lease (another target while one is active is
migration_in_progress). Returns {migration_id, lease_id, target_node, state, accepted, reason}; reasons
include lease_not_active, same_cluster, storage_migration_unsupported, unknown_target_node,
target_not_provisionable, source_cluster_unknown. Poll migration_status; cancel_migration aborts a
pre-copy saga. Requires an OPERATOR-tier token."""),
    # cancel_migration: release a wedged migration's held target reservation (guest untouched). A cross-tenant
    # infra action on ANY lease's migration — operator-only, like migrate_lease that starts one.
    Verb("cancel_migration", "operator", 'cancelled', HANDLER, "mcp_operator_tools.cancel_migration", doc="""\
OPERATOR only: cancel a migration saga, releasing its held target reservation WITHOUT destroying the
workload — the guest stays on the source and the lease is untouched. CONFIRM-SAFE: a PRE-COPY saga (exactly
REQUESTED / RESERVED) is settled + ABORTED immediately (works even when migrate is disabled); ANY MIGRATING
saga is REFUSED `copy_in_flight` — a copy may already be in flight (the migrate task is POSTed before its
UPID is persisted), so settling it would re-open the over-commit the broker prevents, and the daemon's own
saga already settles it safely (landed → committed, failed → aborted, stuck → held). Returns
{migration_id, cancelled, state, reason}. Acts on ANY
migration (cross-tenant — an operator is not project-bound), like migrate_lease that starts one. Requires an
OPERATOR-tier token (read/consume/provision get {"error":"forbidden_scope"})."""),
    # Template REPLICATION control plane (0034). replicate_template requests an async copy of a template onto
    # another (cluster, storage) — a fleet-wide config action across ANY project (operator-initiated; a project
    # may be named for attribution/governance), so operator-only; cancel_replication / prune_replications are
    # likewise operator infra verbs. replication_status is a READ (a tenant may poll its OWN job — the verb
    # scopes to the caller's project). list_template_instances mirrors the dashboard's operator view of where
    # each template physically lives (unscoped); it is operator-only in this arc because the only caller that
    # can act on it (replicate_template) is operator — the tenant-scoped, auth-visibility-filtered agent view
    # ships with the tenant self-service path in a later PR.
    Verb("replicate_template", "operator", 'requested', HANDLER, "mcp_operator_tools.replicate_template", doc="""\
OPERATOR only: request an async copy of a template onto another pool; the leader daemon copies it once
`replication_enabled`. `mechanism`: clone_shared (an intra-cluster full clone onto a pool the SOURCE
node reaches) or rebuild_source (replay the recorded build; needs build provenance and the
replication_mechanisms allowlist). `force_source_dirty` (rebuild_source only) overrides
source_hand_modified (audited). `project_id` applies a tenant's image governance; omit it for an
operator copy. Returns {replication_id, requested, reason, job_state, ...}. Reasons include
replication_in_progress, already_present, no_replicable_source, source_hand_modified,
no_replication_path, storage_unreachable, storage_unbound, storage_not_import_capable (declare {import,
images} on the pool), mechanism_not_allowed, template_forbidden. Poll replication_status. Requires an
OPERATOR-tier token."""),
    Verb("cancel_replication", "operator", 'cancelled', HANDLER, "mcp_operator_tools.cancel_replication", doc="""\
OPERATOR only: cancel a REQUESTED replication job — it never created a guest, so this just marks it
FAILED and frees the one-active slot (a copy already COPYING/CONVERTING is refused `not_cancellable`; its
teardown belongs to the data-plane reclaim, a later PR). Idempotent — an unknown or non-REQUESTED job
returns cancelled=false. Returns {replication_id, cancelled, prior_state, reason}. Acts on ANY project's job
(cross-tenant — an operator is not project-bound). Requires an OPERATOR-tier token (read/consume/provision
get {"error":"forbidden_scope"})."""),
    Verb("prune_replications", "operator", None, HANDLER, "mcp_operator_tools.prune_replications", doc="""\
OPERATOR only: retention for the replication job table — delete SETTLED TERMINAL jobs older than
`older_than_days` (optionally scoped to one target cluster), so a decommissioned cluster's job history can't
FK-block it forever. A negative `older_than_days` is a no-op; 0 prunes every settled terminal job. An UNSETTLED
terminal job (its transient guest not provably gone) is RETAINED, never a silent teardown; terminal-lease
waiter links are severed first. Breadcrumbs survive in the audit trail, so the history is not lost — only the
FK anchor. Returns {pruned, reason}. Requires an OPERATOR-tier token (read/consume/provision get
{"error":"forbidden_scope"})."""),
    # PR-5: force a wedged copy job terminal (no teardown) — operator infra
    Verb("force_fail_replication", "operator", 'forced', HANDLER, "mcp_operator_tools.force_fail_replication", doc="""\
OPERATOR only: force a STUCK non-terminal replication job → FAILED WITHOUT any teardown — the manual
release valve for a job whose automated reclaim keeps raising (a wedged PVE / dead node) and so keeps holding
the one-active slot + the byte reservation. It does NOT destroy the copy guest (the reason to force is that
the destroy is broken) and does NOT stamp guest_settled_at, so if a `vmid` was minted that guest may STILL be
running and needs OPERATOR cleanup — the returned `vmid` is its fence handle (`br0kr-repl-<id>`) and this logs
an orphan-risk warning + audits. The reservation's held byte-floor is freed. Idempotent (unknown/terminal →
forced=false). Returns {replication_id, forced, prior_state, vmid, reason}. Requires an OPERATOR-tier token
(read/consume/provision get {"error":"forbidden_scope"})."""),
    # mark_template_source_dirty: attest that a BUILT template's guest was hand-modified (PR-6, 0040) so
    # rebuild_source refuses it — a fleet-wide provenance action on the catalog, operator-only.
    Verb("mark_template_source_dirty", "operator", 'updated', HANDLER, "mcp_operator_tools.mark_template_source_dirty",
         doc="""\
OPERATOR only: set (`dirty=true`) or clear a template's hand-modified flag. Set it whenever you edit a
built template's guest by hand in PVE: rebuild_source then refuses the template (source_hand_modified),
because rebuilding from the recorded image would silently drop your edit. br0kr cannot detect an
in-guest edit, so this is your attestation; a fresh build clears it, and replicate_template(...,
force_source_dirty=true) overrides it. Idempotent. Returns {template_id, updated, source_dirty, reason}
(unknown_template if absent). Requires an OPERATOR-tier token."""),
    Verb("list_template_instances", "operator", None, LOOKUP, "inventory.list_template_instances", scope=SHARED,
         wrap="template_instances",
         doc="""\
OPERATOR only: list every template INSTANCE — the physical copies of the logical templates and WHERE
each lives (cluster/node/storage/vmid), its state/origin, and whether it is bound to a placement. The
operator view an agent would consult before requesting a replication (which templates are missing from a
cluster). Returns {template_instances:[…]}. Requires an OPERATOR-tier token (a tenant-scoped, visibility-
filtered agent view ships with the tenant self-service path in a later PR)."""),
)

BY_NAME: dict[str, Verb] = {v.name: v for v in VERBS}
