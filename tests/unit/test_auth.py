"""Unit tests for the agent-credential auth layer (br0kr/auth.py).

This is the gateway's authorization gate, so its policy is pinned hard here — the pure parts (the
tier→verb matrix, header parsing, project binding, the token digest) need no database, and the two
DB-backed functions (resolve, lease_owned_by) are exercised against a tiny fake connection. The
end-to-end behaviour against real Postgres (a minted token resolves, a revoked/expired one does not,
tier gating + project binding actually refuse) is proven in scripts/harness_auth.py.
"""
from __future__ import annotations

import pytest

from br0kr import auth

pytestmark = pytest.mark.unit


# ── the token digest ───────────────────────────────────────────────────────────────────────────────
def test_hash_token_is_deterministic_and_distinguishing():
    """The same secret always hashes to the same hex digest (so resolution is an index lookup), and two
    different secrets hash differently. Guards a non-deterministic or colliding digest that would make a
    valid token un-resolvable or two tokens indistinguishable."""
    assert auth.hash_token("abc") == auth.hash_token("abc")
    assert auth.hash_token("abc") != auth.hash_token("abd")
    assert len(auth.hash_token("abc")) == 64   # hex SHA-256


def test_generate_secret_is_high_entropy_and_unique():
    """Each generated secret is a long, URL-safe, distinct string. Guards a weak/short or repeating
    generator — the whole 'SHA-256 is enough' argument rests on the secret being high-entropy random."""
    a, b = auth.generate_secret(), auth.generate_secret()
    assert a != b and len(a) >= 32
    assert all(c.isalnum() or c in "-_" for c in a)   # URL-safe alphabet


# ── the tier → verb policy (fail-closed) ─────────────────────────────────────────────────────────────
def test_read_tier_allows_reads_but_not_reservations():
    """A `read` token may call the inventory/capacity/status verbs but NOT reserve/renew/release. Guards a
    read-only credential being able to consume capacity — the core point of the lowest tier."""
    for verb in ("node_capacity", "list_nodes", "list_projects", "list_templates", "lease_status",
                 "queue_view", "whoami"):
        assert auth.tier_allows("read", verb) is True
    for verb in ("request_lease", "schedule_lease", "renew_lease", "release_lease", "cancel_lease"):
        assert auth.tier_allows("read", verb) is False


def test_consume_tier_allows_reads_and_reservations():
    """A `consume` token may do everything `read` can PLUS the lease lifecycle. Guards a regression that
    accidentally denies a consume token its reservation verbs (or that read verbs require consume)."""
    for verb in ("list_nodes", "lease_status", "request_lease", "schedule_lease", "renew_lease",
                 "release_lease", "cancel_lease"):
        assert auth.tier_allows("consume", verb) is True


def test_provision_tier_is_a_superset_of_consume():
    """A `provision` token (the highest tier) is allowed every consume + read verb PLUS its own
    self-service verb. Guards provision being accidentally MORE restrictive than consume."""
    for verb in ("list_nodes", "request_lease", "renew_lease", "cancel_lease", "set_quota"):
        assert auth.tier_allows("provision", verb) is True


def test_set_quota_is_provision_tier_only():
    """The self-service quota verb requires the provision tier — read AND consume tokens are denied
    (forbidden_scope at the gateway), only provision is allowed. Guards a consume/read token raising its
    own quota: the provision tier exists precisely to put self-service config behind a higher bar than
    reserving capacity."""
    assert auth.tier_allows("read", "set_quota") is False
    assert auth.tier_allows("consume", "set_quota") is False
    assert auth.tier_allows("provision", "set_quota") is True


def test_request_template_download_is_provision_tier_only():
    """The phase-A4.2 self-service template DOWNLOAD verb requires the provision tier — a read/consume token
    is denied (forbidden_scope), only provision is allowed. Guards a consume token (which may reserve capacity)
    being able to pull a net-new image + spend its project's disk quota: downloading is self-service config,
    behind the SAME higher bar as set_quota, not the consume bar."""
    assert auth.tier_allows("read", "request_template_download") is False
    assert auth.tier_allows("consume", "request_template_download") is False
    assert auth.tier_allows("provision", "request_template_download") is True


