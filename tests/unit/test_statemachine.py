"""The job state machines (br0kr/statemachine.py) and the guarded UPDATEs that must agree with them (review ARCH-04).

A build, a replication and a migration each move by guarded-CAS UPDATEs written beside their steps. These tests
read every state-setting UPDATE in br0kr/build.py, br0kr/replication.py and br0kr/migration.py, work out which
states it moves FROM (its WHERE guard) and TO (its SET), and fail on an edge the table's machine does not list,
on a declared edge no step takes, and on any UPDATE, guard or SET value the scan cannot read. So "is this edge
legal?" is answered by one table, and a new transition is declared there first. They also pin each machine's
states to the schema that stores them, its live states to the partial indexes over them, and the modules'
named state lists to the machines.
"""
from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path
from types import ModuleType

import pytest

from br0kr import imgbuild, lease, migration, replication, statemachine

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS = sorted((_ROOT / "db" / "migrations").glob("*.sql"))

# table -> (its state column, its machine). template_instance has no machine (its lifecycle is not one of the
# job sagas); the edges these modules may take on it are listed below instead.
_TABLES: dict[str, tuple[str, statemachine.Machine | None]] = {
    "template_build": ("state", statemachine.BUILD),
    "template_replication": ("job_state", statemachine.REPLICATION),
    "migration": ("state", statemachine.MIGRATION),
    "lease": ("state", lease.LEASE),
    "template_instance": ("instance_state", None),
}
_INSTANCE_STATES = ("READY", "REPLICATING", "FAILED", "RETIRED")
_INSTANCE_EDGES = {("REPLICATING", "READY"), ("REPLICATING", "FAILED")}   # a replication's own catalog row
# Every state-setting UPDATE each saga module holds today. Exact, so a step dropping out of the scan fails; add
# one here when you add a step.
_SITES = {"build": 6, "replication": 12, "migration": 3}
_UPDATE = re.compile(r"\bUPDATE\s+(?:ONLY\s+)?(" + "|".join(_TABLES) + r")\b(?:\s+(?:AS\s+)?(?!SET\b)(\w+))?\s+SET\s+",
                     re.IGNORECASE)


def _quoted(text: str) -> set[str]:
    return set(re.findall(r"'(\w+)'", text))


def test_a_machine_derives_its_sets_and_refuses_what_it_does_not_have():
    """A Machine keeps its states in the order declared, derives terminal (no outgoing edge) and live (the rest)
    from its edges, answers can(), renders an IN-list in the order given, and refuses an edge to an undeclared
    state, an empty IN-list or an unknown state in one. Guards a typo becoming a guard that matches nothing, or a
    terminal set that drifts from the edges."""
    m = statemachine.Machine("t", {"A": frozenset({"B", "C"}), "B": frozenset({"C"}), "C": frozenset()})
    assert m.states == ("A", "B", "C") and m.terminal == ("C",) and m.live == ("A", "B")
    assert m.can("A", "C") and not m.can("C", "A") and not m.can("Z", "A")
    assert m.sql_in(("B", "A")) == "('B','A')"
    for bad in ((), ("A", "Z")):
        with pytest.raises(ValueError):
            m.sql_in(bad)
    with pytest.raises(ValueError, match="undeclared"):
        statemachine.Machine("t", {"A": frozenset({"B"})})
    with pytest.raises(TypeError):
        m.transitions["A"] = frozenset()   # type: ignore[index]   # read-only: nothing can add an edge later


# ── the schema: every migration, in order, so a state a later one adds is seen ────────────────────────────

def _statements() -> list[str]:
    return [" ".join(s.split()) for f in _MIGRATIONS
            for s in re.sub(r"--[^\n]*", "", f.read_text(encoding="utf-8")).split(";")]


def _enum(name: str) -> tuple[str, ...]:
    values: list[str] = []
    for s in _statements():
        if m := re.search(rf"CREATE TYPE {name} AS ENUM \(([^)]*)\)", s):
            values = re.findall(r"'(\w+)'", m.group(1))
        values += re.findall(rf"ALTER TYPE {name} ADD VALUE (?:IF NOT EXISTS )?'(\w+)'", s)
    assert values, name
    return tuple(values)


