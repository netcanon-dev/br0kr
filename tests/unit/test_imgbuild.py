"""Unit tests for the template-download validation seam (br0kr/imgbuild.py, phase A4.1).

This module is the PURE supply-chain gate for the riskiest template slice (downloading a net-new image), so
its policy is pinned hard here — especially `source_allowed`, where a naive prefix (or path-traversal)
match would be a real bypass. The actuation (the actual fetch/import/templatize) is covered hermetically by
a mock driver in tests/integration/test_reconciler.py AND proven LIVE on a lab node (PVE 8.4.17) via
scripts/live_imgbuild.py (download→import→templatize→finalize + the build-fence safety properties, GO 18/18
on 2026-06-29). The LEDGER lifecycle is proven on real Postgres in scripts/harness_imgbuild.py. What CAN
drift silently is exactly these pure checks.
"""
from __future__ import annotations

import pytest

from br0kr import imgbuild

pytestmark = pytest.mark.unit

_GOOD = "a" * 64   # a well-formed (if fake) sha-256
_ALLOW = ["https://cloud.debian.org/images/", "https://cloud-images.ubuntu.com/"]


def test_valid_sha256_requires_exactly_64_hex():
    """A SHA-256 must be exactly 64 hex chars. Guards a blank/short/garbage digest passing as a checksum —
    which would make the (mandatory) integrity check a silent no-op, defeating the supply-chain guarantee."""
    assert imgbuild.valid_sha256("A" * 64) is True            # case-insensitive hex
    assert imgbuild.valid_sha256("0123456789abcdef" * 4) is True
    for bad in ("", "abc", "g" * 64, "a" * 63, "a" * 65, " " + "a" * 63):
        assert imgbuild.valid_sha256(bad) is False


def test_source_allowed_matches_scheme_host_and_path_prefix():
    """A url is allowed only when it shares an allowlist entry's https scheme, EXACT host, and path prefix.
    Guards the legitimate case working — an operator-curated mirror path is fetchable."""
    assert imgbuild.source_allowed("https://cloud.debian.org/images/bookworm/latest/x.qcow2", _ALLOW) is True
    assert imgbuild.source_allowed("https://cloud-images.ubuntu.com/jammy/x.img", _ALLOW) is True
    assert imgbuild.source_allowed("https://CLOUD.debian.ORG/images/x", _ALLOW) is True   # host case-insensitive


def test_source_allowed_rejects_lookalike_host_the_bypass():
    """The load-bearing test: a look-alike host (`cloud.debian.org.evil.com`) that a naive
    `url.startswith(prefix)` WOULD accept is REFUSED, because the match is on the parsed host, not a string
    prefix. Guards the exact supply-chain bypass — fetching attacker-controlled bytes that merely embed a
    trusted prefix as a substring of the hostname."""
    assert imgbuild.source_allowed("https://cloud.debian.org.evil.com/images/x.qcow2", _ALLOW) is False
    # an embedded-credential netloc whose hostname matches must also be refused
    assert imgbuild.source_allowed("https://user@cloud.debian.org/images/x", _ALLOW) is False
    # a different host entirely, and a path that doesn't start with the allowed path
    assert imgbuild.source_allowed("https://evil.com/images/x", _ALLOW) is False
    assert imgbuild.source_allowed("https://cloud.debian.org/other/x", _ALLOW) is False


