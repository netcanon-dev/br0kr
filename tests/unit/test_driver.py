"""The driver contract (br0kr/driver.py) matches the real driver and is what the engine is typed against (ARCH-05).

`driver.Driver` is a Protocol of every call the engine makes on a Proxmox node driver. mypy enforces it from both
sides: engine code typed against it can call nothing else, and br0kr/nodes.py storing each `pve.Pve` it builds
as a `Driver` makes mypy check that `pve.Pve` satisfies it. These tests keep the Protocol honest where mypy cannot
see: its members are exactly the driver methods the engine calls, each with the real method's signature, and no
engine module types a driver as the concrete class again (which would let a call bypass the contract).
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from br0kr import driver, pve

pytestmark = pytest.mark.unit

_BR0KR = Path(driver.__file__).resolve().parent
_ENGINE = [p for p in sorted(_BR0KR.glob("*.py")) if p.name not in ("pve.py", "driver.py")]


def _members() -> set[str]:
    return {n for n, v in vars(driver.Driver).items() if not n.startswith("_") and callable(v)}


def test_the_contract_lists_exactly_the_driver_methods_the_engine_calls():
    """The Protocol's methods are exactly the public `pve.Pve` methods some engine module calls (an `x.method(…)`
    call in its code, not its docstrings). Guards a driver method the engine starts calling without it entering
    the contract, and a member left behind after the engine stopped calling it. A name the engine also calls on
    another object (a thread's `start`, an event's `wait`) keeps a member alive; mypy, which checks the calls
    themselves, is the stronger half of this guard."""
    public = {n for n, v in vars(pve.Pve).items() if not n.startswith("_") and callable(v)}
    called = {c.func.attr for p in _ENGINE for c in ast.walk(ast.parse(p.read_text(encoding="utf-8")))
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
    assert _members() == public & called


def test_each_contract_member_has_the_real_drivers_signature():
    """Each Protocol method has the same parameters, defaults and annotations as the `pve.Pve` method it names,
    and the `node` attribute the Protocol declares is one every `pve.Pve` sets. Guards the contract drifting from
    the driver: a renamed or added parameter the engine would then call against the wrong shape."""
    for name in sorted(_members()):
        assert inspect.signature(getattr(driver.Driver, name)) == inspect.signature(getattr(pve.Pve, name)), name
    assert driver.Driver.__annotations__ == {"node": "str"}
    assert pve.Pve("h", "pve2", "id", "secret").node == "pve2"


def test_no_engine_module_types_a_driver_as_the_concrete_class():
    """No annotation in an engine module (a parameter, a return, an annotated assignment) names `pve.Pve`: the
    engine is typed against `Driver`, so mypy holds every driver call to the contract. Guards a new function
    typed with the concrete class, whose calls mypy would then check against `pve.Pve` instead of the contract.
    Constructing a `pve.Pve` (in br0kr/nodes.py) is not an annotation and stays allowed."""
    bad = []
    for path in _ENGINE:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        annotations: list[ast.expr] = []
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = n.args
                annotations += [a.annotation for a in (*args.posonlyargs, *args.args, *args.kwonlyargs,
                                                        *(x for x in (args.vararg, args.kwarg) if x)) if a.annotation]
                if n.returns:
                    annotations.append(n.returns)
            elif isinstance(n, ast.AnnAssign):
                annotations.append(n.annotation)
        bad += [f"{path.name}:{a.lineno}" for a in annotations if re.search(r"\bPve\b", ast.unparse(a))]
        # nor may it take the class by another name (an import of `Pve` itself, or an alias of it)
        bad += [f"{path.name}:{n.lineno} imports Pve" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                and n.module == "br0kr.pve" and any(a.name == "Pve" for a in n.names)]
    assert not bad, f"annotations that name pve.Pve instead of driver.Driver: {bad}"


def test_the_engine_names_the_destroy_verdicts_instead_of_spelling_them():
    """The destroy verdicts are named once, in br0kr/driver.py, with their wire values pinned, and pve.py and the
    engine modules that branch on them (replication, sweeper, reconciler) use the names: no verdict string is
    returned or compared in their code. Guards a verdict spelled out again, where a renamed or retyped value would
    silently stop matching and a teardown could treat a refusal as success (review CODE-06)."""
    vocab = {"DESTROYED": "destroyed", "GONE": "gone", "FENCE_MISMATCH": "fence_mismatch", "DESTROY": "destroy",
             "FENCE_APPEARED": "fence_appeared", "REPL_FENCE_APPEARED": "repl_fence_appeared",
             "NOT_CLONE": "not_clone", "IS_TEMPLATE": "is_template"}
    assert {name: getattr(driver, name) for name in vocab} == vocab
    for module in ("pve", "replication", "sweeper", "reconciler"):
        tree = ast.parse((Path(pve.__file__).resolve().parent / f"{module}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Return, ast.Compare, ast.IfExp)):
                for lit in ast.walk(node):
                    assert not (isinstance(lit, ast.Constant) and lit.value in vocab.values()), (
                        f"br0kr/{module}.py line {lit.lineno} spells the verdict {lit.value!r}")