def _check(table: str, column: str) -> set[str]:
    found = [_quoted(m.group(1)) for s in _statements() if table in s
             for m in [re.search(rf"CHECK \({column} IN \(([^)]*)\)\)", s)] if m]
    assert found, (table, column)
    return found[-1]


def _index_states(name: str, column: str, states: tuple[str, ...]) -> set[str]:
    """The states the latest definition of partial index `name` covers (`col IN (…)` or `col NOT IN (…)`)."""
    found = [s for s in _statements() if re.search(rf"\bCREATE (?:UNIQUE )?INDEX (?:IF NOT EXISTS )?{name}\b", s)]
    assert found, name
    m = re.search(rf"\b{column} (NOT )?IN \(([^)]*)\)", found[-1])
    assert m, (name, found[-1])
    return set(states) - _quoted(m.group(2)) if m.group(1) else _quoted(m.group(2))


def test_each_machine_has_exactly_the_states_its_table_allows():
    """Across every migration in order (a later ALTER TYPE … ADD VALUE or CHECK included), the lease and migration
    machines list their Postgres enum's values in the enum's order, and the build and replication machines list
    exactly the values their table's latest CHECK allows. Guards a state added to the schema but not the machine
    (every guard over it unchecked) or a machine state the table would refuse."""
    assert lease.LEASE.states == _enum("lease_state")
    assert statemachine.MIGRATION.states == _enum("migration_state")
    assert set(statemachine.BUILD.states) == _check("template_build", "state")
    assert set(statemachine.REPLICATION.states) == _check("template_replication", "job_state")


def test_each_machines_live_states_are_the_ones_its_partial_indexes_cover():
    """The partial indexes that hold one live job per target (or per lease), and the replication fence over
    guests that exist, cover exactly the machine's live states (for the fence: the live states that carry a
    vmid, all but REQUESTED, which is also the set lease.py's clone-orphan guard reads). Guards an edge declared
    out of a terminal state: it would make that state live in every query derived from the machine while the
    indexes and hand-written guards still treat it as settled."""
    mig, rep, bld = statemachine.MIGRATION, statemachine.REPLICATION, statemachine.BUILD
    assert _index_states("migration_pending", "state", mig.states) == set(mig.live)
    assert _index_states("migration_in_progress", "state", mig.states) == set(mig.live)
    assert _index_states("replication_one_active", "job_state", rep.states) == set(rep.live)
    assert _index_states("template_build_one_active_per_target", "state", bld.states) == set(bld.live)
    vmid_states = _index_states("replication_cluster_vmid", "job_state", rep.states)
    assert vmid_states == set(rep.live) - {"REQUESTED"} == _quoted(lease._REPL_VMID_ACTIVE_SQL)


def test_the_modules_read_their_state_lists_from_the_machines():
    """imgbuild's terminal, all and claimed build states, replication's live, advance and reclaim states,
    migration's live and terminal states, and lease's edge table and the replication states its clone-orphan
    guard reads are all derived from the machines, with no hand-written copy left (lease.py once kept that guard's
    states as a literal, pinned by a drift test). Guards a second definition coming back and drifting."""
    assert frozenset(statemachine.BUILD.terminal) == imgbuild.TERMINAL_STATES
    assert statemachine.BUILD.states == imgbuild.BUILD_STATES
    assert frozenset(statemachine.BUILD.live) - {"REQUESTED"} == imgbuild.CLAIMED_STATES
    assert statemachine.REPLICATION.live == replication.LIVE_STATES
    assert replication._ADVANCE_STATES == ("REQUESTED", "COPYING", "CONVERTING")
    assert replication._RECLAIM_STATES == ("COPYING", "CONVERTING", "RECLAIMING")
    assert statemachine.MIGRATION.live == migration.ACTIVE_STATES
    assert statemachine.MIGRATION.terminal == migration.TERMINAL_STATES == ("DONE", "ABORTED")
    assert lease.VALID_TRANSITIONS is lease.LEASE.transitions
    assert lease._REPL_VMID_ACTIVE_SQL == "('COPYING','CONVERTING','RECLAIMING')"
    # no module-level tuple or set spelled out of a machine's states (a guard's inline IN-list is SQL, read by the
    # scan below, not a named list)
    for mod, machine in ((imgbuild, statemachine.BUILD), (replication, statemachine.REPLICATION),
                         (migration, statemachine.MIGRATION), (lease, statemachine.REPLICATION)):
        tree = ast.parse(Path(mod.__file__ or "").read_text(encoding="utf-8"))
        for value in _module_values(tree).values():
            for lit in ast.walk(value):
                if isinstance(lit, (ast.Tuple, ast.Set)) and len(lit.elts) > 1 and all(
                        isinstance(e, ast.Constant) and e.value in machine.states for e in lit.elts):
                    raise AssertionError(f"{mod.__name__} line {lit.lineno}: a hand-written list of {machine.name} states")


