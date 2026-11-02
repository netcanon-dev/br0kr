#!/bin/bash
# assert-dr-hba-safe.sh <pg_hba.conf> — DR replication-posture guard (review S09).
#
# Called by pg-primary-tls-entrypoint.sh on EVERY boot in stretched-DR mode (BEFORE handing off to
# Postgres). pg-primary-init.sh runs only ONCE, at initdb: a volume first-booted in DEFAULT (same-host
# starter) mode gets `host replication all all trust` APPENDED to pg_hba.conf. If that volume is later
# served with the DR (TLS) entrypoint — which turns TLS on AND publishes the ledger port on the mesh
# (docker-compose.primary-remote.yml) — that leftover BROAD trust rule lets ANY host that can route to
# PRIMARY_BIND:5432 `pg_basebackup` the whole ledger as the cluster superuser with NO password, while the
# operator believes replication is scram+TLS-gated per the runbook. This guard FAILS CLOSED on that state.
#
# Exit 0 = safe to serve in DR mode. Exit 1 = refuse (a REMOTE-REACHABLE trust replication rule is present,
# or the file could not be scanned). A non-fatal WARNING is printed if no scram replication rule is present.
#
# What is (and is NOT) refused — the address matters, review S09/F1:
#   * REFUSED: a `host*` replication rule with method `trust` whose address is NOT loopback — the init-written
#     `host replication all all trust`, or any hand-widened `0.0.0.0/0` / `samenet` / mesh-CIDR trust rule.
#   * EXEMPT: the stock initdb loopback lines every official-image volume carries — `local replication … trust`
#     (unix socket only, unreachable via the published TCP port) and `host replication … 127.0.0.1/32` /
#     `::1/128 … trust` (a connection through the published port never arrives with a loopback source). These
#     are the ACCEPTED same-host residual; refusing them would crash-loop every correctly-provisioned DR
#     primary on its second boot. (This mirrors scripts/run_standby_dr_smoke.sh's own narrow needle.)
#
# Field-aware via awk (present in postgres:16 AND the CI image); inline `#` comments are stripped before
# matching so a trailing "# …trust…" breadcrumb neither trips nor evades it. Known limitations (a human with
# volume access is already outside the accidental-conversion threat model): a rule split across a `\` line
# continuation, or one in an `include`/`include_dir` file, is not seen; a custom `hba_file` GUC is not read.
set -euo pipefail

HBA="${1:?usage: assert-dr-hba-safe.sh <pg_hba.conf>}"

# Missing = initdb hasn't written it yet (first boot); the DR init branch is about to write a scram rule. Safe.
[ -f "$HBA" ] || exit 0
# Unreadable = we cannot verify the posture → FAIL CLOSED (a running Postgres could still load a trust rule).
if [ ! -r "$HBA" ]; then
  echo "assert-dr-hba-safe: FATAL — $HBA is not readable; cannot verify the DR replication posture. Refusing." >&2
  exit 1
fi

# Emit each REMOTE-REACHABLE trust replication rule (empty output = none). `db_is_repl`: the `replication`
# keyword alone or as a comma-list member (`all` does NOT cover the replication pseudo-db). `is_loopback`
# keys off the ADDRESS field ($4) so an old-style `IP netmask` loopback rule is exempt too. `trust` is matched
# as any field from the address onward (so it is found whether the address is one CIDR token or two netmask
# tokens, and regardless of trailing options).
set +e
DANGER=$(awk '
  { orig = $0; sub(/#.*/, "") }                       # strip inline comment (keep the original for the report)
  NF == 0 { next }                                    # blank / whole-comment line
  {
    if ($2 != "replication" && $2 !~ /(^|,)replication($|,)/) next   # not a replication rule
    if ($1 == "local") next                                          # unix socket only — unreachable remotely
    if ($1 !~ /^host/) next                                          # only host* rules reach the TCP port
    addr = $4
    if (addr == "127.0.0.1/32" || addr == "::1/128" ||
        addr == "127.0.0.1"    || addr == "::1") next                # loopback-only → not remote-reachable
    for (i = 4; i <= NF; i++) if ($i == "trust") { print orig; next }
  }
' "$HBA")
awk_rc=$?
set -e

if [ "$awk_rc" -ne 0 ]; then
  echo "assert-dr-hba-safe: FATAL — could not scan $HBA (awk rc=$awk_rc). Refusing to start." >&2
  exit 1
fi

if [ -n "$DANGER" ]; then
  {
    echo "assert-dr-hba-safe: FATAL — this data volume has a REMOTE-REACHABLE trust replication rule:"
    printf '%s\n' "$DANGER" | sed 's/^/  /'
    echo "assert-dr-hba-safe: it was initialised in DEFAULT (same-host trust) mode and CANNOT be served in"
    echo "  stretched-DR mode — with TLS on and the ledger port published on the mesh, that rule would allow"
    echo "  UNAUTHENTICATED pg_basebackup of the whole ledger as the superuser. Refusing to start."
    echo "  Fix: convert on a FRESH volume with REPLICATION_PASSWORD set (pg-primary-init.sh's DR branch adds"
    echo "  a scram replicator role + a hostssl scram-sha-256 rule + slot) and let the standby re-clone — see"
    echo "  docs/runbooks/docker-deploy.md §9 — OR, on this volume, remove the broad (non-loopback) trust rule"
    echo "  above AND provision the scram replicator role + hostssl rule + slot before retrying."
  } >&2
  exit 1
fi

# Defense-in-depth: a DR primary should carry the scram replication rule its init added. Warn (don't fail —
# an operator may use a custom HBA) if none is present, so a mis-provisioned DR posture surfaces.
if ! awk '
  { sub(/#.*/, "") }
  NF == 0 { next }
  ($2 == "replication" || $2 ~ /(^|,)replication($|,)/) {
    for (i = 1; i <= NF; i++) if ($i == "scram-sha-256") { found = 1 }
  }
  END { exit !found }
' "$HBA"; then
  {
    echo "assert-dr-hba-safe: WARNING — no active 'replication … scram-sha-256' rule found in pg_hba.conf."
    echo "  A stretched-DR primary expects pg-primary-init.sh's DR branch to have added one; the remote"
    echo "  standby will not be able to authenticate. Proceeding (this is not a credential-bypass)."
  } >&2
fi

exit 0
