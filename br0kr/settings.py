"""settings.py — the `operator_setting` SINGLETON: runtime-tunable daemon/gateway knobs, hot-applied from the
ledger without a restart (migration 0021).

Every knob resolves **COALESCE(ledger override, env var, code default)**, so the ledger row is a pure
OVERRIDE layer: an all-NULL row (the migration seed) is byte-identical to the pre-Settings behaviour, and env
stays a valid config path. These are reconciler/queue POLICY dials — the daemon re-reads them each tick and
applies them BETWEEN passes (never mid-admit), and the gateway reads `queue_max` per request — so they debit
NO capacity scope and cannot weaken the admission floor. Deliberately NOT here: `tick_seconds` / `reload_every`
/ health & bind ports (process-launch, would foot-gun the loop) and every secret.

Pure-ish: every function takes a live connection and touches only `operator_setting`; `validate_patch` is
fully pure. The caller owns the transaction/commit (the daemon reads on its autocommit work conn; the
dashboard's `_action` commits; the gateway reads within its request txn).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import psycopg


@dataclass(frozen=True)
class Knob:
    """One tunable knob: its `operator_setting` column + the env var that SEEDS it (None = ledger/default
    only — e.g. the reaper's release ceiling, which had no env before) + the code default + a `kind` used to
    parse the env value and validate an edit. `minimum` bounds an int knob (defense-in-depth behind the DB
    CHECK). `gateway` marks a knob read in the gateway process (queue_max, replication_cooldown_seconds), not
    only in the daemon tick; the dashboard shows it. Under the bundled compose every broker process receives every
    knob's env seed (docker-compose.yml x-broker-env), so the env layer agrees across containers."""

    name: str
    kind: str          # 'int' | 'bool' | 'str'
    env: str | None
    default: Any
    minimum: int | None = None
    gateway: bool = False
    choices: tuple[str, ...] | None = None   # enum str knob (e.g. log_level) — validated + a GUI dropdown


# Display + apply order. Mirrors the env reads in daemon.main() / mcp_common._queue_max() / reaper.
KNOBS: tuple[Knob, ...] = (
    Knob("queue_max", "int", "BR0KR_QUEUE_MAX", 1000, minimum=0, gateway=True),
    Knob("reap_grace", "int", "BR0KR_REAP_GRACE", 30, minimum=0),
    Knob("sweep_every", "int", "BR0KR_SWEEP_EVERY", 12, minimum=0),
    Knob("sweep_dry_run", "bool", "BR0KR_SWEEP_DRY_RUN", False),
    Knob("provision_limit", "int", "BR0KR_PROVISION_LIMIT", 8, minimum=0),
    Knob("promote_limit", "int", "BR0KR_PROMOTE_LIMIT", 16, minimum=0),
    Knob("max_release_attempts", "int", None, 5, minimum=0),
    # The PROVISIONING budget in seconds (0048, review REL-01): the reaper reclaims a lease still PROVISIONING
    # this long after begin_provisioning as RELEASING(provision_stuck), whatever its expires_at (PROVISIONING is
    # renewable, so expiry alone never ended it for a heartbeating agent). Default 1200 = 20 min, above the
    # provisioner's own clone + grow + start wait budgets (see lease.PROVISION_STUCK_SECONDS for why reclaiming is
    # safe even when PVE is slower); 0 = OFF. Daemon-only (the reaper runs in its tick);
    # no env seed, like max_release_attempts: an operator dial set from the dashboard, hot-applied.
    Knob("provision_stuck_seconds", "int", None, 1200, minimum=0),
    Knob("build_limit", "int", "BR0KR_BUILD_LIMIT", 0, minimum=0),
    Knob("migrate_limit", "int", "BR0KR_MIGRATE_LIMIT", 0, minimum=0),
    Knob("guest_ip_prefix", "str", "BR0KR_GUEST_IP_PREFIX", None),
    # The reconciler daemon's log level, hot-applied each tick (0022). Default INFO matches configure_logging's
    # startup default, so an all-NULL row + no env is byte-identical; the daemon resolves ledger→LOG_LEVEL→INFO
    # and setLevel()s. Daemon-only (the gateway/dashboard have no tick loop → keep their startup LOG_LEVEL env).
    Knob("log_level", "str", "LOG_LEVEL", "INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")),
    # Audit-trail retention in DAYS (0024): the daemon prunes operator_audit rows older than this, on a
    # throttled cadence. Default 0 = OFF (keep forever — enabling the one sanctioned DELETE on the append-only
    # trail is a deliberate opt-in); a byte-identical all-NULL seed. Daemon-only (the prune runs in the tick).
    Knob("audit_retention_days", "int", "BR0KR_AUDIT_RETENTION_DAYS", 0, minimum=0),
    # Placement gate (0033). full_clone: the clone-storage resolution (br0kr/placement.py §4.2) branches
    # full-vs-linked, and the gate runs in BOTH the gateway's admit() AND the daemon. The bundled compose now
    # passes BR0KR_VM_FULL_CLONE to both (x-broker-env, review OPS-02), but a hand-run process may still see a
    # different env, so this operator_setting row is the SHARED source of truth (the env-drift-across-
    # containers class §3.5 closes): all-NULL + the env = full clones, byte-identical to before.
    # placement_assert: the mandatory post-clone disk-location assertion's audited break-glass — default ON
    # (True); set false only to disable the tripwire deliberately.
    Knob("full_clone", "bool", "BR0KR_VM_FULL_CLONE", True),
    Knob("placement_assert", "bool", None, True),
    # Cross-node placement (0037). OFF (default) = v1 node-local: a templated lease is served ONLY from an instance
    # HOSTED ON its own node (placement._USABLE_INSTANCE_PICK). ON = the gate ALSO admits a READY instance on a
    # DIFFERENT node in the SAME cluster whose storage REACHES the placement node (i.e. genuinely SHARED) — the
    # daemon clones from the instance's HOME node with `--target <placement node>`. Read in BOTH the gateway's
    # admit()/schedule() AND the daemon's recheck_instance, so — like full_clone — it MUST be a shared
    # operator_setting (an env-only value would let them disagree, §3.5). Safe because the pick already requires
    # the instance's storage to reach the placement node, so ON only ever admits a shared-storage copy; every
    # other guard (post-clone assertion, vmid fence, same-cluster) is unchanged. all-NULL row = OFF, byte-
    # identical to the shipped node-local gate.
    Knob("cross_node_clone", "bool", "BR0KR_CROSS_NODE_CLONE", False),
    # Replication control plane (0034). The enabled-mechanism allowlist a replication request is checked against
    # (gateway.request_replication) — a COMMA list, so it can widen to more mechanisms without a schema change;
    # default 'clone_shared' (the only mechanism implemented). A SHARED operator_setting so the gateway and the
    # (future) daemon agree. NOT an enum `choices` knob precisely so a future 'clone_shared,rebuild_source' is a
    # single ledger edit; request_replication splits + validates membership itself. (`replication_enabled` is a
    # LEADER-PUBLISHED fact, NOT a settings Knob — read directly in br0kr/replication.py.)
    Knob("replication_mechanisms", "str", "BR0KR_REPLICATION_MECHANISMS", "clone_shared"),
    # Replication data plane (0035). The cross-job COOLDOWN window in seconds: after a job for a
    # (template, target cluster, target pool) key terminates FAILED, request_replication refuses a fresh job for
    # the SAME key with `replication_cooldown` until this many seconds pass (from the FAILED row's updated_at),
    # so a persistently-failing target isn't hammered by a mint+reclaim retry loop. Read in the GATEWAY
    # (request_replication) — a SHARED operator_setting, not env, so the gateway and dashboard act-as agree and it
    # is dashboard-editable (the env-drift-across-containers class §3.5). Default 900 (15min); 0 = no cooldown.
    Knob("replication_cooldown_seconds", "int", "BR0KR_REPLICATION_COOLDOWN_SECONDS", 900, minimum=0, gateway=True),
    # Clone-path content-capability gate (0042, PR-D). OFF (default) = the tenant clone path
    # (placement._resolve_clone_storage, in admit()/recheck_instance) does NOT check the target pool's declared
    # `images` capability — byte-identical to the pre-PR-D gate. ON = a clone (full OR linked) whose target pool is NOT
    # `images`-capable is REFUSED `storage_not_capable` BEFORE PVE fails the disk-create (closes a grant-then-kill).
    # FAIL-CLOSED when ON: an UNDECLARED target pool (content_types NULL) refuses too — so declare fleet-wide
    # content coverage (admin.set_storage_content) BEFORE flipping this on, or every clone onto an undeclared pool
    # refuses. Read in admit() AND recheck_instance AND the scheduler's placeability probe → a SHARED
    # operator_setting, never env-only (the §3.5 anti-env-drift rule, like cross_node_clone/full_clone). An UNBOUND
    # seed (no storage row) is skipped even when ON (nothing to declare; pre-0041 parity).
    Knob("clone_content_enforcement", "bool", "BR0KR_CLONE_CONTENT_ENFORCEMENT", False),
    # Reachability-discovery CADENCE in SECONDS (0043, PR-D backlog). 0 = OFF (the default — no periodic
    # discovery, byte-identical: no extra PVE traffic, no new log surface). > 0 = the daemon runs the READ-ONLY
    # placement.discover_reachability drift report every this-many seconds (monotonic elapsed, a real wall-clock
    # interval, NOT a tick count) and LOGs the drift bucket counts + publishes a drift gauge — so an operator sees
    # storage/bridge reachability drift on a schedule without hand-running the MCP verb. Daemon-ONLY (like
    # log_level/audit_retention_days: the daemon's tick loop cheaply re-reads + applies it and runs the report;
    # the gateway/dashboard have no tick loop). REPORT-ONLY: discover_reachability WRITES NOTHING to gate-read
    # declarations (the §11 discovery-writes-nothing hard rule — a future auto_confirm on any discovery cadence is
    # FORBIDDEN); this knob only sets HOW OFTEN the report runs. all-NULL row + no env = 0 = OFF, byte-identical.
    Knob("discovery_interval_seconds", "int", "BR0KR_DISCOVERY_INTERVAL_SECONDS", 0, minimum=0),
    # Multi-disk request-surface master switch (0047, the multi-disk arc PR-5). OFF (default) = the request
    # surface (gateway.request_lease/schedule_lease) REFUSES a non-empty `data_disks` ask `multi_disk_disabled`
    # BEFORE any admission lock/debit — so the surface can DEPLOY while multi-disk stays inert (an empty ask
    # never reads this knob, so the single-disk path is byte-identical). ON = a data-disk ask is accepted,
    # validated, debited per-pool, and actuated (PR-4). Flip ON deliberately once every tenant-nameable DATA
    # pool is images-capable (design §11-#1). Read ONLY in the GATEWAY request surface (the daemon actuates
    # whatever child rows exist, knob-agnostic — no cross-process drift to close), so gateway=True; a ledger
    # column (not env) purely so the go-live flip is a reversible dashboard edit. all-NULL row + no env = OFF,
    # byte-identical to the deployed-but-inert default.
    Knob("multi_disk_enabled", "bool", "BR0KR_MULTI_DISK_ENABLED", False, gateway=True),
)
_BY_NAME: dict[str, Knob] = {k.name: k for k in KNOBS}
_COLS: tuple[str, ...] = tuple(k.name for k in KNOBS)
MAX_STR_LEN = 64   # guest_ip_prefix ceiling — mirrors the 0021 CHECK
_INT4_MAX = 2_147_483_647   # every operator_setting int knob is an int4 column (migration 0021)


