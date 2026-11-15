"""Unit tests for br0kr/metrics_snapshot.py — the ledger-wide series one /metrics scrape reads.

The scrape itself (a fresh connection, the cached snapshot, a failed read) is covered by tests/integration/test_daemon.py;
these pin the SQL it runs.
"""
from __future__ import annotations

from br0kr import lease, metrics_snapshot


def test_the_pinned_gauge_counts_only_pins_br0kr_built():
    """br0kr_leases_pinned counts live pinned leases br0kr built (ownership provisioned), the pins a project's
    max_pinned counts (pin._LIVE_PINNED) and the dashboard's pinned_count shows. A lease br0kr did not build (0055:
    an adopted guest, a capacity-only record) is pinned by schema. Guards the gauge reading 6 after six records
    while every project shows no pins used."""
    sql = " ".join(metrics_snapshot._LIVE_PINNED_COUNT.split())
    assert sql.startswith("SELECT count(*) FROM lease WHERE pinned AND state IN (")
    assert sql.endswith(f"AND ownership = '{lease.PROVISIONED}'"), sql
