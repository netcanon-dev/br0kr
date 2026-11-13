"""Which capability is on which actuating surface, declared as data (review CODE-05).

Two surfaces call the same gateway/admin verbs: the MCP gateway's tools (one row each in br0kr/verbs.py) and the
dashboard's POST actions (br0kr/dashboard_actions._ACTIONS). Nothing used to say which verb was meant to be on which
surface, so a verb added to one and not the other went unnoticed. Each row of `SURFACES` names one capability: its
MCP tool, its dashboard path (a POST action, or for a read the GET route that shows it), and why it is absent from a
surface that lacks it. The tests fail when a tool or an action is added without a row, when a row names one that no
longer exists, when a missing side has no reason, and when a tool and its paired action do not reach a common function.
Adding a tool or an action means adding its row here.

A dashboard path is an HTTP route; the page need not have a control for every one (/api/quota and /api/lease/release
are called with curl or a script). The dashboard's GET-only reads with no MCP tool (networks, tokens, audit, ...) are
not rows: the table covers every MCP tool and every POST action.
"""
from __future__ import annotations

import importlib
import inspect
import types
from dataclasses import dataclass

import pytest

from br0kr import dashboard, dashboard_actions, verbs

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class Surface:
    """One capability: its MCP tool, its dashboard path, and why a side it is missing from lacks it."""

    name: str
    mcp: str | None
    gui: str | None
    why: str | None = None


# Why a capability is on one surface only.
SETUP = ("fleet and tenancy setup and its repairs: an operator does them on the dashboard; the operator MCP tools "
         "are the runtime operations an operator agent drives")
AGENT_TOKEN = ("an agent token: an operator issues and manages it (the dashboard; br0kr-admin creates and revokes), "
               "or the OAuth sign-in mints one; no MCP tool does")
OPERATOR_LOGIN = "a dashboard operator's own credential: managed on the dashboard only"
BULK = "the bulk form, so an agent acts on many leases in one call; the dashboard has no multi-select for it"
LIVE_PVE = "reads Proxmox live, and the dashboard holds no Proxmox credential"
SELF_SERVICE = ("a tenant's self-service within its operator's ceiling; on the dashboard the operator sets the quota "
                "itself (/api/project/edit)")
WAIT = "a server-side wait for agents; the dashboard polls /api/lease/status while it shows a VM booting"

