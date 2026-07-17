"""Local HTTP server for the aka_no_claw_web command bridge (issue #30).

Exposes command and local speech-to-text endpoints over a stdlib
``ThreadingHTTPServer``:

* ``POST /api/command``        — blocking JSON, for short non-chat commands.
* ``POST /api/command/stream`` — newline-delimited JSON (NDJSON) events for the
                                 chat path (start / delta / heartbeat / done /
                                 error), so long chat output streams.
* ``POST /api/command/transcribe`` — multipart audio to local faster-whisper.

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
from multipart import MultipartError, ParserLimitReached, parse_form_data

from .command_bridge import CommandBridge
from .command_bridge_models import RequestValidationError, parse_request
from .local_stt import (
    LocalWhisperTranscriber,
    SttPayloadTooLarge,
    SttRequestError,
    SttRuntimeError,
    build_audio_request,
)
from .voice import metrics as voice_metrics

logger = logging.getLogger(__name__)

# Transport envelope version stamped on every JSON object response and NDJSON
# stream event (aka_no_claw#77 D2.4 follow-up), mirroring reputation_snapshot's
# after_request hook. A response with NO envelope_version is the legacy/
# implicit-v0 case; the frontend client accepts it during the compatibility
# window per docs/CROSS_REPO_CONTRACTS.md.
COMMAND_BRIDGE_ENVELOPE_VERSION = 1


def _stamp_envelope_version(payload: dict) -> dict:
    """Add envelope_version to a JSON-object payload if not already present.
    Never overrides an existing value (mirrors reputation_snapshot's hook)."""
    if "envelope_version" in payload:
        return payload
    return {**payload, "envelope_version": COMMAND_BRIDGE_ENVELOPE_VERSION}


# Mesh VPN CGNAT range (NordVPN Meshnet / Tailscale) — always allowed so the
# user's own phone reaches the bridge over the encrypted mesh.
_MESHNET_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_PRIVATE_LAN = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)

_STT_MULTIPART_HEADER_LIMIT = 4
_STT_MULTIPART_HEADER_SIZE_LIMIT = 2048
_STT_MULTIPART_PART_LIMIT = 2  # required file + optional language
_STT_MULTIPART_SPOOL_LIMIT = 256 * 1024


def _parse_stt_multipart(
    stream,
    *,
    content_type: str,
    content_length: int,
    max_audio_bytes: int,
    default_language: str | None = None,
):
    if content_type.split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise SttRequestError("Content-Type 必須是 multipart/form-data。")

    forms = files = None
    file_parts = []
    try:
        forms, files = parse_form_data(
            {
                "wsgi.input": stream,
                "CONTENT_TYPE": content_type,
                "CONTENT_LENGTH": str(content_length),
            },
            strict=True,
            ignore_errors=False,
            header_limit=_STT_MULTIPART_HEADER_LIMIT,
            headersize_limit=_STT_MULTIPART_HEADER_SIZE_LIMIT,
            part_limit=_STT_MULTIPART_PART_LIMIT,
            partsize_limit=max_audio_bytes,
            spool_limit=min(max_audio_bytes, _STT_MULTIPART_SPOOL_LIMIT),
            memory_limit=_STT_MULTIPART_SPOOL_LIMIT * 2,
            disk_limit=max_audio_bytes,
        )
        file_parts = list(files.iterallitems())
        uploads = files.getall("file")
        if len(uploads) != 1 or len(file_parts) != 1:
            raise SttRequestError("multipart 請求必須且只能包含一個 file 檔案欄位。")
        upload = uploads[0]
        if not upload.filename:
            raise SttRequestError("file 欄位必須提供 filename。")

        unknown_fields = set(forms.keys()) - {"language"}
        if unknown_fields:
            raise SttRequestError("multipart 含有不支援的欄位。")
        languages = forms.getall("language")
        if len(languages) > 1:
            raise SttRequestError("language 欄位只能出現一次。")
        language = languages[0].strip() if languages else default_language
        if language and (
            not 2 <= len(language) <= 3
            or not language.isascii()
            or not language.isalpha()
        ):
            raise SttRequestError("language 必須是 2 到 3 碼語言代碼。")

        return build_audio_request(
            upload.raw,
            mime_type=upload.content_type,
            max_audio_bytes=max_audio_bytes,
            language=language,
        )
    finally:
        for _, part in file_parts:
            part.close()


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


