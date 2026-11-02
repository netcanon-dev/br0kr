#!/usr/bin/env python3
"""
LIVE template-DOWNLOAD/BUILD proof against a REAL Proxmox node — exercises the new br0kr.pve actuation
methods (phase A4.1) exactly as the daemon's build flow (br0kr.reconciler.build_template) drives them, end
to end, against a throwaway build VM. NOT hermetic: it downloads a real cloud image (server-side sha256
verify), creates a build VM that imports the disk, converts it to a template, finalizes it, and proves the
build-fence safety properties — then tears it all down. Safe by construction:

  * Only ever touches the throwaway vmid in BR0KR_LIVE_VMID (must be >= 9900); refuses anything lower, so it
    can NEVER touch an existing VM. Uses a FIXED vmid (not driver.nextid()) for deterministic teardown.
  * Stamps the per-build fence (br0kr-build-<id>) — the same safety handle the reclaim relies on — and only
    applies the TEMPLATE_TAG marker AFTER template conversion (so no template=0 guest ever carries it).
  * Sanity-checks the source url against the allowlist gate (imgbuild.source_allowed) BEFORE downloading, so
    this proof can only ever fetch what the real request-time gate (admin.request_template_build) would pass.
  * Cleans up the build VM (fence-checked) AND best-effort frees the downloaded import volume at the end (and
    on failure). The wrapper (scripts/run_live_imgbuild.sh) revokes the ephemeral token + re-verifies teardown.

This proves the Proxmox-side actuation that is otherwise covered only hermetically (a mock driver in
tests/integration/test_reconciler.py). The LEDGER lifecycle (claim/mark_importing/mark_ready atomic catalog
INSERT/reclaim) is already proven on real Postgres by scripts/harness_imgbuild.py; this run proves the
DRIVER half those tests mock, in the EXACT order reconciler.build_template issues it.

Env:
  PVE_HOST           Proxmox host/IP (e.g. 192.168.1.10)
  PVE_NODE           node name (e.g. pve1)
  PVE_TOKEN_ID       an EPHEMERAL API token id  (user@realm!tokenname)   — the wrapper revokes it after
  PVE_TOKEN_SECRET   the ephemeral token secret  (piped in host-to-host, never committed/printed)
  BR0KR_LIVE_URL     the image url to download   (must be covered by BR0KR_LIVE_ALLOW)
  BR0KR_LIVE_SHA256  the image's sha256 (64 hex) — Proxmox re-verifies the fetched bytes against it
  BR0KR_LIVE_STORAGE the build storage (must support BOTH `import` and `images` content — e.g. an NFS/dir)
  BR0KR_LIVE_ALLOW   the source-allowlist prefix to test the url against (e.g. https://host/path/)
  BR0KR_LIVE_TARGET  the target catalog template id (drives the deterministic download filename)
  BR0KR_LIVE_VMID    the throwaway build vmid (must be >= 9900, e.g. 9903)
  BR0KR_BUILD_ID     a synthetic build id for the build fence (default 99001; no ledger in this proof)
"""
from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path
from urllib.parse import quote

import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import imgbuild, pve

# The broker talks to the cluster's self-signed cert with verify=False (same as every pve.Pve call on the
# trusted LAN), so urllib3 emits an InsecureRequestWarning on EVERY request — multiplied across the polling
# waits it drowns the proof output. Silence it here (cosmetic; the trust model is the documented LAN posture).
warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)

HOST = os.environ["PVE_HOST"]
NODE = os.environ["PVE_NODE"]
TOKEN_ID = os.environ["PVE_TOKEN_ID"]
TOKEN_SECRET = os.environ["PVE_TOKEN_SECRET"]
URL = os.environ["BR0KR_LIVE_URL"].strip()
SHA256 = os.environ["BR0KR_LIVE_SHA256"].strip()
STORAGE = os.environ["BR0KR_LIVE_STORAGE"]
ALLOW = [os.environ["BR0KR_LIVE_ALLOW"].strip()]
TARGET = os.environ["BR0KR_LIVE_TARGET"]
VMID = int(os.environ["BR0KR_LIVE_VMID"])
BUILD_ID = int(os.environ.get("BR0KR_BUILD_ID", "99001"))
NAME = "br0kr-live-imgbuild"

if VMID < 9900:
    raise SystemExit(f"refusing to operate on vmid {VMID} — live proof only touches throwaway 99xx vmids")

FILENAME = imgbuild.download_filename(TARGET, URL)
IMPORT_FROM = f"{STORAGE}:import/{FILENAME}"
IMPORT_VOLID = f"{STORAGE}:import/{FILENAME}"


def driver() -> pve.Pve:
    return pve.Pve(HOST, NODE, TOKEN_ID, TOKEN_SECRET, timeout=60)


def beat() -> None:
    """A tiny dotted heartbeat — stands in for the daemon's on_progress=self._beat liveness pulse, threaded
    through every long pve.wait so a multi-minute download/import doesn't look wedged."""
    sys.stdout.write(".")
    sys.stdout.flush()


def is_template(d: pve.Pve, vmid: int) -> bool:
    """Whether the guest is template=1 — read from the authoritative per-guest config (no public getter on
    the driver; reaching _get here is fine in a proof harness)."""
    cfg = d._get(f"/nodes/{d.node}/qemu/{vmid}/config")  # noqa: SLF001
    return int(cfg.get("template", 0) or 0) == 1