def test_source_allowed_matches_path_at_a_segment_boundary_not_a_substring():
    """The path half of the match is SEGMENT-BOUNDED, not a bare substring: a prefix WITHOUT a trailing
    slash (`/debian-cloud-images`) must NEVER match the sibling `/debian-cloud-images-attacker/…` — the
    cross-namespace supply-chain bypass on shared-host object stores (GCS/S3 buckets-as-path-prefixes) a
    naive `url.startswith(prefix)` would allow. It must still match the exact path AND any true child path.
    Guards the review's confirmed MEDIUM: the checksum can't save you here (the attacker picks both the URL
    and its digest, so the server-side verify passes on the malicious image)."""
    allow = ["https://storage.googleapis.com/debian-cloud-images"]   # no trailing slash
    assert imgbuild.source_allowed("https://storage.googleapis.com/debian-cloud-images-attacker/evil.qcow2",
                                   allow) is False   # the bypass — REFUSED
    assert imgbuild.source_allowed("https://storage.googleapis.com/debian-cloud-images/bookworm/x.qcow2",
                                   allow) is True    # a true child path — allowed
    assert imgbuild.source_allowed("https://storage.googleapis.com/debian-cloud-images", allow) is True  # exact
    # a bare-host prefix still trusts every path on that host (the intended host-only allowlist)
    assert imgbuild.source_allowed("https://mirror.example/any/where/x.img", ["https://mirror.example"]) is True


def test_source_allowed_rejects_dot_segment_and_percent_traversal():
    """The segment-boundary match runs on the RAW (un-normalized) path, so a `..` or percent-encoded
    `%2e%2e` segment would pass the boundary check (`/images/../images-evil/x` literally starts with
    `/images/`) yet RESOLVE to a sibling namespace once the HTTP client follows it. So source_allowed
    rejects ANY path carrying a `.`/`..` segment or percent-encoding, for BOTH trailing-slash and
    no-trailing-slash prefixes. Guards the R2 finding: the traversal re-opening of the segment-boundary fix."""
    for allow in (["https://cloud.debian.org/images/"], ["https://cloud.debian.org/images"]):
        assert imgbuild.source_allowed("https://cloud.debian.org/images/../images-evil/x.qcow2", allow) is False
        assert imgbuild.source_allowed("https://cloud.debian.org/images/%2e%2e/images-evil/x.qcow2", allow) is False
        assert imgbuild.source_allowed("https://cloud.debian.org/images/%2E%2E/evil.qcow2", allow) is False
        # a legitimate deep child path with no traversal still passes (the guard isn't over-broad)
        assert imgbuild.source_allowed("https://cloud.debian.org/images/bookworm/latest/x.qcow2", allow) is True


def test_source_allowed_requires_https_and_rejects_control_chars():
    """A non-https scheme (downgrade) and a url carrying any control char / whitespace / newline (an
    injection vector into the Proxmox download-url API) are refused. Guards a plaintext-fetch downgrade and
    a CRLF/space-smuggled url reaching the actuation."""
    assert imgbuild.source_allowed("http://cloud.debian.org/images/x", _ALLOW) is False    # not https
    assert imgbuild.source_allowed("ftp://cloud.debian.org/images/x", _ALLOW) is False
    assert imgbuild.source_allowed("https://cloud.debian.org/images/x\n", _ALLOW) is False  # newline
    assert imgbuild.source_allowed("https://cloud.debian.org/images/ x", _ALLOW) is False   # space


def test_source_allowed_is_fail_closed_on_empty_or_malformed_allowlist():
    """An empty allowlist allows NOTHING (the default-deny posture — no source is trusted until the operator
    adds one), and a malformed/non-https allowlist entry is ignored rather than matching. Guards a download
    surface that is open before any source is curated, and a junk allowlist row widening the gate."""
    assert imgbuild.source_allowed("https://cloud.debian.org/images/x", []) is False
    assert imgbuild.source_allowed("https://cloud.debian.org/images/x", ["not-a-url"]) is False
    assert imgbuild.source_allowed("https://cloud.debian.org/images/x", ["http://cloud.debian.org/"]) is False


