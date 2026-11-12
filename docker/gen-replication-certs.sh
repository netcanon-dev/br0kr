#!/usr/bin/env bash
# Mint a self-signed CA + a primary server cert for TLS replication (the stretched DR standby).
# Homelab-grade: a private CA you keep OFF the repo; the remote standby pins it via sslrootcert so
# `sslmode=verify-full` authenticates the primary (defeats a man-in-the-middle on the mesh).
#
#   bash docker/gen-replication-certs.sh <primary-mesh-hostname-or-IP> [out-dir] [ca-key-dir]
#
# The CN/SAN MUST be the exact host the standby connects to (PRIMARY_HOST in .env.standby), or
# verify-full rejects the cert. Re-run is a no-op unless you delete the out-dir first.
#
# out-dir (default ./certs) gets ca.crt, server.crt and server.key: it is what pg-primary mounts. The CA's
# private key goes to ca-key-dir (default <out-dir>-ca), which nothing mounts: a CA key inside the container
# would let anything that reads the mount mint a cert the standby trusts. Keep it offline after minting.
set -euo pipefail

CN="${1:?usage: gen-replication-certs.sh <primary-mesh-hostname-or-IP> [out-dir] [ca-key-dir]}"
OUT="${2:-./certs}"
OUT="${OUT%/}"; [ -n "$OUT" ] || OUT=/      # ./certs/ (tab completion) must not give ./certs/-ca
CA_DIR="${3:-${OUT}-ca}"
CA_DIR="${CA_DIR%/}"; [ -n "$CA_DIR" ] || CA_DIR=/
mkdir -p "$OUT"

if [ -f "$OUT/server.key" ]; then
  echo "certs already present in $OUT (delete the dir to regenerate) — leaving them"
  if [ -f "$OUT/ca.key" ]; then
    echo "WARNING: $OUT/ca.key is the CA's private key, and $OUT is mounted into pg-primary." >&2
    echo "         Move it out (mkdir -p '$CA_DIR' && mv '$OUT/ca.key' '$CA_DIR/') and keep it offline." >&2
  fi
  exit 0
fi
# The key directory must not be the mounted directory or inside it. Create it 0700 only if it is new, so a
# pre-existing directory passed as the third argument (a home directory, say) keeps its mode.
created_ca_dir=0
if [ ! -d "$CA_DIR" ]; then mkdir -p -m 700 "$CA_DIR"; created_ca_dir=1; fi
out_abs="$(cd "$OUT" && pwd -P)"
ca_abs="$(cd "$CA_DIR" && pwd -P)"
case "$ca_abs/" in
  "$out_abs/"*)
    [ "$created_ca_dir" = 1 ] && rmdir "$CA_DIR" 2>/dev/null
    echo "ERROR: ca-key-dir ($CA_DIR) is inside out-dir ($OUT), which pg-primary mounts; pick one outside it" >&2
    exit 2;;
esac

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
  -keyout "$CA_DIR/ca.key" -out "$OUT/ca.crt" -subj "/CN=br0kr-replication-ca" >/dev/null 2>&1

# Server key + CSR (CN/SAN = the primary's advertised host), signed by the CA with the SAN ext.
openssl req -new -nodes -newkey rsa:2048 -keyout "$OUT/server.key" -out "$OUT/server.csr" \
  -subj "/CN=${CN}" >/dev/null 2>&1
openssl x509 -req -in "$OUT/server.csr" -CA "$OUT/ca.crt" -CAkey "$CA_DIR/ca.key" -CAcreateserial \
  -days 3650 -out "$OUT/server.crt" -extfile "$OUT/san.cnf" -extensions v3 >/dev/null 2>&1

chmod 600 "$OUT/server.key" "$CA_DIR/ca.key"
rm -f "$OUT/server.csr" "$OUT/san.cnf" "$OUT/ca.srl" "$CA_DIR/ca.srl"
echo "wrote CA (ca.crt) + primary server cert (CN/SAN=${CN}) to ${OUT}/; the CA key to ${CA_DIR}/ca.key"
echo "  - mount ${OUT} into pg-primary (docker-compose.primary-remote.yml); it holds no private CA key"
echo "  - keep ${CA_DIR} off the server once you no longer need to mint (it signs certs the standby trusts)"
echo "  - copy ${OUT}/ca.crt to the remote standby host (docker-compose.standby.yml mounts it)"
