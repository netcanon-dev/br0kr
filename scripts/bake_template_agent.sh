#!/usr/bin/env bash
# bake_template_agent.sh — install + enable qemu-guest-agent INTO a cloud image on a Proxmox node, using
# libguestfs's virt-customize, so every guest cloned from a template built on that image boots with a RUNNING
# guest agent. br0kr REQUIRES the guest agent for IP discovery (`cloudinit.pick_guest_ip` reads the agent's
# network-get-interfaces): a template whose guest has no running agent never reports an IP, so the daemon can't
# stamp it and the guest is orphaned/reaped. Run this on the node against the freshly DOWNLOADED image FILE,
# BEFORE importing it as a template — never against a template's in-use backing disk (a linked clone's overlays
# would be corrupted, and virt-customize's live-disk guard is unreliable on NFS). See docs/runbooks/vm-access.md.
#
# Usage:  bake_template_agent.sh <image-file>
#   e.g.  bake_template_agent.sh /var/lib/vz/template/qcow/debian-12-genericcloud-amd64.qcow2
#
# Requires: libguestfs-tools (virt-customize) on the node (`apt install libguestfs-tools`). Operates OFFLINE on the
# disk file, so the template/VM must NOT be running (virt-customize refuses a live disk). Idempotent: installing
# an already-present package and enabling an already-enabled service are both no-ops, so re-running is safe.
set -euo pipefail

PKG="qemu-guest-agent"
SVC="qemu-guest-agent"

img="${1:-}"
if [ -z "$img" ]; then
  echo "usage: $0 <image-file>" >&2
  exit 2
fi
if ! command -v virt-customize >/dev/null 2>&1; then
  echo "ERROR: virt-customize not found — install libguestfs-tools on this node" >&2
  exit 3
fi
if [ ! -f "$img" ]; then
  echo "ERROR: image file not found: $img" >&2
  exit 4
fi

echo "[bake] installing '$PKG' (+ best-effort enable of '$SVC') into $img (offline virt-customize)"
# --install is the LOAD-BEARING step: it adds the package via the image's own package manager, and on Debian/
# Ubuntu the agent unit is DEVICE-activated (BindsTo the virtio guest-agent port), so install alone yields a
# running agent. The enable is BEST-EFFORT insurance for images that ship the package present but preset-off /
# with an [Install] section — `2>/dev/null || true` so an image whose unit has NO [Install] (systemctl enable
# would exit 1) can't fail the whole bake after --install already succeeded.
virt-customize -a "$img" \
  --install "$PKG" \
  --run-command "systemctl enable $SVC 2>/dev/null || true"

echo "[bake] done — a template built from $img yields clones that boot with $SVC running"