# ── the scan ─────────────────────────────────────────────────────────────────────────────────────────────

def _module_values(tree: ast.Module) -> dict[str, ast.expr]:
    out: dict[str, ast.expr] = {}
    for st in tree.body:
        if isinstance(st, ast.Assign):
            for t in st.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = st.value
        elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None:
            out[st.target.id] = st.value
    return out


def _render(node: ast.AST, mod: ModuleType) -> str:
    """The SQL text of an execute() argument: literals, `+` joins, and f-strings whose expressions are
    module-level values. Anything else becomes `{?}`, which no guard or SET reading accepts, so an UPDATE that
    sets a state through one fails the scan rather than escaping it."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                try:
                    parts.append(str(eval(compile(ast.Expression(v.value), "<sql>", "eval"), vars(mod))))
                except Exception:
                    parts.append("{?}")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _render(node.left, mod) + _render(node.right, mod)
    if isinstance(node, ast.Name) and isinstance(getattr(mod, node.id, None), str):
        return str(getattr(mod, node.id))
    return "{?}"


def _split_top(text: str, sep: str) -> list[str]:
    """`text` split on ` sep ` (case-insensitive) or, for ",", on commas, outside any parentheses."""
    parts, depth, start, i = [], 0, 0, 0
    pat = re.compile(rf"\s{sep}\s", re.IGNORECASE) if sep != "," else None
    while i < len(text):
        c = text[i]
        depth += c == "("
        depth -= c == ")"
        if depth == 0:
            if sep == "," and c == ",":
                parts.append(text[start:i])
                start = i + 1
            elif pat and (m := pat.match(text, i)):
                parts.append(text[start:i])
                start = i = m.end()
                continue
        i += 1
    parts.append(text[start:])
    return [p.strip() for p in parts]


def _updates(sql: str):
    """Each `UPDATE <job table> [alias] SET … [FROM …] WHERE …` in a statement (a CTE can hold one before its
    main one), as (table, alias, SET clause, WHERE clause)."""
    s = " ".join(sql.split())
    for m in _UPDATE.finditer(s):
        rest = s[m.end():]
        end = re.search(r" RETURNING |\) UPDATE |\) SELECT |$", rest, re.IGNORECASE)
        body = rest[:end.start()] if end else rest
        where = _split_top(body, "WHERE")
        sets = _split_top(where[0], "FROM")[0]
        yield m.group(1).lower(), m.group(2), sets, (where[1] if len(where) > 1 else "")


def _set_target(sets: str, column: str, alias: str | None) -> str | None:
    """The state a SET clause writes: 'X', '%s' for a parameter, '{?}' when it writes the column in any other way
    (CASE, a tuple assignment, a named parameter, unresolved text), or None when it leaves the column alone."""
    col = rf"(?:{alias}\.)?{column}" if alias else column
    for part in _split_top(sets, ","):
        lhs, _eq, rhs = part.partition("=")
        lhs, rhs = lhs.strip(), rhs.strip()
        if "{?}" in lhs or (lhs.startswith("(") and re.search(rf"(?<![\w]){column}\b", lhs)):
            return "{?}"
        if re.fullmatch(col, lhs, re.IGNORECASE):
            if m := re.fullmatch(r"'(\w+)'", rhs):
                return m.group(1)
            return "%s" if rhs == "%s" else "{?}"
    return None


def _guard(where: str, column: str, table: str, states: tuple[str, ...]) -> set[str] | None:
    """The states a WHERE guard admits for `column`, read only from its top-level conjuncts (`col = 'X'`,
    `col IN (…)`, `col NOT IN (…)`) and from the claim shape `id = (SELECT id FROM <table> WHERE …)`; their
    intersection when there are several. None (unreadable) when there is no guard, an OR at the top level, or a
    conjunct naming the column any other way: inside an OR group, an EXISTS, `<>`, `= ANY`."""
    if "{?}" in where or len(_split_top(where, "OR")) > 1:
        return None
    col = rf"(?:\w+\.)?{column}"
    found: list[set[str]] = []
    for conj in _split_top(where, "AND"):
        if m := re.fullmatch(rf"{col}\s*=\s*'(\w+)'", conj):
            found.append({m.group(1)})
        elif m := re.fullmatch(rf"{col}\s+NOT IN\s*\(([^()]*)\)", conj, re.IGNORECASE):
            found.append(set(states) - _quoted(m.group(1)))
        elif m := re.fullmatch(rf"{col}\s+IN\s*\(([^()]*)\)", conj, re.IGNORECASE):
            found.append(_quoted(m.group(1)))
        elif m := re.fullmatch(rf"id\s*=\s*\(SELECT id FROM {table} WHERE (.*)\)", conj, re.IGNORECASE):
            inner = _guard(re.split(r"\s(?:ORDER BY|LIMIT|FOR UPDATE)\b", m.group(1), flags=re.IGNORECASE)[0],
                           column, table, states)
            if inner is None:
                return None
            found.append(inner)
        elif re.search(rf"(?<![\w]){column}\b", conj):
            return None
    return set.intersection(*found) if found else None


def _scan(name: str) -> tuple[list[tuple[str, int, str, str, str]], list[str]]:
    """For br0kr/<name>.py: (every UPDATE that sets a state, as (function, line, table, WHERE, target)) and the
    problems that make the scan untrustworthy: an UPDATE of a job table in a string the scan cannot reach from an
    execute()/executemany() call, or one it reached but could not split."""
    mod = importlib.import_module(f"br0kr.{name}")
    src = Path(mod.__file__ or "").read_text(encoding="utf-8")
    tree = ast.parse(src)
    values = _module_values(tree)
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    owner = {id(c): fn.name for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
             for c in ast.walk(fn)}
    reached: set[int] = set()
    sites, problems = [], []
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr in ("execute", "executemany") and call.args):
            continue
        todo, seen = [call.args[0]], set()
        while todo:                                   # the argument, and the module-level values it names
            n = todo.pop()
            for sub in ast.walk(n):
                reached.add(id(sub))
                if isinstance(sub, ast.Name) and sub.id in values and sub.id not in seen:
                    seen.add(sub.id)
                    todo.append(values[sub.id])
        sql = _render(call.args[0], mod)
        parsed = list(_updates(sql))
        if len(parsed) != len(_UPDATE.findall(" ".join(sql.split()))):
            problems.append(f"line {call.lineno}: an UPDATE the scan could not split")
        for table, alias, sets, where in parsed:
            to = _set_target(sets, _TABLES[table][0], alias)
            if to is not None:
                sites.append((owner.get(id(call), "<module>"), call.lineno, table, where, to))
    for n in ast.walk(tree):
        if (isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
                and _UPDATE.search(" ".join(n.value.split())) and id(n) not in reached):
            problems.append(f"line {n.lineno}: UPDATE SQL the scan cannot follow from an execute call")
    return sites, problems


def _set_state_targets() -> tuple[set[str], list[str]]:
    """The states migration.py's callers pass to `_set_state` (whose UPDATE takes its target as a parameter), and
    every call that passes it some other way than a positional string literal."""
    tree = ast.parse(Path(migration.__file__ or "").read_text(encoding="utf-8"))
    targets, bad = set(), []
    for c in ast.walk(tree):
        if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "_set_state":
            if len(c.args) >= 3 and isinstance(c.args[2], ast.Constant) and isinstance(c.args[2].value, str) \
                    and not any(k.arg == "state" for k in c.keywords):
                targets.add(c.args[2].value)
            else:
                bad.append(f"line {c.lineno}: _set_state target is not a string literal")
    return targets, bad


def _edges(name: str) -> tuple[set[tuple[str, str, str]], list[str]]:
    """Every (table, from, to) the saga module's guarded UPDATEs take, and everything the scan could not read."""
    sites, problems = _scan(name)
    assert len(sites) == _SITES[name], f"{name}: {len(sites)} state-setting UPDATEs, expected {_SITES[name]}"
    edges: set[tuple[str, str, str]] = set()
    for fn, line, table, where, to in sites:
        column, machine = _TABLES[table]
        frm = _guard(where, column, table, machine.states if machine else _INSTANCE_STATES)
        targets = {to}
        if (name, fn, to) == ("migration", "_set_state", "%s"):
            targets, bad = _set_state_targets()
            problems += bad
        if frm is None or targets & {"{?}", "%s"}:
            problems.append(f"{fn} line {line}: cannot read the edge ({table}: {where[:80]!r} -> {to})")
            continue
        edges |= {(table, f, t) for f in frm for t in targets}
    return edges, problems


