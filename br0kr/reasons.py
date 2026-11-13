"""reasons.py — the request path's refusal reasons and error codes, the codes an agent's ask is answered with.

An agent branches on these strings: it retries `insufficient_node_cpu`, fixes its ask on `ssh_pubkey_malformed`, gives
up on `unknown_template`. Spelled as bare literals across admission, the gateway, the scheduler and placement, a typo
was a new, undocumented reason that no test noticed (review CODE-01). Here each one is a named constant, and the
agent-facing modules import the names. tests/unit/test_reasons.py fails on a literal of a code in `ALL` anywhere in
those modules, and on any literal in a reason position there (a returned string, a `reason`/`error` value, a
comparison with a `.reason`, a result's constructor argument) that is not in `ALL`. So a new or misspelt reason fails
the suite until it is added here (and documented, review UX-04).

The classification sets at the bottom say how the request path treats a reason: QUEUEABLE parks a `queue=true` ask,
NODE_FULL / STORAGE / PROJECT / TEMPLATE decide whether placement tries the next node or answers at once.

Scope: the tenant-facing request surface (admission, cloud-init and data-disk validation, the gateway and MCP verbs,
placement, pins, auth, the queue view) plus the operator verbs that share those modules, and the few bookkeeping
reasons migration and replication pass as `reason=`. NOT here yet: the lease-lifecycle release reasons the reaper,
reconciler and daemon write into `lease.released_reason` (`expired`, `provision_stuck`, `provision_error`, ...),
which an agent reads back from `lease_status`; the migration and replication job reasons (`reservation_lost`,
`source_changed`, ...), which a read-tier token reads back from `migration_status` / `replication_status`; and the
operator-only modules' own codes (admin, the dashboard). Review UX-04's reason-code reference documents those beside
this vocabulary. The ledger's replication-posture codes (br0kr/ledgerhealth.py) are a different vocabulary and stay
there.
"""
from __future__ import annotations

from typing import Final

# ── Request validation (admission.validate, cloudinit, multidisk): the ask is malformed; fix it, never retry ──────
MISSING_IDEM_KEY: Final = "missing_idem_key"
BOOLEAN_RESOURCES: Final = "boolean_resources"
NON_FINITE_RESOURCES: Final = "non_finite_resources"
NON_POSITIVE_RESOURCES: Final = "non_positive_resources"
NEGATIVE_DISK: Final = "negative_disk"
RESOURCE_TOO_LARGE: Final = "resource_too_large"
INVALID_TTL: Final = "invalid_ttl"
STORAGE_WITHOUT_DISK: Final = "storage_without_disk"
DISK_WITHOUT_STORAGE: Final = "disk_without_storage"
MISSING_PROJECT_ID: Final = "missing_project_id"
MISSING_TEMPLATE_ID: Final = "missing_template_id"
CLOUD_INIT_REQUIRES_PUBKEY: Final = "cloud_init_requires_pubkey"
SSH_PUBKEY_EMPTY: Final = "ssh_pubkey_empty"
SSH_PUBKEY_TOO_LONG: Final = "ssh_pubkey_too_long"
SSH_PUBKEY_MALFORMED: Final = "ssh_pubkey_malformed"
SSH_PUBKEY_UNSUPPORTED_TYPE: Final = "ssh_pubkey_unsupported_type"
CI_USER_EMPTY: Final = "ci_user_empty"
CI_USER_TOO_LONG: Final = "ci_user_too_long"
CI_USER_MALFORMED: Final = "ci_user_malformed"
IP_CONFIG_EMPTY: Final = "ip_config_empty"
IP_CONFIG_TOO_LONG: Final = "ip_config_too_long"
IP_CONFIG_MALFORMED: Final = "ip_config_malformed"
DATA_DISKS_REQUIRE_TEMPLATE: Final = "data_disks_require_template"
TOO_MANY_DATA_DISKS: Final = "too_many_data_disks"
DATA_DISK_MALFORMED: Final = "data_disk_malformed"

