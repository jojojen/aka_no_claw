"""Local-only web chat test interface (issue #23).

Minimal vertical slice: a localhost HTTP server that lets a browser send a
message to the existing 龍蝦 command handlers and shows the reply. The first
version only routes ``/zh`` to the existing translate handler — exactly the
same code path the Telegram bot uses — so the two stay behaviourally in sync.

Intentionally tiny: stdlib ``http.server`` only, no new dependency, no
multi-turn session, no auth. Bound to 127.0.0.1 and additionally rejects any
non-loopback client as defence in depth.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import urlsplit

from assistant_runtime import AssistantSettings

from .telegram_bot import build_translate_handler

logger = logging.getLogger(__name__)

# Private-only access surface: the Mac loopback plus the CGNAT range used by
# mesh VPNs (NordVPN Meshnet / Tailscale, 100.64.0.0/10) so the user's own
# phone can reach the page over the encrypted mesh — but ordinary LAN
# (192.168.x) and the public internet stay blocked even when bound to 0.0.0.0.
_MESHNET_CGNAT = ipaddress.ip_network("100.64.0.0/10")

_CHAT_WEB_CHAT_ID = "chat-web"
_WEB_FRONTEND_PORT = 5173


def _is_allowed_client(client_host: str) -> bool:
    try:
        ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if ip.is_loopback:
        return True
    return ip.version == 4 and ip in _MESHNET_CGNAT


def _web_frontend_url(host_header: str | None) -> str:
    """Build the HTTPS URL for the full React console from an incoming Host.

    The legacy chat-web service remains on port 8780 for its private API, but
    its old one-command HTML page is no longer the user-facing console. Keep
    phone and mesh hostnames intact while sending page requests to the Vite
    frontend started alongside the command bridge on port 5173.
    """
    raw_host = (host_header or "localhost").strip()
    parsed = urlsplit(f"//{raw_host}")
    hostname = parsed.hostname or "localhost"
    if ":" in hostname:
        hostname = f"[{hostname}]"
    return f"https://{hostname}:{_WEB_FRONTEND_PORT}/"

# First version supports /zh only (issue #23 non-goals: no /research, /snsbuzz…).
_UNSUPPORTED_MESSAGE = (
    "這個本機聊天測試頁目前只支援 /zh。\n用法：/zh <要翻成繁體中文的文字>"
)


def build_chat_router(settings: AssistantSettings) -> Callable[[str], str]:
    """Return ``route(message) -> reply`` over the supported command set.

    Only ``/zh`` is wired for the first version; anything else returns a clear
    "not supported yet" message rather than guessing.
    """

    zh_handler = build_translate_handler(settings, target="zh")

    def route(message: str) -> str:
        text = (message or "").strip()
        if not text:
            return _UNSUPPORTED_MESSAGE
        command, _, remainder = text.partition(" ")
        if command.lower() == "/zh":
            return zh_handler(remainder.strip(), _CHAT_WEB_CHAT_ID)
        return _UNSUPPORTED_MESSAGE

    return route


def _build_handler(router: Callable[[str], str]) -> type[BaseHTTPRequestHandler]:
    class ChatWebHandler(BaseHTTPRequestHandler):
        def _is_allowed(self) -> bool:
            return _is_allowed_client(self.client_address[0])

        def do_GET(self) -> None:  # noqa: N802
            if not self._is_allowed():
                self.send_error(HTTPStatus.FORBIDDEN, "Private access only")
                return
            if self.path in ("/", "/chat"):
                self.send_response(HTTPStatus.TEMPORARY_REDIRECT)
                self.send_header("Location", _web_frontend_url(self.headers.get("Host")))
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.end_headers()
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_POST(self) -> None:  # noqa: N802
            if not self._is_allowed():
                self.send_error(HTTPStatus.FORBIDDEN, "Private access only")
                return
            if self.path != "/api/chat":
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError):
                self._write_json({"error": "Invalid JSON"}, status=HTTPStatus.BAD_REQUEST)
                return
            message = str(data.get("message") or "").strip()
            if not message:
                self._write_json({"error": "message is required"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                reply = router(message)
            except Exception as exc:  # pragma: no cover - defensive, surface as error
                logger.exception("chat-web router failed message=%r", message)
                self._write_json(
                    {"error": f"後端處理失敗：{exc}"},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            self._write_json({"reply": reply})

        def log_message(self, format: str, *args: object) -> None:
            return

        def _write_html(self, html: str, *, status: int = 200) -> None:
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _write_json(self, payload: dict[str, object], *, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ChatWebHandler


def serve_chat_web(
    *,
    settings: AssistantSettings,
    host: str = "127.0.0.1",
    port: int = 8780,
    open_browser: bool = False,
) -> int:
    router = build_chat_router(settings)
    server = ThreadingHTTPServer((host, port), _build_handler(router))
    url = f"http://{host}:{port}/chat"
    logger.info("Chat-web server starting host=%s port=%s", host, port)
    print(f"OpenClaw local chat test page running at {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
