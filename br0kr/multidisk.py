"""multidisk.py — the PURE seam for MULTI-DISK lease placement: a tenant boot disk PLUS one-or-more DATA
disks, each on its own storage pool (design of record: docs/designs/multi-disk-placement.md).

Today a lease governs one disk (`lease.storage_id` + `lease.req_disk_gb`, the boot disk). The multi-disk
arc lets a tenant ALSO request N data disks, each a NEW empty volume on its own pool. This module holds the
pure, I/O-free core of that ask — the value, its validation, and the per-POOL byte aggregation the
admission floor debits. The anchoring model decision (design §2): a data disk is a NEW empty `qm set`
volume, NEVER a clone, so it carries none of the boot disk's clone/linked semantics.

Two load-bearing purities live here so the admission seam (br0kr/admission.py) and the placement resolver
(br0kr/placement.py, a later PR) can never disagree:

  * ``validate_data_disks`` — the single config-as-data gate a data-disk ask passes before any row is
    written (the ``imgbuild.validate_build_request`` pattern): a well-formed pool + positive int8 size, a
    data disk only on a TEMPLATED lease (a bare reservation clones nothing to attach to), and a count cap.
  * ``required_by_pool`` — the DEDUP+AGGREGATE that turns a ``(boot pool, boot size, data disks)`` ask into
    a per-DISTINCT-pool byte requirement. A pool named by the boot disk AND a data disk (or by two data
    disks) SUMS into ONE requirement — the load-bearing guard for BOTH the admission advisory-lock SET (one
    ``storage:{pool}`` lock per distinct pool) and the per-pool shortfall check. Under-aggregating re-opens
    the exact last-slot double-book ``admit()`` exists to prevent (design §5, §8).

Everything here is pure (no DB, no network). The ledger row is db/migrations/0046 (``lease_data_disk``). The
admission WRITER now calls this module (PR-2): ``admission.validate`` runs ``validate_data_disks`` and
``admit``/``enqueue``/``promote_pinned`` debit per-pool via ``required_by_pool``. The placement resolver, the
PVE actuation, and the request surface land in later PRs — so the arc is still INERT to live TENANTS (no
request surface populates ``AdmissionRequest.data_disks`` until PR-5), but this module is no longer unread.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# int8 (bigint) ceiling — lease_data_disk.req_disk_gb is a bigint column (0046), exactly like
# lease.req_disk_gb; mirrors admission._BIGINT_MAX so an over-range size is a CLEAN reason here, never an
# uncaught NumericValueOutOfRange escaping the child-row INSERT.
_BIGINT_MAX = 9_223_372_036_854_775_807
# The default ceiling on data disks per lease — bounds the advisory-lock set + the PVE device slots (design
# §11 open-decision #2: a hard default for the MVP, promotable to an operator_setting knob later).
MAX_DATA_DISKS = 8


@dataclass(frozen=True)
class DataDisk:
    """One tenant-requested DATA disk: the quota/landing POOL it must be created on (``storage_id``, a
    ``storage`` ROW id — the ask, immutable) and its size in whole GB (``req_disk_gb``). A NEW empty volume,
    never a clone — so, unlike the boot disk, it has no clone/linked-pin semantics (design §2). Frozen +
    hashable so an ``AdmissionRequest`` carrying a tuple of these stays hashable for the idempotency surface
    (a later PR)."""

    storage_id: str
    req_disk_gb: int


def validate_data_disks(
    data_disks: tuple[DataDisk, ...], *, has_template: bool, max_disks: int = MAX_DATA_DISKS,
) -> str | None:
    """None if the data-disk ask is well-formed, else a REASON string (the ``validate_build_request``
    contract). Checks, in a fixed order (pinned by a test):

      1. an EMPTY ask is always fine (a single-disk lease) — returns None regardless of ``has_template``;
      2. ``data_disks_require_template`` — a data disk only makes sense on a TEMPLATED lease (the boot disk
         is a clone; a bare capacity reservation materialises no guest to attach a disk to, design §6);
      3. ``too_many_data_disks`` — more than ``max_disks`` (bounds the lock set + device slots);
      4. ``data_disk_malformed`` — a blank/non-str ``storage_id``, or a ``req_disk_gb`` that is not a
         positive int at or below the int8 ceiling (``bool`` REJECTED — the JSON-true-is-1 trap, the same
         guard ``imgbuild``'s numeric checks and ``settings.validate_patch`` apply; ``admission.validate``
         rejects a bool BOOT ``req_disk_gb``/``req_mem_mb`` identically via ``boolean_resources``, so the
         boot and data disks agree on the trap).

    Pure — the whole matrix is unit-testable."""
    if not data_disks:
        return None
    if not has_template:
        return "data_disks_require_template"
    if len(data_disks) > max_disks:
        return "too_many_data_disks"
    for d in data_disks:
        if not isinstance(d.storage_id, str) or not d.storage_id:
            return "data_disk_malformed"
        if isinstance(d.req_disk_gb, bool) or not isinstance(d.req_disk_gb, int):
            return "data_disk_malformed"
        if d.req_disk_gb <= 0 or d.req_disk_gb > _BIGINT_MAX:
            return "data_disk_malformed"
    return None


_DATA_DISK_KEYS = frozenset({"storage_id", "req_disk_gb"})


def coerce_data_disks(raw: object) -> tuple[tuple[DataDisk, ...], str | None]:
    """Coerce a loosely-typed request-surface ``data_disks`` value — the JSON an MCP/HTTP client sends: a
    LIST of ``{"storage_id": <str>, "req_disk_gb": <int>}`` objects, or None/omitted — into a typed
    ``DataDisk`` tuple. Returns ``(disks, None)`` when the container SHAPE is well-formed, or ``((), reason)``
    on a shape error (always ``data_disk_malformed``). None or an empty list → ``((), None)`` (a single-disk
    lease). A shape error is:

      * ``raw`` is not a list/tuple (e.g. a bare object or a string);
      * an item that is not a JSON object (Mapping);
      * an item whose keys are not EXACTLY {storage_id, req_disk_gb} — a MISSING or an UNKNOWN key.
        Fail-closed on unknown keys so a typo like ``size_gb`` is REFUSED, never silently dropped onto a disk
        with a missing ``req_disk_gb`` (which would otherwise KeyError, or worse, coerce to a wrong size).

    Deliberately shape-ONLY: the VALUE gates (a blank/non-str pool, a non-positive/bool/over-int8 ``req_disk_gb``,
    the per-lease count cap, the template requirement) stay in ``validate_data_disks``, which the admission
    writer runs next — so a JSON ``req_disk_gb: "10"`` (a string) coerces into a DataDisk HERE and is rejected
    THERE as ``data_disk_malformed`` (one gate owns value policy, this one owns transport shape). Pure — the
    whole matrix is unit-testable without a DB or a transport."""
    if raw is None:
        return (), None
    if not isinstance(raw, (list, tuple)):
        return (), "data_disk_malformed"
    out: list[DataDisk] = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item.keys()) != _DATA_DISK_KEYS:
            return (), "data_disk_malformed"
        out.append(DataDisk(storage_id=item["storage_id"], req_disk_gb=item["req_disk_gb"]))
    return tuple(out), None


def required_by_pool(
    boot_storage_id: str | None, boot_disk_gb: int, data_disks: tuple[DataDisk, ...],
) -> dict[str, int]:
    """The per-DISTINCT-pool byte requirement of a whole multi-disk ask — the SUM of the boot disk's bytes
    (when it debits a pool: ``boot_storage_id`` set) and every data disk's bytes, keyed by pool row id. A
    pool named more than once (boot == a data pool, or two data disks on one pool) SUMS into a SINGLE entry
    — the load-bearing dedup for BOTH the admission advisory-lock SET (one ``storage:{pool}`` lock per
    distinct pool) and the per-pool shortfall (a later PR). Under-aggregating would let two disks on one
    pool each pass a check against the same headroom and double-book it (design §5).

    ``boot_storage_id`` None (a template clone with no ask pool, or a bare reservation) contributes no boot
    term. Pure."""
    out: dict[str, int] = {}
    if boot_storage_id is not None:
        out[boot_storage_id] = out.get(boot_storage_id, 0) + int(boot_disk_gb)
    for d in data_disks:
        out[d.storage_id] = out.get(d.storage_id, 0) + int(d.req_disk_gb)
    return out
