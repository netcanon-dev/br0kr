#!/usr/bin/env python3
"""Meta-test: fail if any test function lacks a docstring.

The rule (docs/standards/testing.md §4) is machine-enforced, not aspirational: every
`test_*` function states what it verifies and the failure it guards against. Run by
tests/validate.{sh,ps1}; also useful standalone:

    python tests/check_test_docs.py

Exit 0 = all documented; exit 1 = some undocumented (lists file:line name).
Covers both plain `def test_*` functions and unittest `def test_*` methods.
"""
import ast
import glob
import sys

missing = []
for f in sorted(glob.glob("tests/**/test_*.py", recursive=True)):
    with open(f, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=f)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name.startswith("test_") and not ast.get_docstring(node):
            missing.append(f"{f}:{node.lineno}  {node.name}")

if missing:
    print("undocumented tests (docs/standards/testing.md §4 — add a docstring "
          "saying WHAT it verifies + the failure it guards against):")
    print("  " + "\n  ".join(missing))
    sys.exit(1)
print("all test functions carry a docstring")