def test_bulk_lease_verbs_mirror_their_single_verb_tiers():
    """The bulk fan-out verbs (br0kr/bulk.py) require the SAME tier as their single counterparts: the write
    bulk ops (renew_leases/release_leases/cancel_leases) are consume-tier (a read token is denied), and the
    bulk read (lease_statuses) is read-tier (every tenant tier may call it). Guards a bulk verb slipping
    onto the surface at the wrong bar — a read token gaining bulk release, or the bulk status wrongly
    demanding consume."""
    for verb in ("renew_leases", "release_leases", "cancel_leases"):
        assert auth.tier_allows("read", verb) is False
        assert auth.tier_allows("consume", verb) is True
        assert auth.tier_allows("provision", verb) is True
    for scope in ("read", "consume", "provision"):
        assert auth.tier_allows(scope, "lease_statuses") is True


def test_tier_allows_is_fail_closed_for_unknown_verb_or_scope():
    """An unknown verb (a tool added without registering its tier) and an unknown scope (a malformed
    credential) are BOTH denied — fail closed. Guards a new tool silently reaching the surface
    unauthorized, or a garbage scope being treated as privileged."""
    assert auth.tier_allows("consume", "drop_database") is False   # verb not in VERB_TIER
    assert auth.tier_allows("consume", "list_projects") is True
    assert auth.tier_allows("root", "list_nodes") is False          # scope not in TIER_RANK
    assert auth.tier_allows("", "list_nodes") is False


# ── the operator tier (a PARALLEL scope — infra/cross-tenant, NOT a rank above provision) ────────────
def test_operator_tier_may_observe_and_run_operator_verbs_but_no_tenant_op():
    """An `operator` token may call the operator verbs (node_cordon, migrate_lease, force_fail_build) AND the
    read-tier observability verbs (so it can inspect the fleet), but NEVER a tenant lease/quota verb — it isn't
    a tenant (no project to bind to). Guards an operator token reserving/renewing capacity or raising a quota,
    and guards it silently LOSING the fleet reads it needs to operate."""
    for verb in ("node_cordon", "migrate_lease", "force_fail_build", "cancel_migration",
                 "discover_reachability", "dismiss_discovery_finding", "clear_discovery_dismissal"):
        assert auth.tier_allows("operator", verb) is True
    # 0044/PR-H: the discovery dismiss/clear verbs are operator-only — a tenant can never suppress a fleet finding
    for tenant in ("read", "consume", "provision"):
        assert auth.tier_allows(tenant, "dismiss_discovery_finding") is False
        assert auth.tier_allows(tenant, "clear_discovery_dismissal") is False
    # may OBSERVE — incl. the bulk READ (lease_statuses is read-tier) and whoami (identity echo → project None)
    for verb in ("list_nodes", "list_projects", "queue_view", "migration_status", "fleet_stats",
                 "lease_statuses", "whoami"):
        assert auth.tier_allows("operator", verb) is True
    # NEVER a tenant lease/quota op — incl. the bulk WRITE forms (they bind to a project it doesn't have)
    for verb in ("request_lease", "schedule_lease", "renew_lease", "release_lease", "cancel_lease",
                 "set_quota", "request_template_download",
                 "renew_leases", "release_leases", "cancel_leases"):
        assert auth.tier_allows("operator", verb) is False


def test_replication_verbs_have_the_right_tiers():
    """The replication control plane (0034): replicate_template / cancel_replication / prune_replications are
    OPERATOR-only infra verbs (a tenant token — read/consume/provision — can never call them, and an operator
    can); list_template_instances is likewise operator-only in this arc. replication_status is a READ (an
    operator OR any tenant tier may poll — the verb scopes a tenant to its own job). Guards a replication verb
    being unlisted (fail-closed → denied) or mis-tiered so a tenant could request/cancel a cross-tenant copy."""
    for verb in ("replicate_template", "cancel_replication", "prune_replications", "list_template_instances"):
        assert auth.tier_allows("operator", verb) is True
        for tenant in ("read", "consume", "provision"):
            assert auth.tier_allows(tenant, verb) is False   # never a tenant op
    # replication_status is read-tier: an operator observes any, and every tenant tier may poll (its own).
    assert auth.tier_allows("operator", "replication_status") is True
    for tenant in ("read", "consume", "provision"):
        assert auth.tier_allows(tenant, "replication_status") is True