@pytest.mark.parametrize("name", sorted(_SITES))
def test_every_guarded_update_takes_an_edge_its_machine_lists(name):
    """Every UPDATE in the saga module that sets a build, replication, migration or lease state moves only from
    states its WHERE guard admits to a state the table's machine allows from each of them; one that sets a
    template_instance state takes only a replication's own catalog edge (REPLICATING → READY / FAILED). A guard,
    a SET value or an UPDATE the scan can't read fails rather than passing unchecked, and the module holds exactly
    its known number of state-setting UPDATEs. Guards a new or edited step taking an edge nobody declared, or a
    guard widened past its machine (an OR, an unguarded row, a state set through text the scan can't see)."""
    edges, problems = _edges(name)
    assert not problems, "\n".join(problems)
    bad = []
    for table, f, t in sorted(edges):
        machine = _TABLES[table][1]
        if not (machine.can(f, t) if machine else (f, t) in _INSTANCE_EDGES):
            bad.append(f"{table} {f} -> {t} is not an edge of its machine")
    assert not bad, "\n".join(bad)


def test_every_declared_job_edge_is_one_some_step_takes():
    """The build, replication and migration machines declare exactly the edges the saga modules' guarded UPDATEs
    take, no more. Guards a machine edge no code implements: a declared state the stale reclaim (or any step)
    was never taught to move, whose job would then wedge in it while every derived query treats it as live."""
    taken: set[tuple[str, str, str]] = set()
    for name in _SITES:
        taken |= _edges(name)[0]
    for table in ("template_build", "template_replication", "migration"):
        machine = _TABLES[table][1]
        assert machine is not None
        declared = {(f, t) for f, ts in machine.transitions.items() for t in ts}
        assert {(f, t) for tb, f, t in taken if tb == table} == declared, table


