# br0kr/ — the package

## What it is

The broker's Python package: the processes (`python -m br0kr.daemon`, `br0kr.mcp_server`, `br0kr.dashboard`,
`br0kr.oauth`, `br0kr.migrate`), the `br0kr.cli` token tool, and the modules they share. The
[module map](../docs/architecture.md#module-map) has one row per module: what it does, whether it writes the
ledger or acts on Proxmox, and what proves it.

## Architecture

- **The race-critical seam is `admission.py`.** Every capacity grant is made there, under the scope advisory locks;
  `admit()` and `promote_pinned()` share one locked core. Capacity is freed only by the guarded-CAS transitions in
  `lease.py`.
- **The leader daemon is the only writer to Proxmox.** `daemon.py`, elected by `leader.py`, runs the tick: the step
  table in `tick_steps.py`, whose steps live in the `tick_*.py` modules and drive `reconciler.py`, `reaper.py`,
  `sweeper.py` and the job modules, all through `pve.py`'s fenced driver.
- **Inputs reach handlers through tables:** the MCP verb registry in `verbs.py`, the dashboard's route map in
  `dashboard.py` and its action table `_ACTIONS` in `dashboard_actions.py`, and the daemon's `tick_steps.STEPS`.
- **One ledger seam:** every connection is opened by `db.py`, which pins READ COMMITTED and adds keepalives and
  retry.

## Adding a module

1. Put the capability in a new file whose docstring opens with one line on what the file is.
2. Wire it where inputs are dispatched: a row in `verbs.py` for an MCP tool (below), a route in `dashboard.py` or
   an `_ACTIONS` row in `dashboard_actions.py` (below), or a row in `tick_steps.STEPS` (below).
3. If it grants capacity, it calls `admission.admit()` or `promote_pinned()` and adds no debit of its own; if it
   changes a lease's state, it calls a `lease.py` transition.
4. Add its row to the module map; `tests/unit/test_architecture_doc.py` fails until you do.
5. In the same commit: a unit test for the pure parts, and a real-Postgres harness (`scripts/harness_<name>.py`) for
   anything that locks or races.

## Adding an MCP tool

1. A tool that reads through one `gateway.py` or `inventory.py` function, or acts on one or many of the caller's
   leases, is a row of one shape (`mcp_kinds.py`): write that function, then add its row to `verbs.VERBS` with
   its tier, its verdict and its description. That is the whole change.
2. A tool that fits no shape is a HANDLER: its function in `mcp_tools.py` (tenant) or `mcp_operator_tools.py`
   (operator tier, served only by the operator gateway), calling `mcp_common._authorize` first, and its row.
3. Rerun `scripts/gen_api_reference.py` and `scripts/gen_mcp_contract.py`: the fixture's diff is what changed
   for agents, and `tests/unit/test_mcp_contract.py` fails until it is committed.

## Adding a dashboard action

1. The verb is a function with a docstring in the module that owns the write (`admin.py`, `gateway.py`, …). It
   does not commit; the dashboard's `_action` handler commits and writes the audit row. Its first sentence is the
   action's description in `docs/api.md`.
2. In `dashboard_actions.py`: a parser that turns the body into the verb's arguments and raises `_Reject(reason)`
   on a bad one, and the `_ACTIONS` row `path: (parse, verb)`. If whether to audit depends on the result, add its
   predicate to `_RESULT_AUDIT` (and to `_RESULT_REFUSED` if it can refuse for capacity).
3. Rerun `scripts/gen_api_reference.py` (`tests/unit/test_api_reference.py` fails until you do), and give each new
   reason code a hint in the page (`static/dashboard.js`); `tests/unit/test_dashboard.py` fails on a `_Reject` code
   or an `admin.py` validator code that has none.

## Adding a tick step

1. Write the step's `run(daemon, conn, summary)` in the `tick_*.py` module that owns that kind of work, or in a new
   one. It beats (`daemon._beat()`) before each slow item, passes `on_progress=daemon._beat` into PVE waits, and
   starts no new item once `daemon._halted()`.
2. Add one row to `tick_steps.STEPS`: its place is its order, `due` says when it runs, and `connect` picks a
   non-autocommit connection if it takes an advisory lock across a read and a write.
3. A counter it reports is a new `TickSummary` field declaring its metric class; `/metrics` renders it by itself.
4. `daemon.py` does not change: a step in an existing `tick_*.py` module is a diff to two files (a new module also
   needs its row in the architecture module map). `tests/unit/test_tick_steps.py` pins the loop and the table's
   gates; test the step itself beside its module's other tests.

## Failure behaviour

| Condition | Behaviour |
|---|---|
| The ledger is unreachable | a transient error is retried with bounded backoff; a misconfiguration fails fast (`db.py`) |
| The ledger isn't READ COMMITTED, or its schema is older than the build | the daemon and gateway refuse to serve |
| The leader loses its lock | it starts no new step; a standby daemon takes over |
| A Proxmox call fails or its HTTP request times out mid-provision | the lease moves to RELEASING and the reaper destroys the fenced guest |
| A create, clone or start task outlives its wait | the lease stays PROVISIONING until it expires or has been there `provision_stuck_seconds`, then is reclaimed; a clone not yet fenced is removed by the orphan sweep once the lease is terminal |
| A destroy keeps failing | after `max_release_attempts` the lease ends `RELEASE_FAILED` instead of holding capacity |

## Testing

[../tests/README.md](../tests/README.md) has the test pyramid and the one mock seam per boundary;
[../scripts/README.md](../scripts/README.md) lists the harnesses and how `scripts/run_harness.sh` runs them.
