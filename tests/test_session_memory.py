"""Issue #32 — server-side short-term session memory for aka_no_claw_web.

The store owns a single JSON snapshot on the Mac mini so the mobile console
restores its session after a reload / reconnect. These tests pin the contract
the frontend depends on: round-trip save/load, idempotent delete, corrupt/missing
files fail soft to an empty session (never crash), expired snapshots are dropped,
and retention trims by count + byte budget. The HTTP route tests assert
GET/POST/DELETE /api/command/session wire into the bridge.
"""
from __future__ import annotations

import io
import json
import time
from http.server import BaseHTTPRequestHandler
from types import SimpleNamespace

import pytest

from openclaw_adapter import command_bridge_server as srv
from openclaw_adapter.command_bridge import CommandBridge
from openclaw_adapter.session_memory import (
    MAX_BYTES,
    MAX_MESSAGES,
    MAX_SCALAR_LEN,
    SCHEMA_VERSION,
    SessionMemoryStore,
    empty_session,
)


@pytest.fixture
def store(tmp_path):
    return SessionMemoryStore(str(tmp_path / "web_console_memory"))


# --- empty / missing -------------------------------------------------------
def test_load_missing_returns_empty(store):
    s = store.load()
    assert s == empty_session()
    assert s["messages"] == []
    assert s["schema_version"] == SCHEMA_VERSION


# --- round trip ------------------------------------------------------------
def test_save_then_load_round_trips(store):
    snap = {
        "messages": [{"id": "1", "role": "user", "text": "hi"}],
        "mode": "chat",
        "chat_backend": "local",
        "investment_submode": "deep_product_research",
        "active_job_id": "job-1",
    }
    stored = store.save(snap)
    assert stored["updated_at"] is not None
    assert stored["schema_version"] == SCHEMA_VERSION

    loaded = store.load()
    assert loaded["messages"] == snap["messages"]
    assert loaded["mode"] == "chat"
    assert loaded["chat_backend"] == "local"
    assert loaded["investment_submode"] == "deep_product_research"
    assert loaded["active_job_id"] == "job-1"


def test_save_ignores_unknown_fields(store):
    store.save({"messages": [], "mode": "chat", "secret": "drop me", "knowledge": {"x": 1}})
    loaded = store.load()
    assert "secret" not in loaded
    assert "knowledge" not in loaded


def test_save_rejects_non_object(store):
    from openclaw_adapter.session_memory import SessionWriteError

    with pytest.raises(SessionWriteError):
        store.save(["not", "an", "object"])


# --- delete ----------------------------------------------------------------
def test_clear_removes_snapshot(store):
    store.save({"messages": [{"id": "1", "role": "user", "text": "hi"}]})
    store.clear()
    assert store.load() == empty_session()


def test_clear_missing_is_idempotent(store):
    store.clear()  # no file yet
    store.clear()
    assert store.load() == empty_session()


# --- corrupt / bad shape fail soft ----------------------------------------
def test_corrupt_json_returns_empty(store, tmp_path):
    store.save({"messages": [{"id": "1", "role": "user", "text": "hi"}]})
    store._path.write_text("{ this is not json ", encoding="utf-8")
    assert store.load() == empty_session()  # no crash


def test_non_object_json_returns_empty(store):
    store.save({"messages": []})
    store._path.write_text("[1, 2, 3]", encoding="utf-8")
    assert store.load() == empty_session()


# --- retention: expiry -----------------------------------------------------
def test_expired_snapshot_loads_empty(store):
    store.save({"messages": [{"id": "1", "role": "user", "text": "old"}]})
    data = json.loads(store._path.read_text(encoding="utf-8"))
    data["updated_at"] = time.time() - (8 * 24 * 60 * 60)  # 8 days ago
    store._path.write_text(json.dumps(data), encoding="utf-8")
    assert store.load() == empty_session()


# --- retention: message count ---------------------------------------------
def test_retention_trims_to_max_messages(store):
    msgs = [{"id": str(i), "role": "user", "text": f"m{i}"} for i in range(MAX_MESSAGES + 50)]
    stored = store.save({"messages": msgs})
    assert len(stored["messages"]) == MAX_MESSAGES
    # oldest dropped, newest kept
    assert stored["messages"][-1]["id"] == str(MAX_MESSAGES + 49)
    assert stored["messages"][0]["id"] == str(50)


# --- retention: byte budget (hard limit) ----------------------------------
def test_retention_single_huge_message_dropped_to_empty(store, monkeypatch):
    # MAX_BYTES is a hard limit: a single message that alone exceeds the budget
    # must be dropped, leaving an empty list (not an over-budget snapshot).
    monkeypatch.setattr("openclaw_adapter.session_memory.MAX_BYTES", 200)
    big = "x" * 500  # one message serialized ≈ 550 bytes > 200
    stored = store.save({"messages": [{"id": "1", "role": "assistant", "text": big}]})
    assert stored["messages"] == []
    size = len(json.dumps(stored, ensure_ascii=False).encode("utf-8"))
    assert size <= 200


def test_retention_trims_to_byte_budget(store, monkeypatch):
    # Shrink the budget so a few fat messages exceed it without building 5MB.
    monkeypatch.setattr("openclaw_adapter.session_memory.MAX_BYTES", 2000)
    big = "x" * 800
    msgs = [{"id": str(i), "role": "assistant", "text": big} for i in range(10)]
    stored = store.save({"messages": msgs})
    size = len(json.dumps(stored, ensure_ascii=False).encode("utf-8"))
    assert size <= 2000
    assert len(stored["messages"]) < 10  # oldest dropped to fit
    assert stored["messages"][-1]["id"] == "9"  # newest survives


