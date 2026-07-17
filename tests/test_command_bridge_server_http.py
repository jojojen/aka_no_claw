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
        self.approval_error: Exception | None = None
        self.queue_entries = []

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

    def resolve_workflow_approval(self, payload):
        if self.approval_error is not None:
            raise self.approval_error
        self.approval_payload = payload
        return {"status": STATUS_OK, "message": "approved", "approval": {"resolution": "approved"}}

    def load_prompt_queue(self, session_id):
        return {"status": STATUS_OK, "session_id": session_id or "web-default", "entries": self.queue_entries, "running_prompt_id": None}

    def create_prompt_queue_entry(self, payload):
        self.queue_payload = payload
        self.queue_entries = [{"prompt_id": "p1", "session_id": "s1", "version": 1, "position": 0, "intent": "next_turn", "mode": "chat", "text": "later", "status": "queued"}]
        return {"status": STATUS_OK, "entry": self.queue_entries[0], "entries": self.queue_entries, "running_prompt_id": None}

    def edit_prompt_queue_entry(self, prompt_id, payload):
        self.queue_edit = (prompt_id, payload)
        return {"status": STATUS_OK, "entries": self.queue_entries, "running_prompt_id": None}

    def cancel_prompt_queue_entry(self, prompt_id, *, session_id, expected_version):
        self.queue_cancel = (prompt_id, session_id, expected_version)
        return {"status": STATUS_OK, "entries": [], "running_prompt_id": None}

    def reorder_prompt_queue(self, payload):
        self.queue_reorder = payload
        return {"status": STATUS_OK, "entries": self.queue_entries, "running_prompt_id": None}


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


def test_approval_route_relays_typed_decision(server):
    base, bridge = server
    payload = {
        "approval_id": "a", "session_id": "s", "run_id": "r",
        "decision_token": "t", "decision": "approve",
    }
    with _post(base, "/api/command/approval", payload) as resp:
        data = json.loads(resp.read())
    assert bridge.approval_payload == payload
    assert data["approval"]["resolution"] == "approved"
    assert data["envelope_version"] == 1


def test_queue_routes_relay_versioned_mutations(server):
    base, bridge = server
    payload = {"intent": "next_turn", "request": {"mode": "chat", "input": "later", "session_id": "s1"}}
    with _post(base, "/api/command/queue", payload) as response:
        created = json.loads(response.read())
    assert bridge.queue_payload == payload
    assert created["entry"]["prompt_id"] == "p1"
    with urllib.request.urlopen(base + "/api/command/queue?session_id=s1", timeout=5) as response:
        assert json.loads(response.read())["entries"][0]["text"] == "later"
    edit = urllib.request.Request(
        base + "/api/command/queue/p1", data=json.dumps({"session_id": "s1", "text": "edited", "expected_version": 1}).encode(),
        method="PATCH", headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(edit, timeout=5):
        pass
    assert bridge.queue_edit[0] == "p1"
    delete = urllib.request.Request(base + "/api/command/queue/p1?session_id=s1&expected_version=1", method="DELETE")
    with urllib.request.urlopen(delete, timeout=5):
        pass
    assert bridge.queue_cancel == ("p1", "s1", 1)


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (ValueError("bad approval"), 400),
        (KeyError("missing"), 404),
        (PermissionError("binding mismatch"), 403),
        (RuntimeError("already resolved"), 409),
    ],
)
def test_approval_route_maps_typed_failures(server, error, expected_status):
    base, bridge = server
    bridge.approval_error = error
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(base, "/api/command/approval", {})
    assert excinfo.value.code == expected_status
    data = json.loads(excinfo.value.read())
    assert data["status"] == "error"
    assert data["envelope_version"] == 1
