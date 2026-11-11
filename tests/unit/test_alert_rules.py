"""Drift guard for the shipped Prometheus alert rules, docker/prometheus/br0kr-alerts.yml (review OPS-03).

promtool (scripts/check_alert_rules.sh, CI's `alert-rules` job) proves the rules parse and that each one fires on
its condition and stays quiet on the near-misses. What promtool cannot know is whether the rules still match THIS
code and these docs. So this pins:

  * every metric an expression names is one the code emits: a renamed gauge would otherwise leave an alert that
    can never fire, and nothing would say so;
  * every alert has a severity, a summary, and a runbook_url pointing at its own heading in docs/runbooks/alerts.md,
    and the runbook's table lists exactly the shipped alerts;
  * every alert has a promtool case that expects it to FIRE (a new alert cannot ship untested);
  * CI runs promtool, from a digest-pinned image with no network;
  * the one place the runbooks repeat an expression (ledger-ha.md's table) matches the rule, and no doc carries a
    second copy of rule YAML.
"""
from __future__ import annotations

import re
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest
import yaml

from br0kr import daemon, gateway, metrics, observability, pve
from br0kr.observability import HealthState

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "docker" / "prometheus" / "br0kr-alerts.yml"
PROMTOOL_TESTS = ROOT / "docker" / "prometheus" / "br0kr-alerts.test.yml"
RUNBOOK = ROOT / "docs" / "runbooks" / "alerts.md"
RUNBOOK_URL = "https://github.com/netcanon-dev/br0kr/blob/main/docs/runbooks/alerts.md#"


def _rules() -> list[dict[str, Any]]:
    doc = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    return [rule for group in doc["groups"] for rule in group["rules"]]


class _EverySlo(dict):  # type: ignore[type-arg]
    """A fleet_stats stand-in that answers 1 for every key, so render_metrics emits every SLO and fleet gauge."""

    def __bool__(self) -> bool:
        return True

    def get(self, key: object, default: object = None) -> object:
        return 1


def _emitted_metrics() -> set[str]:
    """Every metric name the daemon's /metrics and the gateway's listener can emit, from the code itself: the daemon
    render with every tick field, SLO gauge and replication gauge present, plus the process-local families."""
    tick = list(fields(daemon.TickSummary))
    cumulative = {f.metadata.get("cum_key", f.name): 0 for f in tick if f.metadata.get("metric", "counter") == "counter"}
    state = HealthState(role=observability.ROLE_LEADER, leader=True, last_tick_monotonic=0.0,
                        last={f.name: 0 for f in tick}, cumulative=cumulative)
    repl = {"readable": True, "is_primary": True, "configured": True, "max_replay_lag_bytes": 1,
            "slots": [{"name": "s", "active": True, "lost": False, "retained_wal_bytes": 1}]}
    body = observability.render_metrics(state, True, {"ACTIVE": 1, observability.PINNED_KEY: 0}, now=1.0,
                                        slo=_EverySlo(), repl=repl, progress=0.5)
    names = set(re.findall(r"^# TYPE (\S+) \S+$", body, re.M))
    for family in (gateway.ADMIT_OUTCOMES, pve.PVE_REQUEST_SECONDS):
        if isinstance(family, metrics.LabelledSummary):
            names |= {family.name + "_sum", family.name + "_count"}
        else:
            names.add(family.name)
    return names


def test_every_expression_names_a_metric_the_code_emits():
    """Each br0kr_* name in an alert expression is emitted by the daemon or the gateway. A rename on either side
    (say br0kr_held_leases) would otherwise leave an alert that silently can never fire."""
    emitted = _emitted_metrics()
    for rule in _rules():
        used = set(re.findall(r"\bbr0kr_[a-z0-9_]+", rule["expr"]))
        assert used, rule["alert"]
        assert used <= emitted, (rule["alert"], sorted(used - emitted))


def test_every_alert_is_labelled_summarised_and_linked_to_its_own_runbook_section():
    """Each alert carries severity critical|warning, a summary, and a runbook_url that is alerts.md's anchor for a
    heading named after it; the runbook's table lists exactly the shipped alerts. Guards a page that arrives with
    no instructions, a link to a section that was renamed, and a runbook still describing a deleted alert."""
    runbook = RUNBOOK.read_text(encoding="utf-8")
    headings = set(re.findall(r"^### (Br0kr\w+)$", runbook, re.M))
    table = set(re.findall(r"^\| \[`(Br0kr\w+)`\]\(#(\w+)\)", runbook, re.M))
    names = [rule["alert"] for rule in _rules()]
    assert len(names) == len(set(names))
    for rule in _rules():
        name = rule["alert"]
        assert rule["labels"]["severity"] in ("critical", "warning"), name
        assert rule["annotations"]["summary"], name
        assert rule["annotations"]["runbook_url"] == RUNBOOK_URL + name.lower(), name
    assert headings == set(names)
    assert table == {(n, n.lower()) for n in names}


def test_every_alert_has_a_promtool_case_that_expects_it_to_fire():
    """br0kr-alerts.test.yml loads the shipped rules and has, for every alert, at least one case expecting it to
    fire. A new alert without one would pass `promtool test rules` untested."""
    doc = yaml.safe_load(PROMTOOL_TESTS.read_text(encoding="utf-8"))
    assert doc["rule_files"] == ["br0kr-alerts.yml"]
    fired = {case["alertname"] for test in doc["tests"] for case in test["alert_rule_test"] if case["exp_alerts"]}
    assert fired == {rule["alert"] for rule in _rules()}


def test_ci_runs_promtool_from_a_pinned_image_with_no_network():
    """CI's alert-rules job runs scripts/check_alert_rules.sh, which pins the Prometheus image by digest and runs
    promtool with no network and the rules mounted read-only. Guards the promtool proof being dropped from CI, and
    a floating image tag."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert re.search(r"^  alert-rules:\n(?:    .*\n|\n)*?      - name: .*\n        run: bash scripts/check_alert_rules.sh$",
                     ci, re.M)
    script = (ROOT / "scripts" / "check_alert_rules.sh").read_text(encoding="utf-8")
    assert re.search(r"prom/prometheus:v\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", script)
    assert "--network none" in script and ":/rules:ro" in script
    assert "check rules br0kr-alerts.yml" in script and "test rules br0kr-alerts.test.yml" in script


def test_the_runbooks_repeat_no_rule_yaml_and_ledger_has_table_matches_the_rules():
    """No doc carries a copy of rule YAML (ledger-ha.md used to, and its sync-standby rule paged forever on an async
    ledger). The ledger-ha.md table that names each ledger alert's expression and `for` must match the shipped
    rule exactly."""
    for doc in (ROOT / "docs").rglob("*.md"):
        assert "- alert: " not in doc.read_text(encoding="utf-8"), doc
    by_name = {rule["alert"]: rule for rule in _rules()}
    ledger_ha = (ROOT / "docs" / "runbooks" / "ledger-ha.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| `(Br0krLedger\w+)`[^|]*\| `([^`]+)` \| ([^|]+) \|$", ledger_ha, re.M)
    assert {name for name, _e, _f in rows} == {n for n in by_name if n.startswith("Br0krLedger")}
    for name, expr, for_cell in rows:
        rule = by_name[name]
        assert expr == rule["expr"], name
        assert for_cell.split(",")[0].strip() == rule.get("for", "0m"), name
        if "keep_firing_for" in rule:
            assert f"`keep_firing_for: {rule['keep_firing_for']}`" in for_cell, name