def test_valid_template_id_requires_a_dns_slug():
    """A catalog template id must be an RFC-1123 dns-slug because the replication engine interpolates it into a
    PVE guest name (`repl-<job>-<id>`, replication._replica_name), which PVE 400s otherwise. valid_template_id
    accepts dot-separated labels of [A-Za-z0-9] with interior dashes, bounded at MAX_TEMPLATE_ID_LEN; it rejects
    every shape that would build+catalog fine yet BRICK every clone_shared/rebuild_source of the template —
    the exact residual PR #124 deferred (a spaces/underscore/paren id passing ingress), plus the empty-label /
    edge-dash / trailing-newline / over-length edges of the same hole."""
    for good in ("debian12", "ubuntu-noble", "ubuntu-24.04", "a", "24", "ABC-123",
                 "x" * imgbuild.MAX_TEMPLATE_ID_LEN):
        assert imgbuild.valid_template_id(good) is True
    for bad in ("", "ubuntu_noble", "my tmpl (v2)", "-foo", "foo-", "foo..bar", "foo.", ".foo",
                "foo/bar", "foo bar", "foo\n", "x" * (imgbuild.MAX_TEMPLATE_ID_LEN + 1)):
        assert imgbuild.valid_template_id(bad) is False
    # Drift-guard the 40<->60 coupling the length cap exists for: a max-length id + a 14-digit job id must still
    # fit replication._replica_name's `[:60]` guest-name budget (repl-<job>-<id>), so a bump of either constant
    # can't silently reopen the truncation trailing-dash edge the CHANGELOG claims closed.
    assert len(f"repl-{10 ** 14 - 1}-" + "x" * imgbuild.MAX_TEMPLATE_ID_LEN) <= 60


def test_validate_build_request_orders_its_reasons():
    """The pre-write gate returns the specific reason in order: missing target, then a NON-dns-slug target id
    (`invalid_target_template_id`, since it becomes a catalog id fed to the replication guest name), then
    missing storage/url, a malformed SHA-256, then the ALLOWLIST (the security control — an un-allowlisted url
    is `source_not_allowed`). A well-formed, allowlisted request passes. Guards a request reaching the ledger/
    daemon with a bad id, a bad checksum, or an untrusted source, and an over-strict gate rejecting a good one."""
    base = {"url": "https://cloud.debian.org/images/x.qcow2", "sha256": _GOOD,
            "target_template_id": "deb12", "storage_id": "local-zfs", "allowed_prefixes": _ALLOW}
    assert imgbuild.validate_build_request(**base) is None
    assert imgbuild.validate_build_request(**{**base, "target_template_id": ""}) == "missing_target_template_id"
    assert imgbuild.validate_build_request(
        **{**base, "target_template_id": "bad id"}) == "invalid_target_template_id"
    assert imgbuild.validate_build_request(**{**base, "storage_id": ""}) == "missing_storage_id"
    assert imgbuild.validate_build_request(**{**base, "url": ""}) == "missing_url"
    assert imgbuild.validate_build_request(**{**base, "sha256": "short"}) == "invalid_sha256"
    assert imgbuild.validate_build_request(**{**base, "url": "https://evil.com/x"}) == "source_not_allowed"


def test_valid_source_prefix_requires_https_and_a_host():
    """An allowlist ENTRY is usable only if it is a control-char-free https URL with a host — else it would
    silently never match and admin rejects it (`invalid_source_prefix`). Guards a dead allowlist row (an
    operator thinks they trusted a source but no download ever matches) and a non-https/garbage prefix."""
    assert imgbuild.valid_source_prefix("https://cloud.debian.org/images/") is True
    assert imgbuild.valid_source_prefix("https://cloud.debian.org") is True
    for bad in ("", "http://cloud.debian.org/", "ftp://x/", "https:///nopath", "not-a-url",
                "https://cloud.debian.org/\n"):
        assert imgbuild.valid_source_prefix(bad) is False


