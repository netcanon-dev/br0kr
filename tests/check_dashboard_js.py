#!/usr/bin/env python3
"""check_dashboard_js.py — the JS syntax gate for the dashboard client.

The dashboard's client JavaScript lives inside a Python string (br0kr/dashboard_page.py's PAGE), so
ruff / py_compile / mypy cannot see a JS syntax error in it — a stray backtick or a broken statement
compiles fine as Python yet ships a dead page. This helper extracts the client script block from that
string and runs `node --check` on it, the real syntax gate. It is wired into tests/validate.sh as one
step; it is NOT a pytest test (no test_ prefix), mirroring tests/check_test_docs.py.

It reads br0kr/dashboard_page.py as TEXT (no `import br0kr`) so it runs under a bare `python
tests/check_dashboard_js.py` from the repo root regardless of PYTHONPATH, and pulls in no runtime deps.
To find the page's script tag it searches ONLY inside the PAGE string literal (everything up to and
including the PAGE assignment line is discarded — including this repo's own module docstrings, which
mention the tag name in prose), so the regex can't latch onto a tag name in a comment. PAGE is a single
static string literal, the same assumption the backtick-parity test in tests/unit/test_dashboard.py makes.

node is not in every gate image (the validate container is python-only), so this SELF-SKIPS green when
node is absent — enforced wherever node exists (local dev, a node-equipped CI step), never failing the
node-less --strict run. The backtick-parity test is the dependency-free backstop that always runs.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_PAGE_MODULE = Path(__file__).resolve().parent.parent / "br0kr" / "dashboard_page.py"
_PAGE_LITERAL = 'PAGE = """'


def main() -> int:
    if shutil.which("node") is None:
        print("skip check-dashboard-js: node not installed")
        return 0
    text = _PAGE_MODULE.read_text(encoding="utf-8")
    _before, sep, page = text.partition(_PAGE_LITERAL)
    if not sep:
        print(f"check-dashboard-js: no `{_PAGE_LITERAL}` literal in {_PAGE_MODULE.name}", file=sys.stderr)
        return 1
    m = re.search(r"<script>(.*?)</script>", page, re.S)
    if m is None:
        print(f"check-dashboard-js: no script block in {_PAGE_MODULE.name} PAGE", file=sys.stderr)
        return 1
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(m.group(1))
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
