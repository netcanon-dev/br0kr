"""Unit tests for the PVE driver's one request seam, `Pve._request` (br0kr/pve.py, review OPS-03).

Every PVE API call goes through `_request`, which times it and counts its outcome in `PVE_REQUEST_SECONDS`
(`br0kr_pve_request_seconds{op,outcome}`). `requests.request` is stubbed, so no PVE is needed. Pinned: each outcome
class (ok, denied, http_error, timeout, unreachable) lands in its own series whether the call returns or raises,
the four verb helpers route through the seam with their method and form data, and the reply's `data` comes back
unchanged. The counter is process-wide, so each test reads deltas.
"""
from __future__ import annotations

import time
import types
from typing import Any

import pytest
import requests

from br0kr import pve

pytestmark = pytest.mark.unit


class _Reply:
    def __init__(self, status: int, data: Any = None) -> None:
        self.status_code = status
        self._data = data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            r = requests.Response()
            r.status_code = self.status_code
            raise requests.HTTPError(response=r)

    def json(self) -> dict[str, Any]:
        return {"data": self._data}


def _count(op: str, outcome: str) -> int:
    return pve.PVE_REQUEST_SECONDS.snapshot().get((op, outcome), (0.0, 0))[1]


def _driver() -> pve.Pve:
    return pve.Pve("pve.invalid", "node1", "id", "secret", timeout=3)


def test_every_verb_goes_through_the_seam_with_its_method_and_data(monkeypatch):
    """_get/_post/_put/_delete each send their HTTP method, the full API path, the token header and (for POST/PUT)
    the form data through ONE requests.request call, and return the reply's `data`. Guards a verb that bypasses the
    seam: its calls would vanish from br0kr_pve_request_seconds while still hitting PVE."""
    seen: list[tuple[str, str, Any]] = []

    def fake_request(method, url, *, headers, data, verify, timeout):
        seen.append((method, url, data))
        assert headers["Authorization"] == "PVEAPIToken=id=secret" and verify is False and timeout == 3
        return _Reply(200, {"method": method})

    monkeypatch.setattr(pve.requests, "request", fake_request)
    d = _driver()
    assert d._get("/cluster/nextid") == {"method": "GET"}
    assert d._post("/nodes/node1/qemu", {"vmid": 9901}) == {"method": "POST"}
    assert d._put("/nodes/node1/qemu/9901/resize", {"size": "10G"}) == {"method": "PUT"}
    assert d._delete("/nodes/node1/qemu/9901") == {"method": "DELETE"}
    base = "https://pve.invalid:8006/api2/json"
    assert seen == [("GET", base + "/cluster/nextid", None),
                    ("POST", base + "/nodes/node1/qemu", {"vmid": 9901}),
                    ("PUT", base + "/nodes/node1/qemu/9901/resize", {"size": "10G"}),
                    ("DELETE", base + "/nodes/node1/qemu/9901", None)]


@pytest.mark.parametrize("status,outcome", [(200, "ok"), (401, "denied"), (403, "denied"),
                                            (404, "http_error"), (500, "http_error")])
def test_an_http_answer_is_counted_by_its_status_class(monkeypatch, status, outcome):
    """A 2xx counts `ok`; 401/403 count `denied` (an ACL gap, never transient: the one to alert on first); any
    other 4xx/5xx counts `http_error`, and the HTTPError still reaches the caller. Guards a refusal that is raised
    but not counted, or counted as ok."""
    monkeypatch.setattr(pve.requests, "request", lambda *a, **k: _Reply(status, 1))
    before = _count("get", outcome)
    if status >= 400:
        with pytest.raises(requests.HTTPError):
            _driver()._get("/version")
    else:
        assert _driver()._get("/version") == 1
    assert _count("get", outcome) == before + 1


def test_a_2xx_whose_body_is_not_the_api_reply_counts_as_bad_reply(monkeypatch):
    """A 200 whose body is not JSON, or has no `data`, raises to the caller as before but counts `bad_reply`, not
    `ok`. Guards a proxy or error page answering 200 reading as a healthy PVE on /metrics."""
    class _NotJson(_Reply):
        def json(self) -> dict[str, Any]:
            raise ValueError("Expecting value")

    monkeypatch.setattr(pve.requests, "request", lambda *a, **k: _NotJson(200))
    before = (_count("get", "bad_reply"), _count("get", "ok"))
    with pytest.raises(ValueError):
        _driver()._get("/version")

    class _NoData(_Reply):
        def json(self) -> dict[str, Any]:
            return {"errors": {"vmid": "bad"}}

    monkeypatch.setattr(pve.requests, "request", lambda *a, **k: _NoData(200))
    with pytest.raises(KeyError):
        _driver()._get("/version")
    assert (_count("get", "bad_reply"), _count("get", "ok")) == (before[0] + 2, before[1])


@pytest.mark.parametrize("exc,outcome", [(requests.Timeout("slow"), "timeout"),
                                         (requests.ConnectionError("refused"), "unreachable")])
def test_no_http_answer_is_counted_as_timeout_or_unreachable(monkeypatch, exc, outcome):
    """A call that gets no HTTP answer is still counted, `timeout` for a timeout and `unreachable` for any other
    transport failure, and the exception still reaches the caller. Guards a down PVE node reading as silence on
    /metrics rather than as a burst of failures."""
    def boom(*_a, **_k):
        raise exc

    monkeypatch.setattr(pve.requests, "request", boom)
    before = _count("post", outcome)
    with pytest.raises(type(exc)):
        _driver()._post("/nodes/node1/qemu", {})
    assert _count("post", outcome) == before + 1


def test_the_seam_records_elapsed_time(monkeypatch):
    """The `_sum` series grows by the call's wall-clock time (a monotonic clock read around the request), so a
    slow PVE raises the mean latency. Guards a seam that counts calls but records no duration."""
    clock = iter([100.0, 102.5])
    # Replace pve's own `time` reference only; patching time.monotonic itself would reach every module.
    monkeypatch.setattr(pve, "time", types.SimpleNamespace(monotonic=lambda: next(clock), sleep=time.sleep))
    monkeypatch.setattr(pve.requests, "request", lambda *a, **k: _Reply(200, None))
    before = pve.PVE_REQUEST_SECONDS.snapshot().get(("delete", "ok"), (0.0, 0))
    _driver()._delete("/x")
    total, count = pve.PVE_REQUEST_SECONDS.snapshot()[("delete", "ok")]
    assert count == before[1] + 1 and total == pytest.approx(before[0] + 2.5)
