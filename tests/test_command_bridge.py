"""Issue #30 — Local command bridge for aka_no_claw_web.

These tests assert the bridge's routing contract: chat goes to the selected
backend (local Ollama vs cloud big-pickle), Translation reuses the existing
``/zh`` handler (text) and the shared OCR+繁中翻譯 pipeline (image, #43), deep
product research reuses ``/research``, while seller snapshot returns structured
``unsupported`` (allowed for MVP). Streaming emits the documented event
sequence. The real handlers/models are stubbed so the routing — not the
network — is what's under test.
"""
from __future__ import annotations

import io
import threading
import time
from types import SimpleNamespace

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


def test_parse_request_decodes_image_base64():
    import base64
    raw = b"\x89PNG\r\n hello bytes"
    req = parse_request({
        "mode": "translation", "submode": "image_translation", "input": "",
        "attachments": [{
            "type": "image", "filename": "a.png", "content_type": "image/png",
            "data_base64": base64.b64encode(raw).decode("ascii"),
        }],
    })
    assert req.attachments[0].data == raw


def test_parse_request_rejects_bad_base64():
    with pytest.raises(RequestValidationError):
        parse_request({
            "mode": "translation", "submode": "image_translation", "input": "",
            "attachments": [{"type": "image", "data_base64": "not!!base64!!"}],
        })


# --- translation routing --------------------------------------------------
def test_translation_text_routes_to_zh(bridge):
    req = parse_request({"mode": "translation", "submode": "text_translation",
                         "input": "これはペンです"})
    resp = bridge.handle(req)
    assert resp.status == STATUS_OK
    assert resp.message == "[zh]これはペンです"
    assert resp.submode == SUBMODE_TEXT_TRANSLATION
    assert bridge._calls["/zh"] == [("これはペンです", "web-bridge")]


def test_translation_image_without_bytes_is_error(bridge):
    """Image submode but no actual image bytes → readable structured error."""
    req = parse_request({
        "mode": "translation", "submode": "image_translation", "input": "",
        "attachments": [{"type": "image", "filename": "a.jpg", "content_type": "image/jpeg"}],
    })
    resp = bridge.handle(req)
    assert resp.status == STATUS_ERROR
    assert resp.submode == SUBMODE_IMAGE_TRANSLATION
    assert "圖片" in resp.message
    assert bridge._calls["/zh"] == []  # never touched the text translator


def _png_base64() -> str:
    import base64
    # 1x1 transparent PNG.
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return base64.b64encode(png).decode("ascii")


def test_translation_image_success_routes_to_pipeline(bridge, monkeypatch):
    from openclaw_adapter.image_translate import ImageTranslateResult

    seen: dict[str, object] = {}

    def _fake_renderer(image_path, caption=None):
        from pathlib import Path
        p = Path(image_path)
        seen["existed_during_render"] = p.exists()
        seen["bytes"] = p.read_bytes()
        seen["caption"] = caption
        seen["path"] = p
        return ImageTranslateResult(
            ok=True, source_language="日文", ocr_text="ペン", translation="筆", message="",
        )

    monkeypatch.setattr(bridge, "_image_translate_renderer", lambda: _fake_renderer)
    req = parse_request({
        "mode": "translation", "submode": "image_translation", "input": "",
        "attachments": [{"type": "image", "filename": "a.png",
                         "content_type": "image/png", "data_base64": _png_base64()}],
    })
    resp = bridge.handle(req)
    assert resp.status == STATUS_OK
    assert resp.submode == SUBMODE_IMAGE_TRANSLATION
    assert "筆" in resp.message
    assert "日文" in resp.message
    assert "ペン" in resp.message  # OCR原文 included
    # The pipeline saw the real uploaded bytes on disk...
    assert seen["existed_during_render"] is True
    assert seen["bytes"]
    # ...and the temp file is cleaned up afterwards.
    assert not seen["path"].exists()


def test_translation_image_failure_surfaces_message(bridge, monkeypatch):
    from openclaw_adapter.image_translate import ImageTranslateResult

    def _fake_renderer(image_path, caption=None):
        return ImageTranslateResult(
            ok=False, source_language="", ocr_text="", translation="",
            message="這張圖片裡沒有辨識到任何文字。",
        )

    monkeypatch.setattr(bridge, "_image_translate_renderer", lambda: _fake_renderer)
    req = parse_request({
        "mode": "translation", "submode": "image_translation", "input": "",
        "attachments": [{"type": "image", "filename": "a.png",
                         "content_type": "image/png", "data_base64": _png_base64()}],
    })
    resp = bridge.handle(req)
    assert resp.status == STATUS_ERROR
    assert "沒有辨識到" in resp.message


