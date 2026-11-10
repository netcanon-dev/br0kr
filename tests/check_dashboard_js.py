#!/usr/bin/env python3
r"""check_dashboard_js.py — the JS syntax gate for the dashboard client.

The dashboard's client JavaScript lives inside a Python string (br0kr/dashboard_page.py's PAGE), so
ruff / py_compile / mypy cannot see a JS syntax error in it — a stray backtick or a broken statement
compiles fine as Python yet ships a dead page. This helper extracts the client script block from that
string and runs `node --check` on it, the real syntax gate. It is wired into tests/validate.sh as one
step; it is NOT a pytest test (no test_ prefix), mirroring tests/check_test_docs.py.

It checks the script AS SERVED: the PAGE literal's runtime value (`ast.literal_eval` of the assignment), not
the source text. The two differ wherever the source holds a Python escape, and that difference shipped a
dead dashboard once: a JS `\\'` written as `\'` in the source is a plain `'` at runtime, which ends the JS
string early. The source text parsed as valid JS, so the old text-based check passed. The hermetic test
tests/unit/test_dashboard.py::test_page_literal_holds_no_escape_that_changes_the_served_text guards the
same class without node.

It parses br0kr/dashboard_page.py with `ast` (no `import br0kr`), so it runs under a bare `python
tests/check_dashboard_js.py` from the repo root regardless of PYTHONPATH, and pulls in no runtime deps.

node is not in every gate image (the validate container is python-only), so this SELF-SKIPS green when
node is absent — enforced wherever node exists (local dev, a node-equipped CI step), never failing the
node-less --strict run. The backtick-parity and escape tests are the dependency-free backstops that always run.
"""
from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_PAGE_MODULE = Path(__file__).resolve().parent.parent / "br0kr" / "dashboard_page.py"


def page_script() -> str | None:
    """The client script exactly as the browser receives it: the <script> block of PAGE's runtime value, or
    None when the module has no PAGE string literal or the page has no script block."""
    tree = ast.parse(_PAGE_MODULE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "PAGE" for t in node.targets):
            value = ast.literal_eval(node.value)
            if not isinstance(value, str):
                return None
            # A plain split, not a regex: the page is our own string, so this extracts, never sanitizes. The
            # `<script src=…>` asset tag does not match "<script>", so this is the inline client script.
            _head, sep, rest = value.partition("<script>")
            body, end, _tail = rest.partition("</script>")
            return body if sep and end else None
    return None


def main() -> int:
    if shutil.which("node") is None:
        print("skip check-dashboard-js: node not installed")
        return 0
    script = page_script()
    if script is None:
        print(f"check-dashboard-js: no PAGE string with a script block in {_PAGE_MODULE.name}", file=sys.stderr)
        return 1
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        result = subprocess.run(["node", "--check", path], check=False)
        if result.returncode == 0:
            print("check-dashboard-js: node --check ok")
        return result.returncode
    finally:
        Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