def free_import_volume(d: pve.Pve) -> None:
    """Best-effort delete of the downloaded import volume — the daemon LEAVES this (the documented residual);
    the proof removes it so the node is left pristine. Swallows errors (the wrapper's `pvesm free` is backstop)."""
    try:
        d._delete(f"/nodes/{d.node}/storage/{STORAGE}/content/{quote(IMPORT_VOLID, safe='')}")  # noqa: SLF001
        print(f"  (freed import volume {IMPORT_VOLID})")
    except Exception as exc:  # noqa: BLE001
        print(f"  (free import volume {IMPORT_VOLID} hit {exc!r} — wrapper will retry)")


def cleanup(d: pve.Pve) -> None:
    """Fence-checked teardown of the build VM + best-effort free of the import volume, idempotent."""
    try:
        if d.exists(VMID):
            # the guest may carry the build fence (mid-flow) or only the marker (finalized) — re-stamp the
            # fence so the fence-checked destroy always applies to OUR throwaway vmid (99xx-guarded above).
            d.configure(VMID, {"tags": f"{pve.build_fence_tag(BUILD_ID)};{pve.TEMPLATE_TAG}"})
            rc = d.destroy_build_vm(VMID, BUILD_ID)
            print(f"  (cleanup destroy of {VMID} -> {rc})")
    except Exception as exc:  # noqa: BLE001
        print(f"  (cleanup of {VMID} hit {exc!r})")
    free_import_volume(d)


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(bool(cond))
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    d = driver()
    print(f"=== LIVE template-build proof on {NODE} (download -> import -> template, build vmid {VMID}) ===")
    print(f"    url={URL}\n    storage={STORAGE}  filename={FILENAME}\n")
    cleanup(d)   # start from a clean slate

    # 0. the source url must pass the SAME allowlist gate the request-time path enforces (no I/O).
    check("source_url_passes_allowlist_gate", imgbuild.source_allowed(URL, ALLOW),
          f"allow={ALLOW[0]}")
    check("sha256_is_wellformed", imgbuild.valid_sha256(SHA256))

    try:
        # 1. download with SERVER-SIDE sha256 verification (no guest yet). A bad checksum FAILS here.
        print("• downloading (server-side sha256 verify) ", end="")
        rc = d.download_image(STORAGE, URL, SHA256, FILENAME, on_progress=beat)
        print()
        check("download_verified_ok", rc == "OK", f"rc={rc}")
        if rc != "OK":
            return _finish(results)   # nothing imported — stop

        # 2. create the build VM importing the disk, stamped with the BUILD FENCE only (template=0).
        print("• creating build VM (import-from) ", end="")
        rc = d.build_template_vm(VMID, NAME, BUILD_ID, IMPORT_FROM, STORAGE, cores=1, memory=512,
                                 on_progress=beat)
        print()
        check("build_vm_created_ok", rc == "OK", f"rc={rc}")
        check("build_vm_exists", d.exists(VMID))
        tags = d.read_tags(VMID)
        check("build_fence_stamped", pve.parse_build_fence(tags) == BUILD_ID, f"tags={tags!r}")
        check("marker_absent_pre_template", not pve.has_template_marker(tags),
              "a template=0 guest must NOT carry the marker (else the clone-orphan sweep grabs it)")
        check("not_a_template_yet", not is_template(d, VMID))

        # 2b (phase A4.2). Read the imported disk's PROVISIONED size — the quota-settle input. A project
        #    download charges this against the tenant's disk quota; the reconciler FAILS the build if it can't
        #    be read (fail-closed), so a positive GB here is the live proof of read_disk_size_gb.
        size_gb = d.read_disk_size_gb(VMID)
        check("disk_size_read_is_positive_gb", isinstance(size_gb, int) and size_gb >= 1, f"size={size_gb}GB")

        # 3. convert to a TEMPLATE, THEN tag (fence + marker) — template=1 before the marker ever lands.
        print("• converting to template ", end="")
        rc = d.make_template(VMID, BUILD_ID, on_progress=beat)
        print()
        check("make_template_ok", rc == "OK", f"rc={rc}")
        check("is_template_now", is_template(d, VMID))
        tags = d.read_tags(VMID)
        check("build_fence_kept_through_convert", pve.parse_build_fence(tags) == BUILD_ID, f"tags={tags!r}")
        check("marker_set_after_convert", pve.has_template_marker(tags))

        # 4. finalize: strip the build fence (mark_ready won) leaving a clean catalog template (marker only).
        d.finalize_template(VMID)
        tags = d.read_tags(VMID)
        check("fence_stripped_on_finalize", pve.parse_build_fence(tags) is None, f"tags={tags!r}")
        check("marker_kept_on_finalize", pve.has_template_marker(tags))

        # 5. SAFETY: a finalized catalog template carries NO build fence, so the build-orphan reclaim must
        #    REFUSE it (fence_mismatch) — a finished template is never reclaimed as a half-built one.
        rc = d.destroy_build_vm(VMID, BUILD_ID)
        check("finalized_template_protected_from_reclaim", rc == "fence_mismatch", f"rc={rc}")
        check("finalized_template_still_exists", d.exists(VMID))

        # 6. POSITIVE destroy path: re-stamp the fence (simulating a pre-finalize/failed build) and prove the
        #    fence-checked destroy reclaims it — this is the failure-cleanup + stale-reclaim handle.
        d.configure(VMID, {"tags": f"{pve.build_fence_tag(BUILD_ID)};{pve.TEMPLATE_TAG}"})
        rc = d.destroy_build_vm(VMID, BUILD_ID)
        check("fenced_build_destroyed", rc == "destroyed", f"rc={rc}")
        check("build_vm_is_gone", not d.exists(VMID))
    finally:
        cleanup(d)

    return _finish(results)


def _finish(results: list[bool]) -> int:
    n = len(results)
    allok = bool(results) and all(results)
    print("\n" + ("GO" if allok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
