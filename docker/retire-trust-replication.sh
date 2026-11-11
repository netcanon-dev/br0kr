#!/bin/bash
# retire-trust-replication.sh <pg_hba.conf> <user> — replace the bundled ledger's legacy trust replication rule
# with scram (review SEC-05).
#
# Before this release, pg-primary-init.sh appended `host replication all all trust` at initdb, so the bundled
# standby cloned and streamed with no password, and so could anything else on the ledger's compose network. A
# standby cloned from such a primary carries the same line in its own copy of the file, and a standby accepts
# replication connections too. So both bundled ledger entrypoints run this on every start
# (docker/pg-primary-entrypoint.sh and docker/pg-standby-entrypoint.sh): each such line becomes
# `host replication "<user>" samenet scram-sha-256`, the rule a new volume gets at initdb. The bundled standby
# authenticates with PGPASSWORD (docker-compose.yml), so it keeps streaming without a re-clone.
#
# Conservative and idempotent: only that exact rule is touched (type host, database replication, user all,
# address all, method trust, nothing after it). The stock loopback trust lines initdb writes, a DR volume's
# hostssl rule and every other line stay byte for byte, and a file with no such rule is not written at all. Any
# other replication trust rule that a remote client could reach (one an operator wrote or edited) is left alone
# but named in a WARNING on every start. Before the first rewrite the original is kept as pg_hba.conf.pre-sec05
# (a later rewrite keeps that first copy), which also fails the run early if the volume is full. The file is rewritten in place (same inode, owner and mode),
# because Postgres requires it to stay owned by its OS user. A missing file (a first boot, before initdb) is not
# an error. A file that cannot be scanned is FATAL, so the server does not start on an unknown rule set.
set -euo pipefail
HBA="${1:?usage: retire-trust-replication.sh <pg_hba.conf> <user>}"
export BR0KR_RETIRE_ROLE="${2:?usage: retire-trust-replication.sh <pg_hba.conf> <user>}"
[ -f "$HBA" ] || exit 0
fatal() { echo "retire-trust-replication: FATAL - $1. Refusing to start." >&2; exit 1; }

legacy='!/^[[:space:]]*#/ && NF == 5 && $1 == "host" && $2 == "replication" && $3 == "all" && $4 == "all" && $5 == "trust"'
rc=0
awk "$legacy { found = 1 } END { exit !found }" "$HBA" || rc=$?
if [ "$rc" -eq 0 ]; then
  tmp="$(mktemp)" || fatal "could not create a temporary file (is /tmp writable?)"
  trap 'rm -f "$tmp"' EXIT
  # The role comes from the environment, not `awk -v`, which would interpret backslash escapes in it.
  awk "
    /^# br0kr: allow the hot standby to stream WAL \\(trust/ {
      print \"# br0kr: the hot standby streams WAL with scram, from the ledger's own network (review SEC-05).\"; next }
    $legacy { print \"host    replication    \\\"\" ENVIRON[\"BR0KR_RETIRE_ROLE\"] \"\\\"    samenet    scram-sha-256\"; next }
    { print }" "$HBA" > "$tmp" || fatal "could not rewrite $HBA"
  [ -s "$tmp" ] || fatal "the rewrite of $HBA came out empty"
  if [ ! -e "$HBA.pre-sec05" ]; then
    cp -p "$HBA" "$HBA.pre-sec05" || fatal "could not keep a copy of $HBA (is the volume full?)"
  fi
  cat "$tmp" > "$HBA" || fatal "could not write $HBA (the original is in $HBA.pre-sec05)"
  echo "retire-trust-replication: replaced the trust replication rule in $HBA with scram for $BR0KR_RETIRE_ROLE" \
    "(review SEC-05; the original is in $HBA.pre-sec05)"
elif [ "$rc" -ne 1 ]; then
  fatal "could not scan $HBA (awk rc=$rc)"
fi

# Any replication trust rule left that a TCP client off loopback can match: the same test as the S09 guard
# (docker/assert-dr-hba-safe.sh), which refuses such a rule outright once the port is published. Here the
# port is not published, so it is a warning, but the operator should know replication still needs no password.
left="$(awk '
  { orig = $0; sub(/#.*/, "") }
  NF == 0 { next }
  {
    if ($2 != "replication" && $2 !~ /(^|,)replication($|,)/) next
    if ($1 !~ /^host/) next
    addr = $4
    if (addr == "127.0.0.1/32" || addr == "::1/128" || addr == "127.0.0.1" || addr == "::1") next
    for (i = 4; i <= NF; i++) if ($i ~ /^trust\r?$/) { print orig; next }
  }' "$HBA")" || fatal "could not scan $HBA for other trust rules"
if [ -n "$left" ]; then
  {
    echo "retire-trust-replication: WARNING - $HBA still lets replication in with no password from off loopback;"
    echo "  this script changes only the exact rule older releases wrote, so this one is left as it is:"
    printf '%s\n' "$left" | sed 's/^/  /'
    echo "  Change it to scram-sha-256 (docs/runbooks/day2-operations.md, the release that retires trust replication)."
  } >&2
fi
exit 0