def _build_handler(
    bridge: CommandBridge,
    *,
    lan_enabled: bool,
    transcriber: LocalWhisperTranscriber | None = None,
    voice_store: object | None = None,
    voice_embedding_backend: object | None = None,
    voice_utterance_ttl_seconds: float = 1800.0,
) -> type[BaseHTTPRequestHandler]:
    class CommandBridgeHandler(BaseHTTPRequestHandler):
        def _is_allowed(self) -> bool:
            return _is_allowed_client(self.client_address[0], lan_enabled=lan_enabled)

        def _send_cors_headers(self) -> None:
            origin = self.headers.get("Origin")
            self.send_header("Access-Control-Allow-Origin", origin or "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-OpenClaw-Event-Version")
            self.send_header("Access-Control-Max-Age", "600")

        def do_OPTIONS(self) -> None:  # noqa: N802
            if not self._is_allowed():
                self.send_error(HTTPStatus.FORBIDDEN, "Private access only")
                return
            self.send_response(HTTPStatus.NO_CONTENT)
            self._send_cors_headers()
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _read_request(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            data = json.loads(raw.decode("utf-8")) if raw else {}
            return parse_request(data)

        def _read_json_object(self) -> dict:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            data = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(data, dict):
                raise ValueError("請求必須是 JSON 物件。")
            return data

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
            elif path == "/api/command/cancel":
                self._handle_cancel()
            elif path == "/api/command/music":
                self._handle_music()
            elif path == "/api/command/bluetooth":
                self._handle_bluetooth()
            elif path == "/api/command/ir":
                self._handle_ir()
            elif path == "/api/command/workflow":
                self._handle_workflow()
            elif path == "/api/command/approval":
                self._handle_approval()
            elif path == "/api/command/queue":
                self._handle_queue_create()
            elif path == "/api/command/queue/reorder":
                self._handle_queue_reorder()
            elif path == "/api/command/schedulehome":
                self._handle_schedulehome()
            elif path == "/api/command/session":
                self._handle_session_save()
            elif path == "/api/command/chat-settings":
                self._handle_chat_settings_save()
            elif path == "/api/command/transcribe":
                self._handle_transcribe()
            elif path == "/api/command/voice/confirm":
                self._handle_voice_confirm()
            elif path == "/api/command/voice/reset":
                self._write_json(bridge.reset_voice_personalization())
            elif path == "/api/command/voice/feedback":
                self._handle_voice_feedback()
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
            elif split.path == "/api/command/events":
                params = parse_qs(split.query)
                session_id = (params.get("session_id") or [None])[0]
                raw_after = (params.get("after") or [None])[0]
                raw_limit = (params.get("limit") or ["500"])[0]
                try:
                    after = int(raw_after) if raw_after is not None else None
                    limit = int(raw_limit)
                except ValueError:
                    self._write_json({"status": "error", "message": "after 與 limit 必須是整數。"}, status=HTTPStatus.BAD_REQUEST)
                    return
                result = bridge.read_session_events(session_id=session_id, after=after, limit=limit)
                status = HTTPStatus.GONE if result.get("status") == "cursor_expired" else (
                    HTTPStatus.BAD_REQUEST if result.get("status") == "error" else HTTPStatus.OK
                )
                self._write_json(result, status=status)
            elif split.path == "/api/command/queue":
                session_id = (parse_qs(split.query).get("session_id") or [None])[0]
                result = bridge.load_prompt_queue(session_id)
                status = HTTPStatus.SERVICE_UNAVAILABLE if result.get("status") == "disabled" else HTTPStatus.OK
                self._write_json(result, status=status)
            elif split.path == "/api/command/music/now":
                self._write_json(bridge.now_playing())
            elif split.path == "/api/command/model-routes":
                self._write_json(bridge.model_routes())
            elif split.path == "/api/command/chat-settings":
                self._write_json(bridge.load_chat_settings())
            elif split.path == "/api/command/voice/prototypes":
                self._write_json(bridge.list_voice_prototypes())
            elif split.path == "/api/command/voice/metrics":
                self._write_json(
                    {"status": "ok", **voice_metrics.METRICS.snapshot()}
                )
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._is_allowed():
                self.send_error(HTTPStatus.FORBIDDEN, "Private access only")
                return
            split = urlsplit(self.path)
            if split.path == "/api/command/session":
                self._write_json(bridge.clear_session())
            elif split.path.startswith("/api/command/queue/"):
                prompt_id = split.path.rsplit("/", 1)[-1]
                params = parse_qs(split.query)
                raw_version = (params.get("expected_version") or [None])[0]
                try:
                    expected_version = int(raw_version) if raw_version is not None else None
                except ValueError:
                    self._write_json({"status": "error", "message": "expected_version 必須是整數。"}, status=HTTPStatus.BAD_REQUEST)
                    return
                result = bridge.cancel_prompt_queue_entry(
                    prompt_id,
                    session_id=(params.get("session_id") or [None])[0],
                    expected_version=expected_version,
                )
                self._write_json(result, status=self._queue_status(result))
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_PATCH(self) -> None:  # noqa: N802
            if not self._is_allowed():
                self.send_error(HTTPStatus.FORBIDDEN, "Private access only")
                return
            split = urlsplit(self.path)
            if not split.path.startswith("/api/command/queue/"):
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return
            prompt_id = split.path.rsplit("/", 1)[-1]
            try:
                result = bridge.edit_prompt_queue_entry(prompt_id, self._read_json_object())
            except (ValueError, UnicodeDecodeError) as exc:
                self._write_json({"status": "error", "message": f"無效的請求：{exc}"}, status=HTTPStatus.BAD_REQUEST)
                return
            self._write_json(result, status=self._queue_status(result))

        @staticmethod
        def _queue_status(result: dict) -> HTTPStatus:
            if result.get("status") == "conflict":
                return HTTPStatus.CONFLICT
            if result.get("status") == "capacity":
                return HTTPStatus.TOO_MANY_REQUESTS
            if result.get("status") == "disabled":
                return HTTPStatus.SERVICE_UNAVAILABLE
            if result.get("status") == "error":
                return HTTPStatus.BAD_REQUEST
            return HTTPStatus.OK

        def _handle_queue_create(self) -> None:
            try:
                result = bridge.create_prompt_queue_entry(self._read_json_object())
            except (ValueError, UnicodeDecodeError) as exc:
                self._write_json({"status": "error", "message": f"無效的請求：{exc}"}, status=HTTPStatus.BAD_REQUEST)
                return
            self._write_json(result, status=self._queue_status(result))

        def _handle_queue_reorder(self) -> None:
            try:
                result = bridge.reorder_prompt_queue(self._read_json_object())
            except (ValueError, UnicodeDecodeError) as exc:
                self._write_json({"status": "error", "message": f"無效的請求：{exc}"}, status=HTTPStatus.BAD_REQUEST)
                return
            self._write_json(result, status=self._queue_status(result))

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

        def _handle_cancel(self) -> None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                raw = self.rfile.read(length) if length else b""
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError) as exc:
                self._write_json({"status": "error", "message": f"無效的請求：{exc}"},
                                 status=HTTPStatus.BAD_REQUEST)
                return
            job_id = str(data.get("job_id") or "")
            if not job_id:
                self._write_json({"status": "error", "message": "缺少 job_id。"},
                                 status=HTTPStatus.BAD_REQUEST)
                return
            self._write_json(bridge.cancel_job(job_id))

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

        def _handle_bluetooth(self) -> None:
            """生活 mode Bluetooth surface (aka_no_claw#38 / web#7). A button press
            carries ``callback_data`` (re-scan or connect a device); an empty body
            returns the scanned device list + connect buttons."""
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
                self._write_json(bridge.run_bluetooth_action(callback_data))
                return
            self._write_json(bridge.run_bluetooth_command(str(data.get("input") or "")))

        def _handle_ir(self) -> None:
            """生活 mode IR/home-appliance surface. ``input`` carries an /ir slash
            command such as ``/ir send ceiling_light power``; ``callback_data``
            replays a backend-generated IR button when /ir devices is exposed."""
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
                self._write_json(bridge.run_ir_action(callback_data))
                return
            self._write_json(bridge.run_ir_command(str(data.get("input") or "")))

        def _handle_chat_settings_save(self) -> None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                raw = self.rfile.read(length) if length else b""
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError) as exc:
                self._write_json(
                    {"status": "error", "message": f"無效的請求：{exc}"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            if not isinstance(data, dict):
                self._write_json(
                    {"status": "error", "message": "請求必須是 JSON 物件。"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            result = bridge.save_chat_settings(data)
            status = HTTPStatus.OK if result.get("status") != "error" else HTTPStatus.BAD_REQUEST
            self._write_json(result, status=status)

        def _handle_voice_confirm(self) -> None:
            """#82 PR1: execute a clarification candidate. Body carries only
            ``action_id``; the bridge re-resolves the registry server-side."""
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
            action_id = str(data.get("action_id") or "")
            if not action_id:
                self._write_json({"status": "error", "message": "缺少 action_id。"},
                                 status=HTTPStatus.BAD_REQUEST)
                return
            learning_token = str(data.get("learning_token") or "") or None
            self._write_json(
                bridge.confirm_voice_action(action_id, learning_token=learning_token)
            )

        def _handle_voice_feedback(self) -> None:
            """#82 PR4: negative feedback「不是這個」against the prototype that
            triggered a direct dispatch (design §7.6)."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
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
            prototype_id = str(data.get("prototype_id") or "")
            if not prototype_id:
                self._write_json({"status": "error", "message": "缺少 prototype_id。"},
                                 status=HTTPStatus.BAD_REQUEST)
                return
            self._write_json(bridge.report_voice_direct_rejection(prototype_id))

        def _persist_utterance(self, *, utterance_id, request, result) -> str:
            """#82 PR2: store the utterance row (embedding included when a
            backend is configured) with a short TTL. Raw audio only lives in
            the request buffer and is dropped when this request ends. Any
            failure here is fail-soft — STT must keep working (design §13.4).
            Returns the embedding_status reported to the client."""
            if voice_store is None:
                return "disabled"
            embedding = None
            model_version = None
            status = "disabled"
            if voice_embedding_backend is not None:
                try:
                    embedding = voice_embedding_backend.embed(request.data)  # type: ignore[attr-defined]
                    model_version = voice_embedding_backend.model_version  # type: ignore[attr-defined]
                    status = "ready"
                except Exception:  # noqa: BLE001
                    logger.exception("voice embedding failed; storing without vector")
                    status = "error"
            try:
                voice_store.gc_expired()  # type: ignore[attr-defined]
                voice_store.save_utterance(  # type: ignore[attr-defined]
                    utterance_id=utterance_id,
                    transcript=result.transcript,
                    duration_ms=int((result.duration_seconds or 0) * 1000),
                    ttl_seconds=voice_utterance_ttl_seconds,
                    language=result.language,
                    language_probability=result.language_probability,
                    embedding=embedding,
                    embedding_model_version=model_version,
                )
            except Exception:  # noqa: BLE001
                logger.exception("voice utterance persistence failed (fail-soft)")
                return "error"
            return status

        def _handle_transcribe(self) -> None:
            if transcriber is None:
                self._write_json(
                    {"status": "error", "message": "本機語音轉文字尚未設定。"},
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except (TypeError, ValueError):
                self.close_connection = True
                self._write_json(
                    {"status": "error", "message": "無效的 Content-Length。"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            if length <= 0:
                self.close_connection = True
                self._write_json(
                    {"status": "error", "message": "缺少有效的 Content-Length。"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            if length > transcriber.max_request_bytes:
                self.close_connection = True
                self._write_json(
                    {"status": "error", "message": "錄音請求超過大小上限。"},
                    status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )
                return
            try:
                request = _parse_stt_multipart(
                    self.rfile,
                    content_type=self.headers.get("Content-Type", ""),
                    content_length=length,
                    max_audio_bytes=transcriber.max_audio_bytes,
                    default_language=getattr(
                        getattr(bridge, "settings", None), "openclaw_stt_language", None
                    ),
                )
                result = transcriber.transcribe(request)
            except ParserLimitReached as exc:
                self._write_json(
                    {"status": "error", "message": f"multipart 超過限制：{exc}"},
                    status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )
                return
            except (MultipartError, UnicodeDecodeError) as exc:
                self._write_json(
                    {"status": "error", "message": f"無效的 multipart：{exc}"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            except SttPayloadTooLarge as exc:
                self._write_json(
                    {"status": "error", "message": str(exc)},
                    status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )
                return
            except SttRequestError as exc:
                self._write_json(
                    {"status": "error", "message": str(exc)},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            except SttRuntimeError as exc:
                self._write_json(
                    {"status": "error", "message": str(exc)},
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return

            # Opaque id for the voice-personalization pipeline (#82): the
            # frontend echoes it back in the command request's voice metadata
            # so the learning transaction (PR3) can associate the utterance.
            utterance_id = uuid4().hex
            embedding_status = self._persist_utterance(
                utterance_id=utterance_id, request=request, result=result
            )
            response: dict[str, object] = {
                "status": "ok",
                "utterance_id": utterance_id,
                "embedding_status": embedding_status,
                "transcript": result.transcript,
            }
            if result.language is not None:
                response["language"] = result.language
            if result.language_probability is not None:
                response["language_probability"] = result.language_probability
            if result.duration_seconds is not None:
                response["duration_seconds"] = result.duration_seconds
            self._write_json(response)

        def _handle_workflow(self) -> None:
            """Workflow surface (#53). ``callback_data`` (``wfe:…``) replays an
            editor button on the current draft (reorder / delete / save / cancel);
            ``input`` carries a ``/workflow`` subcommand (e.g.
            ``create 每天早上查東京天氣…``) — a natural-language ``create`` drafts a
            workflow and returns an editable card."""
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
                self._write_json(bridge.run_workflow_action(callback_data))
                return
            self._write_json(
                bridge.run_workflow_command(
                    str(data.get("input") or ""),
                    chat_backend=str(data.get("chat_backend") or ""),
                    session_id=str(data.get("session_id") or "") or None,
                )
            )

        def _handle_approval(self) -> None:
            """Resolve one manifest-bound generated-tool approval (#85)."""
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                raw = self.rfile.read(length) if length else b""
                data = json.loads(raw.decode("utf-8")) if raw else {}
                response = bridge.resolve_workflow_approval(data)
            except ValueError as exc:
                self._write_json({"status": "error", "message": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            except KeyError:
                self._write_json({"status": "error", "message": "找不到核准請求"}, status=HTTPStatus.NOT_FOUND)
                return
            except PermissionError as exc:
                self._write_json({"status": "error", "message": str(exc)}, status=HTTPStatus.FORBIDDEN)
                return
            except RuntimeError as exc:
                self._write_json({"status": "error", "message": str(exc)}, status=HTTPStatus.CONFLICT)
                return
            self._write_json(response)

        def _handle_schedulehome(self) -> None:
            """Schedule surface (web#9). ``callback_data`` (``sh:…``) dispatches a
            picker/list button; ``input`` carries a subcommand (add, add_for_wf <id>,
            run/on/off/delete <id>) or capture-mode text."""
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
                self._write_json(bridge.run_schedulehome_action(callback_data))
                return
            self._write_json(bridge.run_schedulehome_command(str(data.get("input") or "")))

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
            event_version = self.headers.get("X-OpenClaw-Event-Version") == "1"
            self.send_response(HTTPStatus.OK)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            stream = bridge.stream(req, request_id)
            cursor = 0
            if event_version:
                bootstrap = bridge.read_session_events(session_id=req.session_id, after=None)
                # A bootstrap page can be capped below the journal head.  Start
                # negotiated live delivery at the atomic latest cursor, not at
                # the end of page one, or large sessions replay old events into
                # an otherwise-live NDJSON response.
                cursor = int(
                    bootstrap.get("latest_cursor", bootstrap.get("server_cursor")) or 0
                )
            try:
                for event in stream:
                    line = (
                        json.dumps(_stamp_envelope_version(event), ensure_ascii=False) + "\n"
                    ).encode("utf-8")
                    self.wfile.write(line)
                    self.wfile.flush()
                    if event_version:
                        page = bridge.read_session_events(session_id=req.session_id, after=cursor)
                        for durable in page.get("events") or []:
                            envelope = {"type": "session_event", "event": durable}
                            self.wfile.write(
                                (json.dumps(_stamp_envelope_version(envelope), ensure_ascii=False) + "\n").encode("utf-8")
                            )
                            self.wfile.flush()
                            cursor = int(durable["seq"])
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
            body = json.dumps(_stamp_envelope_version(payload), ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._send_cors_headers()
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
    transcriber = LocalWhisperTranscriber.from_settings(settings)
    # #82 PR2: voice personalization store + embedding backend. A corrupt DB
    # is reported loudly and the feature is disabled — it is never silently
    # rebuilt, and it never blocks the bridge itself (design §13.4).
    from .voice.embedding import resolve_embedding_backend
    from .voice.prototype_store import VoiceStoreCorruptError, open_voice_store

    try:
        voice_store = open_voice_store(settings)
    except VoiceStoreCorruptError:
        logger.exception(
            "voice store is CORRUPT — voice personalization disabled; "
            "inspect/back up the DB before deleting it"
        )
        voice_store = None
    voice_embedding_backend = resolve_embedding_backend(settings)
    server = ThreadingHTTPServer(
        (bind_host, port),
        _build_handler(
            bridge,
            lan_enabled=lan,
            transcriber=transcriber,
            voice_store=voice_store,
            voice_embedding_backend=voice_embedding_backend,
            voice_utterance_ttl_seconds=float(
                getattr(settings, "openclaw_voice_utterance_ttl_seconds", 1800)
            ),
        ),
    )
    logger.info("command-bridge server starting host=%s port=%s lan=%s", bind_host, port, lan)
    print(f"OpenClaw command bridge running on http://{bind_host}:{port} (lan={lan})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
