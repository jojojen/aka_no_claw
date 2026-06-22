"""Local HTTP server for the aka_no_claw_web command bridge (issue #30).

Exposes two endpoints over a stdlib ``ThreadingHTTPServer`` (no new dependency):

* ``POST /api/command``        — blocking JSON, for short non-chat commands.
* ``POST /api/command/stream`` — newline-delimited JSON (NDJSON) events for the
                                 chat path (start / delta / heartbeat / done /
                                 error), so long chat output streams.

Default bind is ``127.0.0.1``. LAN access (phone on the same Wi-Fi) is opt-in via
``--lan``; even then a client-IP allowlist (loopback + private LAN + mesh CGNAT)
is enforced as defence in depth — never the public internet.
"""

from __future__ import annotations

import ipaddress
import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from assistant_runtime import AssistantSettings

from .command_bridge import CommandBridge
from .command_bridge_models import RequestValidationError, parse_request

logger = logging.getLogger(__name__)

# Mesh VPN CGNAT range (NordVPN Meshnet / Tailscale) — always allowed so the
# user's own phone reaches the bridge over the encrypted mesh.
_MESHNET_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_PRIVATE_LAN = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _is_allowed_client(client_host: str, *, lan_enabled: bool) -> bool:
    try:
        ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if ip.is_loopback:
        return True
    if ip.version == 4 and ip in _MESHNET_CGNAT:
        return True
    if lan_enabled and ip.version == 4 and any(ip in net for net in _PRIVATE_LAN):
        return True
    return False


def _build_handler(bridge: CommandBridge, *, lan_enabled: bool) -> type[BaseHTTPRequestHandler]:
    class CommandBridgeHandler(BaseHTTPRequestHandler):
        def _is_allowed(self) -> bool:
            return _is_allowed_client(self.client_address[0], lan_enabled=lan_enabled)

        def _read_request(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            data = json.loads(raw.decode("utf-8")) if raw else {}
            return parse_request(data)

        def do_POST(self) -> None:  # noqa: N802
            if not self._is_allowed():
                self.send_error(HTTPStatus.FORBIDDEN, "Private access only")
                return
            path = urlsplit(self.path).path
            if path == "/api/command":
                self._handle_blocking()
            elif path == "/api/command/stream":
                self._handle_stream()
            elif path == "/api/command/async":
                self._handle_async()
            elif path == "/api/command/action":
                self._handle_action()
            elif path == "/api/command/music":
                self._handle_music()
            elif path == "/api/command/session":
                self._handle_session_save()
            elif path == "/api/command/restartall":
                self._write_json(bridge.restart_all())
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_GET(self) -> None:  # noqa: N802
            if not self._is_allowed():
                self.send_error(HTTPStatus.FORBIDDEN, "Private access only")
                return
            split = urlsplit(self.path)
            if split.path == "/api/command/poll":
                job_ids = parse_qs(split.query).get("job_id", [])
                if not job_ids:
                    self._write_json({"job_status": "error", "message": "缺少 job_id。"},
                                     status=HTTPStatus.BAD_REQUEST)
                    return
                self._write_json(bridge.poll_job(job_ids[0]))
            elif split.path == "/api/command/session":
                self._write_json(bridge.load_session())
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._is_allowed():
                self.send_error(HTTPStatus.FORBIDDEN, "Private access only")
                return
            if urlsplit(self.path).path == "/api/command/session":
                self._write_json(bridge.clear_session())
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def _handle_async(self) -> None:
            try:
                req = self._read_request()
            except (ValueError, UnicodeDecodeError, RequestValidationError) as exc:
                self._write_json({"status": "error", "message": f"無效的請求：{exc}"},
                                 status=HTTPStatus.BAD_REQUEST)
                return
            result = bridge.start_async(req)
            status = HTTPStatus.OK if result.get("status") == "accepted" else HTTPStatus.BAD_REQUEST
            self._write_json(result, status=status)

        def _handle_action(self) -> None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                raw = self.rfile.read(length) if length else b""
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError) as exc:
                self._write_json({"status": "error", "message": f"無效的請求：{exc}"},
                                 status=HTTPStatus.BAD_REQUEST)
                return
            job_id = str(data.get("job_id") or "")
            callback_data = str(data.get("callback_data") or "")
            if not job_id or not callback_data:
                self._write_json({"status": "error", "message": "缺少 job_id 或 callback_data。"},
                                 status=HTTPStatus.BAD_REQUEST)
                return
            self._write_json(bridge.run_action(job_id, callback_data))

        def _handle_music(self) -> None:
            """生活 mode music surface (aka_no_claw_web#3/#4). A button press
            carries ``callback_data`` (re-invoke the matching music/list
            callback); a text box carries ``input`` (play/search); an empty
            body returns the music menu + control buttons."""
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                raw = self.rfile.read(length) if length else b""
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError) as exc:
                self._write_json({"status": "error", "message": f"無效的請求：{exc}"},
                                 status=HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(data, dict):
                self._write_json({"status": "error", "message": "請求必須是 JSON 物件。"},
                                 status=HTTPStatus.BAD_REQUEST)
                return
            callback_data = str(data.get("callback_data") or "")
            if callback_data:
                self._write_json(bridge.run_music_action(callback_data))
                return
            self._write_json(bridge.run_music_command(str(data.get("input") or "")))

        def _handle_session_save(self) -> None:
            """POST /api/command/session — replace the saved console snapshot.
            The body IS the snapshot (messages + selected mode/backend/submode)."""
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                raw = self.rfile.read(length) if length else b""
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError) as exc:
                self._write_json({"status": "error", "message": f"無效的請求：{exc}"},
                                 status=HTTPStatus.BAD_REQUEST)
                return
            self._write_json(bridge.save_session(data))

        def _handle_blocking(self) -> None:
            try:
                req = self._read_request()
            except (ValueError, UnicodeDecodeError, RequestValidationError) as exc:
                self._write_json({"status": "error", "message": f"無效的請求：{exc}"},
                                 status=HTTPStatus.BAD_REQUEST)
                return
            response = bridge.handle(req)
            self._write_json(response.to_dict())

        def _handle_stream(self) -> None:
            try:
                req = self._read_request()
            except (ValueError, UnicodeDecodeError, RequestValidationError) as exc:
                self._write_json({"status": "error", "message": f"無效的請求：{exc}"},
                                 status=HTTPStatus.BAD_REQUEST)
                return
            request_id = uuid4().hex
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Connection", "close")
            self.end_headers()
            stream = bridge.stream(req, request_id)
            try:
                for event in stream:
                    line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
                    self.wfile.write(line)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # Client cancelled (AbortController) — stop quietly.
                logger.info("command bridge stream: client disconnected request_id=%s", request_id)
            finally:
                # Close the generator deterministically (don't wait for GC) so a
                # GeneratorExit fires inside bridge.stream and any in-flight model
                # worker is aborted the moment the client goes away.
                stream.close()

        def log_message(self, format: str, *args: object) -> None:
            return

        def _write_json(self, payload: dict, *, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return CommandBridgeHandler


def serve_command_bridge(
    *,
    settings: AssistantSettings,
    host: str | None = None,
    port: int = 8781,
    lan: bool = False,
) -> int:
    bind_host = host or ("0.0.0.0" if lan else "127.0.0.1")
    bridge = CommandBridge(settings)
    server = ThreadingHTTPServer((bind_host, port), _build_handler(bridge, lan_enabled=lan))
    logger.info("command-bridge server starting host=%s port=%s lan=%s", bind_host, port, lan)
    print(f"OpenClaw command bridge running on http://{bind_host}:{port} (lan={lan})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
