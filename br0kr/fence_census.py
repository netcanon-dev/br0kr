"""fence_census.py — one fleet-wide read of every br0kr-fenced guest, and what it says about the ledger.

Every guest br0kr creates carries its lease id in a fence tag (`br0kr-lease-<id>`, br0kr/pve.py). The census lists
those tags on every served node in one pass. Three things read it:

* **The hold** (`secure`). br0kr never deletes a lease row, and `begin_provisioning` records a guest's vmid before
  the guest exists, so on a healthy ledger every fence id has a row that recorded a guest. A fence id with NO row
  means the ledger lost it: a restored backup, a promoted standby, a VM failover onto a replica a minute old. A fence
  id whose row recorded no guest means the id was minted again after such a loss. Either sets the
  `sweep_hold_absent` knob (audited), and the orphan sweep destroys no guest without a lease row until an operator
  clears it. The knob's companion column `sweep_hold_ids` records every id the hold was raised for, so an operator's
  clear sticks: the census raises the hold again only for an id not in it (new evidence, whatever its value: a node
  the census could not list may come back with a lower one), and the sweep stops sparing the ids in it.
* **The id floor** (`secure`). On a ledger that is behind, the id sequence is behind too: the next admissions would
  mint ids that guests created in the lost minutes already carry. `secure` moves the sequence past the highest fence
  id (forward only), AFTER the hold is durable: the floor alone erases the watermark signal at the next admission.
* **The rollback verdict and the clash check.** `assess` also names the absent ids at or above the ledger's highest
  lease id (the sweep's per-pass watermark, review S20), judged once over the whole fleet so a node swept before the
  one holding the evidence no longer loses its guests. `clashes` tells the provision step that a guest already carries
  an ADMITTED lease's id (one the gateway admitted before the floor moved); the daemon holds such a lease
  (`lease_id_clash`) instead of building a second guest under one fence.

The daemon takes a census at the start of its first tick as leader, before any provision, and again on every sweep
tick (tick_upkeep.census_step). A node whose listing fails is named in `failed_nodes`: its guests are unknown, so the
sweep skips that node and spares absent guests fleet-wide for the pass.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import psycopg

from br0kr import audit, lease
from br0kr.driver import Driver

log = logging.getLogger(__name__)

# The knob `secure` sets (0053). Named once: the sweep and the daemon read the same attribute.
HOLD_KNOB = "sweep_hold_absent"

# At most this many ids are kept in `sweep_hold_ids` (the lowest). An id past the cap is never acknowledged, so it
# raises the hold again at every census: the fail-safe side, and only a fleet that lost more leases than this sees it.
HOLD_IDS_CAP = 10_000


@dataclass(frozen=True)
class FenceCensus:
    """Every fenced guest on the nodes that answered: `fenced` maps node_id -> ((vmid, lease_id), ...).
    `failed_nodes` names the nodes whose listing raised; their guests are unknown, not absent."""

    fenced: Mapping[str, tuple[tuple[int, int], ...]] = field(default_factory=dict)
    failed_nodes: tuple[str, ...] = ()

    def lease_ids(self) -> set[int]:
        """Every lease id some fenced guest carries."""
        return {lid for guests in self.fenced.values() for _, lid in guests}

    def max_fence_id(self) -> int:
        """The highest lease id any fenced guest carries (0 for a fleet with none)."""
        return max(self.lease_ids(), default=0)

    def guests_of(self, lease_id: int) -> list[tuple[str, int]]:
        """The (node_id, vmid) of every guest fenced to `lease_id`."""
        return [(node, vmid) for node, guests in self.fenced.items() for vmid, lid in guests if lid == lease_id]


def take(
    drivers: Iterable[tuple[str, Driver]],
    *,
    on_progress: Callable[[], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> FenceCensus:
    """List the fenced guests of every node in `drivers` (one `list_fenced` call each). A node that raises is logged
    and named in `failed_nodes`; the others still count. `should_stop` (the daemon's `_halted`) ends the pass at a
    node boundary; the nodes not reached are named failed, so no caller mistakes a partial census for a full one."""
    fenced: dict[str, tuple[tuple[int, int], ...]] = {}
    failed: list[str] = []
    for node_id, driver in drivers:
        if should_stop is not None and should_stop():
            failed.append(node_id)
            continue
        try:
            fenced[node_id] = tuple((int(v), int(lid)) for v, lid in driver.list_fenced())
        except Exception:
            log.exception("fence census: listing node %s failed; its guests are not judged this pass", node_id,
                          extra={"node_id": node_id})
            failed.append(node_id)
        if on_progress is not None:
            on_progress()
    return FenceCensus(fenced=fenced, failed_nodes=tuple(failed))


@dataclass(frozen=True)
class Verdict:
    """The census read against the ledger. `absent` = every fence id with no lease row; `unrecorded` = every fence id
    whose row recorded no guest (vmid NULL: the id was minted again after a loss); both are the hold's evidence.
    `ahead` = the absent ids at or above `watermark`, the ledger's highest lease id (the sweep's per-pass watermark
    signal); `acknowledged` = `sweep_hold_ids`, the ids a hold was already raised for; `hold_on` =
    `sweep_hold_absent` as the ledger has it; `partial` = some node could not be listed."""

    watermark: int
    max_fence_id: int
    absent: tuple[int, ...] = ()
    ahead: tuple[int, ...] = ()
    unrecorded: tuple[int, ...] = ()
    acknowledged: frozenset[int] = frozenset()
    hold_on: bool = False
    partial: bool = False

    @property
    def new_evidence(self) -> tuple[int, ...]:
        """The evidence ids (absent or unrecorded) no hold has been raised for yet, sorted."""
        return tuple(sorted(set(self.absent).union(self.unrecorded) - self.acknowledged))

    @property
    def rollback_suspected(self) -> bool:
        """The watermark signal, less what an operator has already reviewed: an absent id at or above the ledger's
        highest that is not in `acknowledged`. The sweep spares every absent guest while it holds."""
        return any(lid not in self.acknowledged for lid in self.ahead)


def assess(census: FenceCensus, watermark: int, known_ids: Iterable[int],
           acknowledged: Iterable[int] = (), hold_on: bool = False,
           unrecorded_ids: Iterable[int] = ()) -> Verdict:
    """Pure: the verdict for `census`, given the ledger's `watermark` (lease.max_lease_id), which of the census's lease
    ids have a lease row (`known_ids`) and which of those recorded no guest (`unrecorded_ids`), `sweep_hold_ids`
    (`acknowledged`) and `sweep_hold_absent`."""
    known = set(known_ids)
    ids = census.lease_ids()
    absent = tuple(sorted(lid for lid in ids if lid not in known))
    unrecorded = tuple(sorted(lid for lid in set(unrecorded_ids) if lid in ids and lid in known))
    return Verdict(watermark=watermark, max_fence_id=census.max_fence_id(), absent=absent,
                   ahead=tuple(lid for lid in absent if lid >= watermark), unrecorded=unrecorded,
                   acknowledged=frozenset(int(i) for i in acknowledged), hold_on=hold_on,
                   partial=bool(census.failed_nodes))


def hold_state(conn: psycopg.Connection[Any]) -> tuple[bool, frozenset[int]]:
    """(`sweep_hold_absent` is on, `sweep_hold_ids`) straight from the ledger, never from the daemon's hot-applied
    knob: every census and every sweep pass reads the hold through this, so a settings step that keeps failing
    cannot leave it off (in a fresh process the knob starts False). A read error raises; the tick fails closed."""
    row = conn.execute("SELECT COALESCE(sweep_hold_absent, false), COALESCE(sweep_hold_ids, '{}'::bigint[]) "
                       "FROM operator_setting WHERE id = 1").fetchone()
    if not conn.autocommit:
        conn.commit()   # end the read transaction, like the other ledger reads
    if row is None:
        return False, frozenset()
    return bool(row[0]), frozenset(int(i) for i in (row[1] or ()))


def read_verdict(conn: psycopg.Connection[Any], census: FenceCensus) -> Verdict:
    """`assess` against the live ledger: the watermark, which fence ids have rows (and which of those recorded no
    guest), and the hold's two columns."""
    watermark = lease.max_lease_id(conn)
    owners = lease.owners_of(conn, census.lease_ids())
    hold_on, acknowledged = hold_state(conn)
    return assess(census, watermark, owners.keys(), acknowledged, hold_on,
                  unrecorded_ids=[lid for lid, o in owners.items() if o.vmid is None])


