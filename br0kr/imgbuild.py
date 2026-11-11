"""imgbuild.py — the PURE validation seam for operator-driven template downloads (phase A4.1).

A4 lets an operator bring a NET-NEW OS image into the catalog by DOWNLOADING it (vs A1–A3, which pointed
the catalog at an image the operator had already prepared by hand). That is the riskiest template slice —
an arbitrary image download is a supply-chain + disk-blowup hazard — so EVERY download is gated by two
operator controls that this module encodes, with NO I/O so they are exhaustively unit-testable:

  * a SOURCE ALLOWLIST — the url must be covered by an operator-curated trusted prefix (`source_allowed`);
  * a MANDATORY SHA-256 — a 64-hex digest the downloader verifies against the fetched bytes
    (`valid_sha256`); the actual verification is server-side in Proxmox's download-url, this checks the
    operator supplied a well-formed one up front.

The allowlist match is the load-bearing security decision, so it is NOT a naive `str.startswith` (which a
look-alike host like `https://cloud.debian.org.evil.com/` would defeat) — the url and each prefix are
URL-PARSED and matched on https-scheme + EXACT host + path-prefix AT A SEGMENT BOUNDARY (so a prefix
`/images` matches `/images/x` but never the sibling `/images-evil/x`). Like the cluster host SSRF guard, the
url is also rejected if it carries control characters / whitespace (it is handed to the Proxmox API).

Everything here is pure (no DB, no network): the build LIFECYCLE actuation lives in br0kr/reconciler.py +
br0kr/daemon.py, the ledger rows in db/migrations/0015, the operator surface in br0kr/admin.py +
br0kr/dashboard.py. `validate_build_request` is the single gate admin.request_template_build runs before
any row is written.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from br0kr import reasons

# A build is live until it reaches one of these; mirrors the lease lifecycle's terminal set. Kept here (the
# pure module) so the daemon's "claim a live build" and admin's "one active build per target" agree on one
# definition — a drift guard pins it against the 0015 CHECK in tests.
TERMINAL_STATES: frozenset[str] = frozenset({"READY", "FAILED"})
BUILD_STATES: tuple[str, ...] = (
    "REQUESTED", "DOWNLOADING", "IMPORTING", "RECLAIMING", "READY", "FAILED")
# The CLAIMED states: a build the daemon has picked up (out of REQUESTED) and that is not yet terminal —
# DOWNLOADING/IMPORTING while it actuates, plus RECLAIMING once the stale-build reclaim has claimed it for
# teardown (review S04). A vmid is NOT held for the whole window — a DOWNLOADING build has none (the download
# runs guest-free); only an IMPORTING (or a RECLAIMING build claimed out of IMPORTING) carries the allocated
# vmid. These — and ONLY these — are eligible for the stale-build reclaim (review S04/F5a): a still-REQUESTED
# build is QUEUED, not stalled (no guest, never started), so reclaiming it would falsely FAIL a job whose only
# fault is a backlog ahead of it. Defined as the non-requested, non-terminal middle of the lifecycle so the
# daemon's reclaim scan and build.stale_builds/claim_stale_for_reclaim agree on one set (build.py derives its
# SQL IN-list straight from this set, so the two can't drift). RECLAIMING stays in the set precisely so a
# destroy that raises leaves the build reclaimable and a later cutoff retries it.
CLAIMED_STATES: frozenset[str] = frozenset({"DOWNLOADING", "IMPORTING", "RECLAIMING"})

_SHA256_RE = re.compile(r"\A[0-9a-fA-F]{64}\Z")
# A control char / whitespace / newline in a url is an injection vector (the url is passed to the Proxmox
# download-url API and logged) — reject any, mirroring the cloud-init / cluster-host guards.
_CONTROL_RE = re.compile(r"[\x00-\x20\x7f]")


def is_terminal(state: str) -> bool:
    """Whether a build state is terminal (READY/FAILED) — i.e. no daemon will actuate it further. Pure."""
    return state in TERMINAL_STATES


# A sane ceiling on a single declared template-disk budget (GB) — rejects an absurd/overflowing declaration
# before it reaches the per-project quota math. Not a security control (the per-project ceiling is); a guard
# against a typo (1e9 GB) wedging the sum. 16 TiB is far above any real OS image's virtual disk.
MAX_TEMPLATE_DISK_GB = 16384


def valid_size_gb(size_gb: int) -> bool:
    """Whether a declared template-disk budget is a usable positive integer GB within `MAX_TEMPLATE_DISK_GB`.
    The phase-A4.2 project download reserves this against the project's disk quota up front (the actual image
    size is unknown until downloaded), so a blank/zero/negative/absurd value must be refused before the
    reservation. Pure, so the policy is unit-testable without a DB."""
    return isinstance(size_gb, int) and not isinstance(size_gb, bool) and 0 < size_gb <= MAX_TEMPLATE_DISK_GB


# A catalog template id is NOT just a label — the replication engine INTERPOLATES it into a PVE GUEST NAME
# (`replication._replica_name` = ``repl-<job_id>-<template_id>``, the create/clone name AND the adopt-identity
# guard's expected_name), and PVE 400s "Parameter verification failed" unless that name is an RFC-1123 dns-name.
# A build / hand-register that accepted a space/underscore/paren id would land a catalog row that BUILDS and
# clones-once fine (download_filename + vm_name sanitise their OWN derived names) yet 400s EVERY clone_shared /
# rebuild_source of that template forever — a per-template brick, invisible in the node-rotation telemetry. So
# the id is charset-gated at INGRESS to a dns-slug: dot-separated labels, each [A-Za-z0-9]-bounded with interior
# [A-Za-z0-9-] (NO leading/trailing dash, NO empty label around a dot), `\A..\Z`-anchored (a trailing newline
# can't smuggle a control char into the guest name). The consumption seam (`_replica_name`) is left UNTOUCHED —
# it is identity-bearing (a lossy rewrite there could mismatch an already-landed clone's live name).
_TEMPLATE_ID_RE = re.compile(
    r"\A[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\Z")
# Bounded so ``repl-<job_id>-<template_id>`` stays inside `_replica_name`'s [:60] budget for any job id of up
# to 14 digits (`len("repl-") + 14 + len("-") + 40 == 60`, ~10^14 jobs), so within that range the ingress gate
# ALSO closes the [:60]-truncation trailing-dash edge, not just charset. A drift-guard in test_imgbuild.py pins
# the 40<->60 coupling so a bump of either constant can't silently falsify that.
MAX_TEMPLATE_ID_LEN = 40


def valid_template_id(template_id: str) -> bool:
    """Whether `template_id` is a catalog id SAFE to interpolate into the replication engine's PVE guest name
    (`replication._replica_name` = ``repl-<job>-<template_id>``, which PVE 400s unless it is an RFC-1123
    dns-name). Valid = a non-empty, <=`MAX_TEMPLATE_ID_LEN`-char dns-slug: dot-separated labels, each starting
    AND ending alphanumeric with interior ``[A-Za-z0-9-]`` — so NO space/underscore/paren, NO leading/trailing
    dash, NO empty label around a dot, no trailing newline. Gated at INGRESS (`validate_build_request` +
    `admin.create_template`) so a would-be `ubuntu_noble` / `my tmpl (v2)` id is refused UP FRONT rather than
    landing a catalog row that builds+clones-once fine but then bricks every clone_shared / rebuild_source of
    it. Pure, so the policy is unit-testable without a DB or a driver."""
    return bool(template_id) and len(template_id) <= MAX_TEMPLATE_ID_LEN \
        and _TEMPLATE_ID_RE.match(template_id) is not None


def valid_sha256(digest: str) -> bool:
    """Whether `digest` is a well-formed SHA-256 (exactly 64 hex chars). The download itself is verified
    against this server-side by Proxmox; this only guarantees the operator supplied a syntactically valid
    digest (a blank/short/garbage one would make the checksum a no-op). Pure."""
    return bool(_SHA256_RE.match(digest or ""))


def valid_source_prefix(prefix: str) -> bool:
    """Whether `prefix` is a usable allowlist entry: a control-char-free, https URL with a host. An entry
    that fails this would silently never match anything (source_allowed ignores it), so admin rejects it up
    front (`invalid_source_prefix`) rather than persisting a dead allowlist row. Pure."""
    if not prefix or _CONTROL_RE.search(prefix):
        return False
    try:
        p = urlsplit(prefix)
    except ValueError:
        return False
    if "%" in p.path or any(seg in (".", "..") for seg in p.path.split("/")):
        return False   # a dot-segment / percent-encoded allowlist entry can't be matched reliably — reject
    return p.scheme == "https" and bool(p.hostname)


def source_allowed(url: str, allowed_prefixes: list[str]) -> bool:
    """Whether `url` is covered by the operator's source allowlist — the load-bearing supply-chain gate.

    A match requires the url and an allowlisted prefix to share: the **https** scheme (a downgraded/other
    scheme never matches), the EXACT host (netloc, case-insensitive — so a look-alike like
    `cloud.debian.org.evil.com` or an embedded-credential netloc cannot slip past), AND the url's path
    COVERED BY the prefix path at a SEGMENT boundary (`_path_covered`). This is deliberately NOT a bare
    `url.startswith(prefix)` on EITHER half: a host startswith would accept `cloud.debian.org.evil.com`, and
    a PATH startswith would accept the sibling `/debian-cloud-images-attacker/…` against the prefix
    `/debian-cloud-images` (a cross-namespace bypass on shared-host object stores). A url carrying any
    control char / whitespace is rejected outright. A malformed url or a malformed/non-https prefix never
    matches (fail-closed). Pure."""
    if not url or _CONTROL_RE.search(url):
        return False
    try:
        u = urlsplit(url)
    except ValueError:
        return False
    if u.scheme != "https" or not u.hostname:
        return False
    # Reject path TRAVERSAL / percent-encoding the prefix comparison can't see through: urlsplit does NOT
    # normalize `..`/`.` segments nor decode `%2e`, so `/images/../images-evil/x` (or `%2e%2e`) would pass
    # the segment-boundary check yet RESOLVE to a sibling namespace once Proxmox's HTTP client follows it —
    # re-opening the cross-namespace bypass. No legitimate trusted-mirror url needs either, so fail closed.
    if "%" in u.path or any(seg in (".", "..") for seg in u.path.split("/")):
        return False
    for p in allowed_prefixes:
        if not p:
            continue
        try:
            pp = urlsplit(p)
        except ValueError:
            continue
        if pp.scheme != "https" or not pp.hostname:
            continue   # an operator allowlist entry must itself be a well-formed https URL
        # Compare host via .hostname (drops any userinfo/port ambiguity) + a path prefix. The url's own
        # netloc must also be free of userinfo (a `user@host` url whose hostname matches must NOT pass).
        if "@" in u.netloc:
            return False
        if u.hostname.lower() == pp.hostname.lower() and _path_covered(u.path, pp.path):
            return True
    return False


def _path_covered(url_path: str, prefix_path: str) -> bool:
    """Whether `url_path` is covered by `prefix_path` at a PATH-SEGMENT boundary — NOT a bare substring.
    A bare-host prefix (empty/"/" path) trusts the whole host (intended). Otherwise the url path must EQUAL
    the prefix or extend it at a `/` boundary, so the prefix `/debian-cloud-images` can NEVER match the
    sibling `/debian-cloud-images-attacker/evil.qcow2` (the cross-namespace bypass on shared-host object
    stores a naive startswith would allow). Pure."""
    base = prefix_path.rstrip("/")
    if base == "":
        return True   # bare host (or path "/") → trust every path on the host
    return url_path == base or url_path.startswith(base + "/")


_EXT_RE = re.compile(r"\.(qcow2|img|raw|vmdk|qcow)\Z", re.IGNORECASE)
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]")
# PVE's `content=import` download-url accepts ONLY these disk-image extensions — it 400s "invalid filename or
# wrong extension" on anything else, notably `.img` (the name Ubuntu gives its — qcow2 — cloud image) and the
# legacy `.qcow`. The extension is NOT cosmetic: PVE derives the disk FORMAT from it and, AFTER the fetch, probes
# the downloaded bytes and DIES if the detected format doesn't match it (untrusted-import validation). So
# normalising an `.img`/`.qcow`/absent extension to qcow2 is a bet the source IS qcow2 — true for the dominant
# `.img`-named cloud images (Ubuntu et al.); a genuinely-RAW `.img` source must instead be hosted/allowlisted
# under a `.raw` name, or it downloads in full and only then fails PVE's post-fetch format probe.
_IMPORT_EXTS: frozenset[str] = frozenset({"qcow2", "raw", "vmdk"})


def download_filename(target_template_id: str, url: str) -> str:
    """A SAFE, deterministic filename to download an image to on the Proxmox storage: `br0kr-<id>.<ext>`
    where <id> is the target template id with any non `[A-Za-z0-9._-]` char replaced (so it can never carry
    a path separator / `..` / shell-special char into the storage path) and <ext> is the url's recognised
    image extension NORMALISED to one PVE's `content=import` accepts (`qcow2`/`raw`/`vmdk`) — an `img`/`qcow`/
    absent extension becomes `qcow2`. The extension SELECTS the disk format PVE enforces (it rejects `.img` at the
    download gate, then probes the fetched bytes and dies on a format mismatch), so qcow2 is a bet the source is
    qcow2 — true for the dominant `.img`-named cloud images, but a raw-content `.img` must instead be hosted under
    a `.raw` name (see `_IMPORT_EXTS`). Deterministic so a retry of the same build reuses the same file. Pure."""
    safe = _SAFE_ID_RE.sub("_", target_template_id)[:96] or "image"
    m = _EXT_RE.search(urlsplit(url).path)
    ext = m.group(1).lower() if m else "qcow2"
    if ext not in _IMPORT_EXTS:
        ext = "qcow2"
    return f"br0kr-{safe}.{ext}"


# The reap DELETE guard's allow-shape: EXACTLY what `download_filename` can emit — `br0kr-<sanitised id>.<ext>`
# with the id body `[A-Za-z0-9._-]{1,96}` (the producer's `[:96]` cap) and <ext> in `_IMPORT_EXTS`. The body
# admits no `/`, so a matching name can never escape the pool's `import/` dir. Built from `_IMPORT_EXTS` — each
# ext `re.escape`d — so it tracks the producer exactly and a metachar-bearing ext could never silently widen it.
_BROKER_IMPORT_RE = re.compile(
    r"br0kr-[A-Za-z0-9._-]{1,96}\.(?:" + "|".join(re.escape(e) for e in sorted(_IMPORT_EXTS)) + r")")


def is_broker_import_filename(filename: str) -> bool:
    """True iff `filename` is one `download_filename` could have PRODUCED — a broker-owned import artifact
    ``br0kr-<sanitised id>.<ext>`` (<ext> in ``_IMPORT_EXTS``). This is `download_filename`'s SAFETY INVERSE: the
    reap DELETE seam (`pve.free_import_volume`) gates EVERY reap on it, so the pool-wide ``Datastore.Allocate`` the
    reap needs can NEVER be turned — by a bug, a malformed build row, or an injected value — into deleting an
    ARBITRARY volume on the pool (a backup, another guest's disk); it can only ever free broker import scratch. The
    ``[A-Za-z0-9._-]`` body admits no ``/``, so the guarded volid ``<pool>:import/<filename>`` can never escape the
    pool's ``import/`` dir. Pure. **ROUND-TRIP INVARIANT (pinned by a test): for ANY id + url,
    ``is_broker_import_filename(download_filename(id, url))`` is True** — the guard never refuses a legitimate
    reap. Rejects everything else (``vm-<n>-disk-*``, ``base-*``, ``vzdump-*``, a bare/foreign name, a wrong
    extension, a path-bearing value, a non-str)."""
    return isinstance(filename, str) and _BROKER_IMPORT_RE.fullmatch(filename) is not None


_VM_NAME_RE = re.compile(r"[^A-Za-z0-9-]")
_DASH_RUN_RE = re.compile(r"-{2,}")
# PVE's guest `name` must be an RFC-1123 dns-name — the qemu-create 400s "Parameter verification failed" on a
# name with a space / paren / underscore or a leading/trailing '-'. A template's HUMAN display name (e.g.
# "Ubuntu 24.04 (build verify)") is operator-chosen for the CATALOG and routinely isn't one, so the build seam
# must derive a valid guest name from it (the catalog row keeps the raw display name — see `vm_name`).


def vm_name(name: str | None, target_template_id: str) -> str:
    """A valid Proxmox guest name (a single RFC-1123 DNS label) derived from a template's human display `name`,
    falling back to the `target_template_id` then a constant. PVE rejects a qemu-create whose `name` isn't a
    dns-name (a space/paren/underscore, or a leading/trailing '-'), and a display name like
    ``Ubuntu 24.04 (build verify)`` is none — so the build seam sanitises it to a guest name while the CATALOG
    row keeps the raw display name (build.mark_ready stamps template.name from the job row, NOT from this).

    Mapping: every char outside ``[A-Za-z0-9-]`` (dots included, so the result is ONE label — never an empty
    label around a dot) becomes ``-``; adjacent dashes collapse; leading/trailing dashes strip; the result is
    lower-cased and capped at the 63-char DNS-label limit (re-stripped so the cap can't leave a trailing dash).
    If `name` sanitises to empty (all-punctuation / None), the `target_template_id` is tried the same way; if
    THAT is empty too, a constant ``br0kr-tmpl`` is returned — so this NEVER yields an invalid or empty name.
    Pure, so the whole policy is unit-testable without a driver."""
    for candidate in (name, target_template_id):
        slug = _DASH_RUN_RE.sub("-", _VM_NAME_RE.sub("-", candidate or "")).strip("-")
        slug = slug[:63].strip("-").lower()
        if slug:
            return slug
    return "br0kr-tmpl"


def validate_build_request(
    *, url: str, sha256: str, target_template_id: str, storage_id: str, allowed_prefixes: list[str]
) -> str | None:
    """The single pre-write gate for a download request (admin.request_template_build AND the tenant
    gateway.request_template_download run it). Returns a rejection reason or None. Order (matching the code):
    the target id (missing → then its dns-slug CHARSET, `invalid_target_template_id` — the id becomes a catalog
    id INTERPOLATED into a PVE guest name by the replication engine, so it must be a dns-slug up front,
    `valid_template_id`), the remaining required fields (storage, url), the SHA-256 format, then the ALLOWLIST
    (the last is the security control — an un-allowlisted url is `source_not_allowed`). The cluster/storage/
    target existence + the one-active-build rule are checked in the verb against the DB; this is the pure,
    no-I/O half. Pure, so the whole policy is unit-testable without a database or a network."""
    if not target_template_id:
        return reasons.MISSING_TARGET_TEMPLATE_ID
    if not valid_template_id(target_template_id):
        return reasons.INVALID_TARGET_TEMPLATE_ID
    if not storage_id:
        return reasons.MISSING_STORAGE_ID
    if not url:
        return reasons.MISSING_URL
    if not valid_sha256(sha256):
        return reasons.INVALID_SHA256
    if not source_allowed(url, allowed_prefixes):
        return reasons.SOURCE_NOT_ALLOWED
    return None