def test_download_filename_is_safe_and_deterministic():
    """The download filename is `br0kr-<sanitised id>.<ext>`: the target id with any non `[A-Za-z0-9._-]`
    char replaced (so a path separator / `..` / shell char can never reach the storage path), and the url's
    image extension NORMALISED to one PVE's content=import accepts (qcow2/raw/vmdk). Deterministic so a retry
    reuses the file. Guards a path-traversal / injection via the target id or the url, and — the case a live
    build hit — an `.img`/`.qcow`/absent extension that PVE's download-url rejects ('invalid filename or wrong
    extension'): it must normalise to qcow2, not pass `.img` through. The extension selects the format PVE
    enforces post-fetch, and Ubuntu's cloud image IS a qcow2 named `.img`, so qcow2 is the right default (a
    raw-content `.img` would instead need hosting under `.raw`); without it every Ubuntu build 400s at download."""
    assert imgbuild.download_filename("debian12", "https://x/deb.qcow2") == "br0kr-debian12.qcow2"  # kept
    assert imgbuild.download_filename("x", "https://x/d.raw") == "br0kr-x.raw"            # raw kept
    assert imgbuild.download_filename("x", "https://x/d.vmdk") == "br0kr-x.vmdk"          # vmdk kept
    assert imgbuild.download_filename("deb 12", "https://x/y.img") == "br0kr-deb_12.qcow2"  # .img → qcow2
    assert imgbuild.download_filename("x", "https://x/legacy.qcow") == "br0kr-x.qcow2"    # legacy .qcow → qcow2
    assert imgbuild.download_filename("../etc/passwd", "https://x/y.img") == "br0kr-.._etc_passwd.qcow2"
    assert imgbuild.download_filename("x", "https://x/no-extension") == "br0kr-x.qcow2"   # default → qcow2
    assert imgbuild.download_filename("x", "https://x/a.QCOW2") == "br0kr-x.qcow2"        # ext lowercased


def test_is_broker_import_filename_round_trips_download_filename():
    """The reap DELETE guard (`pve.free_import_volume` ← `is_broker_import_filename`) must NEVER refuse a
    LEGITIMATE reap: for ANY target id + url, `is_broker_import_filename(download_filename(id, url))` is True.
    Pins the exact inverse relationship so the guard can't drift STRICTER than the producer and start leaking
    (refusing) real import scratch — the property the auto-reap depends on. Covers the build id form, the
    rebuild `repl-<n>-<template>` form (replication.py's DOWNLOAD filename — the name a future rebuild reap would
    pass; the rebuild path does NOT reap yet, per the CHANGELOG adjacent-gap note), a spaces/punctuation id, a
    path-bearing id (whose `/` download_filename sanitises to `_`, so the output carries no separator), the [:96]
    cap, and every _IMPORT_EXTS extension + the `.img`/`.qcow`/absent → qcow2 normalisation."""
    ids = ["debian12", "ubuntu-noble-verify", "repl-3-ubuntu-noble-verify", "deb 12", "../etc/passwd", "x",
           "A" * 200]
    for i in ids:
        for u in ["https://x/y.qcow2", "https://x/y.raw", "https://x/y.vmdk", "https://x/y.img", "https://x/y.qcow",
                  "https://x/no-ext"]:
            fn = imgbuild.download_filename(i, u)
            assert imgbuild.is_broker_import_filename(fn) is True, f"guard refused a legit reap target: {fn!r}"


def test_is_broker_import_filename_rejects_non_broker_and_path_bearing():
    """The guard must REFUSE anything `download_filename` could NOT have produced, so the reap's pool-wide
    `Datastore.Allocate` can never delete a NON-broker volume: a guest disk (`vm-<n>-disk-*`), a template base
    disk (`base-*`), a BACKUP (`vzdump-*`), a bare/foreign name, a WRONG extension (iso/img/qcow/none/empty),
    a path-bearing value (a `/`, or a full `pool:import/...` volid mistakenly passed as the filename), a
    trailing-space name, the empty string, and a non-str. Guards the exact delete-arbitrary-volume escalation
    this seam exists to prevent."""
    for bad in ["vm-101-disk-0.qcow2", "base-101-disk-0.qcow2", "vzdump-qemu-101-2025_09_28-02_00_00.vma.zst",
                "debian12.qcow2", "br0kr-x.iso", "br0kr-x.img", "br0kr-x.qcow", "br0kr-x", "br0kr-.qcow2",
                "../etc/passwd", "br0kr-x.qcow2/../../y", "omv:import/br0kr-x.qcow2", "", "  ", "br0kr-x.qcow2 "]:
        assert imgbuild.is_broker_import_filename(bad) is False, f"guard wrongly accepted: {bad!r}"
    assert imgbuild.is_broker_import_filename(None) is False   # non-str (a build row with a NULL filename)
    assert imgbuild.is_broker_import_filename(123) is False    # non-str


