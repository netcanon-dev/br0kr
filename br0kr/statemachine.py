"""statemachine.py — the job state machines, declared once: each one's states in order and its legal edges.

A template build, a template replication and a lease migration each move through their states by guarded-CAS
UPDATEs (`… SET state = 'TO' WHERE id = %s AND state = 'FROM' RETURNING id`). The guard is what makes a step
race-free, and it stays hand-written beside its step: the guards differ (a FOR UPDATE SKIP LOCKED claim, a
CTE that reads the row first, an EXISTS on a sibling table), and a generic builder would flatten them. What
this module adds is the table those guards must agree with (review ARCH-04). `Machine` holds a machine's
states in lifecycle order and its edges, and derives the terminal and live sets and the SQL IN-lists, so the
modules' named state lists come from one definition instead of each its own tuple. A guard's inline IN-list is
not derived; tests/unit/test_statemachine.py checks it instead. That test parses every state-setting UPDATE in
build.py, replication.py and migration.py and fails on an edge its machine does not list (and on a declared
edge no step takes), so a new transition is declared here first.

Each machine lists what its SQL allows, not a stricter ideal: where a guard admits any live state (a
force-fail, migration's `_set_state`), the machine has those edges. Tightening one is a change to the guard
and the machine together.

lease.py declares the lease machine with this same class, beside its CAS functions (the race-critical seam).
This module imports nothing from br0kr and does no I/O, so any module can import it; lease.py does, for the
replication states its clone-orphan guard reads.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, eq=False)
class Machine:
    """One state machine. `transitions` maps each state, in lifecycle order, to the states it may move to; a
    state with no outgoing edge is terminal. Construction refuses an edge to a state the machine does not have."""

    name: str
    transitions: Mapping[str, frozenset[str]]

    def __post_init__(self) -> None:
        undeclared = sorted({to for edges in self.transitions.values() for to in edges} - set(self.transitions))
        if undeclared:
            raise ValueError(f"{self.name}: edges to undeclared states {undeclared}")
        frozen = MappingProxyType({s: frozenset(edges) for s, edges in self.transitions.items()})
        object.__setattr__(self, "transitions", frozen)

    @property
    def states(self) -> tuple[str, ...]:
        """Every state, in lifecycle order."""
        return tuple(self.transitions)

    @property
    def terminal(self) -> tuple[str, ...]:
        """The sinks: states with no outgoing edge, in lifecycle order."""
        return tuple(s for s, edges in self.transitions.items() if not edges)

    @property
    def live(self) -> tuple[str, ...]:
        """The states that still move (every non-terminal one), in lifecycle order."""
        return tuple(s for s, edges in self.transitions.items() if edges)

    def can(self, frm: str, to: str) -> bool:
        """Whether `frm` → `to` is an edge of this machine."""
        return to in self.transitions.get(frm, frozenset())

    def sql_in(self, states: Iterable[str]) -> str:
        """A SQL IN-list of `states`, in the order given: `('A','B')`. Refuses an empty list or a state the
        machine does not have, so a typo can't become a guard that quietly matches nothing."""
        chosen = tuple(states)
        unknown = [s for s in chosen if s not in self.transitions]
        if not chosen or unknown:
            raise ValueError(f"{self.name}: not an IN-list of its states: {chosen}")
        return "(" + ",".join(f"'{s}'" for s in chosen) + ")"


# A template download-and-import (template_build, migrations 0015 + 0029; br0kr/build.py writes it).
BUILD = Machine("template build", {
    "REQUESTED": frozenset({"DOWNLOADING", "FAILED"}),                 # claim_next_build; mark_failed/force_fail
    "DOWNLOADING": frozenset({"IMPORTING", "RECLAIMING", "FAILED"}),   # mark_importing; the stale reclaim
    "IMPORTING": frozenset({"READY", "RECLAIMING", "FAILED"}),         # mark_ready
    "RECLAIMING": frozenset({"RECLAIMING", "FAILED"}),   # a later reclaim retries one whose destroy raised
    "READY": frozenset(),
    "FAILED": frozenset(),
})

# A template copy to another cluster (template_replication, migration 0034; br0kr/replication.py writes it).
REPLICATION = Machine("template replication", {
    "REQUESTED": frozenset({"COPYING", "FAILED"}),                     # _claim; cancel_replication
    "COPYING": frozenset({"CONVERTING", "RECLAIMING", "FAILED"}),      # _mark_converting; _reclaim_and_fail
    "CONVERTING": frozenset({"READY", "RECLAIMING", "FAILED"}),        # _mark_ready
    "RECLAIMING": frozenset({"RECLAIMING", "FAILED"}),   # a later reclaim retries one whose destroy raised
    "READY": frozenset(),
    "FAILED": frozenset(),
})

# A lease's cross-cluster move (migration, migration 0003; br0kr/migration.py writes it). The saga runs
# REQUESTED → RESERVED → MIGRATING → DONE, or ABORTED. `_set_state`'s guard admits any live state, so the
# machine also has the edges only that guard's slack allows (REQUESTED → MIGRATING or DONE, MIGRATING →
# MIGRATING): _actuate runs only on a RESERVED or MIGRATING saga, but an overlapping leader can still reach them.
MIGRATION = Machine("lease migration", {
    "REQUESTED": frozenset({"RESERVED", "MIGRATING", "DONE", "ABORTED"}),   # _link_reservation; cancel
    "RESERVED": frozenset({"MIGRATING", "DONE", "ABORTED"}),
    "MIGRATING": frozenset({"MIGRATING", "DONE", "ABORTED"}),   # the guard admits MIGRATING again (a leader
    # overlap); _actuate itself skips re-marking a saga already MIGRATING
    "DONE": frozenset(),
    "ABORTED": frozenset(),
})