# --- retention: scalar field cap ------------------------------------------
def test_scalar_bloat_does_not_exceed_byte_budget(store, monkeypatch):
    # Reviewer finding: a huge active_job_id bypassed MAX_BYTES because _trim
    # only trimmed messages. Now _normalize caps each scalar at MAX_SCALAR_LEN
    # (512) so the saved snapshot is bounded well under the real 5MB limit.
    # Use MAX_BYTES=5000: the 10000-char job_id (>5000 bytes raw) is capped to
    # 512 chars, making the total snapshot ~667 bytes, safely under 5000.
    monkeypatch.setattr("openclaw_adapter.session_memory.MAX_BYTES", 5000)
    huge_job_id = "j" * 10000
    stored = store.save({"messages": [], "active_job_id": huge_job_id})
    size = len(json.dumps(stored, ensure_ascii=False).encode("utf-8"))
    assert size <= 5000


def test_huge_scalar_is_truncated_to_max_scalar_len(store):
    huge = "x" * (MAX_SCALAR_LEN + 100)
    stored = store.save({"messages": [], "active_job_id": huge})
    assert len(stored["active_job_id"]) == MAX_SCALAR_LEN


def test_non_string_scalar_is_set_to_none(store):
    stored = store.save({"messages": [], "mode": {"attack": "vector"}})
    assert stored["mode"] is None


# --- delete: OSError must not return false success -------------------------
def test_clear_oserror_is_reraised(store):
    from unittest.mock import patch

    store.save({"messages": []})

    def boom(self, missing_ok=False):
        raise OSError("Permission denied")

    with patch.object(type(store._path), "unlink", boom):
        with pytest.raises(OSError):
            store.clear()


def test_bridge_clear_failure_returns_error(tmp_path):
    from unittest.mock import patch

    b = _bridge(tmp_path)
    b.save_session({"messages": [{"id": "1", "role": "user", "text": "hi"}]})

    def boom(self, missing_ok=False):
        raise OSError("Permission denied")

    with patch.object(type(b._sessions()._path), "unlink", boom):
        res = b.clear_session()
    assert res["status"] == "error"


# --- bridge wiring ---------------------------------------------------------
def _bridge(tmp_path):
    settings = SimpleNamespace(openclaw_web_memory_dir=str(tmp_path / "mem"))
    return CommandBridge(settings=settings)


def test_bridge_save_load_clear(tmp_path):
    b = _bridge(tmp_path)
    assert b.load_session()["session"]["messages"] == []
    save = b.save_session({"messages": [{"id": "1", "role": "user", "text": "hi"}], "mode": "chat"})
    assert save["status"] == "ok"
    loaded = b.load_session()
    assert loaded["status"] == "ok"
    assert loaded["session"]["messages"][0]["text"] == "hi"
    assert b.clear_session()["status"] == "ok"
    assert b.load_session()["session"]["messages"] == []


def test_bridge_save_non_object_is_error(tmp_path):
    b = _bridge(tmp_path)
    res = b.save_session([1, 2, 3])
    assert res["status"] == "error"


def test_bridge_load_corrupt_is_soft(tmp_path):
    b = _bridge(tmp_path)
    b.save_session({"messages": [{"id": "1", "role": "user", "text": "hi"}]})
    b._sessions()._path.write_text("garbage{", encoding="utf-8")
    res = b.load_session()
    assert res["status"] == "ok"
    assert res["session"]["messages"] == []


# --- HTTP routes -----------------------------------------------------------
class _FakeBridge:
    def __init__(self) -> None:
        self.saved = None
        self.cleared = False

    def load_session(self):
        return {"status": "ok", "session": {"messages": [{"id": "1"}]}}

    def save_session(self, snapshot):
        self.saved = snapshot
        return {"status": "ok", "updated_at": 123.0}

    def clear_session(self):
        self.cleared = True
        return {"status": "ok"}


def _make_handler(fake, method, path, body=b""):
    handler_cls = srv._build_handler(fake, lan_enabled=False)
    h = handler_cls.__new__(handler_cls)
    h.headers = {"Content-Length": str(len(body))}
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    h.request_version = "HTTP/1.1"
    h.protocol_version = "HTTP/1.0"
    h.requestline = f"{method} {path} HTTP/1.1"
    h.responses = BaseHTTPRequestHandler.responses
    h.client_address = ("127.0.0.1", 1)
    h.path = path
    return h


def _read_json(h):
    return json.loads(h.wfile.getvalue().split(b"\r\n\r\n", 1)[1].decode("utf-8"))


def test_route_get_session(tmp_path):
    fake = _FakeBridge()
    h = _make_handler(fake, "GET", "/api/command/session")
    h.do_GET()
    out = _read_json(h)
    assert out["session"]["messages"] == [{"id": "1"}]


def test_route_post_session(tmp_path):
    fake = _FakeBridge()
    body = json.dumps({"messages": [{"id": "9"}], "mode": "chat"}).encode("utf-8")
    h = _make_handler(fake, "POST", "/api/command/session", body)
    h.do_POST()
    out = _read_json(h)
    assert out["status"] == "ok"
    assert fake.saved["messages"] == [{"id": "9"}]


def test_route_delete_session(tmp_path):
    fake = _FakeBridge()
    h = _make_handler(fake, "DELETE", "/api/command/session")
    h.do_DELETE()
    out = _read_json(h)
    assert out["status"] == "ok"
    assert fake.cleared is True
