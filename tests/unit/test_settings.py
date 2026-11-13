"""Unit tests for the operator_setting resolver (br0kr/settings.py).

settings.py is the runtime-knob override layer: each knob resolves COALESCE(ledger override, env, default),
the dashboard edits the ledger layer, and the daemon/gateway hot-apply it. The load-bearing properties are
pure or fake-conn testable: the validator (reject the JSON-float-truncation trap + below-min + unknown key),
the COALESCE precedence + source reporting, the ledger-only `overrides` view the dashboard uses, and the
atomic read-and-clear of the reload flag. The end-to-end against real Postgres (CHECK constraints, the
queue_max cap driving admission, the reaper ceiling) is proven in scripts/harness_settings.py.
"""
from __future__ import annotations

from typing import Any

import pytest

from br0kr import settings

pytestmark = pytest.mark.unit


# ── the validator (the parse-boundary guard) ─────────────────────────────────────────────────────────
def test_validate_patch_accepts_ints_bools_str_and_clears():
    """A well-typed patch is accepted: an int at/above its minimum, a bool, a str within length, and a None
    (which CLEARS the override → fallback). Guards the validator rejecting a legitimate edit."""
    assert settings.validate_patch({"queue_max": 0, "reap_grace": 30}) is None
    assert settings.validate_patch({"sweep_dry_run": True}) is None
    assert settings.validate_patch({"guest_ip_prefix": "192.168.1."}) is None
    assert settings.validate_patch({"queue_max": None, "guest_ip_prefix": None}) is None   # clears
    assert settings.validate_patch({}) is None


def test_validate_patch_rejects_a_float_where_an_int_is_required():
    """A JSON float for an int knob is REJECTED (not silently truncated) — the exact trap the dashboard's
    `_opt_int` once hit. Guards `10.5` becoming `10` behind the DB CHECK. A bool is also not an int here."""
    assert settings.validate_patch({"reap_grace": 10.5}) == "not_an_int:reap_grace"
    assert settings.validate_patch({"queue_max": True}) == "not_an_int:queue_max"   # bool is not an int knob


def test_validate_patch_rejects_below_min_unknown_key_and_bad_types():
    """A negative int (below the knob minimum), an unknown key, a non-bool for a bool knob, a non-str for a
    str knob, and an over-long str are each a distinct reason. Guards a bad edit reaching the ledger / a DB
    CHECK error instead of a clean 400."""
    assert settings.validate_patch({"queue_max": -1}) == "below_min:queue_max"
    assert settings.validate_patch({"nonesuch": 1}) == "unknown_setting:nonesuch"
    assert settings.validate_patch({"sweep_dry_run": 1}) == "not_a_bool:sweep_dry_run"
    assert settings.validate_patch({"guest_ip_prefix": 5}) == "not_a_str:guest_ip_prefix"
    assert settings.validate_patch({"guest_ip_prefix": "x" * 65}) == "too_long:guest_ip_prefix"


def test_validate_patch_rejects_an_int_above_int4_max():
    """An int knob above int4 max is refused 'too_large' — a clean 400 — rather than passing validation and
    failing the UPDATE with NumericValueOutOfRange as an opaque 503 (review S19: every operator_setting int
    knob is an int4 column). Guards an oversized operator value surfacing as an unactionable server error."""
    assert settings.validate_patch({"provision_limit": 3_000_000_000}) == "too_large:provision_limit"
    assert settings.validate_patch({"queue_max": 2_147_483_648}) == "too_large:queue_max"   # int4 max + 1
    assert settings.validate_patch({"queue_max": 2_147_483_647}) is None                    # int4 max is allowed


def test_validate_patch_enforces_log_level_choices():
    """The log_level knob is an ENUM str (0022, `choices`): a valid level is accepted, None CLEARS it, and a
    wrong-CASE or bogus value is rejected 'invalid_choice' — a clean 400, never a DB CHECK error or a
    setLevel crash. A non-str is still 'not_a_str'. Guards the enum gate being dropped (which would let
    'banana' reach the ledger / the daemon's setLevel)."""
    assert settings.validate_patch({"log_level": "DEBUG"}) is None
    assert settings.validate_patch({"log_level": "ERROR"}) is None
    assert settings.validate_patch({"log_level": None}) is None                 # clears → env/default
    assert settings.validate_patch({"log_level": "debug"}) == "invalid_choice:log_level"   # wrong case
    assert settings.validate_patch({"log_level": "TRACE"}) == "invalid_choice:log_level"   # not a real level
    assert settings.validate_patch({"log_level": 5}) == "not_a_str:log_level"