# ── Idempotency and the request surface ─────────────────────────────────────────────────────────────────────────
IDEM_KEY_CONFLICT: Final = "idem_key_conflict"                # the idem_key was used for a different ask
LEASE_NOT_LIVE: Final = "lease_not_live"                      # the idem_key's (or the named) lease has ended
LEASE_QUEUED: Final = "lease_queued"                          # release_lease on a still-queued lease: cancel it
RESERVED_IDEM_KEY: Final = "reserved_idem_key"                # an internal reservation namespace
MULTI_DISK_DISABLED: Final = "multi_disk_disabled"            # data disks are off (operator setting)
LOST_INSERT: Final = "lost_insert"                            # a racing insert of the same idem_key vanished; retry
STALE_REFERENCE: Final = "stale_reference"                    # an id the ask named was deleted mid-admit

# ── Capacity: did not fit now, may later (QUEUEABLE, except node_cordoned) ──────────────────────────────────────
INSUFFICIENT_NODE_CPU: Final = "insufficient_node_cpu"
INSUFFICIENT_NODE_MEM: Final = "insufficient_node_mem"
INSUFFICIENT_STORAGE: Final = "insufficient_storage"
INSUFFICIENT_PROJECT_CPU: Final = "insufficient_project_cpu"
INSUFFICIENT_PROJECT_MEM: Final = "insufficient_project_mem"
PROJECT_LEASE_LIMIT: Final = "project_lease_limit"
NO_CAPACITY: Final = "no_capacity"                            # placement: no candidate node had room
NODE_CORDONED: Final = "node_cordoned"                        # the named node takes no new leases; not queueable

# ── Unknown ids ─────────────────────────────────────────────────────────────────────────────────────────────────
UNKNOWN_NODE: Final = "unknown_node"
UNKNOWN_STORAGE: Final = "unknown_storage"
UNKNOWN_PROJECT: Final = "unknown_project"
UNKNOWN_CLUSTER: Final = "unknown_cluster"
UNKNOWN_TEMPLATE: Final = "unknown_template"
UNKNOWN_TEMPLATE_INSTANCE: Final = "unknown_template_instance"
UNKNOWN_LEASE: Final = "unknown_lease"
UNKNOWN_BUILD: Final = "unknown_build"
UNKNOWN_MIGRATION: Final = "unknown_migration"

# ── The admission queue ─────────────────────────────────────────────────────────────────────────────────────────
QUEUED: Final = "queued"                                      # parked; poll lease_status, heartbeat to hold the spot
QUEUE_FULL: Final = "queue_full"
WAIT_TIMEOUT: Final = "wait_timeout"                          # wait_for_lease: not there yet; call it again
NO_REACHABLE_INSTANCE_REPLICATING: Final = "no_reachable_instance_replicating"   # parked behind a template copy

# ── Placement: where a templated clone may land ─────────────────────────────────────────────────────────────────
TEMPLATE_NOT_ON_CLUSTER: Final = "template_not_on_cluster"    # retired by the cross-cluster widening; reserved
NO_REACHABLE_INSTANCE: Final = "no_reachable_instance"
STORAGE_UNREACHABLE_FROM_NODE: Final = "storage_unreachable_from_node"
LINKED_CLONE_STORAGE_PINNED: Final = "linked_clone_storage_pinned"
STORAGE_UNBOUND: Final = "storage_unbound"
STORAGE_NOT_CAPABLE: Final = "storage_not_capable"
TEMPLATE_BRIDGE_UNDECLARED: Final = "template_bridge_undeclared"
NETWORK_UNREACHABLE_FROM_NODE: Final = "network_unreachable_from_node"
NO_REACHABLE_CLONE_STORAGE: Final = "no_reachable_clone_storage"
UNNAMED_PLACEMENT_REFUSAL: Final = "unnamed_placement_refusal"   # a placement refusal that carried no reason
STORAGE_CAPABILITY_UNDECLARED: Final = "storage_capability_undeclared"
STORAGE_NOT_IMPORT_CAPABLE: Final = "storage_not_import_capable"

