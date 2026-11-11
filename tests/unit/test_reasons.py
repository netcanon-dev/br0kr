"""Drift guard for br0kr/reasons.py, the request path's refusal reasons and error codes (review CODE-01).

Agents branch on these strings. Spelled as bare literals across the request path, a typo was a new reason that no
doc named and no test noticed. These tests pin that:

  * every code is a short snake_case string, named after itself, and listed in `ALL` exactly once;
  * the classification sets the request path branches on (queueable, node-full, storage, project, template, hold,
    and placement's own sets) hold only known codes;
  * the agent-facing modules never spell a code as a literal (they import the constant), and no module anywhere in
    br0kr passes `reason="..."` as a literal;
  * in the agent-facing modules, every literal in a reason position is a known code, so a misspelt or brand-new
    reason fails here rather than reaching an agent;
  * every code is exercised by a test or a real-Postgres harness, by quoted value or by exact name.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from br0kr import admission, placement, reasons, scheduler

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_PKG = _ROOT / "br0kr"
# The modules on the agent-facing request path: no known code may appear in them as a literal.
_AGENT_MODULES = ("admission", "gateway", "scheduler", "pin", "placement", "cloudinit", "multidisk", "mcp_server",
                  "bulk", "imgbuild", "build", "auth", "actas", "inventory", "envelope")
_SNAKE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
# The keys and keyword names that carry a reason, and the calls whose positional strings are one (result types and
# the refusal helpers).
_REASON_KEYS = frozenset({"reason", "error", "blocked_reason", "hold_reason", "released_reason"})
_RESULT_CALL = re.compile(r"(Result|_Fit|Decision)$|^_(refuse|reject|answer)")
# Every code the modules may name: what an agent can receive, plus the internal promote outcomes.
_KNOWN = reasons.ALL | reasons.INTERNAL
# Snake_case strings that sit in a reason position in one module but are not reasons, each with why.
_NOT_REASONS = {
    ("gateway", "admitted"): "admit_outcome: a metric outcome label",
    ("gateway", "replay"): "admit_outcome: a metric outcome label",
    ("gateway", "other"): "admit_outcome: a metric outcome label",
    ("mcp_server", "ledger_unreachable"): "/healthz: the liveness answer, shared with the dashboard and observability",
}


def _constants() -> dict[str, str]:
    return {name: value for name, value in vars(reasons).items() if name.isupper() and isinstance(value, str)}


def _literal_uses(tree: ast.AST) -> list[ast.Constant]:
    """String constants in a reason position: everything except dict keys, subscripts, `.get()`/`.pop()` keys,
    f-string parts and docstrings (a code may also be a dict key, e.g. an answer's `"queued": True`)."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            skip |= {id(k) for k in node.keys if k is not None}
        elif isinstance(node, ast.Subscript):
            skip.add(id(node.slice))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in (
                "get", "pop", "setdefault") and node.args:
            skip.add(id(node.args[0]))
        elif isinstance(node, ast.JoinedStr):
            skip |= {id(v) for v in node.values}
        elif (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)) and node.body
              and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)):
            skip.add(id(node.body[0].value))
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip]


def test_every_code_is_snake_case_named_after_itself_and_in_all():
    """Each constant's value is a short snake_case code, its name is that code upper-cased (so a grep for either finds
    the other), no two constants share a value, and `ALL` is exactly the set of them less the INTERNAL promote
    outcomes. Guards a code with a space or a capital (an agent matching on it breaks), two names for one reason
    drifting apart, and an internal code being published as one an agent can receive (UX-04 documents ALL)."""
    consts = _constants()
    assert len(set(consts.values())) == len(consts)
    for name, value in consts.items():
        assert _SNAKE.fullmatch(value) and len(value) <= 64, value
        assert value.upper() == name, (name, value)
    assert frozenset(consts.values()) == reasons.ALL | reasons.INTERNAL
    assert {reasons.GONE, reasons.NO_NODE} == reasons.INTERNAL and not reasons.ALL & reasons.INTERNAL


def test_the_classification_sets_hold_only_known_codes():
    """The sets the request path branches on hold only codes in ALL, and the modules use the shared sets rather than
    their own copies: admission's QUEUEABLE_REJECTS and the scheduler's per-reason sets ARE the reasons.py sets, and
    placement's sets and hold reasons are subsets. Guards the scheduler re-listing admission's strings by hand (as it
    did) and the two lists diverging."""
    for group in (reasons.QUEUEABLE, reasons.NODE_FULL, reasons.STORAGE, reasons.PROJECT, reasons.TEMPLATE,
                  reasons.HOLD, placement.PLACEMENT_REASONS, placement.PLACEMENT_PER_CANDIDATE,
                  placement.CONTENT_REASONS):
        assert group <= reasons.ALL, sorted(group - reasons.ALL)
    assert admission.QUEUEABLE_REJECTS is reasons.QUEUEABLE
    assert scheduler._NODE_FULL_REASONS is reasons.NODE_FULL and scheduler._STORAGE_REJECTS is reasons.STORAGE
    assert scheduler._PROJECT_REJECTS is reasons.PROJECT and scheduler._TEMPLATE_REJECTS is reasons.TEMPLATE
    holds = {v for k, v in vars(placement).items() if k.startswith("HOLD_") and isinstance(v, str)}
    assert holds == reasons.HOLD