def test_validate_patch_enforces_audit_retention_min():
    """The audit_retention_days knob (0024) is an int with minimum=0: 0 (OFF/keep-forever, the default) and a
    positive N are accepted, None CLEARS it, a NEGATIVE retention is rejected 'below_min' (a clean 400 before
    the DB CHECK), and a non-int (bool/float) is rejected. Guards a negative/garbage retention reaching the
    ledger or the daemon's prune."""
    assert settings.validate_patch({"audit_retention_days": 0}) is None      # OFF is valid
    assert settings.validate_patch({"audit_retention_days": 90}) is None
    assert settings.validate_patch({"audit_retention_days": None}) is None   # clears → env/default
    assert settings.validate_patch({"audit_retention_days": -1}) == "below_min:audit_retention_days"
    assert settings.validate_patch({"audit_retention_days": True}) == "not_an_int:audit_retention_days"
    assert settings.validate_patch({"audit_retention_days": 1.5}) == "not_an_int:audit_retention_days"


def test_discovery_interval_knob_is_an_int_defaulting_off(monkeypatch):
    """The discovery_interval_seconds knob (0043) is a daemon-only int (env BR0KR_DISCOVERY_INTERVAL_SECONDS,
    minimum=0) whose default is 0 = OFF (no periodic reachability-discovery report). 0 (OFF), a positive N, and
    a None (clears → env/default) are accepted; a negative is 'below_min' (a clean 400 before the DB CHECK) and a
    non-int (bool/float) is rejected. Guards the knob being dropped/mis-typed and a negative/garbage cadence
    reaching the ledger or the daemon's tick_upkeep.discovery_due gate."""
    by = {k.name: k for k in settings.KNOBS}
    knob = by["discovery_interval_seconds"]
    assert knob.kind == "int" and knob.default == 0 and knob.minimum == 0 and knob.gateway is False
    assert knob.env == "BR0KR_DISCOVERY_INTERVAL_SECONDS"
    monkeypatch.setattr(settings.os, "environ", {})
    assert settings._env_value(knob) is None                                         # no env → falls to default
    assert settings.resolve(_RowConn(_row()))["discovery_interval_seconds"] == settings.Resolved(0, "default")
    assert settings.validate_patch({"discovery_interval_seconds": 0}) is None         # OFF is valid
    assert settings.validate_patch({"discovery_interval_seconds": 300}) is None
    assert settings.validate_patch({"discovery_interval_seconds": None}) is None      # clears → env/default
    assert settings.validate_patch({"discovery_interval_seconds": -1}) == "below_min:discovery_interval_seconds"
    assert settings.validate_patch({"discovery_interval_seconds": True}) == "not_an_int:discovery_interval_seconds"
    assert settings.validate_patch({"discovery_interval_seconds": 1.5}) == "not_an_int:discovery_interval_seconds"


def test_multi_disk_enabled_knob_is_a_gateway_bool_defaulting_off(monkeypatch):
    """The multi_disk_enabled knob (0047, PR-5) is a GATEWAY bool (env BR0KR_MULTI_DISK_ENABLED) whose default
    is False = OFF — the request surface refuses a data_disks ask until the operator flips it. It resolves
    COALESCE(ledger, env, default=False) like every knob, so an all-NULL seed row is byte-identical to the
    deployed-but-inert default; a bool ledger/env override wins; a non-bool patch is rejected. Guards the knob
    being dropped/mis-typed (which would leave the surface un-gated — multi-disk live the moment PR-5 deploys)."""
    by = {k.name: k for k in settings.KNOBS}
    knob = by["multi_disk_enabled"]
    assert knob.kind == "bool" and knob.default is False and knob.gateway is True
    assert knob.env == "BR0KR_MULTI_DISK_ENABLED"
    monkeypatch.setattr(settings.os, "environ", {})
    assert settings.resolve(_RowConn(_row()))["multi_disk_enabled"] == settings.Resolved(False, "default")
    monkeypatch.setattr(settings.os, "environ", {"BR0KR_MULTI_DISK_ENABLED": "1"})
    assert settings.resolve(_RowConn(_row()))["multi_disk_enabled"] == settings.Resolved(True, "env")
    # a ledger True wins over the env; a False ledger override is still a real override (is-not-None, not truthy)
    assert settings.resolve(_RowConn(_row(multi_disk_enabled=True)))["multi_disk_enabled"] == \
        settings.Resolved(True, "ledger")
    assert settings.validate_patch({"multi_disk_enabled": True}) is None
    assert settings.validate_patch({"multi_disk_enabled": None}) is None    # clears → env/default
    assert settings.validate_patch({"multi_disk_enabled": 1}) == "not_a_bool:multi_disk_enabled"