def test_vm_name_sanitises_a_display_name_to_a_valid_pve_dns_name():
    """The PVE guest `name` must be an RFC-1123 dns-name; an operator's human display name routinely isn't one,
    so vm_name maps it to a SINGLE valid label (every non-[A-Za-z0-9-] char — dots included — → '-', runs
    collapsed, edges stripped, lower-cased, 63-capped). Guards the case a live build hit — a name with spaces
    and parens ('Ubuntu 24.04 (build verify)') 400s the qemu-create 'Parameter verification failed' — and the
    fallback chain: an all-punctuation/None display name falls back to the (sanitised) target id, and both
    empty to a constant, so a build can NEVER send an invalid or empty guest name to PVE."""
    assert imgbuild.vm_name("Ubuntu 24.04 (build verify)", "ubuntu-noble") == "ubuntu-24-04-build-verify"
    assert imgbuild.vm_name("debian-12", "deb") == "debian-12"              # already valid → kept (lower-cased)
    assert imgbuild.vm_name("  --Weird__Name!!  ", "deb") == "weird-name"   # collapse + edge-strip + underscores
    assert imgbuild.vm_name(None, "deb 12") == "deb-12"                     # no display name → sanitised target id
    assert imgbuild.vm_name("()  ()", "!!!") == "br0kr-tmpl"               # both empty after sanitise → constant
    assert imgbuild.vm_name("A" * 80, "deb") == "a" * 63                   # capped at the 63-char label limit
    assert imgbuild.vm_name("x" * 62 + "-yy", "deb") == "x" * 62          # cap lands on '-' → trailing dash stripped


def test_valid_size_gb_bounds_the_declared_disk_budget():
    """A phase-A4.2 project download DECLARES a disk budget (size_gb) reserved against its quota up front, so
    valid_size_gb must accept only a positive integer within MAX_TEMPLATE_DISK_GB and reject everything else.
    Guards: a zero/negative declaration (would reserve nothing / corrupt the sum), an absurd one (overflow the
    quota math), and the Python footgun that True/False are ints (a bool budget is nonsense) and a float/str
    is the wrong type. The size is the load-bearing input the reserve-then-settle quota is built on."""
    assert imgbuild.valid_size_gb(1) is True
    assert imgbuild.valid_size_gb(100) is True
    assert imgbuild.valid_size_gb(imgbuild.MAX_TEMPLATE_DISK_GB) is True
    for bad in (0, -1, imgbuild.MAX_TEMPLATE_DISK_GB + 1, True, False, 1.5, "5", None):
        assert imgbuild.valid_size_gb(bad) is False  # type: ignore[arg-type]


def test_terminal_states_match_the_lifecycle():
    """is_terminal flags READY/FAILED and nothing else, and the BUILD_STATES tuple is the full 0015+0029 CHECK
    set (RECLAIMING added by 0029 — the claimed-for-teardown state). Guards the daemon's 'claim a live build' /
    admin's 'one active build per target' diverging from the DB's state machine — a drift would let a terminal
    build look claimable or an active one look done. RECLAIMING is NON-terminal (still reclaimable)."""
    assert imgbuild.is_terminal("READY") and imgbuild.is_terminal("FAILED")
    for live in ("REQUESTED", "DOWNLOADING", "IMPORTING", "RECLAIMING"):
        assert not imgbuild.is_terminal(live)
    assert set(imgbuild.BUILD_STATES) == {
        "REQUESTED", "DOWNLOADING", "IMPORTING", "RECLAIMING", "READY", "FAILED"}
    assert {"READY", "FAILED"} == imgbuild.TERMINAL_STATES