# ── Holds (lease.hold_reason): admitted, but the daemon will not provision it where it was placed ────────────────
INSTANCE_UNAVAILABLE: Final = "instance_unavailable"
PLACEMENT_REACHABILITY_LOST: Final = "placement_reachability_lost"
CROSS_PROVISION_REFUSED: Final = "cross_provision_refused"
PLACEMENT_UNRESOLVABLE: Final = "placement_unresolvable"
ENV_TEMPLATE_WRONG_CLUSTER: Final = "env_template_wrong_cluster"
SOURCE_NODE_UNROUTABLE: Final = "source_node_unroutable"
NETWORK_UNROUTABLE: Final = "network_unroutable"
ENV_DEFAULT_MULTINODE: Final = "env_default_multinode"        # the daemon's env disk default on a multi-node fleet

# ── Pinned leases ───────────────────────────────────────────────────────────────────────────────────────────────
PIN_DISABLED: Final = "pin_disabled"
PIN_NEEDS_PROJECT: Final = "pin_needs_project"
PIN_NOT_QUEUEABLE: Final = "pin_not_queueable"
PINNED_LIMIT: Final = "pinned_limit"
PIN_REFUSED: Final = "pin_refused"                            # also a released_reason: admitted, then its pin lost
PIN_REFUSED_ON_REPLAY: Final = "pin_refused_on_replay"

# ── Authorisation and tenancy (an MCP `{"error": code}` answer) ─────────────────────────────────────────────────
NO_TOKEN: Final = "no_token"
INVALID_TOKEN: Final = "invalid_token"
FORBIDDEN_SCOPE: Final = "forbidden_scope"
BUSY: Final = "busy"                                          # the token lookup is saturated; retry shortly
PROJECT_FORBIDDEN: Final = "project_forbidden"
LEASE_FORBIDDEN: Final = "lease_forbidden"
TEMPLATE_FORBIDDEN: Final = "template_forbidden"
BATCH_TOO_LARGE: Final = "batch_too_large"
INTERNAL_ERROR: Final = "internal_error"

# ── Self-service quota (set_quota) ──────────────────────────────────────────────────────────────────────────────
SELF_SERVICE_DISABLED: Final = "self_service_disabled"
NON_FINITE: Final = "non_finite"
NON_POSITIVE: Final = "non_positive"
EXCEEDS_CEILING: Final = "exceeds_ceiling"

# ── Template downloads (request_template_download) ──────────────────────────────────────────────────────────────
TEMPLATE_DOWNLOADS_DISABLED: Final = "template_downloads_disabled"
INVALID_SIZE_GB: Final = "invalid_size_gb"
TEMPLATE_EXISTS: Final = "template_exists"
BUILD_IN_PROGRESS: Final = "build_in_progress"
TEMPLATE_DISK_QUOTA_EXCEEDED: Final = "template_disk_quota_exceeded"
MISSING_TARGET_TEMPLATE_ID: Final = "missing_target_template_id"
INVALID_TARGET_TEMPLATE_ID: Final = "invalid_target_template_id"
MISSING_STORAGE_ID: Final = "missing_storage_id"
MISSING_URL: Final = "missing_url"
INVALID_SHA256: Final = "invalid_sha256"
SOURCE_NOT_ALLOWED: Final = "source_not_allowed"
STORAGE_UNDECLARED: Final = "storage_undeclared"

# ── The usage report (project_usage) ────────────────────────────────────────────────────────────────────────────
INVALID_WINDOW: Final = "invalid_window"

