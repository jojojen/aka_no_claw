"""Issue #30 — Local command bridge for aka_no_claw_web.

These tests assert the bridge's routing contract: chat goes to the selected
backend (local Ollama vs cloud big-pickle), Translation reuses the existing
``/zh`` handler, deep product research reuses ``/research``, while image
translation and seller snapshot return structured ``unsupported`` (allowed for
MVP). Streaming emits the documented event sequence. The real handlers/models
are stubbed so the routing — not the network — is what's under test.
"""
from __future__ import annotations

import time

import pytest

from openclaw_adapter.command_bridge import CommandBridge
from openclaw_adapter.command_bridge_models import (
    CHAT_BACKEND_CLOUD_PICKLE,
    CHAT_BACKEND_LOCAL,
    MODE_CHAT,
    MODE_INVESTMENT,
    MODE_TRANSLATION,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNSUPPORTED,
    SUBMODE_DEEP_PRODUCT_RESEARCH,
    SUBMODE_IMAGE_TRANSLATION,
    SUBMODE_SELLER_REPUTATION_SNAPSHOT,
    SUBMODE_TEXT_TRANSLATION,
    RequestValidationError,
    parse_request,
)


class _FakeRegistered:
    def __init__(self, fn):
        self.handler = fn


@pytest.fixture
def bridge(monkeypatch):
    b = CommandBridge(settings=object())
    calls: dict[str, list] = {"/zh": [], "/research": []}

    def _zh(remainder, chat_id):
        calls["/zh"].append((remainder, chat_id))
        return f"[zh]{remainder}"

    def _research(remainder, chat_id):
        calls["/research"].append((remainder, chat_id))
        return (f"[research]{remainder}", {"inline_keyboard": []})  # tuple form

    monkeypatch.setattr(
        b, "_handlers",
        lambda: {"/zh": _FakeRegistered(_zh), "/research": _FakeRegistered(_research)},
    )
    b._calls = calls  # type: ignore[attr-defined]
    return b


# --- parse_request --------------------------------------------------------
def test_parse_request_defaults():
    req = parse_request({"mode": "chat", "input": "hi"})
    assert req.mode == MODE_CHAT
    assert req.chat_backend == CHAT_BACKEND_LOCAL
    assert req.source == "aka_no_claw_web"
    assert req.attachments == ()


def test_parse_request_rejects_bad_mode():
    with pytest.raises(RequestValidationError):
        parse_request({"mode": "nope"})


def test_parse_request_rejects_bad_backend():
    with pytest.raises(RequestValidationError):
        parse_request({"mode": "chat", "chat_backend": "gpt5"})


def test_parse_request_image_attachment():
    req = parse_request({
        "mode": "translation", "submode": "image_translation", "input": "",
        "attachments": [{"type": "image", "filename": "a.jpg", "content_type": "image/jpeg"}],
    })
    assert req.has_image_attachment


# --- translation routing --------------------------------------------------
def test_translation_text_routes_to_zh(bridge):
    req = parse_request({"mode": "translation", "submode": "text_translation",
                         "input": "これはペンです"})
    resp = bridge.handle(req)
    assert resp.status == STATUS_OK
    assert resp.message == "[zh]これはペンです"
    assert resp.submode == SUBMODE_TEXT_TRANSLATION
    assert bridge._calls["/zh"] == [("これはペンです", "web-bridge")]


def test_translation_image_is_unsupported(bridge):
    req = parse_request({
        "mode": "translation", "submode": "image_translation", "input": "",
        "attachments": [{"type": "image", "filename": "a.jpg", "content_type": "image/jpeg"}],
    })
    resp = bridge.handle(req)
    assert resp.status == STATUS_UNSUPPORTED
    assert resp.submode == SUBMODE_IMAGE_TRANSLATION
    assert bridge._calls["/zh"] == []  # never touched the translator


def test_translation_empty_text_is_error(bridge):
    req = parse_request({"mode": "translation", "submode": "text_translation", "input": "  "})
    resp = bridge.handle(req)
    assert resp.status == STATUS_ERROR


