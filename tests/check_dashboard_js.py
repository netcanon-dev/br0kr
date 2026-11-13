#!/usr/bin/env python3
"""check_dashboard_js.py — the JS syntax gate for the dashboard client.

The dashboard's client is a file of its own, br0kr/static/dashboard.js (review UI-08), served as-is at
/assets/app-<hash>/dashboard.js, so the file on disk is exactly what browsers run. ruff / py_compile / mypy do not
read JavaScript, and a syntax error in this file ships a dead page (a stray backtick did once, when the script
still lived in a Python string), so this runs `node --check` on it. It is wired into tests/validate.sh as one
step; it is NOT a pytest test (no test_ prefix), mirroring tests/check_test_docs.py.

node is not everywhere the gate runs (a python-only container has none), so this SELF-SKIPS green when node is
absent: enforced wherever node exists, local dev and CI included (GitHub's ubuntu runner ships node, and CI's
validate job prints `node --check ok`), never failing a node-less --strict run. The backtick-parity test in
tests/unit/test_dashboard.py is the dependency-free backstop that always runs.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "br0kr" / "static" / "dashboard.js"


def main() -> int:
    if shutil.which("node") is None:
        print("skip check-dashboard-js: node not installed")
        return 0
    if not SCRIPT.is_file():
        print(f"check-dashboard-js: {SCRIPT} is missing", file=sys.stderr)
        return 1
    result = subprocess.run(["node", "--check", str(SCRIPT)], check=False)
    if result.returncode == 0:
        print("check-dashboard-js: node --check ok")
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