# ── Operator verbs that share these modules ─────────────────────────────────────────────────────────────────────
ALREADY_TERMINAL: Final = "already_terminal"
ACL_INSUFFICIENT: Final = "acl_insufficient"
MECHANISM_NOT_ALLOWED: Final = "mechanism_not_allowed"
MECHANISM_NOT_IMPLEMENTED: Final = "mechanism_not_implemented"
REPLICATION_COOLDOWN: Final = "replication_cooldown"
REPLICATION_IN_PROGRESS: Final = "replication_in_progress"
TARGET_VANISHED: Final = "target_vanished"
NO_PLACED_INSTANCE: Final = "no_placed_instance"
NODE_UNROUTABLE: Final = "node_unroutable"
NO_BRIDGE_ON_NET0: Final = "no_bridge_on_net0"

# ── Bookkeeping reasons migration and replication write (read back by migration_status / lease_status) ──────────
MIGRATED: Final = "migrated"                                  # a finished migration's migration.reason
MIGRATION_RESERVATION_SETTLED: Final = "migration_reservation_settled"      # released_reason of its target reservation
REPLICATION_RESERVATION_SETTLED: Final = "replication_reservation_settled"  # released_reason of its byte reservation

# ── Internal: why promote_pinned did not promote, read by the scheduler and the daemon, never sent to an agent ────
# (Not the PVE driver's `gone` verdict in br0kr/pve.py: a different vocabulary that happens to share the word.)
GONE: Final = "gone"                                          # the lease left PENDING (cancelled, queue-expired)
NO_NODE: Final = "no_node"                                    # a scheduled entry reached promote with no target node
INTERNAL: Final[frozenset[str]] = frozenset({GONE, NO_NODE})

# Every code an agent can receive: every constant above except INTERNAL. Built from this module's own names, so a
# constant cannot be added and left out.
ALL: Final[frozenset[str]] = frozenset(
    value for name, value in dict(globals()).items() if name.isupper() and isinstance(value, str)) - INTERNAL

# ── How the request path classifies a reason ────────────────────────────────────────────────────────────────────
# "Did not fit right now, but might later": a queue=true request PARKS on these. Hard refusals (an unknown id, a
# conflict, a malformed ask) are never queueable: waiting cannot make a typo succeed.
QUEUEABLE: Final[frozenset[str]] = frozenset({
    INSUFFICIENT_NODE_CPU, INSUFFICIENT_NODE_MEM, INSUFFICIENT_STORAGE,
    INSUFFICIENT_PROJECT_CPU, INSUFFICIENT_PROJECT_MEM, PROJECT_LEASE_LIMIT, NO_CAPACITY,
})
# One node is full or closed: placement tries the next candidate.
NODE_FULL: Final[frozenset[str]] = frozenset({INSUFFICIENT_NODE_CPU, INSUFFICIENT_NODE_MEM, NODE_CORDONED})
# A bad or exhausted pool, or an unbound quota row: the pool is global, so no other node helps. Answer at once.
STORAGE: Final[frozenset[str]] = frozenset({UNKNOWN_STORAGE, INSUFFICIENT_STORAGE, STORAGE_UNBOUND})
# The tenant budget is global across nodes: an over-quota or unknown project repeats on every candidate.
PROJECT: Final[frozenset[str]] = frozenset({
    UNKNOWN_PROJECT, INSUFFICIENT_PROJECT_CPU, INSUFFICIENT_PROJECT_MEM, PROJECT_LEASE_LIMIT})
# A template deleted mid-schedule: every candidate would refuse identically.
TEMPLATE: Final[frozenset[str]] = frozenset({UNKNOWN_TEMPLATE})
# The hold reasons placement names (br0kr/placement.py HOLD_*); the daemon also writes ENV_DEFAULT_MULTINODE.
HOLD: Final[frozenset[str]] = frozenset({
    INSTANCE_UNAVAILABLE, PLACEMENT_REACHABILITY_LOST, CROSS_PROVISION_REFUSED, PLACEMENT_UNRESOLVABLE,
    ENV_TEMPLATE_WRONG_CLUSTER, SOURCE_NODE_UNROUTABLE, NETWORK_UNROUTABLE})
