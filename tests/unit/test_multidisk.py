"""Unit tests for br0kr/multidisk.py — the pure core of the MULTI-DISK placement arc (design
docs/designs/multi-disk-placement.md), plus wiring needles pinning that the admission floor and the
inventory used_gb query gained the lease_data_disk debit term (0046).

Each test states the failure it guards against. The aggregation tests are the load-bearing ones: an
under-aggregating required_by_pool re-opens the exact last-slot double-book admit() exists to prevent.
"""
from __future__ import annotations

from br0kr import admission, inventory, multidisk
from br0kr.multidisk import DataDisk, coerce_data_disks, required_by_pool, validate_data_disks

_INT8_MAX = 9_223_372_036_854_775_807


def test_datadisk_is_frozen_and_hashable() -> None:
    """DataDisk must be frozen+hashable so an AdmissionRequest carrying a tuple of them stays hashable for
    the idempotency surface (a later PR) — a mutable/​unhashable disk would break request equality/replay."""
    d = DataDisk(storage_id="poolB", req_disk_gb=100)
    assert hash(d) == hash(DataDisk(storage_id="poolB", req_disk_gb=100))
    assert d.storage_id == "poolB" and d.req_disk_gb == 100


def test_validate_empty_ask_is_ok_regardless_of_template() -> None:
    """An empty data-disk tuple is a plain single-disk lease — it must validate whether or not the lease is
    templated (a non-templated lease with NO data disks is the untouched status quo). Guards a false
    data_disks_require_template refusal of every existing single-disk request."""
    assert validate_data_disks((), has_template=False) is None
    assert validate_data_disks((), has_template=True) is None


def test_validate_data_disks_require_a_template() -> None:
    """A data disk on a NON-templated lease is refused: a bare capacity reservation clones nothing to attach
    a disk to (design §6). Guards a data disk silently requested on a template-less lease that could never
    be actuated."""
    disks = (DataDisk("poolB", 50),)
    assert validate_data_disks(disks, has_template=False) == "data_disks_require_template"
    assert validate_data_disks(disks, has_template=True) is None


def test_validate_rejects_too_many_data_disks() -> None:
    """More than max_disks is refused (bounds the advisory-lock set + PVE device slots). Guards an
    unbounded request holding a huge lock set / exhausting device slots."""
    over = tuple(DataDisk(f"pool{i}", 10) for i in range(multidisk.MAX_DATA_DISKS + 1))
    assert validate_data_disks(over, has_template=True) == "too_many_data_disks"
    at_cap = tuple(DataDisk(f"pool{i}", 10) for i in range(multidisk.MAX_DATA_DISKS))
    assert validate_data_disks(at_cap, has_template=True) is None


def test_validate_rejects_malformed_disks() -> None:
    """A blank/non-str pool or a non-positive / over-int8 / bool size is data_disk_malformed. bool is
    rejected explicitly (the JSON-true-is-1 trap): a JSON `true` size must not silently debit 1 GB. Guards a
    malformed disk reaching the child-row INSERT as an opaque DB error. (Note: the BOOT path in
    admission.validate does not yet reject a bool req_disk_gb — a known asymmetry to close in PR-2.)"""
    tpl = {"has_template": True}
    assert validate_data_disks((DataDisk("", 10),), **tpl) == "data_disk_malformed"
    assert validate_data_disks((DataDisk("poolB", 0),), **tpl) == "data_disk_malformed"
    assert validate_data_disks((DataDisk("poolB", -5),), **tpl) == "data_disk_malformed"
    assert validate_data_disks((DataDisk("poolB", _INT8_MAX + 1),), **tpl) == "data_disk_malformed"
    assert validate_data_disks((DataDisk("poolB", True),), **tpl) == "data_disk_malformed"  # bool, not int
    # a well-formed disk at the int8 ceiling is fine
    assert validate_data_disks((DataDisk("poolB", _INT8_MAX),), **tpl) is None


def test_validate_check_precedence_is_pinned() -> None:
    """The reason PRECEDENCE (require_template BEFORE too_many BEFORE malformed) is a surface PR-5 clients
    will branch on — pin it with COMPOUND violations so a silent reorder during PR-2 integration can't slip
    through green. Guards e.g. a swapped count-before-template gate returning 'too_many_data_disks' where
    'data_disks_require_template' is documented."""
    over = tuple(DataDisk(f"pool{i}", 10) for i in range(multidisk.MAX_DATA_DISKS + 1))
    # over-count AND no template -> the template gate wins
    assert validate_data_disks(over, has_template=False) == "data_disks_require_template"
    # over-count AND a malformed disk among them -> the count gate wins over the per-disk malformed loop
    over_with_bad = (*over[:-1], DataDisk("", 10))
    assert validate_data_disks(over_with_bad, has_template=True) == "too_many_data_disks"


def test_required_by_pool_sums_boot_and_data() -> None:
    """The boot ask and each data disk contribute to their pool's requirement. Guards a data disk that
    debits nothing (over-sell) or a boot disk dropped from the requirement."""
    req = required_by_pool("poolA", 100, (DataDisk("poolB", 200), DataDisk("poolC", 50)))
    assert req == {"poolA": 100, "poolB": 200, "poolC": 50}


def test_required_by_pool_sums_boot_equal_to_a_data_pool() -> None:
    """A data disk on the SAME pool as the boot disk SUMS into one requirement — the load-bearing dedup: a
    per-disk check against the same pool headroom would let boot(100)+data(50) each pass against a 120 GB
    pool and double-book it. This is the single most important correctness property of PR-2's floor."""
    req = required_by_pool("poolA", 100, (DataDisk("poolA", 50),))
    assert req == {"poolA": 150}


