"""Local command bridge for aka_no_claw_web (issue #30).

Routes the three MVP modes from the mobile console — Chat, Translation,
Investment Research — onto the *existing* OpenClaw handlers, so the web UI never
reimplements command logic and never drifts from the Telegram bot:

* Chat       → local Ollama model or cloud big-pickle, pure chat (Phase 1, no
               tool calls), with a streaming path for long output.
* Translation→ the existing ``/zh`` handler (text). Image translation is
               reported as a structured ``unsupported`` until the bridge grows a
               multipart file route (doc-allowed for MVP).
* Investment → ``商品深入研究`` reuses the existing ``/research`` handler. Seller
               reputation snapshot is ``unsupported`` for MVP.

Handlers are pulled from :func:`telegram_bot._build_registries` (the same
registry the bot uses) so there is one source of truth.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from assistant_runtime import AssistantSettings, build_ssl_context

from .job_store import JobStore
from .session_memory import SessionMemoryStore, SessionWriteError, empty_session
from .service_restart import RESTART_MESSAGE, trigger_restart_all
from .command_bridge_models import (
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
    WebCommandRequest,
    WebCommandResponse,
    stream_delta,
    stream_done,
    stream_error,
    stream_heartbeat,
    stream_start,
)

logger = logging.getLogger(__name__)

_BRIDGE_CHAT_ID = "web-bridge"
_HEARTBEAT_SECONDS = 10.0
# Finished jobs linger this long so a phone that reconnects after a screen-lock
# can still fetch the final report, then they are garbage-collected.
_JOB_TTL_SECONDS = 1800.0

_IMAGE_UNSUPPORTED_MSG = "圖片翻譯目前尚未由本地 command bridge 支援。"
_SELLER_UNSUPPORTED_MSG = "賣家信譽快照目前尚未由本地 command bridge 支援。"

JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_ERROR = "error"
JOB_INTERRUPTED = "interrupted"  # persisted running job whose in-memory worker is gone


class _Job:
    """A long-running command (e.g. ``/research``) decoupled from any HTTP
    connection. Staged ``notifier.send`` milestones accumulate in ``progress``
    so a polling client can show 龍蝦-style progress and survive disconnects."""

    def __init__(self, job_id: str) -> None:
        self.id = job_id
        self.status = JOB_RUNNING
        self.progress: list[str] = []
        self.message: str = ""
        self.actions: list[dict] = []
        self.error: str | None = None
        self.created_at = time.monotonic()   # monotonic for GC comparisons
        self.wall_created_at = time.time()   # wall clock for persisted snapshots
        self.lock = threading.Lock()


class _JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.Lock()

    def create(self) -> _Job:
        job = _Job(uuid4().hex)
        with self._lock:
            self._gc_locked()
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> _Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def append_progress(self, job_id: str, text: str) -> None:
        job = self.get(job_id)
        if job is not None:
            with job.lock:
                job.progress.append(text)

    def _gc_locked(self) -> None:
        cutoff = time.monotonic() - _JOB_TTL_SECONDS
        stale = [
            jid for jid, j in self._jobs.items()
            if j.status != JOB_RUNNING and j.created_at < cutoff
        ]
        for jid in stale:
            self._jobs.pop(jid, None)


class _JobNotifier:
    """ResearchNotifier that appends staged progress to a job, keyed by job id
    (passed as the research ``chat_id``), and persists each progress update so
    a bridge restart can still show intermediate results on reconnect."""

    def __init__(self, jobs: _JobManager, job_id: str, store: "JobStore") -> None:
        self._jobs = jobs
        self._job_id = job_id
        self._store = store

    def send(self, text: str) -> None:
        self._jobs.append_progress(self._job_id, text)
        # Snapshot current progress outside the lock to avoid disk I/O under it.
        job = self._jobs.get(self._job_id)
        if job is None:
            return
        with job.lock:
            progress_snapshot = list(job.progress)
            wall_created_at = job.wall_created_at
        self._store.save({
            "job_id": self._job_id,
            "status": JOB_RUNNING,
            "progress": progress_snapshot,
            "message": "",
            "actions": [],
            "error": None,
            "created_at": wall_created_at,
            "updated_at": time.time(),
        })


class CommandBridge:
    """Stateless-per-request router over the existing OpenClaw handlers."""

    def __init__(self, settings: AssistantSettings) -> None:
        self.settings = settings
        self._command_handlers: dict | None = None
        self._callback_handlers: dict | None = None
        self._view_handlers: dict | None = None
        self._item_deleter_handlers: dict | None = None
        self._registry_lock = threading.Lock()
        self._jobs = _JobManager()
        self._job_store_inst: JobStore | None = None
        self._job_store_lock = threading.Lock()
        self._session_store: SessionMemoryStore | None = None
        self._session_lock = threading.Lock()

    # --- handler registry (lazy, shared with the Telegram bot) ------------
    def _ensure_registries(self) -> None:
        if self._command_handlers is None:
            with self._registry_lock:
                if self._command_handlers is None:
                    from .telegram_bot import _build_registries

                    (
                        command_handlers,
                        callback_handlers,
                        view_handlers,
                        item_deleter_handlers,
                    ) = _build_registries(
                        self.settings,
                        None,
                        research_notifier_factory=lambda chat_id: _JobNotifier(
                            self._jobs, str(chat_id), self._get_job_store()
                        ),
                    )
                    self._callback_handlers = callback_handlers
                    self._command_handlers = command_handlers
                    self._view_handlers = view_handlers
                    self._item_deleter_handlers = item_deleter_handlers

    def _handlers(self) -> dict:
        self._ensure_registries()
        return self._command_handlers  # type: ignore[return-value]

    def _callbacks(self) -> dict:
        self._ensure_registries()
        return self._callback_handlers or {}

    def _views(self) -> dict:
        self._ensure_registries()
        return self._view_handlers or {}

    def _deleters(self) -> dict:
        self._ensure_registries()
        return self._item_deleter_handlers or {}

    def _run_command(self, command: str, remainder: str,
                     chat_id: str = _BRIDGE_CHAT_ID) -> str:
        text, _ = self._run_command_raw(command, remainder, chat_id=chat_id)
        return text

    def _run_command_raw(self, command: str, remainder: str,
                         chat_id: str = _BRIDGE_CHAT_ID) -> tuple[str, object]:
        """Run a handler and keep both the text and any Telegram reply_markup
        (inline_keyboard), so the web console can render the same follow-up
        buttons 龍蝦 shows after /research."""
        registered = self._handlers()[command]
        result = registered.handler(remainder, chat_id)
        if isinstance(result, tuple):
            text = result[0]
            markup = result[1] if len(result) > 1 else None
            return (str(text) if text is not None else "", markup)
        return (str(result) if result is not None else "", None)

    @staticmethod
    def _markup_to_actions(markup: object) -> list[dict]:
        """Flatten a Telegram inline_keyboard into web action buttons. Each
        button keeps its ``callback_data`` so a click can re-invoke the same
        callback handler (e.g. switch the /research view)."""
        actions: list[dict] = []
        if isinstance(markup, dict):
            for row in markup.get("inline_keyboard", []):
                for btn in row:
                    if not isinstance(btn, dict):
                        continue
                    cb = btn.get("callback_data")
                    label = btn.get("text")
                    if cb and label:
                        actions.append({"label": str(label), "callback_data": str(cb)})
        return actions

    # --- blocking entrypoint ---------------------------------------------
    def handle(self, req: WebCommandRequest) -> WebCommandResponse:
        try:
            if req.mode == MODE_CHAT:
                return self._handle_chat_blocking(req)
            if req.mode == MODE_TRANSLATION:
                return self._handle_translation(req)
            if req.mode == MODE_INVESTMENT:
                return self._handle_investment(req)
            return WebCommandResponse(
                status=STATUS_ERROR,
                message=f"未知的模式：{req.mode}",
                mode=req.mode,
            )
        except Exception as exc:  # noqa: BLE001 — surface as structured error
            logger.exception("command bridge failed mode=%s", req.mode)
            return WebCommandResponse(
                status=STATUS_ERROR,
                message=f"後端處理失敗：{exc}",
                mode=req.mode,
                submode=req.submode,
            )

    # --- streaming entrypoint (chat) -------------------------------------
    def stream(self, req: WebCommandRequest, request_id: str) -> Iterator[dict]:
        """Yield streaming event dicts. Chat streams token-by-token (local) or
        in one block with heartbeats (cloud); non-chat modes run blocking and
        emit a single done event so the frontend can use one code path."""
        yield stream_start(request_id)
        try:
            if req.mode == MODE_CHAT:
                yield from self._stream_chat(req)
                return
            # Non-chat modes (translation): reuse the blocking router, emit as
            # one event. Long research runs via the async job + poll endpoints
            # instead, so a mobile screen-lock can't drop a held connection.
            response = self.handle(req)
            if response.status == STATUS_ERROR:
                yield stream_error(response.message)
            else:
                yield stream_done(response.message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("command bridge stream failed mode=%s", req.mode)
            yield stream_error(f"後端處理失敗：{exc}")

    # --- chat ------------------------------------------------------------
    def _handle_chat_blocking(self, req: WebCommandRequest) -> WebCommandResponse:
        prompt = (req.input or "").strip()
        if not prompt:
            return WebCommandResponse(
                status=STATUS_ERROR, message="請輸入訊息。", mode=MODE_CHAT
            )
        if req.chat_backend == CHAT_BACKEND_CLOUD_PICKLE:
            client = self._build_cloud_chat_client()
            if client is None:
                return WebCommandResponse(
                    status=STATUS_ERROR,
                    message="cloud pickle 後端目前無法使用（OpenCode 未設定或無法連線）。",
                    mode=MODE_CHAT,
                )
            message = client.generate(prompt, temperature=0.7)
        else:
            message = self._ollama_generate_blocking(prompt)
        return WebCommandResponse(status=STATUS_OK, message=message, mode=MODE_CHAT)

    def _stream_chat(self, req: WebCommandRequest) -> Iterator[dict]:
        prompt = (req.input or "").strip()
        if not prompt:
            yield stream_error("請輸入訊息。")
            return
        if req.chat_backend == CHAT_BACKEND_CLOUD_PICKLE:
            yield from self._stream_cloud_chat(prompt)
        else:
            yield from self._stream_ollama_chat(prompt)

    # --- async job + poll (long research, decoupled from the connection) --
    def _get_job_store(self) -> JobStore:
        if self._job_store_inst is None:
            with self._job_store_lock:
                if self._job_store_inst is None:
                    import os
                    dir_path = (
                        getattr(self.settings, "openclaw_web_jobs_dir", None)
                        or os.path.join(".openclaw_tmp", "web_jobs")
                    )
                    self._job_store_inst = JobStore(dir_path)
        return self._job_store_inst

    def start_async(self, req: WebCommandRequest) -> dict:
        """Kick off a long command in a background thread and return a job id
        immediately. Only investment deep research is async for the MVP; the
        client then polls :meth:`poll_job` for staged progress + final report,
        which survives mobile screen-locks and connection drops."""
        if req.mode != MODE_INVESTMENT or req.submode not in (
            None, SUBMODE_DEEP_PRODUCT_RESEARCH
        ):
            return {"status": STATUS_ERROR,
                    "message": "非同步任務目前僅支援商品深入研究。"}
        text = (req.input or "").strip()
        if not text:
            return {"status": STATUS_ERROR, "message": "請貼上商品 URL 或輸入商品名稱。"}
        job = self._jobs.create()
        store = self._get_job_store()
        store.save({
            "job_id": job.id,
            "status": JOB_RUNNING,
            "progress": [],
            "message": "",
            "actions": [],
            "error": None,
            "created_at": job.wall_created_at,
            "updated_at": job.wall_created_at,
        })
        store.purge_expired()

        def _worker() -> None:
            try:
                message, markup = self._run_command_raw(
                    "/research", text, chat_id=job.id
                )
                with job.lock:
                    job.message = message
                    job.actions = self._markup_to_actions(markup)
                    job.status = JOB_DONE
                store.save({
                    "job_id": job.id,
                    "status": JOB_DONE,
                    "progress": list(job.progress),
                    "message": message,
                    "actions": list(job.actions),
                    "error": None,
                    "created_at": job.wall_created_at,
                    "updated_at": time.time(),
                })
            except Exception as exc:  # noqa: BLE001
                logger.exception("async research failed job=%s", job.id)
                with job.lock:
                    job.error = str(exc)
                    job.status = JOB_ERROR
                store.save({
                    "job_id": job.id,
                    "status": JOB_ERROR,
                    "progress": list(job.progress),
                    "message": "",
                    "actions": [],
                    "error": str(exc),
                    "created_at": job.wall_created_at,
                    "updated_at": time.time(),
                })

        threading.Thread(target=_worker, daemon=True).start()
        return {"status": "accepted", "job_id": job.id}

    def poll_job(self, job_id: str) -> dict:
        """Snapshot a job: status, staged progress, final report, follow-up actions.

        Falls back to the persisted JobStore when the in-memory job is missing
        (browser reload, bridge restart) and returns the correct terminal state
        or "interrupted" so the client can show a clear message instead of
        confusing not_found.
        """
        job = self._jobs.get(job_id)
        if job is not None:
            with job.lock:
                return {
                    "job_status": job.status,
                    "progress": list(job.progress),
                    "message": job.message,
                    "actions": list(job.actions),
                    "error": job.error,
                }

        # In-memory job is gone — check the persisted snapshot.
        persisted = self._get_job_store().load(job_id)
        if persisted is None:
            return {"job_status": JOB_ERROR, "not_found": True,
                    "message": "找不到此任務（可能已過期，請重新查詢）。"}
        status = persisted.get("status")
        if status == JOB_DONE:
            return {
                "job_status": JOB_DONE,
                "progress": persisted.get("progress") or [],
                "message": persisted.get("message") or "",
                "actions": persisted.get("actions") or [],
                "error": None,
            }
        if status == JOB_ERROR:
            return {
                "job_status": JOB_ERROR,
                "progress": persisted.get("progress") or [],
                "message": "",
                "actions": [],
                "error": persisted.get("error") or "任務失敗",
            }
        # status == running but in-memory worker gone: bridge was restarted.
        return {
            "job_status": JOB_INTERRUPTED,
            "message": "研究任務因系統重啟而中斷，請重新執行 /research。",
            "progress": persisted.get("progress") or [],
            "actions": [],
            "error": None,
        }

    def run_action(self, job_id: str, callback_data: str) -> dict:
        """Re-invoke a research follow-up button (e.g. ``rs:<token>:price``).
        The report is cached under the job id as its chat id, so the click must
        carry the originating job id — matching how 龍蝦 keys callbacks by chat."""
        if self._jobs.get(job_id) is None:
            return {"status": STATUS_ERROR,
                    "message": "找不到此研究結果（可能已過期，請重新執行研究）。"}
        prefix, _, payload = (callback_data or "").partition(":")
        handler = self._callbacks().get(prefix)
        if handler is None:
            return {"status": STATUS_ERROR, "message": f"未知的動作：{prefix or callback_data}"}
        try:
            result = handler(payload, "", job_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("research action failed job=%s cb=%s", job_id, callback_data)
            return {"status": STATUS_ERROR, "message": f"動作執行失敗：{exc}"}
        ack, detail, markup = (list(result) + [None, None, None])[:3] \
            if isinstance(result, tuple) else (result, None, None)
        message = detail if detail else ack
        return {
            "status": STATUS_OK,
            "message": str(message) if message is not None else "",
            "actions": self._markup_to_actions(markup),
        }

    # --- 生活 mode: music control surface (aka_no_claw_web#3 / #4) ---------
    def run_music_command(self, text: str) -> dict:
        """Run the ``/music`` handler for the 生活 mode text box — an empty box
        returns the music menu (text + control buttons); a query plays/searches
        a song. The same handler the Telegram bot uses, so no logic is duped."""
        message, markup = self._run_command_raw("/music", (text or "").strip())
        return {
            "status": STATUS_OK,
            "message": message,
            "actions": self._markup_to_actions(markup),
        }

    def run_music_action(self, callback_data: str) -> dict:
        """Re-invoke a music callback button for the web 生活 mode. Handles the
        ``music:`` family (browse / play / favorite / volume) plus the generic
        list callbacks (``pg`` / ``del`` / ``close``) the favorites list uses —
        the very same handlers the Telegram bot dispatches, so playback safety
        (path re-validation under the music root) is enforced identically."""
        prefix, _, payload = (callback_data or "").partition(":")
        if prefix == "music":
            handler = self._callbacks().get("music")
            if handler is None:
                return {"status": STATUS_ERROR, "message": "音樂功能尚未啟用。", "actions": []}
            try:
                result = handler(payload, "", _BRIDGE_CHAT_ID)
            except Exception as exc:  # noqa: BLE001
                logger.exception("music action failed cb=%s", callback_data)
                return {"status": STATUS_ERROR, "message": f"動作執行失敗：{exc}", "actions": []}
            toast, new_text, markup = (list(result) + [None, None, None])[:3] \
                if isinstance(result, tuple) else (result, None, None)
            message = new_text if new_text else toast
            return {
                "status": STATUS_OK,
                "message": str(message) if message is not None else "",
                "actions": self._markup_to_actions(markup),
            }
        if prefix in ("pg", "del", "close"):
            return self._run_list_action(prefix, payload)
        return {"status": STATUS_ERROR, "message": f"未知的音樂動作：{callback_data}", "actions": []}

    def _run_list_action(self, prefix: str, payload: str) -> dict:
        """Generic paginated-list callbacks (favorites use list kind ``mb``):
        ``pg`` repaginate/toggle mode, ``del`` remove a row then re-render in
        edit mode, ``close`` clear the list."""
        from price_monitor_bot.list_view import LIST_VIEW_MODE_EDIT

        if prefix == "close":
            return {"status": STATUS_OK, "message": "已關閉清單。", "actions": []}
        if prefix == "pg":
            try:
                kind, page_str, mode = payload.split(":", 2)
            except ValueError:
                return {"status": STATUS_ERROR, "message": "清單動作格式錯誤。", "actions": []}
            renderer = self._views().get(kind)
            if renderer is None:
                return {"status": STATUS_ERROR, "message": "找不到這個清單。", "actions": []}
            page = int(page_str) if page_str.lstrip("-").isdigit() else 0
            text, markup, _ = renderer(page=page, mode=mode)
            return {"status": STATUS_OK, "message": str(text or ""),
                    "actions": self._markup_to_actions(markup)}
        # prefix == "del"
        kind, _, item_id = payload.partition(":")
        deleter_entry = self._deleters().get(kind)
        renderer = self._views().get(kind)
        if deleter_entry is None or renderer is None:
            return {"status": STATUS_ERROR, "message": "找不到這個清單。", "actions": []}
        deleter, _label = deleter_entry
        try:
            deleter(item_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("music: favorite delete failed id=%s", item_id)
            return {"status": STATUS_ERROR, "message": f"刪除失敗：{exc}", "actions": []}
        text, markup, _ = renderer(page=0, mode=LIST_VIEW_MODE_EDIT)
        return {"status": STATUS_OK, "message": str(text or ""),
                "actions": self._markup_to_actions(markup)}

    # --- web console session memory (issue #32) --------------------------
    def _sessions(self) -> SessionMemoryStore:
        if self._session_store is None:
            with self._session_lock:
                if self._session_store is None:
                    self._session_store = SessionMemoryStore(
                        self.settings.openclaw_web_memory_dir
                    )
        return self._session_store

    def load_session(self) -> dict:
        """GET — the latest saved console snapshot, or an empty session. Never
        raises; a missing/corrupt/expired file falls back to a blank session."""
        try:
            return {"status": STATUS_OK, "session": self._sessions().load()}
        except Exception as exc:  # noqa: BLE001 — must never crash the API
            logger.exception("session memory: load failed")
            return {"status": STATUS_ERROR, "message": f"讀取 session 失敗：{exc}",
                    "session": empty_session()}

    def save_session(self, snapshot: object) -> dict:
        """POST — replace the saved snapshot. A failed write returns a
        structured error (the frontend keeps its in-memory conversation)."""
        try:
            stored = self._sessions().save(snapshot)
        except SessionWriteError as exc:
            return {"status": STATUS_ERROR, "message": f"儲存 session 失敗：{exc}"}
        except Exception as exc:  # noqa: BLE001
            logger.exception("session memory: save failed")
            return {"status": STATUS_ERROR, "message": f"儲存 session 失敗：{exc}"}
        return {"status": STATUS_OK, "updated_at": stored.get("updated_at")}

    def clear_session(self) -> dict:
        """DELETE — drop the saved snapshot (idempotent)."""
        try:
            self._sessions().clear()
        except Exception as exc:  # noqa: BLE001
            logger.exception("session memory: clear failed")
            return {"status": STATUS_ERROR, "message": f"清除 session 失敗：{exc}"}
        return {"status": STATUS_OK}

    def restart_all(self) -> dict:
        """Schedule a detached full local restart. The current HTTP request must
        return before the command bridge process is stopped by the script."""
        try:
            script_path = trigger_restart_all(settings=self.settings, source="web")
        except Exception as exc:  # noqa: BLE001
            logger.exception("restartall: scheduling failed")
            return {"status": STATUS_ERROR, "message": f"排程重啟失敗：{exc}"}
        logger.info("restartall: scheduled script=%s", script_path)
        return {"status": STATUS_OK, "message": RESTART_MESSAGE}

    def _stream_ollama_chat(self, prompt: str) -> Iterator[dict]:
        endpoint = self.settings.openclaw_local_text_endpoint.rstrip("/")
        model = self._local_model()
        ssl_ctx = build_ssl_context(self.settings) if endpoint.startswith("https://") else None
        url = endpoint if endpoint.endswith("/api/generate") else f"{endpoint}/api/generate"
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": True,
            "think": False,
            "options": {"temperature": 0.7},
        }
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/x-ndjson"},
            method="POST",
        )
        full: list[str] = []
        try:
            with urlopen(request, timeout=self.settings.openclaw_local_text_timeout_seconds,
                         context=ssl_ctx) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except ValueError:
                        continue
                    piece = chunk.get("response")
                    if piece:
                        full.append(piece)
                        yield stream_delta(piece)
                    if chunk.get("done"):
                        break
        except HTTPError as exc:
            yield stream_error(f"本地模型 HTTP {exc.code}。")
            return
        except URLError as exc:
            yield stream_error(f"本地模型無回應：{exc.reason}")
            return
        yield stream_done("".join(full).strip())

    def _stream_cloud_chat(self, prompt: str) -> Iterator[dict]:
        client = self._build_cloud_chat_client()
        if client is None:
            yield stream_error("cloud pickle 後端目前無法使用（OpenCode 未設定或無法連線）。")
            return
        # The cloud client is blocking; run it off-thread and emit heartbeats so
        # idle gaps don't trip client/proxy timeouts, then deliver as one delta.
        result: dict[str, object] = {}
        done = threading.Event()

        def _worker() -> None:
            try:
                result["text"] = client.generate(prompt, temperature=0.7)
            except Exception as exc:  # noqa: BLE001
                result["error"] = str(exc)
            finally:
                done.set()

        worker = threading.Thread(target=_worker, daemon=True)
        worker.start()
        try:
            while not done.wait(timeout=_HEARTBEAT_SECONDS):
                yield stream_heartbeat()
        except GeneratorExit:
            # Client disconnected (phone screen-lock / AbortController). Stop the
            # cloud model worker instead of letting it burn the full timeout —
            # the review flagged a runaway worker as the #30 gap.
            abort = getattr(client, "abort", None)
            if callable(abort):
                logger.info("command bridge: aborting cloud chat worker on disconnect")
                abort()
            raise
        if "error" in result:
            yield stream_error(f"cloud pickle 後端失敗：{result['error']}")
            return
        text = str(result.get("text") or "").strip()
        if text:
            yield stream_delta(text)
        yield stream_done(text)

    def _ollama_generate_blocking(self, prompt: str) -> str:
        from .dynamic_tools import OllamaTextClient

        client = OllamaTextClient(
            endpoint=self.settings.openclaw_local_text_endpoint,
            model=self._local_model(),
            timeout_seconds=self.settings.openclaw_local_text_timeout_seconds,
        )
        return client.generate(prompt, temperature=0.7)

    def _build_cloud_chat_client(self):
        """Big-pickle chat client: direct HTTP when an API key is configured,
        else the opencode CLI, else None when neither is usable."""
        from .dynamic_tools import (
            OpenCodeCliTextClient,
            OpenCodeTextClient,
            probe_opencode_cli,
        )

        raw_model = (self.settings.openclaw_opencode_model or "big-pickle").strip()
        if self.settings.openclaw_opencode_api_key:
            model = raw_model.split("/")[-1] if "/" in raw_model else raw_model
            return OpenCodeTextClient(
                base_url=self.settings.openclaw_opencode_base_url,
                model=model,
                api_key=self.settings.openclaw_opencode_api_key,
                timeout_seconds=180,
            )
        cli_model = raw_model if "/" in raw_model else f"opencode/{raw_model}"
        if probe_opencode_cli(model=cli_model, timeout=20.0):
            return OpenCodeCliTextClient(model=cli_model, timeout_seconds=180)
        return None

    def _local_model(self) -> str:
        return (self.settings.openclaw_local_text_model or "qwen3:14b").split(",")[0].strip()

    # --- translation -----------------------------------------------------
    def _handle_translation(self, req: WebCommandRequest) -> WebCommandResponse:
        if req.submode == SUBMODE_IMAGE_TRANSLATION or req.has_image_attachment:
            return WebCommandResponse(
                status=STATUS_UNSUPPORTED,
                message=_IMAGE_UNSUPPORTED_MSG,
                mode=MODE_TRANSLATION,
                submode=SUBMODE_IMAGE_TRANSLATION,
            )
        text = (req.input or "").strip()
        if not text:
            return WebCommandResponse(
                status=STATUS_ERROR,
                message="請輸入要翻譯的文字。",
                mode=MODE_TRANSLATION,
                submode=SUBMODE_TEXT_TRANSLATION,
            )
        message = self._run_command("/zh", text)
        return WebCommandResponse(
            status=STATUS_OK,
            message=message,
            mode=MODE_TRANSLATION,
            submode=SUBMODE_TEXT_TRANSLATION,
        )

    # --- investment ------------------------------------------------------
    def _handle_investment(self, req: WebCommandRequest) -> WebCommandResponse:
        if req.submode == SUBMODE_SELLER_REPUTATION_SNAPSHOT:
            return WebCommandResponse(
                status=STATUS_UNSUPPORTED,
                message=_SELLER_UNSUPPORTED_MSG,
                mode=MODE_INVESTMENT,
                submode=SUBMODE_SELLER_REPUTATION_SNAPSHOT,
            )
        if req.submode in (None, SUBMODE_DEEP_PRODUCT_RESEARCH):
            text = (req.input or "").strip()
            if not text:
                return WebCommandResponse(
                    status=STATUS_ERROR,
                    message="請貼上商品 URL 或輸入商品名稱。",
                    mode=MODE_INVESTMENT,
                    submode=SUBMODE_DEEP_PRODUCT_RESEARCH,
                )
            message = self._run_command("/research", text)
            return WebCommandResponse(
                status=STATUS_OK,
                message=message,
                mode=MODE_INVESTMENT,
                submode=SUBMODE_DEEP_PRODUCT_RESEARCH,
            )
        return WebCommandResponse(
            status=STATUS_UNSUPPORTED,
            message=f"投資研究子模式尚未支援：{req.submode}",
            mode=MODE_INVESTMENT,
            submode=req.submode,
        )