SURFACES: tuple[Surface, ...] = (
    # the lease lifecycle (an agent for its own project; the dashboard acts as a chosen project)
    Surface("reserve on a node", "request_lease", "/api/lease/create"),
    Surface("reserve where the broker places it", "schedule_lease", "/api/lease/create"),
    Surface("renew a lease", "renew_lease", "/api/lease/renew"),
    Surface("renew many leases", "renew_leases", None, BULK),
    Surface("release a lease", "release_lease", "/api/lease/release"),
    Surface("release many leases", "release_leases", None, BULK),
    Surface("cancel a lease", "cancel_lease", "/api/cancel"),
    Surface("cancel many leases", "cancel_leases", "/api/cancel"),   # the page's "cancel selected": one call per lease
    Surface("pin a lease", "pin_lease", "/api/lease/pin"),
    Surface("a lease's state", "lease_status", "/api/lease/status"),
    Surface("many leases' states", "lease_statuses", None, BULK),
    Surface("wait for a lease", "wait_for_lease", None, WAIT),
    Surface("a queued lease's place", "queue_status", "/api/lease/status"),
    Surface("the admission queue", "queue_view", "/api/queue"),
    Surface("the live leases", "list_leases", "/api/leases"),
    # tenancy
    Surface("who the caller is", "whoami", "/api/whoami"),
    Surface("a project's remaining budget", "project_capacity", "/api/projects"),
    Surface("the projects", "list_projects", "/api/projects"),
    Surface("usage per project", "project_usage", "/api/usage"),
    Surface("a tenant sets its own quota", "set_quota", None, SELF_SERVICE),
    Surface("set a project's base quota", None, "/api/quota", SETUP),
    Surface("edit a project", None, "/api/project/edit", SETUP),
    Surface("create a project", None, "/api/project/create", SETUP),
    Surface("delete a project", None, "/api/project/delete", SETUP),
    Surface("a project's template-disk quota", None, "/api/project/template-disk", SETUP),
    Surface("a project's pinned-lease limit", None, "/api/project/max-pinned", SETUP),
    # the fleet
    Surface("the nodes", "list_nodes", "/api/nodes"),
    Surface("a node's capacity", "node_capacity", "/api/nodes"),
    Surface("the clusters", "list_clusters", "/api/clusters"),
    Surface("a cluster's capacity", "cluster_capacity", "/api/clusters"),
    Surface("the storage pools", "list_storage", "/api/storage"),
    Surface("fleet admission health", "fleet_stats", "/api/fleet"),
    Surface("cordon a node", "node_cordon", "/api/node/cordon"),
    Surface("create a node", None, "/api/node/create", SETUP),
    Surface("edit a node", None, "/api/node/edit", SETUP),
    Surface("delete a node", None, "/api/node/delete", SETUP),
    Surface("create a cluster", None, "/api/cluster/create", SETUP),
    Surface("edit a cluster", None, "/api/cluster/edit", SETUP),
    Surface("delete a cluster", None, "/api/cluster/delete", SETUP),
    Surface("create a storage pool", None, "/api/storage/create", SETUP),
    Surface("edit a storage pool", None, "/api/storage/edit", SETUP),
    Surface("delete a storage pool", None, "/api/storage/delete", SETUP),
    Surface("bind a pool to its cluster", None, "/api/storage/placement", SETUP),
    Surface("declare a pool's nodes", None, "/api/storage/nodes", SETUP),
    Surface("declare a pool's content", None, "/api/storage/content", SETUP),
    Surface("clear a pool quarantine", None, "/api/storage/quarantine/clear", SETUP),
    Surface("add one pool-node pair", None, "/api/storage/node/add", SETUP),
    Surface("remove one pool-node pair", None, "/api/storage/node/remove", SETUP),
    Surface("create a logical network", None, "/api/network/create", SETUP),
    Surface("delete a logical network", None, "/api/network/delete", SETUP),
    Surface("declare a node's bridge", None, "/api/network/node", SETUP),
    Surface("remove a node's bridge", None, "/api/network/node/delete", SETUP),
    Surface("clear a bridge quarantine", None, "/api/network/quarantine/clear", SETUP),
    # discovery: the drift report reads Proxmox, so it is MCP-only; the dashboard confirms and dismisses from it
    Surface("verify declared reach", "verify_declarations", None, LIVE_PVE),
    Surface("discover storage and networks", "discover_reachability", None, LIVE_PVE),
    Surface("dismiss a discovery finding", "dismiss_discovery_finding", "/api/discovery/dismiss"),
    Surface("clear a dismissal", "clear_discovery_dismissal", "/api/discovery/dismissal/clear"),
    # templates and images
    Surface("the template catalog", "list_templates", "/api/templates"),
    Surface("the template instances", "list_template_instances", "/api/template/instances"),
    Surface("a tenant requests an image download", "request_template_download", "/api/template/download"),
    Surface("an operator requests an image build", None, "/api/template/build/request", SETUP),
    Surface("force-fail a build", "force_fail_build", "/api/template/build/force-fail"),
    Surface("detect a template's bridge", "detect_template_bridge", None,
            LIVE_PVE + "; there an operator sets the network by hand (/api/template/network)"),
    Surface("set a template's network", None, "/api/template/network", SETUP),
    Surface("mark a template source hand-modified", "mark_template_source_dirty", "/api/template/source-dirty"),
    Surface("create a template", None, "/api/template/create", SETUP),
    Surface("edit a template", None, "/api/template/edit", SETUP),
    Surface("delete a template", None, "/api/template/delete", SETUP),
    Surface("grant a template", None, "/api/template/grant", SETUP),
    Surface("revoke a template grant", None, "/api/template/revoke", SETUP),
    Surface("allow a download source", None, "/api/template/source/create", SETUP),
    Surface("remove a download source", None, "/api/template/source/delete", SETUP),
    Surface("bind a template instance", None, "/api/instance/bind", SETUP),
    Surface("retire a template instance", None, "/api/instance/retire", SETUP),
    Surface("delete a template instance row", None, "/api/instance/delete", SETUP),
    # replication and migration
    Surface("replicate a template", "replicate_template", "/api/replication/create"),
    Surface("a replication's state", "replication_status", "/api/replications"),
    Surface("cancel a replication", "cancel_replication", "/api/replication/cancel"),
    Surface("prune replication history", "prune_replications", "/api/replication/prune"),
    Surface("force-fail a replication", "force_fail_replication", "/api/replication/force-fail"),
    Surface("migrate a lease", "migrate_lease", "/api/migrate"),
    Surface("a migration's state", "migration_status", "/api/migrations"),
    Surface("cancel a migration", "cancel_migration", "/api/migration/cancel"),
    # credentials and operator settings
    Surface("create an agent token", None, "/api/token/create", AGENT_TOKEN),
    Surface("revoke an agent token", None, "/api/token/revoke", AGENT_TOKEN),
    Surface("change a token's expiry", None, "/api/token/expiry", AGENT_TOKEN),
    Surface("create a dashboard operator", None, "/api/operator/create", OPERATOR_LOGIN),
    Surface("revoke a dashboard operator", None, "/api/operator/revoke", OPERATOR_LOGIN),
    Surface("edit the operator settings", None, "/api/settings/edit", SETUP),
    Surface("reload the fleet config", None, "/api/reload", SETUP),
)


