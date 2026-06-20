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

from assistant_runtime import AssistantSettings

from .telegram_bot import build_translate_handler

logger = logging.getLogger(__name__)

# Private-only access surface: the Mac loopback plus the CGNAT range used by
# mesh VPNs (NordVPN Meshnet / Tailscale, 100.64.0.0/10) so the user's own
# phone can reach the page over the encrypted mesh — but ordinary LAN
# (192.168.x) and the public internet stay blocked even when bound to 0.0.0.0.
_MESHNET_CGNAT = ipaddress.ip_network("100.64.0.0/10")

_CHAT_WEB_CHAT_ID = "chat-web"


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


_CHAT_PAGE_HTML = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>龍蝦本機聊天測試</title>
<style>
  body { font-family: -apple-system, "Segoe UI", sans-serif; max-width: 720px;
         margin: 2rem auto; padding: 0 1rem; color: #1d1d1f; }
  h1 { font-size: 1.25rem; }
  #log { border: 1px solid #d2d2d7; border-radius: 8px; padding: 0.75rem;
         min-height: 240px; margin-bottom: 0.75rem; white-space: pre-wrap;
         overflow-y: auto; max-height: 60vh; }
  .msg { margin: 0.4rem 0; }
  .me { color: #0066cc; }
  .bot { color: #1d1d1f; }
  .err { color: #cc0000; }
  form { display: flex; gap: 0.5rem; }
  input { flex: 1; padding: 0.5rem; border: 1px solid #d2d2d7; border-radius: 6px; }
  button { padding: 0.5rem 1rem; border: 0; border-radius: 6px;
           background: #0066cc; color: #fff; cursor: pointer; }
  button:disabled { background: #999; cursor: default; }
  .hint { color: #6e6e73; font-size: 0.85rem; }
</style>
</head>
<body>
  <h1>龍蝦本機聊天測試</h1>
  <p class="hint">本機限定。第一版只支援 <code>/zh &lt;文字&gt;</code>。</p>
  <div id="log"></div>
  <form id="form">
    <input id="message" autocomplete="off" placeholder="/zh 測試" autofocus>
    <button id="send" type="submit">送出</button>
  </form>
<script>
  const log = document.getElementById("log");
  const form = document.getElementById("form");
  const input = document.getElementById("message");
  const send = document.getElementById("send");

  function add(cls, prefix, text) {
    const div = document.createElement("div");
    div.className = "msg " + cls;
    div.textContent = prefix + text;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const message = input.value.trim();
    if (!message) return;
    add("me", "你：", message);
    input.value = "";
    send.disabled = true;
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        add("err", "錯誤：", data.error || ("HTTP " + res.status));
      } else {
        add("bot", "龍蝦：", data.reply);
      }
    } catch (err) {
      add("err", "錯誤：", "無法連線到本機龍蝦後端 (" + err + ")");
    } finally {
      send.disabled = false;
      input.focus();
    }
  });
</script>
</body>
</html>
"""


def _build_handler(router: Callable[[str], str]) -> type[BaseHTTPRequestHandler]:
    class ChatWebHandler(BaseHTTPRequestHandler):
        def _is_allowed(self) -> bool:
            return _is_allowed_client(self.client_address[0])

        def do_GET(self) -> None:  # noqa: N802
            if not self._is_allowed():
                self.send_error(HTTPStatus.FORBIDDEN, "Private access only")
                return
            if self.path in ("/", "/chat"):
                self._write_html(_CHAT_PAGE_HTML)
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