# ── env parsing ──────────────────────────────────────────────────────────────────────────────────────
def test_env_value_parses_by_kind_and_ignores_garbage(monkeypatch):
    """The env seed parses by kind: a bool env is '1'→True else False, an int env parses (a non-int env is
    IGNORED → None so it falls to the default, never crashing the reader), an empty str env → None, and a
    knob with no env var (max_release_attempts) is always None. Guards a garbage env taking down the reader."""
    monkeypatch.setattr(settings.os, "environ", {
        "BR0KR_SWEEP_DRY_RUN": "1", "BR0KR_REAP_GRACE": "45", "BR0KR_QUEUE_MAX": "notanint",
        "BR0KR_GUEST_IP_PREFIX": ""})
    by = {k.name: k for k in settings.KNOBS}
    assert settings._env_value(by["sweep_dry_run"]) is True
    assert settings._env_value(by["reap_grace"]) == 45
    assert settings._env_value(by["queue_max"]) is None          # garbage int env ignored → falls to default
    assert settings._env_value(by["guest_ip_prefix"]) is None    # empty str → unset
    assert settings._env_value(by["max_release_attempts"]) is None   # no env var at all


def test_replication_mechanisms_knob_is_a_str_defaulting_clone_shared(monkeypatch):
    """The replication_mechanisms knob (0034) is a SHARED str knob (env BR0KR_REPLICATION_MECHANISMS) whose
    default is 'clone_shared' — a COMMA allowlist gateway.request_replication splits + checks. It is a plain str
    knob (NOT an enum `choices`) so it can widen to 'clone_shared,rebuild_source' in a later PR without a schema
    change. Guards the knob being dropped, mis-typed, or over-constrained to a single choice."""
    by = {k.name: k for k in settings.KNOBS}
    knob = by["replication_mechanisms"]
    assert knob.kind == "str" and knob.default == "clone_shared" and knob.choices is None
    assert knob.env == "BR0KR_REPLICATION_MECHANISMS"
    # No ledger/env override → resolves to the code default; validate_patch accepts a widened comma allowlist.
    monkeypatch.setattr(settings.os, "environ", {})
    assert settings._env_value(knob) is None
    assert settings.validate_patch({"replication_mechanisms": "clone_shared,rebuild_source"}) is None
    assert settings.validate_patch({"replication_mechanisms": 5}) == "not_a_str:replication_mechanisms"


def test_provision_stuck_seconds_knob_defaults_to_twenty_minutes():
    """The provision_stuck_seconds knob (0048, review REL-01) is a daemon-only int with NO env seed, default 1200
    (20 min) and minimum 0 (0 = the reaper step is off). Guards the knob being dropped, mis-typed, or defaulting
    to OFF, which would silently restore the unbounded PROVISIONING phase; and a negative budget reaching the
    ledger (a clean 'below_min' before the DB CHECK)."""
    from br0kr import lease
    by = {k.name: k for k in settings.KNOBS}
    knob = by["provision_stuck_seconds"]
    assert knob.kind == "int" and knob.env is None and knob.gateway is False and knob.minimum == 0
    assert knob.default == 1200 == lease.PROVISION_STUCK_SECONDS
    assert settings.resolve(_RowConn(_row()))["provision_stuck_seconds"] == settings.Resolved(1200, "default")
    assert settings.validate_patch({"provision_stuck_seconds": 0}) is None
    assert settings.validate_patch({"provision_stuck_seconds": -5}) == "below_min:provision_stuck_seconds"


# ── COALESCE(ledger, env, default) + source ──────────────────────────────────────────────────────────
class _RowConn:
    """A fake connection whose SELECT of the singleton returns `row` (a tuple in settings._COLS order, or
    None for a missing row). Only `resolve`/`overrides` use it."""

    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def execute(self, _sql: str, _params: Any = None) -> _RowConn:
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


def _row(**overrides: Any) -> tuple[Any, ...]:
    """Build a settings row tuple (in _COLS order) with the named columns overridden, rest None."""
    return tuple(overrides.get(name) for name in settings._COLS)