def test_every_tool_and_every_action_has_a_row():
    """Every MCP tool is in exactly one row and every dashboard POST action in at least one (one action may serve
    two tools: /api/lease/create both pins and places), and every row names a tool, an action or a GET route that
    exists. Guards a verb reaching one surface with nobody deciding about the other (the gap review CODE-05
    found), and a row outliving a renamed or removed tool or path."""
    tools = [s.mcp for s in SURFACES if s.mcp is not None]
    assert len(tools) == len(set(tools)), sorted({t for t in tools if tools.count(t) > 1})
    assert set(tools) == {v.name for v in verbs.VERBS}, (
        f"tools with no row: {sorted({v.name for v in verbs.VERBS} - set(tools))}; "
        f"rows naming no tool: {sorted(set(tools) - {v.name for v in verbs.VERBS})}")
    paths = {s.gui for s in SURFACES if s.gui is not None}
    missing = set(dashboard_actions._ACTIONS) - paths
    assert not missing, f"actions with no row: {sorted(missing)}"
    unknown = paths - set(dashboard_actions._ACTIONS) - set(dashboard._ROUTES)
    assert not unknown, f"rows naming no dashboard path: {sorted(unknown)}"
    names = [s.name for s in SURFACES]
    assert len(names) == len(set(names)), sorted({n for n in names if names.count(n) > 1})


def test_a_surface_missing_from_a_row_says_why():
    """A row with no MCP tool or no dashboard path gives the reason; a row on both surfaces gives none, and no row
    is on neither. Guards a capability left off a surface by accident looking like a decision."""
    for s in SURFACES:
        assert s.mcp is not None or s.gui is not None, s.name
        on_both = s.mcp is not None and s.gui is not None
        assert bool(s.why) != on_both, f"{s.name}: a reason exactly when one surface is missing"


def test_a_write_tool_maps_to_a_post_action_not_a_read_route():
    """A row whose tool changes something (a consume, provision or operator verb that is not a LOOKUP read) maps to
    a POST action, never a GET route. Guards a write verb being marked as on the dashboard because a page shows its
    result, when no dashboard control performs it."""
    for s in SURFACES:
        if s.mcp is None or s.gui is None:
            continue
        v = verbs.BY_NAME[s.mcp]
        if v.tier != "read" and v.kind != verbs.LOOKUP:
            assert s.gui in dashboard_actions._ACTIONS, f"{s.mcp} ({v.tier}) maps to a read route {s.gui}"


def _names(code: types.CodeType) -> set[str]:
    """Every global or attribute name a function's code (and the code nested in it) refers to."""
    out = set(code.co_names)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            out |= _names(const)
    return out


# Module and helper names both sides share without that meaning they reach the same verb.
_PLUMBING = frozenset({"gateway", "admin", "pin", "replication", "actas", "auth", "commit", "mcp_common", "_conn",
                       "_authorize", "_audit_write", "_audit_refused", "bind_project", "get", "dict"})


def test_a_tool_and_its_action_reach_a_common_function():
    """For each row on both surfaces with a POST action, the MCP tool's code (its handler, or the function a
    shaped row calls) and the action's verb (followed one level into the act-as layer, br0kr/actas.py) refer to a
    common function by name, such as gateway.cancel_lease or pin.set_pinned. Guards a row pairing a tool with an
    unrelated action (cancel_lease with /api/lease/release passed the name checks), which would claim a parity
    that does not exist."""
    actas = importlib.import_module("br0kr.actas")
    for s in SURFACES:
        if s.mcp is None or s.gui not in dashboard_actions._ACTIONS:
            continue
        v = verbs.BY_NAME[s.mcp]
        module, name = v.call.rsplit(".", 1)
        if v.kind == verbs.HANDLER:
            handler = inspect.unwrap(getattr(importlib.import_module(f"br0kr.{module}"), name))
            tool = _names(handler.__code__)
        else:
            tool = {name}
        action = _names(dashboard_actions._ACTIONS[s.gui][1].__code__)
        for n in list(action):
            f = getattr(actas, n, None)
            if inspect.isfunction(f):
                action |= _names(f.__code__)
        assert (tool & action) - _PLUMBING, f"{s.mcp} and {s.gui} share no function"
