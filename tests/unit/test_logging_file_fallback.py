"""An unwritable log file degrades to stderr-only logging instead of stopping the process (review SEC-02).

The containers' root filesystem is read-only, so a `BR0KR_LOG_FILE` left pointing into it (or at a volume the
broker user cannot write) used to raise at process start, which under `restart: unless-stopped` crash-loops
every broker service at once.
"""
from __future__ import annotations

import io
import logging
import logging.handlers
import sys

import pytest

from br0kr.logging_setup import configure_logging

pytestmark = pytest.mark.unit


@pytest.fixture
def _restore_logging():
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        if handler not in saved:
            handler.close()
    for handler in saved:
        root.addHandler(handler)
    root.setLevel(level)


@pytest.mark.usefixtures("_restore_logging")
def test_unwritable_log_file_falls_back_to_stderr(tmp_path):
    """A log path whose directory cannot be created logs one WARNING naming the path, attaches no file
    handler, and leaves the stream handler working. Guards the read-only container root turning a stale
    BR0KR_LOG_FILE into a crash loop of every broker service. (A directory under a regular file cannot be
    created even as root, so this holds wherever the suite runs.)"""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    stream = io.StringIO()
    configure_logging(level=logging.INFO, json_output=False, log_file=str(blocker / "sub" / "broker.log"),
                      stream=stream)
    logging.getLogger("br0kr.test").info("still logging")
    out = stream.getvalue()
    assert "is not writable" in out and str(blocker / "sub" / "broker.log") in out
    assert "still logging" in out
    assert not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in logging.getLogger().handlers)


@pytest.mark.usefixtures("_restore_logging")
def test_writable_log_file_still_gets_a_file_handler(tmp_path):
    """A writable path still gets its rotating file handler, and a line logged afterwards lands in the file.
    Guards the fallback swallowing the normal case."""
    path = tmp_path / "logs" / "broker.log"
    configure_logging(level=logging.INFO, json_output=False, log_file=str(path), stream=sys.stderr)
    logging.getLogger("br0kr.test").info("to the file")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "to the file" in path.read_text(encoding="utf-8")
