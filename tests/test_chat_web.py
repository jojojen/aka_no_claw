from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from assistant_runtime import AssistantSettings
from openclaw_adapter import chat_web
from openclaw_adapter.chat_web import build_chat_router
from openclaw_adapter.toolset import build_tool_registry


def test_router_routes_zh_to_translate_handler(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_build_translate_handler(settings, *, target):
        assert target == "zh"

        def handler(remainder: str, chat_id: str) -> str:
            calls.append((remainder, chat_id))
            return f"翻譯[{remainder}]"

        return handler

    monkeypatch.setattr(chat_web, "build_translate_handler", fake_build_translate_handler)
    route = build_chat_router(AssistantSettings())

    assert route("/zh 測試") == "翻譯[測試]"
    assert calls == [("測試", "chat-web")]


def test_router_rejects_unsupported_commands(monkeypatch) -> None:
    monkeypatch.setattr(
        chat_web,
        "build_translate_handler",
        lambda settings, *, target: (lambda remainder, chat_id: "should-not-run"),
    )
    route = build_chat_router(AssistantSettings())

    assert "/zh" in route("/research foo")
    assert "/zh" in route("")
    assert "/zh" in route("hello world")


def _serve(router):
    server = ThreadingHTTPServer(("127.0.0.1", 0), chat_web._build_handler(router))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def test_chat_page_redirects_to_full_console_and_api_stays_available() -> None:
    server = _serve(lambda message: f"echo:{message}")
    host, port = server.server_address
    base = f"http://{host}:{port}"
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            opener.open(f"{base}/chat")
        except urllib.error.HTTPError as exc:
            assert exc.code == 307
            assert exc.headers["Location"] == "https://127.0.0.1:5173/"
        else:
            raise AssertionError("legacy chat page should redirect to the full console")

        req = urllib.request.Request(
            f"{base}/api/chat",
            data=json.dumps({"message": "/zh hi"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload == {"reply": "echo:/zh hi"}
    finally:
        server.shutdown()
        server.server_close()


def test_web_frontend_url_preserves_mesh_hostname_and_supports_ipv6() -> None:
    assert chat_web._web_frontend_url("jen-mac-mini.nord:8780") == "https://jen-mac-mini.nord:5173/"
    assert chat_web._web_frontend_url("[::1]:8780") == "https://[::1]:5173/"


def test_api_requires_message() -> None:
    server = _serve(lambda message: "unused")
    host, port = server.server_address
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}/api/chat",
            data=json.dumps({"message": "  "}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req)
            raised = False
        except urllib.error.HTTPError as exc:
            raised = True
            assert exc.code == 400
            assert "message is required" in json.loads(exc.read().decode("utf-8"))["error"]
        assert raised
    finally:
        server.shutdown()
        server.server_close()


def test_is_allowed_client_loopback_and_meshnet() -> None:
    assert chat_web._is_allowed_client("127.0.0.1") is True
    assert chat_web._is_allowed_client("::1") is True
    assert chat_web._is_allowed_client("::ffff:127.0.0.1") is True
    # NordVPN Meshnet / Tailscale CGNAT range.
    assert chat_web._is_allowed_client("100.121.169.134") is True
    assert chat_web._is_allowed_client("100.64.0.0") is True
    # Ordinary LAN and public are rejected.
    assert chat_web._is_allowed_client("192.168.11.34") is False
    assert chat_web._is_allowed_client("10.0.0.5") is False
    assert chat_web._is_allowed_client("8.8.8.8") is False
    assert chat_web._is_allowed_client("not-an-ip") is False


def test_chat_web_command_registered() -> None:
    registry = build_tool_registry(AssistantSettings())
    names = {tool.name for tool in registry.tools()}
    assert "assistant.chat-web" in names