def test_operator_verbs_are_operator_tier_only():
    """node_cordon, migrate_lease, and force_fail_build require the operator tier — every TENANT tier
    (read/consume/provision) is denied. Guards a tenant token cordoning a shared node, migrating another
    tenant's lease, or force-failing another tenant's build (a cross-tenant infra action has NO home in the
    per-project tiers — this is the wall the tier closes)."""
    for scope in ("read", "consume", "provision"):
        assert auth.tier_allows(scope, "node_cordon") is False
        assert auth.tier_allows(scope, "migrate_lease") is False
        assert auth.tier_allows(scope, "force_fail_build") is False
        assert auth.tier_allows(scope, "cancel_migration") is False
    assert auth.tier_allows("operator", "node_cordon") is True
    assert auth.tier_allows("operator", "migrate_lease") is True
    assert auth.tier_allows("operator", "force_fail_build") is True
    assert auth.tier_allows("operator", "cancel_migration") is True


def test_operator_is_parallel_not_a_rank_above_provision():
    """The operator scope is PARALLEL, not a rank above provision — a provision token (top tenant tier) still
    cannot call operator verbs, and an operator token cannot call provision verbs. Guards a linear-rank
    mistake that would make either a superset of the other (an operator spending quota, or a provision token
    cordoning nodes fleet-wide)."""
    assert auth.tier_allows("provision", "migrate_lease") is False
    assert auth.tier_allows("operator", "set_quota") is False


def test_valid_scopes_is_exactly_the_four_known_tiers():
    """VALID_SCOPES (what admin.validate_token accepts + the 0020 CHECK pins) is exactly the four known tiers
    including operator, so minting an operator token is allowed and a typo'd tier is refused. Guards a drift
    between the mint validator, the tier policy, and the DB CHECK."""
    assert sorted(auth.VALID_SCOPES) == ["consume", "operator", "provision", "read"]


# ── Authorization header parsing ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("header,expected", [
    ("Bearer sekret", "sekret"),
    ("bearer sekret", "sekret"),     # scheme is case-insensitive (RFC 7235)
    ("BEARER  sekret  ", "sekret"),  # surrounding whitespace stripped
    (None, None),
    ("", None),
    ("sekret", None),                # no scheme prefix → rejected
    ("Basic sekret", None),          # wrong scheme
    ("Bearer ", None),               # empty credential
    ("Bearer    ", None),            # whitespace-only credential
])
def test_bearer_secret_parses_only_a_well_formed_bearer(header, expected):
    """The bearer extractor returns the secret only for a well-formed `Bearer <secret>` (scheme
    case-insensitive, value stripped, non-empty), else None. Guards a bare value or a wrong scheme being
    accepted as a token, and a blank credential resolving to an empty-string 'secret'."""
    assert auth.bearer_secret(header) == expected


# ── project binding (the tenant isolation invariant) ─────────────────────────────────────────────────
def test_bind_project_forces_the_tokens_own_project():
    """A reservation binds to the token's OWN project: passing None or the token's own project binds there,
    and passing a DIFFERENT project is refused 'project_forbidden'. Guards the core isolation property — a
    token spending another tenant's quota."""
    p = auth.Principal(project_id="team-a", scope="consume", token_id=1)
    assert auth.bind_project(p, None) == ("team-a", None)
    assert auth.bind_project(p, "team-a") == ("team-a", None)
    assert auth.bind_project(p, "team-b") == (None, "project_forbidden")


# ── DB-backed resolution ─────────────────────────────────────────────────────────────────────────────
class _ResolveConn:
    """A fake connection: the resolve SELECT returns `row`; the throttled last_used UPDATE is recorded in
    `touched` (and `fetchone` is never called on it). `raise_on_touch` simulates the audit-bump failing."""

    def __init__(self, row: tuple | None, *, raise_on_touch: bool = False) -> None:
        self._row = row
        self.touched: list[tuple] = []
        self._raise = raise_on_touch

    def execute(self, sql: str, params: tuple) -> _ResolveConn:
        if "UPDATE api_token" in sql:
            if self._raise:
                raise RuntimeError("touch failed")
            self.touched.append(params)
        return self

    def fetchone(self) -> tuple | None:
        return self._row


