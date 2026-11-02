#!/usr/bin/env bash
# Mint a self-signed CA + a primary server cert for TLS replication (the stretched DR standby).
# Homelab-grade: a private CA you keep OFF the repo; the remote standby pins it via sslrootcert so
# `sslmode=verify-full` authenticates the primary (defeats a man-in-the-middle on the mesh).
#
#   bash docker/gen-replication-certs.sh <primary-mesh-hostname-or-IP> [out-dir]
#
# The CN/SAN MUST be the exact host the standby connects to (PRIMARY_HOST in .env.standby), or
# verify-full rejects the cert. Re-run is a no-op unless you delete the out-dir first.
set -euo pipefail

CN="${1:?usage: gen-replication-certs.sh <primary-mesh-hostname-or-IP> [out-dir]}"
OUT="${2:-./certs}"
mkdir -p "$OUT"

if [ -f "$OUT/server.key" ]; then
  echo "certs already present in $OUT (delete the dir to regenerate) — leaving them"
  exit 0
fi

# SAN type: an IPv4 literal goes in as IP:, anything else as DNS:.
SAN="DNS:${CN}"
if printf '%s' "$CN" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then SAN="IP:${CN}"; fi

cat > "$OUT/san.cnf" <<CNF
[req]
distinguished_name = dn
[dn]
[v3]
subjectAltName = ${SAN}
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
CNF

# CA (10y)
openssl req -new -x509 -days 3650 -nodes -newkey rsa:2048 \
  -keyout "$OUT/ca.key" -out "$OUT/ca.crt" -subj "/CN=br0kr-replication-ca" >/dev/null 2>&1

# Server key + CSR (CN/SAN = the primary's advertised host), signed by the CA with the SAN ext.
openssl req -new -nodes -newkey rsa:2048 -keyout "$OUT/server.key" -out "$OUT/server.csr" \
  -subj "/CN=${CN}" >/dev/null 2>&1
openssl x509 -req -in "$OUT/server.csr" -CA "$OUT/ca.crt" -CAkey "$OUT/ca.key" -CAcreateserial \
  -days 3650 -out "$OUT/server.crt" -extfile "$OUT/san.cnf" -extensions v3 >/dev/null 2>&1

chmod 600 "$OUT/server.key" "$OUT/ca.key"
rm -f "$OUT/server.csr" "$OUT/san.cnf" "$OUT/ca.srl"
echo "wrote CA (ca.crt) + primary server cert (CN/SAN=${CN}) to ${OUT}/"
echo "  - mount ${OUT} into pg-primary (docker-compose.primary-remote.yml)"
echo "  - copy ${OUT}/ca.crt to the remote standby host (docker-compose.standby.yml mounts it)"