@dataclass(frozen=True)
class Resolved:
    """A knob's effective value + where it came from (`ledger` override / `env` seed / `default`), so the
    dashboard can show 'env is overriding your setting' and an operator understands what wins."""

    value: Any
    source: str   # 'ledger' | 'env' | 'default'


def _env_value(knob: Knob) -> Any:
    """The knob's value from its env var, or None if it has no env or the env is unset/blank/garbage. A bool env
    is '1'→True / anything-else→False (matching daemon.main()'s `== "1"`); a non-int int-env is IGNORED (falls
    through to the default) rather than crashing the reader. A BLANK value is unset for every kind: compose
    passes `${VAR:-}` (an empty string) for each knob the operator did not set (docker-compose.yml
    x-broker-env), and reading that as a bool False would report an 'env' override nobody made."""
    if knob.env is None:
        return None
    raw = os.environ.get(knob.env)
    if raw is None or not raw.strip():
        return None
    if knob.kind == "bool":
        return raw == "1"
    if knob.kind == "int":
        try:
            return int(raw)
        except ValueError:
            return None
    return raw or None   # str: empty → None (unset)


def _read_row(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    """The singleton's knob columns as a dict (col → value, value None when not overridden), or an empty dict
    if the row is somehow absent (treated as all-fallback). No commit — the caller owns the txn."""
    cols = ", ".join(_COLS)
    row = conn.execute(f"SELECT {cols} FROM operator_setting WHERE id = 1").fetchone()
    if row is None:
        return {}
    return dict(zip(_COLS, row, strict=True))   # row is a SELECT of exactly _COLS → lengths always match


def resolve(conn: psycopg.Connection[Any]) -> dict[str, Resolved]:
    """Every knob's effective value + source, COALESCE(ledger, env, default). A ledger/env value of False or 0
    is a real override (the check is `is not None`, not truthiness), so `sweep_dry_run=false` or
    `build_limit=0` set explicitly still win over the env/default."""
    row = _read_row(conn)
    out: dict[str, Resolved] = {}
    for knob in KNOBS:
        ledger = row.get(knob.name)
        if ledger is not None:
            out[knob.name] = Resolved(ledger, "ledger")
            continue
        env = _env_value(knob)
        if env is not None:
            out[knob.name] = Resolved(env, "env")
            continue
        out[knob.name] = Resolved(knob.default, "default")
    return out


def effective(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    """Just the resolved VALUES (name → value) — the daemon's hot-apply convenience."""
    return {name: r.value for name, r in resolve(conn).items()}


def overrides(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    """The LEDGER override layer ONLY (col → value, None = not overridden) — what the dashboard displays and
    edits. Distinct from `resolve`, which also folds in the per-process env + code default. The bundled compose
    gives the dashboard the same knob seeds as the daemon and gateway, but a process run by hand (or another
    orchestrator) may not, so the dashboard reports only what is authoritative everywhere: the SHARED ledger
    override, with the code default as a hint; a blank field clears the override → each process falls back
    to its own env/default."""
    return _read_row(conn)


def validate_patch(patch: dict[str, Any]) -> str | None:
    """Pure validator for a settings edit — None if OK, else a reason string. An unknown key is rejected; a
    key mapped to None CLEARS the override (always allowed → falls back to env/default). An int knob must be a
    real int (bool and float REJECTED — guards the JSON-float-truncation trap) at/above its minimum AND at or
    below int4 max (`too_large` — every knob is an int4 column, so an over-range value would otherwise fail the
    UPDATE as an opaque 503 instead of this clean reason); a bool knob a bool; a str knob a str within
    MAX_STR_LEN AND, if it is an enum knob (`choices` set, e.g. log_level), one of those choices
    (`invalid_choice`). Fully pure, so the whole matrix is unit-testable."""
    for key, val in patch.items():
        knob = _BY_NAME.get(key)
        if knob is None:
            return f"unknown_setting:{key}"
        if val is None:
            continue
        if knob.kind == "int":
            if isinstance(val, bool) or not isinstance(val, int):
                return f"not_an_int:{key}"
            if knob.minimum is not None and val < knob.minimum:
                return f"below_min:{key}"
            if val > _INT4_MAX:
                return f"too_large:{key}"        # every int knob is an int4 column (0021) — refuse over-range
        elif knob.kind == "bool":
            if not isinstance(val, bool):
                return f"not_a_bool:{key}"
        elif knob.kind == "str":
            if not isinstance(val, str):
                return f"not_a_str:{key}"
            if len(val) > MAX_STR_LEN:
                return f"too_long:{key}"
            if knob.choices is not None and val not in knob.choices:
                return f"invalid_choice:{key}"   # an enum str (log_level) must be one of the allowed values
    return None


def set_settings(conn: psycopg.Connection[Any], patch: dict[str, Any]) -> None:
    """Apply a VALIDATED patch to the singleton: each named knob column is set (None CLEARS it → fallback),
    `updated_at` bumped. Unknown keys are ignored (validate_patch rejects them first). Caller commits. SQL is
    built from the fixed knob-name allowlist + bound params, so it is injection-safe."""
    cols = [key for key in patch if key in _BY_NAME]
    if not cols:
        return
    assignments = ", ".join(f"{c} = %s" for c in cols) + ", updated_at = now()"
    conn.execute(f"UPDATE operator_setting SET {assignments} WHERE id = 1", [patch[c] for c in cols])


def request_reload(conn: psycopg.Connection[Any]) -> None:
    """Set the cross-process reload flag — the dashboard's 'Reload fleet now' (the daemon runs in a separate
    process, so it can't be SIGHUP'd from the web tier). Idempotent; caller commits."""
    conn.execute("UPDATE operator_setting SET reload_requested = true WHERE id = 1")


def take_reload(conn: psycopg.Connection[Any]) -> bool:
    """Atomically READ-AND-CLEAR the reload flag: True if a reload was pending (and it's now cleared), else
    False. The daemon calls this each tick on its leader work conn; the single-statement UPDATE ... WHERE
    reload_requested RETURNING makes the read+clear race-free against a concurrent dashboard set."""
    row = conn.execute(
        "UPDATE operator_setting SET reload_requested = false "
        "WHERE id = 1 AND reload_requested RETURNING true"
    ).fetchone()
    return row is not None