def test_resolve_returns_principal_and_bumps_last_used_on_a_live_token():
    """A live token resolves to its Principal (project + scope + id) and its last_used_at is bumped (the
    throttled UPDATE runs). Guards resolution dropping the project/scope, and the audit bump not firing."""
    conn = _ResolveConn((42, "team-a", "consume"))
    p = auth.resolve(conn, "the-secret")
    assert p == auth.Principal(project_id="team-a", scope="consume", token_id=42)
    assert conn.touched == [(42,)]   # last_used_at bumped for the resolved token id


def test_resolve_returns_none_when_no_live_token_matches():
    """No matching live token (wrong/revoked/expired — the SELECT's WHERE handles all three) → None, and no
    last_used bump. Guards a miss being treated as authenticated, or touching a non-existent row."""
    conn = _ResolveConn(None)
    assert auth.resolve(conn, "nope") is None
    assert conn.touched == []


def test_resolve_survives_a_failing_last_used_bump():
    """If the best-effort last_used_at bump raises, resolve STILL returns the Principal — an audit-stamp
    failure must never deny a valid token. Guards a transient write error on the bump locking a tenant out."""
    conn = _ResolveConn((7, "team-b", "read"), raise_on_touch=True)
    assert auth.resolve(conn, "s") == auth.Principal(project_id="team-b", scope="read", token_id=7)


def test_resolve_looks_the_token_up_by_its_hash_not_the_plaintext():
    """Resolution queries by `hash_token(secret)`, never the raw secret — the ledger stores only the hash,
    so a lookup by plaintext would never match. Guards a regression that selects on the secret directly."""
    captured: list[tuple] = []

    class _Capture(_ResolveConn):
        def execute(self, sql: str, params: tuple) -> _ResolveConn:
            if "SELECT" in sql:
                captured.append(params)
            return super().execute(sql, params)

    auth.resolve(_Capture((1, "p", "read")), "my-secret")
    assert captured == [(auth.hash_token("my-secret"),)]   # the hash, not "my-secret"


# ── named dashboard operators (0026): a SEPARATE credential from api_token ─────────────────────────────
class _DashOpConn:
    """A fake conn for resolve_dashboard_operator: the SELECT returns `row`; the throttled `UPDATE
    dashboard_operator` bump is recorded in `touched`. `raise_on_touch` simulates the bump failing."""

    def __init__(self, row: tuple | None, *, raise_on_touch: bool = False) -> None:
        self._row = row
        self.touched: list[tuple] = []
        self._raise = raise_on_touch

    def execute(self, sql: str, params: tuple) -> _DashOpConn:
        if "UPDATE dashboard_operator" in sql:
            if self._raise:
                raise RuntimeError("touch failed")
            self.touched.append(params)
        return self

    def fetchone(self) -> tuple | None:
        return self._row


def test_resolve_dashboard_operator_returns_id_name_and_bumps_last_used():
    """A LIVE named operator resolves to (id, name) and its last_used_at is bumped. Distinct from `resolve`
    (api_token / MCP): a dashboard credential never yields a Principal/MCP scope — it drives ONLY the dashboard.
    Guards resolution dropping the name or the bump not firing."""
    conn = _DashOpConn((5, "dana"))
    assert auth.resolve_dashboard_operator(conn, "the-secret") == (5, "dana")
    assert conn.touched == [(5,)]   # last_used_at bumped for the resolved id


def test_resolve_dashboard_operator_returns_none_on_a_miss():
    """No matching live named operator (wrong/revoked/expired — the WHERE handles all three) → None, no bump.
    Guards a miss being treated as authenticated, or touching a non-existent row."""
    conn = _DashOpConn(None)
    assert auth.resolve_dashboard_operator(conn, "nope") is None
    assert conn.touched == []


def test_resolve_dashboard_operator_survives_a_failing_bump():
    """A failing best-effort last_used bump must NOT deny a valid named operator (an audit-stamp failure can't
    lock an operator out). Guards a transient write error on the bump denying a good credential."""
    conn = _DashOpConn((9, "alice"), raise_on_touch=True)
    assert auth.resolve_dashboard_operator(conn, "s") == (9, "alice")


