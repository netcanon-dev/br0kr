# br0kr/ — the package

## What it is

The broker's Python package: the processes (`python -m br0kr.daemon`, `br0kr.mcp_server`, `br0kr.dashboard`,
`br0kr.oauth`, `br0kr.migrate`) and the modules they share. The [module map](../docs/architecture.md#module-map) has
one row per module: what it does, whether it writes the ledger or acts on Proxmox, and what proves it.

## Architecture

- **The race-critical seam is `admission.py`.** Every capacity grant is made there, under the scope advisory locks;
  `admit()` and `promote_pinned()` share one locked core. Capacity is freed only by the guarded-CAS transitions in
  `lease.py`.
- **The leader daemon is the only writer to Proxmox.** `daemon.py`, elected by `leader.py`, drives `reconciler.py`,
  `reaper.py`, `sweeper.py` and the job modules, all through `pve.py`'s fenced driver.
- **Inputs reach handlers through tables:** the tool registrations in `mcp_server.py`, and the route map and
  `_ACTIONS` in `dashboard.py`.
- **One ledger seam:** every connection is opened by `db.py`, which pins READ COMMITTED and adds keepalives and
  retry.

## Adding a module

1. Put the capability in a new file whose docstring opens with one line on what the file is.
2. Wire it where inputs are dispatched: a tool registration in `mcp_server.py` (with its rule in
   `envelope.RESULT_FLAG`), a route or `_ACTIONS` entry in `dashboard.py`, or a step in the daemon's tick.
3. If it grants capacity, it calls `admission.admit()` or `promote_pinned()` and adds no debit of its own; if it
   changes a lease's state, it calls a `lease.py` transition.
4. Add its row to the module map; `tests/unit/test_architecture_doc.py` fails until you do.
5. In the same commit: a unit test for the pure parts, and a real-Postgres harness (`scripts/harness_<name>.py`) for
   anything that locks or races.

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