def test_required_by_pool_sums_two_data_disks_on_one_pool() -> None:
    """Two data disks on the same pool SUM (not the last-writer's size). Guards the same double-book among
    data disks alone."""
    req = required_by_pool(None, 0, (DataDisk("poolB", 30), DataDisk("poolB", 70)))
    assert req == {"poolB": 100}


def test_required_by_pool_no_boot_ask_omits_the_boot_term() -> None:
    """A template clone with no ask pool (or a bare reservation) has boot_storage_id None → no boot term,
    only data disks. Guards a spurious None-keyed requirement entry."""
    req = required_by_pool(None, 0, (DataDisk("poolB", 40),))
    assert req == {"poolB": 40}
    assert required_by_pool(None, 0, ()) == {}


# ── coerce_data_disks: the request-surface JSON→typed shape gate (PR-5) ────────────────────────────────────
def test_coerce_none_and_empty_are_a_single_disk_lease() -> None:
    """None (the omitted param) and an empty list both coerce to an empty tuple with NO reason — the
    single-disk request path. Guards the surface treating 'no data disks' as an error, which would break
    every existing single-disk request the moment the param exists."""
    assert coerce_data_disks(None) == ((), None)
    assert coerce_data_disks([]) == ((), None)


def test_coerce_wellformed_list_becomes_datadisk_tuple() -> None:
    """A list of {storage_id, req_disk_gb} objects coerces to the matching ordered DataDisk tuple, reason
    None — the typed value admission.validate/admit then consume. Guards a mis-mapping (wrong field, dropped
    order) that would attach the wrong disk to the wrong pool."""
    disks, reason = coerce_data_disks([{"storage_id": "poolB", "req_disk_gb": 50},
                                       {"storage_id": "poolC", "req_disk_gb": 10}])
    assert reason is None
    assert disks == (DataDisk("poolB", 50), DataDisk("poolC", 10))


def test_coerce_rejects_non_list_container() -> None:
    """A non-list `data_disks` (a bare object, a string, a number) is data_disk_malformed — the transport
    contract is a LIST of disks. Guards a client passing a single object instead of a list and it being
    silently mis-parsed instead of cleanly refused."""
    assert coerce_data_disks({"storage_id": "poolB", "req_disk_gb": 50}) == ((), "data_disk_malformed")
    assert coerce_data_disks("poolB") == ((), "data_disk_malformed")
    assert coerce_data_disks(5) == ((), "data_disk_malformed")


def test_coerce_rejects_non_object_item_and_wrong_keys() -> None:
    """Each item must be an object whose keys are EXACTLY {storage_id, req_disk_gb}: a non-object item, a
    MISSING key, or an UNKNOWN key (a typo like size_gb) is data_disk_malformed — fail-closed, so a typo'd
    field is refused, NEVER silently dropped onto a disk with a missing size. Guards the silent-drop that
    would attach a disk the tenant never fully specified (or KeyError at construction)."""
    assert coerce_data_disks(["poolB"]) == ((), "data_disk_malformed")                     # item not an object
    assert coerce_data_disks([{"storage_id": "poolB"}]) == ((), "data_disk_malformed")      # missing req_disk_gb
    assert coerce_data_disks([{"req_disk_gb": 50}]) == ((), "data_disk_malformed")          # missing storage_id
    assert coerce_data_disks(                                                               # unknown key (typo)
        [{"storage_id": "poolB", "req_disk_gb": 50, "size_gb": 99}]) == ((), "data_disk_malformed")


def test_coerce_defers_value_policy_to_validate_data_disks() -> None:
    """coerce is SHAPE-only: a well-shaped item with a BAD VALUE (a JSON-string size, a bool, a blank pool)
    coerces here without error — the value gate is validate_data_disks, run next by the admission writer. Pin
    the seam split so a value check is not accidentally duplicated into (or dropped from) coerce: the string
    size passes coerce, then validate rejects the SAME tuple as data_disk_malformed."""
    disks, reason = coerce_data_disks([{"storage_id": "poolB", "req_disk_gb": "10"}])   # a JSON string size
    assert reason is None and disks == (DataDisk("poolB", "10"),)                        # shape ok...
    assert validate_data_disks(disks, has_template=True) == "data_disk_malformed"        # ...value rejected there


def test_admission_storage_avail_debits_lease_data_disk() -> None:
    """Wiring needle: admission._STORAGE_AVAIL must subtract the lease_data_disk term with the SAME
    _ACTIVE_SQL state filter as the boot term — a fake cursor can't catch a missing SQL term, so pin the
    query text. Guards the data-disk floor silently dropping out of the debit (over-sell, the unsafe
    direction)."""
    sql = admission._STORAGE_AVAIL
    assert "lease_data_disk" in sql
    # the data term joins the disk to its lease and filters on the same active-state set as the boot term
    assert sql.count(admission._ACTIVE_SQL) >= 2


def test_inventory_list_storage_mirrors_the_data_disk_term() -> None:
    """Wiring needle: inventory._LIST_STORAGE's used_gb must ADD the same lease_data_disk term admit
    subtracts, WITH the same active-state filter, or the dashboard's per-pool used/available drifts from the
    actual debit. Guards a stale inventory mirror AND a data term missing its _ACTIVE_SQL clause (which would
    show a PENDING/terminal lease's data disk as used)."""
    assert "lease_data_disk" in inventory._LIST_STORAGE
    # _ACTIVE_SQL must appear in the boot sum, the data term, AND lease_count → at least 3 uses; a data term
    # that dropped its state filter would leave only 2.
    assert inventory._LIST_STORAGE.count(inventory._ACTIVE_SQL) >= 3