def test_resolve_falls_back_to_default_when_neither_ledger_nor_env(monkeypatch):
    """An all-NULL row with no env → every knob is its code DEFAULT, source 'default'. This is the seed-row
    state: byte-identical to the pre-Settings behaviour. Guards a missing override resolving to anything but
    the documented default."""
    monkeypatch.setattr(settings.os, "environ", {})
    res = settings.resolve(_RowConn(_row()))
    assert res["queue_max"] == settings.Resolved(1000, "default")
    assert res["reap_grace"].value == 30 and res["reap_grace"].source == "default"
    assert res["sweep_dry_run"] == settings.Resolved(False, "default")
    assert res["max_release_attempts"].value == 5


def test_resolve_uses_env_when_no_ledger_override(monkeypatch):
    """With no ledger override but an env var set, the knob resolves to the ENV value, source 'env' — env
    stays a valid config path. Guards the env layer being ignored (which would silently reset an env-tuned
    deployment to defaults on upgrade)."""
    monkeypatch.setattr(settings.os, "environ", {"BR0KR_REAP_GRACE": "45", "BR0KR_SWEEP_DRY_RUN": "1"})
    res = settings.resolve(_RowConn(_row()))
    assert res["reap_grace"] == settings.Resolved(45, "env")
    assert res["sweep_dry_run"] == settings.Resolved(True, "env")


def test_resolve_ledger_override_wins_over_env_incl_falsey_values(monkeypatch):
    """A ledger override WINS over the env, and a falsey override (0 / False) is a real override (the check
    is `is not None`, not truthiness) — so `build_limit=0` or `sweep_dry_run=false` set in the ledger stick
    even when the env says otherwise. Guards a 0/False override being swallowed as 'unset'."""
    monkeypatch.setattr(settings.os, "environ", {"BR0KR_REAP_GRACE": "45", "BR0KR_BUILD_LIMIT": "3"})
    res = settings.resolve(_RowConn(_row(reap_grace=99, build_limit=0, sweep_dry_run=False)))
    assert res["reap_grace"] == settings.Resolved(99, "ledger")       # ledger beats env
    assert res["build_limit"] == settings.Resolved(0, "ledger")       # 0 is a real override, not 'unset'
    assert res["sweep_dry_run"] == settings.Resolved(False, "ledger")


def test_resolve_log_level_defaults_to_info_and_honours_overrides(monkeypatch):
    """log_level resolves COALESCE(ledger, LOG_LEVEL env, 'INFO') like every knob — default INFO (matching
    configure_logging's startup default, so an all-NULL seed row is byte-identical), the env when set, and a
    ledger override winning over the env. Guards the daemon's hot-apply resolving to the wrong level."""
    monkeypatch.setattr(settings.os, "environ", {})
    assert settings.resolve(_RowConn(_row()))["log_level"] == settings.Resolved("INFO", "default")
    monkeypatch.setattr(settings.os, "environ", {"LOG_LEVEL": "WARNING"})
    assert settings.resolve(_RowConn(_row()))["log_level"] == settings.Resolved("WARNING", "env")
    assert settings.resolve(_RowConn(_row(log_level="DEBUG")))["log_level"] == settings.Resolved("DEBUG", "ledger")


def test_overrides_returns_only_the_ledger_layer():
    """`overrides` (what the dashboard edits) returns the raw ledger values — None where not overridden — and
    does NOT fold in env/default (the dashboard shares neither the daemon's nor the gateway's env, so it must
    manage the shared ledger layer). Guards the dashboard showing an env-resolved value it can't see."""
    ov = settings.overrides(_RowConn(_row(queue_max=42)))
    assert ov["queue_max"] == 42 and ov["reap_grace"] is None and ov["sweep_dry_run"] is None


# ── the reload flag (atomic read-and-clear) ──────────────────────────────────────────────────────────
class _ReloadConn:
    """A fake conn for take_reload: the UPDATE ... WHERE reload_requested RETURNING true returns a row iff a
    reload was pending (then the flag flips false), else None."""

    def __init__(self, pending: bool) -> None:
        self._pending = pending

    def execute(self, sql: str, _params: Any = None) -> _ReloadConn:
        self._took = "reload_requested = false" in sql and self._pending
        if "reload_requested = false" in sql:
            self._pending = False
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        return (True,) if self._took else None


def test_take_reload_returns_true_and_clears_once():
    """take_reload returns True when a reload was pending (and clears it) and False otherwise — so a set flag
    triggers exactly ONE reload, not a reload every tick. Guards a stuck flag re-reloading the fleet forever
    or a set flag being dropped."""
    conn = _ReloadConn(pending=True)
    assert settings.take_reload(conn) is True     # pending → consumed
    assert settings.take_reload(conn) is False    # already cleared → no repeat
    assert settings.take_reload(_ReloadConn(pending=False)) is False