# --- investment routing ---------------------------------------------------
def test_investment_research_routes_to_research(bridge):
    req = parse_request({"mode": "investment", "submode": "deep_product_research",
                         "input": "https://jp.mercari.com/item/x"})
    resp = bridge.handle(req)
    assert resp.status == STATUS_OK
    assert resp.message == "[research]https://jp.mercari.com/item/x"
    assert resp.submode == SUBMODE_DEEP_PRODUCT_RESEARCH
    assert bridge._calls["/research"] == [("https://jp.mercari.com/item/x", "web-bridge")]


def test_investment_seller_snapshot_is_unsupported(bridge):
    req = parse_request({"mode": "investment", "submode": "seller_reputation_snapshot",
                         "input": "seller-1"})
    resp = bridge.handle(req)
    assert resp.status == STATUS_UNSUPPORTED
    assert resp.submode == SUBMODE_SELLER_REPUTATION_SNAPSHOT
    assert bridge._calls["/research"] == []


def test_investment_no_submode_defaults_to_research(bridge):
    req = parse_request({"mode": "investment", "input": "寶可夢 BOX"})
    resp = bridge.handle(req)
    assert resp.status == STATUS_OK
    assert resp.message == "[research]寶可夢 BOX"


# --- chat routing ---------------------------------------------------------
def test_chat_local_uses_ollama(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_ollama_generate_blocking", lambda prompt: f"local:{prompt}")
    req = parse_request({"mode": "chat", "input": "hello", "chat_backend": "local"})
    resp = bridge.handle(req)
    assert resp.status == STATUS_OK
    assert resp.message == "local:hello"


def test_chat_cloud_uses_cloud_client(bridge, monkeypatch):
    class _Client:
        def generate(self, prompt, *, temperature=0.0):
            return f"cloud:{prompt}"

    monkeypatch.setattr(bridge, "_build_cloud_chat_client", lambda: _Client())
    req = parse_request({"mode": "chat", "input": "hello", "chat_backend": "cloud_pickle"})
    resp = bridge.handle(req)
    assert resp.status == STATUS_OK
    assert resp.message == "cloud:hello"


def test_chat_cloud_unavailable_is_error(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_build_cloud_chat_client", lambda: None)
    req = parse_request({"mode": "chat", "input": "hello", "chat_backend": "cloud_pickle"})
    resp = bridge.handle(req)
    assert resp.status == STATUS_ERROR


def test_handler_exception_becomes_structured_error(bridge, monkeypatch):
    def _boom(remainder, chat_id):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(
        bridge, "_handlers", lambda: {"/zh": _FakeRegistered(_boom)},
    )
    req = parse_request({"mode": "translation", "submode": "text_translation", "input": "x"})
    resp = bridge.handle(req)
    assert resp.status == STATUS_ERROR
    assert "kaboom" in resp.message


# --- streaming ------------------------------------------------------------
def test_stream_chat_local_emits_start_delta_done(bridge, monkeypatch):
    def _fake_stream(prompt):
        from openclaw_adapter.command_bridge_models import stream_delta, stream_done
        yield stream_delta("par")
        yield stream_delta("tial")
        yield stream_done("partial")

    monkeypatch.setattr(bridge, "_stream_ollama_chat", _fake_stream)
    req = parse_request({"mode": "chat", "input": "hi", "chat_backend": "local"})
    events = list(bridge.stream(req, "rid-1"))
    assert events[0] == {"type": "start", "request_id": "rid-1"}
    assert events[1] == {"type": "delta", "text": "par"}
    assert events[-1] == {"type": "done", "message": "partial"}


def test_stream_non_chat_runs_blocking_then_done(bridge):
    req = parse_request({"mode": "translation", "submode": "text_translation", "input": "abc"})
    events = list(bridge.stream(req, "rid-2"))
    assert events[0]["type"] == "start"
    assert events[-1] == {"type": "done", "message": "[zh]abc"}


def test_stream_non_chat_error_emits_error_event(bridge):
    req = parse_request({"mode": "translation", "submode": "image_translation", "input": "",
                         "attachments": [{"type": "image"}]})
    # image translation -> unsupported (not error), so done carries the message
    events = list(bridge.stream(req, "rid-3"))
    assert events[-1]["type"] == "done"


# --- async job + poll (long research) -------------------------------------
def _wait_job(bridge, job_id, want):
    snap = bridge.poll_job(job_id)
    for _ in range(100):
        snap = bridge.poll_job(job_id)
        if snap["job_status"] == want:
            break
        time.sleep(0.02)
    return snap


def test_async_research_accumulates_progress_then_done(bridge, monkeypatch):
    def _fake_run_raw(command, remainder, chat_id="web-bridge"):
        assert command == "/research"
        bridge._jobs.append_progress(chat_id, "⏳ 開始")
        bridge._jobs.append_progress(chat_id, "✅ 抓到商品頁")
        return (f"[research]{remainder}", {"inline_keyboard": []})

    monkeypatch.setattr(bridge, "_run_command_raw", _fake_run_raw)
    req = parse_request({"mode": "investment", "submode": "deep_product_research",
                         "input": "寶可夢 BOX"})
    start = bridge.start_async(req)
    assert start["status"] == "accepted"
    snap = _wait_job(bridge, start["job_id"], "done")
    assert snap["job_status"] == "done"
    assert snap["message"] == "[research]寶可夢 BOX"
    assert snap["progress"] == ["⏳ 開始", "✅ 抓到商品頁"]


def test_async_research_handler_error_becomes_error_job(bridge, monkeypatch):
    def _boom(command, remainder, chat_id="web-bridge"):
        raise RuntimeError("scrape exploded")

    monkeypatch.setattr(bridge, "_run_command_raw", _boom)
    req = parse_request({"mode": "investment", "input": "X"})
    snap = _wait_job(bridge, bridge.start_async(req)["job_id"], "error")
    assert snap["job_status"] == "error"
    assert "scrape exploded" in (snap["error"] or "")


# --- research follow-up buttons (龍蝦 inline_keyboard → web actions) -------
def test_research_actions_surface_in_poll(bridge, monkeypatch):
    markup = {"inline_keyboard": [
        [{"text": "摘要", "callback_data": "rs:tok:summary"},
         {"text": "看市價", "callback_data": "rs:tok:price"}],
        [{"text": "看賣家", "callback_data": "rs:tok:seller"}],
    ]}
    monkeypatch.setattr(bridge, "_run_command_raw",
                        lambda *a, **k: ("[research]X", markup))
    job_id = bridge.start_async(parse_request({"mode": "investment", "input": "X"}))["job_id"]
    snap = _wait_job(bridge, job_id, "done")
    assert [a["label"] for a in snap["actions"]] == ["摘要", "看市價", "看賣家"]
    assert snap["actions"][0]["callback_data"] == "rs:tok:summary"


def test_run_action_switches_view(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_run_command_raw",
                        lambda *a, **k: ("[research]X", {"inline_keyboard": []}))
    job_id = bridge.start_async(parse_request({"mode": "investment", "input": "X"}))["job_id"]
    _wait_job(bridge, job_id, "done")

    def _rs(payload, original_text, chat_id):
        token, _, view = payload.partition(":")
        return ("已切換研究視圖", f"detail:{view}:{chat_id}",
                {"inline_keyboard": [[{"text": "摘要", "callback_data": f"rs:{token}:summary"}]]})

    monkeypatch.setattr(bridge, "_callbacks", lambda: {"rs": _rs})
    res = bridge.run_action(job_id, "rs:tok:price")
    assert res["status"] == STATUS_OK
    assert res["message"] == f"detail:price:{job_id}"
    assert res["actions"][0]["label"] == "摘要"


def test_run_action_unknown_job_is_error(bridge):
    res = bridge.run_action("nope", "rs:tok:price")
    assert res["status"] == STATUS_ERROR


def test_async_rejects_non_research():
    b = CommandBridge(settings=object())
    res = b.start_async(parse_request({"mode": "chat", "input": "hi"}))
    assert res["status"] == STATUS_ERROR


def test_poll_unknown_job_is_not_found():
    b = CommandBridge(settings=object())
    snap = b.poll_job("does-not-exist")
    assert snap["not_found"] is True
    assert snap["job_status"] == "error"


# --- client allowlist -----------------------------------------------------
def test_loopback_allowed_lan_blocked_by_default():
    from openclaw_adapter.command_bridge_server import _is_allowed_client

    assert _is_allowed_client("127.0.0.1", lan_enabled=False)
    assert _is_allowed_client("100.115.92.1", lan_enabled=False)  # mesh CGNAT
    assert not _is_allowed_client("192.168.1.50", lan_enabled=False)
    assert _is_allowed_client("192.168.1.50", lan_enabled=True)
    assert not _is_allowed_client("8.8.8.8", lan_enabled=True)
