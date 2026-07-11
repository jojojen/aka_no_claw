"""HTTP-layer characterization tests for command_bridge_server (#74 R1.0).

These lock the transport contract BEFORE the R1 decomposition moves code:
route → status code, JSON envelope stamping, NDJSON framing/ordering, and
malformed-body handling, exercised over a real ThreadingHTTPServer on an
ephemeral loopback port with a duck-typed fake bridge. The bridge's own
behavior is covered in test_command_bridge.py; here only the wire format is
under test, so any facade refactor that changes what the frontend actually
sees fails these tests immediately.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from openclaw_adapter.command_bridge_models import (
    MODE_CHAT,
    STATUS_OK,
    WebCommandResponse,
    stream_delta,
    stream_done,
    stream_job,
    stream_start,
)
from openclaw_adapter.command_bridge_server import _build_handler


class _FakeBridge:
    """Duck-typed stand-in exposing only what the exercised routes call."""

    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.polled: list[str] = []

    def handle(self, req):
        return WebCommandResponse(
            status=STATUS_OK, message=f"echo:{req.input}", mode=MODE_CHAT
        )

    def stream(self, req, request_id):
        yield stream_start(request_id)
        yield stream_job("job-7")
        yield stream_delta("研究中…\n")
        yield stream_done("最終答案")

    def poll_job(self, job_id):
        self.polled.append(job_id)
        return {"job_status": "done", "message": "answer", "progress": [],
                "actions": [], "error": None}

    def cancel_job(self, job_id):
        self.cancelled.append(job_id)
        return {"status": STATUS_OK, "job_status": "interrupted",
                "message": "已要求取消，將於下一個安全點停止。"}


@pytest.fixture()
def server():
    bridge = _FakeBridge()
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), _build_handler(bridge, lan_enabled=False)
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base, bridge
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(base: str, path: str, payload: bytes | dict):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    req = urllib.request.Request(
        base + path, data=body, headers={"Content-Type": "application/json"}
    )
    return urllib.request.urlopen(req, timeout=5)


def test_blocking_route_json_contract(server):
    base, _ = server
    with _post(base, "/api/command", {"mode": "chat", "input": "hi"}) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("application/json")
        data = json.loads(resp.read())
    assert data["status"] == STATUS_OK
    assert data["message"] == "echo:hi"
    assert data["mode"] == MODE_CHAT
    assert data["envelope_version"] == 1  # stamped on every JSON object


def test_blocking_route_malformed_json_is_400(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(base, "/api/command", b"{not json")
    assert excinfo.value.code == 400
    data = json.loads(excinfo.value.read())
    assert data["status"] == "error"
    assert data["envelope_version"] == 1  # errors are stamped too


def test_blocking_route_invalid_request_shape_is_400(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(base, "/api/command", {"mode": "no_such_mode", "input": "x"})
    assert excinfo.value.code == 400
    assert json.loads(excinfo.value.read())["status"] == "error"


def test_stream_route_ndjson_framing_and_ordering(server):
    base, _ = server
    with _post(base, "/api/command/stream", {"mode": "chat", "input": "hi"}) as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("application/x-ndjson")
        lines = [ln for ln in resp.read().decode("utf-8").splitlines() if ln]
    events = [json.loads(ln) for ln in lines]  # one JSON object per line
    types = [e["type"] for e in events]
    # Ordering contract (§7 of the R1 inventory): start first, exactly one
    # terminal event, and it comes last; the recovery job id precedes it.
    assert types[0] == "start"
    assert types.count("start") == 1
    terminals = [t for t in types if t in ("done", "error", "redirect")]
    assert terminals == ["done"]
    assert types[-1] == "done"
    assert types.index("job") < types.index("done")
    # Every event on the wire is envelope-stamped.
    assert all(e["envelope_version"] == 1 for e in events)
    assert events[-1]["message"] == "最終答案"


def test_poll_route_requires_job_id(server):
    base, bridge = server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(base + "/api/command/poll", timeout=5)
    assert excinfo.value.code == 400
    with urllib.request.urlopen(base + "/api/command/poll?job_id=j1", timeout=5) as resp:
        data = json.loads(resp.read())
    assert bridge.polled == ["j1"]
    assert data["job_status"] == "done"
    assert data["envelope_version"] == 1


def test_cancel_route_requires_job_id_then_relays(server):
    base, bridge = server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(base, "/api/command/cancel", {})
    assert excinfo.value.code == 400
    with _post(base, "/api/command/cancel", {"job_id": "j9"}) as resp:
        data = json.loads(resp.read())
    assert bridge.cancelled == ["j9"]
    assert data["job_status"] == "interrupted"


def test_unknown_route_is_404(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(base, "/api/command/nope", {})
    assert excinfo.value.code == 404
