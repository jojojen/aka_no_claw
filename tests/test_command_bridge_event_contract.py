from __future__ import annotations

from types import SimpleNamespace
import time
import io
import json
from http.server import BaseHTTPRequestHandler

from openclaw_adapter.command_bridge import CommandBridge
from openclaw_adapter.command_bridge_models import MODE_CHAT, WebCommandRequest, WebCommandResponse, stream_delta, stream_done
from openclaw_adapter import command_bridge_server as server


def _bridge(tmp_path) -> CommandBridge:
    return CommandBridge(SimpleNamespace(
        openclaw_web_memory_dir=str(tmp_path / "memory"),
        openclaw_web_event_dir=str(tmp_path / "events"),
        openclaw_web_event_max_bytes=1024 * 1024,
        openclaw_web_event_max_payload_bytes=64 * 1024,
        openclaw_web_jobs_dir=str(tmp_path / "jobs"),
    ))


def _request(text: str = "hello") -> WebCommandRequest:
    return WebCommandRequest(mode=MODE_CHAT, input=text, session_id="web-default")


def test_blocking_path_persists_complete_lifecycle(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(
        bridge, "_handle_unrecorded",
        lambda req: WebCommandResponse(status="ok", message="world", mode=MODE_CHAT),
    )

    assert bridge.handle(_request()).message == "world"
    events = bridge.read_session_events(after=0)["events"]
    assert [event["type"] for event in events] == [
        "session.created", "user.message", "run.accepted", "run.started",
        "planner.completed", "judge.completed", "assistant.message", "run.completed",
    ]
    assert events[-1]["run_id"] == events[1]["run_id"]


def test_stream_deltas_are_transport_only_but_final_message_is_durable(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_stream_chat", lambda req: iter([stream_delta("hel"), stream_done("hello")]))

    stream = list(bridge.stream(_request(), "request-1"))
    assert [event["type"] for event in stream] == ["start", "delta", "done"]
    durable = bridge.read_session_events(after=0)["events"]
    assert "assistant.delta" not in [event["type"] for event in durable]
    assert durable[-2]["type"] == "assistant.message"
    assert durable[-2]["payload"]["text"] == "hello"


def test_cursor_pages_are_replayable_without_duplicates(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(
        bridge, "_handle_unrecorded",
        lambda req: WebCommandResponse(status="ok", message="world", mode=MODE_CHAT),
    )
    bridge.handle(_request("one"))
    first = bridge.read_session_events(after=0, limit=3)
    second = bridge.read_session_events(after=first["server_cursor"], limit=20)
    sequences = [event["seq"] for event in first["events"] + second["events"]]
    assert sequences == sorted(set(sequences))
    assert first["latest_cursor"] == second["latest_cursor"] == sequences[-1]
    assert second["has_more"] is False


def test_snapshot_post_only_imports_messages_once_and_clear_is_journal_aware(tmp_path):
    bridge = _bridge(tmp_path)
    bridge.load_session()  # bootstrap empty journal before old-client POST
    bridge.save_session({"messages": [{"role": "user", "text": "legacy"}], "mode": "chat"})
    bridge.save_session({"messages": [{"role": "user", "text": "tampered"}], "mode": "chat"})
    assert [message["text"] for message in bridge.load_session()["session"]["messages"]] == ["legacy"]
    bridge.clear_session()
    assert bridge.load_session()["session"]["messages"] == []


def test_async_completion_injects_one_terminal_message_into_durable_history(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    monkeypatch.setattr(bridge, "_run_command_raw", lambda *args, **kwargs: ("research complete", None))
    req = WebCommandRequest(mode="investment", input="item", submode="deep_product_research", session_id="web-default")

    accepted = bridge.start_async(req)
    for _ in range(40):
        polled = bridge.poll_job(accepted["job_id"])
        if polled["job_status"] != "running":
            break
        time.sleep(0.01)
    assert polled["job_status"] == "done"
    assert polled["run_id"]
    events = bridge.read_session_events(after=0)["events"]
    messages = [event for event in events if event["type"] == "assistant.message"]
    terminals = [event for event in events if event["type"] == "run.completed"]
    assert [event["payload"]["text"] for event in messages] == ["research complete"], events
    assert len(terminals) == 1


def _handler(bridge, method: str, path: str, body: bytes = b"", headers: dict[str, str] | None = None):
    handler_cls = server._build_handler(bridge, lan_enabled=False)
    handler = handler_cls.__new__(handler_cls)
    handler.headers = {"Content-Length": str(len(body)), **(headers or {})}
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler.request_version = "HTTP/1.1"
    handler.protocol_version = "HTTP/1.0"
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.responses = BaseHTTPRequestHandler.responses
    handler.client_address = ("127.0.0.1", 1)
    handler.path = path
    return handler


def _json_response(handler):
    return json.loads(handler.wfile.getvalue().split(b"\r\n\r\n", 1)[1].decode())


def test_events_endpoint_validates_cursor_and_returns_projection_on_expiry():
    class _Bridge:
        def read_session_events(self, **kwargs):
            return {"status": "cursor_expired", "projection": {"messages": []}}

    handler = _handler(_Bridge(), "GET", "/api/command/events?after=0")
    handler.do_GET()
    assert _json_response(handler)["status"] == "cursor_expired"


def test_negotiated_ndjson_includes_new_durable_event_envelopes():
    durable = {"seq": 1, "type": "run.accepted", "event_id": "event-1"}

    class _Bridge:
        def stream(self, req, request_id):
            yield {"type": "start", "request_id": request_id}
            yield {"type": "done", "message": "ok"}

        def read_session_events(self, *, after=None, **kwargs):
            if after is None:
                return {"status": "ok", "events": [], "server_cursor": 0, "latest_cursor": 0}
            if after == 0:
                return {"status": "ok", "events": [durable], "server_cursor": 1}
            return {"status": "ok", "events": [], "server_cursor": 1}

    body = json.dumps({"mode": "chat", "input": "hi"}).encode()
    handler = _handler(
        _Bridge(), "POST", "/api/command/stream", body,
        {"X-OpenClaw-Event-Version": "1"},
    )
    handler.do_POST()
    payloads = [json.loads(line) for line in handler.wfile.getvalue().split(b"\r\n\r\n", 1)[1].splitlines()]
    assert payloads[1]["type"] == "session_event"
    assert payloads[1]["event"] == durable


def test_negotiated_ndjson_starts_at_journal_head_not_first_bootstrap_page():
    historic = {"seq": 500, "type": "run.completed", "event_id": "historic"}
    current = {"seq": 1001, "type": "run.accepted", "event_id": "current"}

    class _Bridge:
        def stream(self, req, request_id):
            yield {"type": "start", "request_id": request_id}
            yield {"type": "done", "message": "ok"}

        def read_session_events(self, *, after=None, **kwargs):
            if after is None:
                return {
                    "status": "ok", "events": [historic],
                    "server_cursor": 500, "latest_cursor": 1000, "has_more": True,
                }
            if after == 1000:
                return {
                    "status": "ok", "events": [current],
                    "server_cursor": 1001, "latest_cursor": 1001, "has_more": False,
                }
            return {
                "status": "ok", "events": [],
                "server_cursor": 1001, "latest_cursor": 1001, "has_more": False,
            }

    body = json.dumps({"mode": "chat", "input": "hi"}).encode()
    handler = _handler(
        _Bridge(), "POST", "/api/command/stream", body,
        {"X-OpenClaw-Event-Version": "1"},
    )
    handler.do_POST()
    payloads = [
        json.loads(line)
        for line in handler.wfile.getvalue().split(b"\r\n\r\n", 1)[1].splitlines()
    ]
    durable = [payload["event"] for payload in payloads if payload["type"] == "session_event"]
    assert durable == [current]