def clashes(census: FenceCensus | None, lease_id: int) -> bool:
    """Whether a guest already carries `lease_id`'s fence: for an ADMITTED lease (which has built nothing yet) that
    guest belongs to an earlier lease minted under the same id. False without a census."""
    return census is not None and lease_id in census.lease_ids()


@dataclass(frozen=True)
class Secured:
    """What `secure` found and changed: `hold_on` = the hold's state in the ledger afterwards; `hold_set` = this call
    turned it on; `floor_raised` = the id the sequence moved to (None: no move needed, or the lock timed out)."""

    hold_on: bool = False
    hold_set: bool = False
    floor_raised: int | None = None


def _set_hold(conn: psycopg.Connection[Any], new: tuple[int, ...]) -> bool:
    """Turn the hold on and add the `new` evidence ids to `sweep_hold_ids` (kept distinct and sorted, the lowest
    HOLD_IDS_CAP of them), in ONE statement so the two never disagree, and commit it. False if the singleton row is
    missing (nothing to hold with: logged)."""
    row = conn.execute(
        "UPDATE operator_setting SET sweep_hold_absent = true, "
        "sweep_hold_ids = ARRAY(SELECT DISTINCT i FROM unnest(COALESCE(sweep_hold_ids, '{}'::bigint[]) "
        "|| %s::bigint[]) AS u(i) ORDER BY i LIMIT %s), updated_at = now() "
        "WHERE id = 1 RETURNING id", (list(new), HOLD_IDS_CAP)).fetchone()
    if not conn.autocommit:
        conn.commit()   # durable before the floor moves, and before audit.record (which needs a committed write)
    if row is None:
        log.error("fence census: operator_setting has no row; cannot set %s", HOLD_KNOB)
    return row is not None