def test_resolve_dashboard_operator_looks_up_by_hash_not_plaintext():
    """Resolution queries by `hash_token(secret)`, never the raw secret (the ledger stores only the hash).
    Guards a regression that selects on the plaintext (which would never match)."""
    captured: list[tuple] = []

    class _Capture(_DashOpConn):
        def execute(self, sql: str, params: tuple) -> _DashOpConn:
            if "SELECT" in sql:
                captured.append(params)
            return super().execute(sql, params)

    auth.resolve_dashboard_operator(_Capture((1, "n")), "my-secret")
    assert captured == [(auth.hash_token("my-secret"),)]


# ── lease ownership ──────────────────────────────────────────────────────────────────────────────────
class _LeaseConn:
    def __init__(self, row: tuple | None) -> None:
        self._row = row

    def execute(self, _sql: str, _params: tuple) -> _LeaseConn:
        return self

    def fetchone(self) -> tuple | None:
        return self._row


def test_lease_owned_by_allows_only_the_owning_project():
    """A lease whose project matches is owned (True, None); another tenant's lease, a non-existent lease,
    and an ungoverned (NULL-project) lease are ALL refused with the SAME 'lease_forbidden' — so a token
    can't act on another's lease nor enumerate which ids exist. Guards cross-tenant lease access and id
    enumeration."""
    assert auth.lease_owned_by(_LeaseConn(("team-a",)), 1, "team-a") == (True, None)
    assert auth.lease_owned_by(_LeaseConn(("team-b",)), 1, "team-a") == (False, "lease_forbidden")
    assert auth.lease_owned_by(_LeaseConn(None), 999, "team-a") == (False, "lease_forbidden")
    assert auth.lease_owned_by(_LeaseConn((None,)), 2, "team-a") == (False, "lease_forbidden")


# ── per-project image governance (the template_forbidden gate, phase A3) ───────────────────────────────
class _CountingConn(_LeaseConn):
    """A _LeaseConn that also records how many times execute() ran — to assert template_allowed does NOT
    touch the DB at all when no template is chosen (the common, fast path)."""

    def __init__(self, row: tuple | None) -> None:
        super().__init__(row)
        self.executes = 0

    def execute(self, _sql: str, _params: tuple) -> _CountingConn:
        self.executes += 1
        return self


def test_template_allowed_skips_the_db_when_no_template_chosen():
    """A request with template_id None (the operator-default image) is ALWAYS allowed WITHOUT a query — the
    gate only governs an explicitly chosen catalog template. Guards an unnecessary read on the hot
    no-template path, and the default image ever being refused governance it doesn't participate in."""
    conn = _CountingConn(None)
    assert auth.template_allowed(conn, None, "team-a") == (True, None)
    assert conn.executes == 0   # no DB hit when no image is chosen


@pytest.mark.parametrize("row,expected", [
    ((True, False), (True, None)),                 # public, not granted → allowed (public needs no grant)
    ((True, True), (True, None)),                  # public AND granted → allowed (grant is redundant)
    ((False, True), (True, None)),                 # private but granted to this tenant → allowed
    ((False, False), (False, "template_forbidden")),  # private, ungranted → the ONLY refusal
])
def test_template_allowed_decides_on_public_or_grant(row, expected):
    """The gate allows a template that is `public` OR `granted` to the tenant, and refuses ONLY a private,
    ungranted one with `template_forbidden`. This is the core A3 policy: a non-public image is usable solely
    by a project on its allowlist. Guards the boolean logic inverting (forbidding a public/granted image, or
    admitting a private ungranted one — the leak the whole feature exists to stop)."""
    assert auth.template_allowed(_LeaseConn(row), "deb12", "team-a") == expected


def test_template_allowed_defers_unknown_template_to_admission():
    """A template_id that doesn't exist (no catalog row) is treated as ALLOWED here (True) — admission is
    the AUTHORITATIVE existence check and returns `unknown_template` under the node lock, so the gate must
    not pre-empt it with a misleading `template_forbidden`. Guards a typo'd id reporting a governance
    refusal instead of the honest 'no such template', and the gate duplicating admission's existence check."""
    assert auth.template_allowed(_LeaseConn(None), "ghost", "team-a") == (True, None)
