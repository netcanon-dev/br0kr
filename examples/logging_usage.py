"""Worked example for br0kr's logging standard.

Run it both ways to see the two shapes:

    python examples/logging_usage.py            # plain text
    LOG_FORMAT=json python examples/logging_usage.py   # JSON lines
    LOG_LEVEL=DEBUG python examples/logging_usage.py    # env overrides level

Shows: configure-once, per-module logger, a run_id threaded through nested
calls + a spawned worker, structured extras, and that secrets/PII are redacted.
"""
from __future__ import annotations

import logging
import threading

from br0kr.logging_setup import (
    configure_logging,
    generate_correlation_id,
    push_context,
    set_context,
)

# One module-level logger per module. Never log on the root directly.
log = logging.getLogger(__name__)


def handle_request(payload: dict) -> None:
    """Top of a logical operation: mint a run_id and tag the whole scope."""
    with push_context(run_id=generate_correlation_id(), op="handle_request"):
        log.info("request received", extra={"path": payload.get("path")})
        _do_work(payload)            # nested logs inherit run_id + op
        log.info("request complete")


def _do_work(payload: dict) -> None:
    # A structured extra: queryable as a JSON key, redacted if its key is
    # sensitive. "item_count" is fine; "api_key" below is scrubbed.
    log.info("working", extra={"item_count": len(payload.get("items", []))})

    # Secrets / PII are redacted whether passed as an extra KEY...
    log.info("calling upstream", extra={"api_key": "sk-secret-value-123456789",
                                        "email": "someone@example.com"})
    # ...or embedded in the message TEXT (value-pattern scrub).
    log.info("auth header was Bearer abcdef0123456789")

    try:
        raise RuntimeError("upstream returned 503")
    except RuntimeError:
        # exc_info=True captures the traceback (also redacted).
        log.error("upstream call failed", exc_info=True)


def _worker(job_id: str) -> None:
    # IMPORTANT: a spawned thread does NOT inherit parent context. Re-establish
    # it on the worker's first line.
    set_context(run_id=generate_correlation_id(), worker="export", job_id=job_id)
    log.info("worker started")
    log.info("worker done")


def main() -> None:
    # Configure once at process start. Reads LOG_FORMAT / LOG_LEVEL from env.
    # In a real app, prefer apply_settings(profile=...) to honor saved config.
    configure_logging(log_file="app.log")

    handle_request({"path": "/reconcile", "items": [1, 2, 3]})

    t = threading.Thread(target=_worker, args=("job-42",))
    t.start()
    t.join()


if __name__ == "__main__":
    main()