def secure(conn: psycopg.Connection[Any], verdict: Verdict) -> Secured:
    """Act on a verdict (from `read_verdict`), the hold FIRST: it is the fail-safe half. With new evidence (a fence id
    with no row, or whose row recorded no guest, not already in `sweep_hold_ids`), turn `sweep_hold_absent` on, record
    the ids, commit, audit and log CRITICAL. Then move the lease id sequence past the highest fence id (forward only;
    a no-op on a healthy ledger, whose sequence is past every fence, and lock-free then). The floor never moves
    without the hold: if the hold could not be set (no settings row), the floor waits too, because the floor alone
    erases the watermark signal at the next admission. A lock timeout on the floor is logged and left for the next
    census; any other ledger error raises, after the hold is already durable."""
    hold_on = verdict.hold_on
    hold_set = False
    new = verdict.new_evidence
    if new:
        if not _set_hold(conn, new):
            log.error("fence census: the hold could not be set, so the lease id floor is not moved either "
                      "(%d evidence id(s), %d..%d)", len(new), new[0], new[-1], extra={"absent_count": len(new)})
            return Secured(hold_on=hold_on)
        hold_on = hold_set = True
        absent, unrecorded = set(verdict.absent), set(verdict.unrecorded)
        audit.record(conn, surface="daemon", action="sweep_hold_absent_set", actor="fence_census",
                     params={"watermark": verdict.watermark, "absent": [i for i in new if i in absent][:20],
                             "unrecorded": [i for i in new if i in unrecorded][:20], "absent_count": len(new)})
        log.critical("LEDGER BEHIND THE FLEET: %d fenced guest(s) name lease ids the ledger has no row for, or whose "
                     "row recorded no guest (%d..%d; its highest is %d). Set sweep_hold_absent: the sweep now spares "
                     "every guest without a lease row until an operator clears it (docs/runbooks/ledger-ha.md).",
                     len(new), new[0], new[-1], verdict.watermark,
                     extra={"absent_count": len(new), "watermark": verdict.watermark})
    floor: int | None = None
    if verdict.max_fence_id > 0:
        try:
            floor = lease.raise_id_floor(conn, verdict.max_fence_id)
        except psycopg.errors.LockNotAvailable:
            log.warning("fence census: could not lock lease to move the id floor to %d; retrying next census",
                        verdict.max_fence_id, extra={"to_id": verdict.max_fence_id})
    if floor is not None:
        audit.record(conn, surface="daemon", action="lease_id_floor_raised", actor="fence_census",
                     params={"floor": floor, "watermark": verdict.watermark})
        log.critical("LEDGER BEHIND THE FLEET: a guest carries lease id %d but the ledger's id sequence had not "
                     "reached it; moved the sequence so new leases start above %d", floor, floor,
                     extra={"to_id": floor, "watermark": verdict.watermark})
    return Secured(hold_on=hold_on, hold_set=hold_set, floor_raised=floor)