def test_the_scan_reads_each_kind_of_guard_and_refuses_what_it_cannot_read():
    """The scan's parser, on each statement shape the modules use and on the ones it must refuse: a plain CAS; a
    NOT IN guard read against the machine; a claim whose guard sits in its own-table sub-select; a sub-select
    inside SET; a CTE holding a second UPDATE; an aliased, lower-case UPDATE with a FROM; and, refused (None or
    '{?}'), an OR-widened guard, a guard only inside an EXISTS on another row, `<>`, a CASE or tuple SET, a named
    parameter, and unresolved text. Guards the edge test passing because its parser silently stopped seeing."""
    b = statemachine.BUILD.states

    def one(sql: str) -> tuple[str, str | None, str, str]:
        [u] = list(_updates(sql))
        return u

    t, a, s, w = one("UPDATE template_build SET state = 'READY', x = 1 WHERE id = %s AND state = 'IMPORTING' "
                     "RETURNING id")
    assert (t, _set_target(s, "state", a), _guard(w, "state", t, b)) == ("template_build", "READY", {"IMPORTING"})
    _, _, _, w = one("UPDATE template_build SET state = 'FAILED' WHERE id = %s AND state NOT IN ('READY','FAILED')")
    assert _guard(w, "state", "template_build", b) == set(statemachine.BUILD.live)
    _, _, _, w = one("UPDATE template_build SET state = 'DOWNLOADING' WHERE id = (SELECT id FROM template_build "
                     "WHERE state = 'REQUESTED' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id")
    assert _guard(w, "state", "template_build", b) == {"REQUESTED"}
    _, a, s, w = one("UPDATE lease SET state = 'PROVISIONING', c = (SELECT c FROM node WHERE node.id = lease.n) "
                     "WHERE id = %s AND state = 'ADMITTED' RETURNING id")
    assert _set_target(s, "state", a) == "PROVISIONING" and _guard(w, "state", "lease", lease.LEASE.states) == {"ADMITTED"}
    both = list(_updates("WITH j AS (UPDATE template_replication SET job_state = 'READY' WHERE id = %s AND job_state"
                         " = 'CONVERTING' RETURNING id) UPDATE template_instance SET instance_state = 'READY' WHERE "
                         "replication_id = (SELECT id FROM j) AND instance_state = 'REPLICATING' RETURNING id"))
    assert [u[0] for u in both] == ["template_replication", "template_instance"]
    assert _guard(both[1][3], "instance_state", "template_instance", _INSTANCE_STATES) == {"REPLICATING"}
    _, a, s, w = one("update lease l set state = 'RELEASING' from due where l.id = due.id and l.state = 'ACTIVE'")
    assert a == "l" and _set_target(s, "state", a) == "RELEASING" and _guard(w, "state", "lease", ()) == {"ACTIVE"}
    for where in ("id = %s AND (state = 'DOWNLOADING' OR state = 'READY')", "id = %s AND state = 'A' OR %s",
                  "id = %s AND EXISTS (SELECT 1 FROM template_build b2 WHERE b2.id = %s AND state = 'IMPORTING')",
                  "id = %s AND state <> 'READY'", "id = %s AND state = ANY(%s)", "id = %s", "id = {?}"):
        assert _guard(where, "state", "template_build", b) is None, where
    for sets in ("state = CASE WHEN x THEN 'A' ELSE 'B' END", "(error, state) = (%s, 'FAILED')",
                 "state = %(s)s", "{?}"):
        assert _set_target(sets, "state", None) == "{?}", sets
    assert _set_target("x = 1, updated_at = now()", "state", None) is None


