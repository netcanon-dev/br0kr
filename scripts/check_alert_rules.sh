#!/usr/bin/env bash
# Validate the shipped Prometheus alert rules (docker/prometheus/br0kr-alerts.yml) with promtool: `check rules`
# (syntax, PromQL, every field) and `test rules` (br0kr-alerts.test.yml: each alert fires on its condition and stays
# quiet on the near-misses). CI runs this; run it after changing a rule or a threshold. Needs docker; no network
# beyond pulling the image, no ports.
#
# The image is pinned by digest. To bump it, pull the new tag and copy its digest:
#   docker pull prom/prometheus:vX.Y.Z && docker inspect --format '{{index .RepoDigests 0}}' prom/prometheus:vX.Y.Z
# The rules use keep_firing_for, so promtool must be 2.42 or newer.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RULES="$(cd "${HERE}/../docker/prometheus" && pwd)"
IMAGE="${PROMTOOL_IMAGE:-prom/prometheus:v3.15.0@sha256:efd719c99d83b060d9daefdcf00360461adf279f45ef5391f8d111892118753e}"

promtool() {
  docker run --rm --network none --entrypoint promtool -v "${RULES}:/rules:ro" -w /rules "$IMAGE" "$@"
}
promtool --version | head -1
promtool check rules br0kr-alerts.yml
promtool test rules br0kr-alerts.test.yml
echo "alert rules: OK"