def _leaves(expr: ast.expr | None) -> list[ast.Constant]:
    """The string constants an expression can evaluate to: itself, either arm of a conditional, an operand of
    `or`/`and`, or an element of a tuple/list/set (the right side of `reason in (...)`). Calls and f-strings are not
    descended into: their strings are arguments and fragments, not the value."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return [expr]
    if isinstance(expr, ast.IfExp):
        return _leaves(expr.body) + _leaves(expr.orelse)
    if isinstance(expr, ast.BoolOp):
        return [c for v in expr.values for c in _leaves(v)]
    if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
        return [c for e in expr.elts for c in _leaves(e)]
    return []


def _names_a_reason(expr: ast.expr) -> bool:
    name = expr.attr if isinstance(expr, ast.Attribute) else expr.id if isinstance(expr, ast.Name) else ""
    return name.lower().endswith(("reason", "error"))


def _is_reason_target(target: ast.expr) -> bool:
    """An assignment target that holds a reason: a `...reason`/`...error` name or attribute (`reason`,
    `blocked_reason`, `_FK_REASON`), or a subscript store under a reason key (`out["reason"]`)."""
    if isinstance(target, ast.Subscript):
        return isinstance(target.slice, ast.Constant) and target.slice.value in _REASON_KEYS
    return _names_a_reason(target)


def _reason_positions(tree: ast.AST) -> list[ast.Constant]:
    """String constants in a reason position: a returned value, the value of a `reason`/`error`-like dict key or
    keyword, either side of a comparison with a `...reason`/`...error` name, a value assigned to a reason-named
    target (and, for a dict literal such as `_FK_REASON`, its values), or a positional argument of a result type
    (`AdmissionResult`, `PromoteResult`, `_Fit`, `PlacementDecision`) or a refusal helper."""
    found: list[ast.Constant] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Return):
            found += _leaves(node.value)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(_is_reason_target(t) for t in targets):
                found += _leaves(node.value)
                if isinstance(node.value, ast.Dict):
                    found += [c for v in node.value.values for c in _leaves(v)]
        elif isinstance(node, ast.Dict):
            found += [c for k, v in zip(node.keys, node.values, strict=True)
                      if isinstance(k, ast.Constant) and k.value in _REASON_KEYS for c in _leaves(v)]
        elif isinstance(node, ast.keyword) and node.arg in _REASON_KEYS:
            found += _leaves(node.value)
        elif isinstance(node, ast.Compare) and any(_names_a_reason(e) for e in (node.left, *node.comparators)):
            found += [c for e in (node.left, *node.comparators) for c in _leaves(e)]
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if _RESULT_CALL.search(name):
                found += [c for a in node.args for c in _leaves(a)]
    return found


@pytest.mark.parametrize("module", _AGENT_MODULES)
def test_agent_facing_modules_spell_no_code_as_a_literal(module):
    """No string constant in a reason position equals a code in ALL: the module imports the name. A typo in a literal
    is a new, undocumented reason; a typo in a name is an AttributeError that mypy and every test catch. Dict keys
    and lookups are exempt (`"queued": True` is a key)."""
    tree = ast.parse((_PKG / f"{module}.py").read_text(encoding="utf-8"))
    bare = sorted({(n.lineno, n.value) for n in _literal_uses(tree) if n.value in _KNOWN})
    assert not bare, f"{module}.py spells reason codes as literals (use br0kr.reasons): {bare}"


@pytest.mark.parametrize("module", _AGENT_MODULES)
def test_a_reason_position_holds_only_a_known_code(module):
    """In the agent-facing modules, a snake_case literal in a reason position (a returned string, a `reason`/`error`
    value, a comparison with a `.reason`, an assignment to a reason-named target, a result's constructor argument)
    must be a known code, or one of that module's listed non-reasons. Guards the failure CODE-01 names: a misspelt
    or brand-new reason (`_Fit("insufficent_cpu")`, `{"reason": "quota_exceded"}`) is not a known code, so the
    literal check above cannot see it, and it would reach an agent undocumented. Free text (a sentence with spaces)
    is not snake_case and is not checked."""
    tree = ast.parse((_PKG / f"{module}.py").read_text(encoding="utf-8"))
    unknown = sorted({(n.lineno, n.value) for n in _reason_positions(tree)
                      if _SNAKE.fullmatch(n.value) and n.value not in _KNOWN and (module, n.value) not in _NOT_REASONS})
    assert not unknown, (f"{module}.py puts strings that are not in br0kr/reasons.py in a reason position: {unknown}. "
                         "A reason belongs in reasons.py; a string that is not one goes in this test's _NOT_REASONS.")


def test_every_listed_non_reason_is_still_used():
    """Each _NOT_REASONS entry still sits in a reason position of its module. Guards the allowlist outliving the code
    it excused, where a later reason-position typo of the same word would pass unseen."""
    stale = [(module, value) for module, value in _NOT_REASONS
             if value not in {n.value for n in _reason_positions(
                 ast.parse((_PKG / f"{module}.py").read_text(encoding="utf-8")))}]
    assert not stale, f"no longer used, drop from _NOT_REASONS: {stale}"


def test_the_reason_position_check_catches_a_misspelt_reason():
    """The reason-position scan finds a misspelt code in each shape it covers (a return, a tuple return, a
    comparison and an `in (...)` test on a `.reason`, a result call bare and module-qualified, a reason dict value and
    keyword, an assignment to a reason name, a subscript store and a reason map's values), and ignores dict keys,
    `.get()` keys and free text. Guards the gate above going quiet: if `_reason_positions` stopped seeing one of
    these shapes, a typo there would pass every test again."""
    src = """
_FK_REASON = {"lease_net_fkey": "map_typo"}
def f(res, x):
    if x:
        return "insufficent_node_cpu"
    if x is None:
        return None, "tuple_typo"
    if res.reason == "templat_forbidden":
        return _Fit("unknwn_node")
    if res.reason in ("in_typo",):
        return admission.PromoteResult(False, "qualified_typo")
    reason = "assigned_typo"
    out = {"reason": "quota_exceded" if x else None, "queued": x.get("lease_id")}
    out["reason"] = "subscript_typo"
    return AdmissionResult(None, admitted=False, idempotent=False, reason="no_capcity") or "fine words here"
"""
    found = {c.value for c in _reason_positions(ast.parse(src))}
    assert {"map_typo", "insufficent_node_cpu", "tuple_typo", "templat_forbidden", "unknwn_node", "in_typo",
            "qualified_typo", "assigned_typo", "quota_exceded", "subscript_typo", "no_capcity"} <= found
    assert not found & {"queued", "lease_id", "lease_net_fkey"}


def test_no_module_passes_a_literal_reason_keyword():
    """Nowhere in br0kr is `reason=` given a snake_case string literal (the review's check: `grep -rnE
    'reason="[a-z_]+"' br0kr/*.py` is empty). The release reasons migration and replication write come from
    reasons.py; the ledger's replication-posture codes are named constants in ledgerhealth.py."""
    offenders = []
    for path in sorted(_PKG.glob("*.py")):
        if path.name == "reasons.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.keyword) and node.arg == "reason" and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str) and _SNAKE.fullmatch(node.value.value):
                offenders.append(f"{path.name}:{node.value.lineno} {node.value.value}")
    assert not offenders, offenders


# Codes no test produces, each for a stated reason. Keep this short: a new code belongs in a test.
_UNTESTED = {
    reasons.UNNAMED_PLACEMENT_REFUSAL: "a guard for a placement refusal without a reason, which resolve_placement "
                                       "never returns",
    reasons.TARGET_VANISHED: "a delete racing the replicate insert, between the plan and the row (FK race fallback)",
}


def test_every_code_is_exercised_by_a_test_or_a_harness():
    """Every code in ALL appears in a test or a real-Postgres harness: as a quoted string, as `reasons.NAME`, or
    through a module's alias for it (`placement.HOLD_UNRESOLVABLE`), each matched exactly (`reasons.PIN_REFUSED` is
    not satisfied by `reasons.PIN_REFUSED_ON_REPLAY`, nor a code by a prose mention). Guards a reason that ships with
    nothing ever producing or checking it (the review found four). The listed exceptions say why; this module itself
    does not count."""
    files = [p for p in sorted((_ROOT / "tests").rglob("*.py")) if p.name != "test_reasons.py"]
    files += sorted((_ROOT / "scripts").glob("harness_*.py"))
    corpus = "\n".join(p.read_text(encoding="utf-8") for p in files)
    aliases: dict[str, set[str]] = {}
    for module in (admission, placement, scheduler):
        for name, value in vars(module).items():
            if name.isupper() and isinstance(value, str) and value in _KNOWN:
                aliases.setdefault(value, set()).add(f"{module.__name__.rsplit('.', 1)[-1]}.{name}")

    def mentioned(name: str, value: str) -> bool:
        exact = [rf"""['"]{value}['"]""", rf"\breasons\.{name}\b"]
        exact += [rf"\b{re.escape(alias)}\b" for alias in aliases.get(value, ())]
        return any(re.search(pattern, corpus) for pattern in exact)

    missing = sorted(v for n, v in _constants().items() if v not in _UNTESTED and not mentioned(n, v))
    assert not missing, missing
    assert not [v for v in _UNTESTED if mentioned(v.upper(), v)], \
        "a listed exception is tested now: drop it from _UNTESTED"
