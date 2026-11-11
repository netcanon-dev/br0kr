"""Drift guard for docs/reference/reason-codes.md, the table of every reason code an agent can receive (review UX-04).

The reason string is an agent's only error channel, so each code it can get needs a row saying what it means and what
to do about it. These tests pin the table to the code:

  * the vocabulary table lists exactly `br0kr/reasons.ALL`: a new code without a row fails, and so does a row for a
    code that no longer exists;
  * the internal promote outcomes (`reasons.INTERNAL`) never appear, since no agent receives them;
  * every code in the "outside the vocabulary" tables is still spelled somewhere in br0kr/ (a stale row fails), and
    none of them belongs in the vocabulary table instead;
  * every row is complete, every table line parses as a row, and its agent action starts with one of the legend's
    action words (the legend lists exactly those words);
  * a queueable code tells the agent about queue=true, and a non-queueable one does not;
  * the request tools' docstrings point an agent at the table and name the ttl and storage-capability codes.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from br0kr import reasons

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_DOC = _ROOT / "docs" / "reference" / "reason-codes.md"
_ROW = re.compile(r"^\| `([a-z0-9_]+)(:<[a-z_]+>)?` \|(.*)\|\s*$")
# How an agent-action cell may begin (the doc's legend lists the same words).
_ACTIONS = ("Fix the ask", "Retry", "Wait", "Do not retry", "Ask the operator", "Request a new lease", "Nothing")


def _block(name: str) -> str:
    text = _DOC.read_text(encoding="utf-8")
    m = re.search(rf"<!-- {name}: begin -->(.*?)<!-- {name}: end -->", text, re.S)
    assert m, f"docs/reference/reason-codes.md lost its `<!-- {name}: begin/end -->` markers"
    return m.group(1)


def _rows(block: str) -> list[tuple[str, bool, list[str]]]:
    """(code, is_composite, cells after the code) for each table row in a block."""
    out = []
    for line in block.splitlines():
        m = _ROW.match(line)
        if m:
            out.append((m.group(1), m.group(2) is not None, [c.strip() for c in m.group(3).split("|")]))
    return out


def test_the_vocabulary_table_lists_exactly_the_codes_an_agent_can_receive():
    """The vocabulary table has one row per code in reasons.ALL, none twice and none extra. Guards a new reason
    shipping undocumented (the failure UX-04 names: ~15 codes an agent could receive were written down nowhere)
    and a row outliving its code."""
    codes = [code for code, _composite, _cells in _rows(_block("vocabulary"))]
    assert len(codes) == len(set(codes)), sorted({c for c in codes if codes.count(c) > 1})
    assert not reasons.ALL - set(codes), f"codes with no row: {sorted(reasons.ALL - set(codes))}"
    assert not set(codes) - reasons.ALL, f"rows for codes not in br0kr/reasons.py: {sorted(set(codes) - reasons.ALL)}"


def test_internal_codes_are_not_documented_as_agent_facing():
    """No row anywhere names a reasons.INTERNAL code (`gone`, `no_node`): the scheduler and the daemon read them, an
    agent never does. Guards the table telling an agent to handle a code it cannot receive."""
    every = {code for name in ("vocabulary", "outside") for code, _c, _cells in _rows(_block(name))}
    assert not every & reasons.INTERNAL


def test_codes_outside_the_vocabulary_still_exist_in_the_code():
    """Each code in the outside-the-vocabulary tables (lease release reasons, migration and replication job reasons)
    is spelled in br0kr/ as a string (a composite `prefix:<detail>` row by its prefix), and none is in reasons.ALL,
    whose codes belong in the vocabulary table. Guards a stale row for a reason the code no longer writes."""
    source = "\n".join(p.read_text(encoding="utf-8") for p in sorted((_ROOT / "br0kr").glob("*.py")))
    rows = _rows(_block("outside"))
    assert rows, "the outside-the-vocabulary tables are empty"
    quote = "['\"]"
    stale = [code for code, composite, _cells in rows
             if not re.search(quote + code + (":" if composite else quote), source)]
    assert not stale, f"no longer in br0kr/: {stale}"
    misplaced = sorted({code for code, _c, _cells in rows} & reasons.ALL)
    assert not misplaced, f"in br0kr/reasons.py, so they belong in the vocabulary table: {misplaced}"


def test_every_row_is_complete_and_names_an_action():
    """Each table line in the two blocks parses as a row (so no row is silently skipped by the checks above), each
    row has its four cells (where, meaning, agent action, operator action) filled, the agent action starts with one
    of the action words, and the legend lists exactly those words. Guards a row an agent cannot act on (a blank
    cell, advice that does not say whether to fix, retry, wait or stop) and the legend and the check drifting."""
    for name in ("vocabulary", "outside"):
        block = _block(name)
        unparsed = [line for line in block.splitlines() if line.startswith("|") and not _ROW.match(line)
                    and not line.startswith(("| Code |", "|---"))]
        assert not unparsed, unparsed
        for code, _composite, cells in _rows(block):
            assert len(cells) == 4 and all(cells), (code, cells)
            assert cells[2].startswith(_ACTIONS), (code, cells[2])
    legend = re.findall(r"^  - \*\*([A-Za-z ]+)\*\*:", _DOC.read_text(encoding="utf-8"), re.M)
    assert tuple(legend) == _ACTIONS, legend


def test_queueable_codes_point_at_queue_true_and_node_cordoned_does_not():
    """Every code in reasons.QUEUEABLE tells the agent it can resend with queue=true, and node_cordoned, which sits
    beside them but is not queueable, does not. Guards the table offering the queue for a refusal it never parks
    (the agent would wait for nothing) or hiding it for one it does."""
    agent = {code: cells[2] for code, _c, cells in _rows(_block("vocabulary"))}
    assert all("queue=true" in agent[code] for code in reasons.QUEUEABLE), sorted(
        c for c in reasons.QUEUEABLE if "queue=true" not in agent[c])
    assert "queue=true" not in agent[reasons.NODE_CORDONED]


def test_the_request_tools_point_agents_at_the_table():
    """request_lease and schedule_lease, whose docstrings are what an agent reads, each name the table and the
    invalid_ttl and storage_not_capable refusals, and the agent guide links the table. Guards the table existing where
    no agent would find it, and the review's two most-missed codes dropping out of the agent's view."""
    tree = ast.parse((_ROOT / "br0kr" / "mcp_server.py").read_text(encoding="utf-8"))
    docs = {n.name: ast.get_docstring(n) or "" for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for tool in ("request_lease", "schedule_lease"):
        for needle in ("docs/reference/reason-codes.md", reasons.INVALID_TTL, reasons.STORAGE_NOT_CAPABLE):
            assert needle in docs[tool], (tool, needle)
    guide = (_ROOT / "docs" / "runbooks" / "connecting-an-agent.md").read_text(encoding="utf-8")
    assert "../reference/reason-codes.md" in guide