def test_the_job_modules_name_their_in_lists_instead_of_spelling_them():
    """The state IN-lists the job modules (build, imgbuild, replication, migration) and their readers (admin,
    gateway, inventory, placement) put in their SQL come from the machines or placement.LIVE_INSTANCE_STATES (review
    CODE-06): each named list holds exactly its states, and no SQL in those modules spells out an IN-list of two or
    more build, replication, migration or template-instance states. Guards a hand-copied state list coming back into
    a guard and drifting from the machine (the review counted seventeen)."""
    assert replication.TERMINAL_IN == "('READY','FAILED')"
    assert replication._COPY_STATES == ("COPYING", "CONVERTING") and replication._COPY_IN == "('COPYING','CONVERTING')"
    assert migration._TERMINAL_SQL == "('DONE','ABORTED')"
    assert migration._CANCELLABLE_SQL == "('REQUESTED','RESERVED')"
    assert migration._PENDING_SQL == "('REQUESTED','RESERVED','MIGRATING')"
    assert imgbuild.TERMINAL_IN == "('READY','FAILED')"
    states = set().union(*(m.states for m in (statemachine.BUILD, statemachine.REPLICATION, statemachine.MIGRATION)))
    states |= {"REPLICATING", "READY", "RETIRED", "FAILED"}   # template_instance.instance_state (0032's CHECK)
    spelled = re.compile(r"IN \(('[A-Z_]+'(?:,\s*'[A-Z_]+')+)\)")
    for mod in ("build", "imgbuild", "replication", "migration", "admin", "gateway", "inventory", "placement"):
        text = Path(importlib.import_module(f"br0kr.{mod}").__file__ or "").read_text(encoding="utf-8")
        for m in spelled.finditer(text):
            listed = {s.strip(" '") for s in m.group(1).split(",")}
            assert not listed <= states, f"br0kr/{mod}.py spells out {m.group(0)}"