def test_translation_image_bad_type_is_error(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_image_translate_renderer", lambda: (_ for _ in ()).throw(AssertionError))
    req = parse_request({
        "mode": "translation", "submode": "image_translation", "input": "",
        "attachments": [{"type": "image", "filename": "a.txt",
                         "content_type": "text/plain", "data_base64": _png_base64()}],
    })
    resp = bridge.handle(req)
    assert resp.status == STATUS_ERROR
    assert "不支援的檔案類型" in resp.message


def test_translation_image_pipeline_unavailable_is_unsupported(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_image_translate_renderer", lambda: None)
    req = parse_request({
        "mode": "translation", "submode": "image_translation", "input": "",
        "attachments": [{"type": "image", "filename": "a.png",
                         "content_type": "image/png", "data_base64": _png_base64()}],
    })
    resp = bridge.handle(req)
    assert resp.status == STATUS_UNSUPPORTED
    assert resp.submode == SUBMODE_IMAGE_TRANSLATION


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


def test_restart_all_schedules_detached_script(bridge, monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(
        "openclaw_adapter.command_bridge.trigger_restart_all",
        lambda *, settings, source: calls.append(source) or "/tmp/restart.sh",
    )

    resp = bridge.restart_all()

    assert resp["status"] == STATUS_OK
    assert "重啟龍蝦" in resp["message"]
    assert calls == ["web"]


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
    # image submode with no bytes -> structured error, surfaced as an error event.
    events = list(bridge.stream(req, "rid-3"))
    assert events[-1]["type"] == "error"


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


# --- streaming cancellation on client disconnect (#30 review gap) ---------
def test_stream_cloud_disconnect_aborts_worker(bridge, monkeypatch):
    """When the phone drops mid-stream the generator is closed; the cloud model
    worker must be aborted, not left running until its 180s timeout."""
    monkeypatch.setattr(
        "openclaw_adapter.command_bridge._HEARTBEAT_SECONDS", 0.02, raising=False
    )

    class _BlockingClient:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.aborted = threading.Event()

        def generate(self, prompt, *, temperature=0.0):
            self.started.set()
            if self.aborted.wait(timeout=5.0):
                raise RuntimeError("aborted by disconnect")
            return "should never finish"

        def abort(self) -> None:
            self.aborted.set()

    client = _BlockingClient()
    monkeypatch.setattr(bridge, "_build_cloud_chat_client", lambda: client)

    gen = bridge._stream_cloud_chat("hello")
    first = next(gen)  # worker now running, blocked; we get a heartbeat
    assert first["type"] == "heartbeat"
    assert client.started.wait(1.0)
    gen.close()  # simulate client disconnect -> GeneratorExit
    assert client.aborted.wait(1.0), "cloud worker was not aborted on disconnect"


def test_stream_ollama_disconnect_closes_response(monkeypatch):
    """Closing the local stream generator must close the upstream HTTP response
    so the Ollama read aborts instead of draining the whole reply."""
    settings = SimpleNamespace(
        openclaw_local_text_endpoint="http://localhost:11434",
        openclaw_local_text_model="qwen3:14b",
        openclaw_local_text_timeout_seconds=30,
    )
    b = CommandBridge(settings=settings)

    class _FakeResp:
        def __init__(self) -> None:
            self.closed = False
            self._lines = [b'{"response":"hi"}\n', b'{"response":" there"}\n']
            self._i = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

        def __iter__(self):
            return self

        def __next__(self):
            if self._i >= len(self._lines):
                raise StopIteration
            line = self._lines[self._i]
            self._i += 1
            return line

        def close(self):
            self.closed = True

    resp = _FakeResp()
    monkeypatch.setattr(
        "openclaw_adapter.command_bridge.urlopen", lambda *a, **k: resp, raising=True
    )
    gen = b._stream_ollama_chat("hi")
    first = next(gen)
    assert first == {"type": "delta", "text": "hi"}
    gen.close()  # client disconnect mid-stream
    assert resp.closed, "upstream Ollama response was not closed on disconnect"


def test_handle_stream_closes_generator_on_client_disconnect(monkeypatch):
    """The server's _handle_stream must close the bridge generator in a finally
    block when the socket write fails, so GeneratorExit fires and any in-flight
    worker is cancelled — not left dangling for the GC."""
    from http.server import BaseHTTPRequestHandler

    from openclaw_adapter import command_bridge_server as srv

    closed = {"v": False}

    class _Gen:
        def __iter__(self):
            return self

        def __next__(self):
            return {"type": "heartbeat"}

        def close(self):
            closed["v"] = True

    class _FakeBridge:
        def stream(self, req, request_id):
            return _Gen()

    class _FakeWFile:
        def __init__(self) -> None:
            self.writes = 0

        def write(self, _b):
            self.writes += 1
            if self.writes >= 2:  # headers flush ok, first data line drops
                raise BrokenPipeError("client gone")

        def flush(self):
            pass

    handler_cls = srv._build_handler(_FakeBridge(), lan_enabled=False)
    h = handler_cls.__new__(handler_cls)
    body = b'{"mode":"chat","input":"hi"}'
    h.headers = {"Content-Length": str(len(body))}
    h.rfile = io.BytesIO(body)
    h.wfile = _FakeWFile()
    h.request_version = "HTTP/1.1"
    h.protocol_version = "HTTP/1.0"
    h.requestline = "POST /api/command/stream HTTP/1.1"
    h.responses = BaseHTTPRequestHandler.responses
    h.client_address = ("127.0.0.1", 12345)

    h._handle_stream()
    assert closed["v"], "generator was not closed on client disconnect"


# --- 生活 mode: music control surface (aka_no_claw_web#3 / #4) -------------
def test_music_command_runs_music_handler(bridge, monkeypatch):
    markup = {"inline_keyboard": [[{"text": "🔇 靜音", "callback_data": "music:mute"}]]}
    monkeypatch.setattr(bridge, "_run_command_raw",
                        lambda command, text, **k: (f"[music]{command}:{text}", markup))
    res = bridge.run_music_command("")
    assert res["status"] == STATUS_OK
    assert res["message"] == "[music]/music:"
    assert res["actions"][0]["callback_data"] == "music:mute"


def test_now_playing_returns_song_name(bridge, monkeypatch):
    from openclaw_adapter import music_command
    monkeypatch.setattr(music_command, "now_playing", lambda settings: "蒼のワルツ")
    res = bridge.now_playing()
    assert res["status"] == STATUS_OK
    assert res["name"] == "蒼のワルツ"


def test_now_playing_null_when_idle(bridge, monkeypatch):
    from openclaw_adapter import music_command
    monkeypatch.setattr(music_command, "now_playing", lambda settings: None)
    res = bridge.now_playing()
    assert res["status"] == STATUS_OK
    assert res["name"] is None


def test_music_action_volume_routes_to_music_callback(bridge, monkeypatch):
    def _music_cb(payload, original_text, chat_id):
        assert payload == "louder"
        return ("目前音量：80/100。", None, None)  # toast only

    monkeypatch.setattr(bridge, "_callbacks", lambda: {"music": _music_cb})
    res = bridge.run_music_action("music:louder")
    assert res["status"] == STATUS_OK
    assert res["message"] == "目前音量：80/100。"
    assert res["actions"] == []


def test_music_action_browse_returns_rerender_text_and_buttons(bridge, monkeypatch):
    markup = {"inline_keyboard": [[{"text": "🎵 song", "callback_data": "music:sd:tok"}]]}

    def _music_cb(payload, original_text, chat_id):
        return (None, "📁 資料夾內容", markup)  # new_text -> rerender

    monkeypatch.setattr(bridge, "_callbacks", lambda: {"music": _music_cb})
    res = bridge.run_music_action("music:ls:root:0")
    assert res["message"] == "📁 資料夾內容"
    assert res["actions"][0]["callback_data"] == "music:sd:tok"


def test_music_action_list_pg_routes_to_view(bridge, monkeypatch):
    markup = {"inline_keyboard": [[{"text": "▶️ fav", "callback_data": "music:pf:1"}]]}
    monkeypatch.setattr(bridge, "_views",
                        lambda: {"mb": lambda page, mode: ("最愛清單", markup, page)})
    res = bridge.run_music_action("pg:mb:0:r")
    assert res["status"] == STATUS_OK
    assert res["message"] == "最愛清單"
    assert res["actions"][0]["callback_data"] == "music:pf:1"


def test_music_action_list_del_then_rerenders_edit(bridge, monkeypatch):
    deleted: list[str] = []
    monkeypatch.setattr(bridge, "_deleters",
                        lambda: {"mb": (lambda i: deleted.append(i) or True, "最愛")})
    monkeypatch.setattr(bridge, "_views",
                        lambda: {"mb": lambda page, mode: (f"清單[{mode}]", None, page)})
    res = bridge.run_music_action("del:mb:abc")
    assert deleted == ["abc"]
    assert res["status"] == STATUS_OK
    assert "清單[" in res["message"]


def test_music_action_close_clears(bridge):
    res = bridge.run_music_action("close:mb")
    assert res["status"] == STATUS_OK
    assert res["actions"] == []


def test_music_action_unknown_prefix_is_error(bridge):
    res = bridge.run_music_action("bogus:thing")
    assert res["status"] == STATUS_ERROR


def test_ir_command_normalizes_full_slash_command(monkeypatch):
    b = CommandBridge(settings=object())
    seen: dict[str, str] = {}

    def _run(command, remainder):
        seen["command"] = command
        seen["remainder"] = remainder
        return ("IR sent", {"inline_keyboard": []})

    monkeypatch.setattr(b, "_run_command_raw", _run)
    res = b.run_ir_command("/ir send ceiling_light power")
    assert seen == {"command": "/ir", "remainder": "send ceiling_light power"}
    assert res["status"] == STATUS_OK
    assert res["message"] == "IR sent"


def test_server_music_route_dispatches_callback(monkeypatch):
    """POST /api/command/music with callback_data → run_music_action; with
    input → run_music_command; empty body → music menu."""
    from http.server import BaseHTTPRequestHandler

    from openclaw_adapter import command_bridge_server as srv

    seen: dict[str, object] = {}

    class _FakeBridge:
        def run_music_action(self, cb):
            seen["action"] = cb
            return {"status": STATUS_OK, "message": "act", "actions": []}

        def run_music_command(self, text):
            seen["command"] = text
            return {"status": STATUS_OK, "message": "cmd", "actions": []}

    def _invoke(body: bytes) -> dict:
        handler_cls = srv._build_handler(_FakeBridge(), lan_enabled=False)
        h = handler_cls.__new__(handler_cls)
        h.headers = {"Content-Length": str(len(body))}
        h.rfile = io.BytesIO(body)
        h.wfile = io.BytesIO()
        h.request_version = "HTTP/1.1"
        h.protocol_version = "HTTP/1.0"
        h.requestline = "POST /api/command/music HTTP/1.1"
        h.responses = BaseHTTPRequestHandler.responses
        h.client_address = ("127.0.0.1", 1)
        h._handle_music()
        raw = h.wfile.getvalue().split(b"\r\n\r\n", 1)[1]
        import json as _json
        return _json.loads(raw.decode("utf-8"))

    out = _invoke(b'{"callback_data":"music:louder"}')
    assert seen["action"] == "music:louder"
    assert out["message"] == "act"

    seen.clear()
    out = _invoke(b'{"input":"\\u30ed\\u30c3\\u30af"}')
    assert seen["command"] == "ロック"
    assert out["message"] == "cmd"

    seen.clear()
    out = _invoke(b"{}")
    assert seen["command"] == ""


def test_server_ir_route_dispatches_command_and_callback(monkeypatch):
    from http.server import BaseHTTPRequestHandler

    from openclaw_adapter import command_bridge_server as srv

    seen: dict[str, object] = {}

    class _FakeBridge:
        def run_ir_action(self, cb):
            seen["action"] = cb
            return {"status": STATUS_OK, "message": "act", "actions": []}

        def run_ir_command(self, text):
            seen["command"] = text
            return {"status": STATUS_OK, "message": "cmd", "actions": []}

    def _invoke(body: bytes) -> dict:
        handler_cls = srv._build_handler(_FakeBridge(), lan_enabled=False)
        h = handler_cls.__new__(handler_cls)
        h.headers = {"Content-Length": str(len(body))}
        h.rfile = io.BytesIO(body)
        h.wfile = io.BytesIO()
        h.request_version = "HTTP/1.1"
        h.protocol_version = "HTTP/1.0"
        h.requestline = "POST /api/command/ir HTTP/1.1"
        h.responses = BaseHTTPRequestHandler.responses
        h.client_address = ("127.0.0.1", 1)
        h._handle_ir()
        raw = h.wfile.getvalue().split(b"\r\n\r\n", 1)[1]
        import json as _json
        return _json.loads(raw.decode("utf-8"))

    out = _invoke(b'{"callback_data":"ir:send:ceiling_light:power"}')
    assert seen["action"] == "ir:send:ceiling_light:power"
    assert out["message"] == "act"

    seen.clear()
    out = _invoke(b'{"input":"/ir send ceiling_light power"}')
    assert seen["command"] == "/ir send ceiling_light power"
    assert out["message"] == "cmd"


# --- client allowlist -----------------------------------------------------
def test_loopback_allowed_lan_blocked_by_default():
    from openclaw_adapter.command_bridge_server import _is_allowed_client

    assert _is_allowed_client("127.0.0.1", lan_enabled=False)
    assert _is_allowed_client("100.115.92.1", lan_enabled=False)  # mesh CGNAT
    assert not _is_allowed_client("192.168.1.50", lan_enabled=False)
    assert _is_allowed_client("192.168.1.50", lan_enabled=True)
    assert not _is_allowed_client("8.8.8.8", lan_enabled=True)
