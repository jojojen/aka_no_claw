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
import re
import threading
import time
from collections.abc import Iterator
from urllib.parse import quote
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from assistant_runtime import AssistantSettings, build_ssl_context

from .job_store import JobStore
from .session_memory import SessionMemoryStore, SessionWriteError, empty_session
from .service_restart import RESTART_MESSAGE, trigger_restart_all
from .command_bridge_models import (
    CHAT_BACKEND_CLOUD_MISTRAL,
    CHAT_BACKEND_CLOUD_PICKLE,
    CHAT_BACKEND_CLOUD_POOL,
    CHAT_BACKEND_GEMINI,
    CHAT_BACKEND_LOCAL,
    CHAT_TOOL_BLUETOOTH,
    CHAT_TOOL_IR,
    CHAT_TOOL_MUSIC,
    CHAT_TOOL_SEARCH,
    MUSIC_ACTION_PLAN,
    ChatToolPolicy,
    ChatToolRequest,
    ChatToolResult,
    ChatTurn,
    MODE_CHAT,
    MODE_INVESTMENT,
    MODE_TRANSLATION,
    ModelAttempt,
    ModelMetadata,
    MusicIntent,
    ROUTER_DECISION_TOOL,
    RouterDecision,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNSUPPORTED,
    SUBMODE_DEEP_PRODUCT_RESEARCH,
    SUBMODE_IMAGE_TRANSLATION,
    SUBMODE_SELLER_REPUTATION_SNAPSHOT,
    SUBMODE_TEXT_TRANSLATION,
    WebCommandRequest,
    WebCommandResponse,
    make_chat_tool_request,
    parse_router_decision,
    stream_delta,
    stream_done,
    stream_error,
    stream_heartbeat,
    stream_redirect,
    stream_start,
)
from .task_loop import (
    BoundedTaskLoop,
    ContinuationState,
    LoopContext,
    StepOutcome,
    resume_loop,
)

logger = logging.getLogger(__name__)

_BRIDGE_CHAT_ID = "web-bridge"
# Fixed chat id for the single-user web workflow editor. The editor keys draft
# sessions by chat id; the web console is one user, so a constant id lets a
# draft survive across the separate HTTP requests of a draft → edit → save flow.
_WF_WEB_CHAT_ID = "web-workflow"
_SH_WEB_CHAT_ID = "web-schedule"
_HEARTBEAT_SECONDS = 10.0
# Finished jobs linger this long so a phone that reconnects after a screen-lock
# can still fetch the final report, then they are garbage-collected.
_JOB_TTL_SECONDS = 1800.0

_SELLER_UNSUPPORTED_MSG = "賣家信譽快照目前尚未由本地 command bridge 支援。"

# Bridge-specific catch for natural "工作流" phrases like
# "幫我做一個先問候再開燈的工作流" that telegram_nl misclassifies
# as play_music because "開燈" fires other detection before the workflow check.
_WF_BRIDGE_VERB_RE = re.compile(r"做|建立?|弄|規劃|設計|create", re.IGNORECASE)

# Bridge-specific catch for "排程 + creation verb" phrases like "幫我建立排程" or
# "排程執行 greeting_workflow" that don't need the full embedding path.
_SH_BRIDGE_RE = re.compile(
    r"(新增|建立|設定|排定|幫我).{0,10}排程|排程.{0,5}(執行|跑)",
    re.IGNORECASE,
)

# Match slug-like words (lowercase + digits + underscore) containing an underscore.
# Used to extract a workflow_id from a free-text schedule phrase.
_WF_SLUG_RE = re.compile(r"\b([a-z][a-z0-9_\-]{2,})\b")

# Web Chat continuity (#44). Prepended so the model continues the conversation
# (e.g. resolves 「她/它/這個」 against earlier turns) instead of treating each
# message as a one-shot. Blocking and streaming chat share build_chat_prompt so
# the two paths can't drift.
_CHAT_SYSTEM_PROMPT = (
    "你是 aka_no_claw 的本機聊天助理。下面是這段對話最近的內容，"
    "請延續上下文回答使用者最新的訊息（例如代名詞「她／它／這個」指的是先前提到的主題），"
    "並以繁體中文自然作答。"
)
_CHAT_ROLE_LABELS = {"user": "使用者", "assistant": "助理", "system": "系統"}
_MODEL_STATUS_OK = "ok"
_MODEL_STATUS_ERROR = "error"
_MODEL_STATUS_NOT_CONFIGURED = "not_configured"
_MODEL_STATUS_QUOTA_EXHAUSTED = "quota_exhausted"
_MODEL_STATUS_RATE_LIMITED = "rate_limited"
_GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"


class _GeminiRequestError(RuntimeError):
    def __init__(self, message: str, *, status: str = _MODEL_STATUS_ERROR) -> None:
        super().__init__(message)
        self.status = status


class _GeminiTextClient:
    """Minimal Google Gemini generateContent client for web chat."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: int,
        ssl_context: object | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = max(1, timeout_seconds)
        self.ssl_context = ssl_context

    def generate(self, prompt: str, *, temperature: float = 0.7) -> str:
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature},
        }
        url = (
            f"{_GEMINI_API_BASE}/models/{quote(self.model, safe='')}:generateContent"
            f"?key={quote(self.api_key, safe='')}"
        )
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds, context=self.ssl_context) as response:
                body = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:800]
            except Exception:
                detail = ""
            raise _GeminiRequestError(
                f"Gemini HTTP {exc.code}: {detail}",
                status=_gemini_http_status(exc.code, detail),
            ) from exc
        except URLError as exc:
            raise _GeminiRequestError(
                f"Gemini request failed: {exc.reason}", status=_MODEL_STATUS_ERROR
            ) from exc
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise _GeminiRequestError(
                "Gemini returned invalid JSON", status=_MODEL_STATUS_ERROR
            ) from exc
        text = _extract_gemini_text(data)
        if not text:
            raise _GeminiRequestError("Gemini returned no text", status=_MODEL_STATUS_ERROR)
        return text


def _gemini_http_status(code: int, detail: str) -> str:
    lowered = detail.lower()
    if code == 429 or "resource_exhausted" in lowered or "quota" in lowered:
        return _MODEL_STATUS_QUOTA_EXHAUSTED
    if code == 403 and ("rate" in lowered or "quota" in lowered):
        return _MODEL_STATUS_RATE_LIMITED
    return _MODEL_STATUS_ERROR


def _is_gemini_fallback_status(status: str) -> bool:
    return status in {_MODEL_STATUS_QUOTA_EXHAUSTED, _MODEL_STATUS_RATE_LIMITED}


def _extract_gemini_text(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    parts: list[str] = []
    for candidate in data.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        for part in content.get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
    return "".join(parts).strip()

# Web Chat contextual tool routing (#45). A local router LLM decides, per chat
# turn, whether to answer directly or call a closed allowlist of tools.
# The router gets the recent history so it can resolve pronouns into a
# self-contained search query (e.g. 「她的歌」→「初音未來 歌曲」). It must emit a
# single strict-JSON object; anything else is treated as "direct" (fail soft).
_ROUTER_TIMEOUT_CAP_SECONDS = 30
_ROUTER_SYSTEM_PROMPT_TEMPLATE = (
    "你是 aka_no_claw 聊天助理的路由器。根據對話判斷要不要使用工具來回答使用者的『最新訊息』。\n"
    "可用工具：\n{tool_lines}\n"
    "其他情況（閒聊、改寫、翻譯、一般常識、可由上文直接回答）一律 direct，不要用工具。\n"
    "只輸出一個 JSON 物件，不要加任何多餘文字或說明：\n"
    '{{"decision":"direct|tool","tool":"{tool_choices}","query":"...","reason_summary":"..."}}\n'
    "當 decision=tool 時，query 必須是該工具可直接執行的參數；"
    "若使用者用代名詞（她／它／這個），請用對話紀錄把主詞補回 query。"
)
_SEARCH_SYNTHESIS_PROMPT = (
    "你是 aka_no_claw 聊天助理。請根據下面的網路搜尋結果，用繁體中文回答使用者的問題。\n"
    "規則：\n"
    "1. 只根據提供的來源作答，不要編造來源裡沒有的事實。\n"
    "2. 若來源不足以回答，請誠實說明，不要硬掰。\n"
    "3. 引用具體資訊時可標註對應的來源編號 [n]。\n"
    "4. 回答精簡自然，不要整段照抄摘要。"
)
# Tool-usage indicators the user sees directly in the chat (#45). Two layers:
#  - a LIVE "正在調用…工具中" notice streamed before the tool runs, so the user
#    can see a tool is being invoked while they wait; and
#  - a persistent "已使用工具" banner on the finished answer, so the call is
#    still evident after streaming completes. Both are always on (never gated by
#    the debug flag) — only the synthesis-model label stays behind that flag.
_TOOL_USED_PREFIX = "🔧 已使用工具："
_TOOL_FRIENDLY_NAMES = {
    CHAT_TOOL_SEARCH: "網路搜尋",
    CHAT_TOOL_MUSIC: "音樂控制",
    CHAT_TOOL_BLUETOOTH: "藍牙控制",
    CHAT_TOOL_IR: "紅外線控制",
}

# Search snippets are external (search-engine) text fed into the final synthesis
# LLM, so they are budgeted before entering the prompt: per-field caps bound any
# single title/snippet, and a total cap bounds the whole pack. This protects
# prompt size and shrinks the prompt-injection surface from upstream snippets.
# URLs are NOT truncated (a clipped URL is useless) — the visible sources block
# always carries the full URL.
_SOURCE_PACK_TITLE_CAP = 200
_SOURCE_PACK_SNIPPET_CAP = 500
_SOURCE_PACK_TOTAL_CAP = 4000

# Central chat tool registry (#46): maps tool name → (policy, executor).
# The executor signature is (bridge, ChatToolRequest) → ChatToolResult.
# Adding a new tool means: (a) whitelist it in command_bridge_models.CHAT_TOOLS,
# (b) add an entry here. No other file needs to change.
_SEARCH_TOOL_POLICY = ChatToolPolicy(
    display_name="網路搜尋",
    max_query_chars=256,
    max_source_field_chars=_SOURCE_PACK_SNIPPET_CAP,
    max_source_pack_chars=_SOURCE_PACK_TOTAL_CAP,
)
_MUSIC_TOOL_POLICY = ChatToolPolicy(display_name="音樂控制", max_query_chars=128)
_BLUETOOTH_TOOL_POLICY = ChatToolPolicy(display_name="藍牙控制", max_query_chars=128)
_IR_TOOL_POLICY = ChatToolPolicy(display_name="紅外線控制", max_query_chars=128)


def _clip(text: str, cap: int) -> str:
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    return text[:cap].rstrip() + "…"


def _tool_calling_notice(tool: str) -> str:
    name = _TOOL_FRIENDLY_NAMES.get(tool, tool)
    return f"🔧 正在調用「{name}（{tool}）」工具中…"


def build_chat_prompt(user_input: str, history: tuple[ChatTurn, ...] = ()) -> str:
    """Assemble the chat prompt from recent history + the current input.

    With no history this is just the bare input (back-compat with the old
    stateless behaviour). With history it becomes ``system + recent turns +
    current input`` as a single string, which works for both the local Ollama
    and cloud-pickle backends. Server-side trimming/sanitization already happened
    in parse_request; this only formats."""
    user_input = (user_input or "").strip()
    if not history:
        return user_input
    lines = [_CHAT_SYSTEM_PROMPT, "", "對話紀錄："]
    for turn in history:
        label = _CHAT_ROLE_LABELS.get(turn.role, turn.role)
        lines.append(f"{label}：{turn.content}")
    lines += ["", f"使用者：{user_input}", "助理："]
    return "\n".join(lines)

_NO_IMAGE_MSG = "請附上要翻譯的圖片。"
_BAD_IMAGE_TYPE_MSG = "不支援的檔案類型，請改用 JPG / PNG / WEBP / GIF 等圖片格式。"
_SUPPORTED_IMAGE_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".heif",
)
_IMAGE_SUFFIX_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}

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


def _is_supported_image(att) -> bool:
    """Whether an image attachment is a format the OCR pipeline can open. Trust an
    explicit content_type when present (must be image/*); otherwise fall back to
    the filename extension."""
    ct = (att.content_type or "").strip().lower()
    if ct:
        return ct.startswith("image/")
    name = (att.filename or "").lower()
    return any(name.endswith(ext) for ext in _SUPPORTED_IMAGE_EXTENSIONS)


def _image_temp_suffix(att) -> str:
    name = (att.filename or "").lower()
    for ext in _SUPPORTED_IMAGE_EXTENSIONS:
        if name.endswith(ext):
            return ext
    ct = (att.content_type or "").strip().lower()
    return _IMAGE_SUFFIX_BY_CONTENT_TYPE.get(ct, ".img")


class _WorkflowShimRunner:
    """Local-only runner for the web workflow surface.

    The web bridge needs enough of the ``DynamicToolRunner`` protocol for both
    workflow authoring and execution:
      - ``tools_dir`` for workflow_store path derivation
      - ``catalog`` so NL drafting grounds on real generated-tool slugs
      - ``client`` as the local drafting fallback
      - ``run_tool_step`` so ``/workflow run`` works from web

    Keep this shim local-only: never enable cloud-failover restart side effects
    from the web bridge process."""

    def __init__(self, settings: AssistantSettings) -> None:
        from .dynamic_tools import DynamicToolRunner, OllamaTextClient, _resolve_tools_dir

        self.tools_dir = _resolve_tools_dir()
        self.client = OllamaTextClient(
            endpoint=settings.openclaw_local_text_endpoint,
            model=(settings.openclaw_local_text_model or "qwen3:14b").split(",")[0].strip(),
            timeout_seconds=settings.openclaw_local_text_timeout_seconds,
        )
        self._tool_runner = DynamicToolRunner(
            client=self.client,
            tools_dir=self.tools_dir,
            knowledge_db=None,
            fast_model=self.client.model,
            strong_model=self.client.model,
            cloud_failover_restart=False,
            distill_enabled=False,
        )
        self.catalog = self._tool_runner.catalog

    def run_tool_step(self, slug: str, explicit_params: dict) -> tuple[bool, str]:
        return self._tool_runner.run_tool_step(slug, explicit_params)


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
        self._image_renderer = None
        self._image_renderer_built = False
        self._image_renderer_lock = threading.Lock()
        # #51 PR3: per-conversation paused music plans awaiting a track choice.
        # Maps a conversation key -> {"state": ContinuationState dict, "candidates": [names]}.
        # In-process only: a resume is a follow-up turn within the same bridge run.
        self._music_continuations: dict[str, dict] = {}
        self._music_cont_lock = threading.Lock()
        # #53: workflow surface for web chat (NL draft + editable card buttons).
        # Built lazily — the shared editor must persist draft sessions across the
        # separate HTTP requests that a draft → reorder → save flow spans.
        self._workflow_handler: object | None = None
        self._workflow_editor: object | None = None
        self._workflow_lock = threading.Lock()
        # Embedding intent fast-path for workflow creation redirect (#web8 B2).
        # Built lazily on first chat message; None means disabled (no embedder).
        self._intent_fp: object | None = None
        self._intent_fp_built = False
        self._intent_fp_lock = threading.Lock()
        # Schedule surface for web chat (web#9). Lazily built so the store
        # singleton is shared with any Telegram-side scheduler that also runs.
        self._sh_handler: object | None = None
        self._sh_cb_handler: object | None = None
        self._sh_store: object | None = None
        self._sh_lock = threading.Lock()

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

    def _get_intent_fast_path(self):
        if not self._intent_fp_built:
            with self._intent_fp_lock:
                if not self._intent_fp_built:
                    from .intent_fast_path import build_intent_fast_path
                    self._intent_fp = build_intent_fast_path(self.settings)
                    self._intent_fp_built = True
        return self._intent_fp

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
        """Convert a Telegram inline_keyboard to web action buttons, preserving
        the row index so the frontend can re-group buttons into their original
        keyboard rows (avoids misaligned layouts on multi-entry lists)."""
        actions: list[dict] = []
        if isinstance(markup, dict):
            for row_idx, row in enumerate(markup.get("inline_keyboard", [])):
                for btn in row:
                    if not isinstance(btn, dict):
                        continue
                    cb = btn.get("callback_data")
                    label = btn.get("text")
                    if cb and label:
                        actions.append({"label": str(label), "callback_data": str(cb), "row": row_idx})
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
                yield stream_done(response.message, model_metadata=response.model_metadata)
        except Exception as exc:  # noqa: BLE001
            logger.exception("command bridge stream failed mode=%s", req.mode)
            yield stream_error(f"後端處理失敗：{exc}")

    # --- chat ------------------------------------------------------------
    def _handle_chat_blocking(self, req: WebCommandRequest) -> WebCommandResponse:
        text = (req.input or "").strip()
        if not text:
            return WebCommandResponse(
                status=STATUS_ERROR, message="請輸入訊息。", mode=MODE_CHAT
            )
        # #51 PR3: if this conversation has a paused music plan and the user's
        # message names one of the offered tracks, resume the loop (play that
        # track) instead of routing — the live resume client for the bounded loop.
        resumed = self._maybe_resume_music_plan(req, text)
        if resumed is not None:
            return resumed
        decision = self._route_chat_decision(req)
        if decision is not None and decision.decision == ROUTER_DECISION_TOOL:
            try:
                tool_result = self._run_chat_tool(req, decision)
                logger.info(
                    "[chat-tool] tool=%s sources=%d summary=%r",
                    decision.tool, tool_result.source_count, tool_result.result_summary,
                )
                return WebCommandResponse(
                    status=STATUS_OK,
                    message=tool_result.answer,
                    mode=MODE_CHAT,
                    model_metadata=tool_result.model_metadata,
                )
            except Exception as exc:  # noqa: BLE001 — surface, don't crash the turn
                logger.exception("chat tool failed tool=%s", decision.tool)
                return WebCommandResponse(
                    status=STATUS_ERROR,
                    message=f"工具執行失敗：{exc}",
                    mode=MODE_CHAT,
                )
        # Direct chat (router said direct, or its output was untrusted/unavailable).
        prompt = build_chat_prompt(req.input, req.history)
        if req.chat_backend == CHAT_BACKEND_CLOUD_PICKLE:
            client = self._build_cloud_chat_client()
            if client is None:
                return WebCommandResponse(
                    status=STATUS_ERROR,
                    message="cloud pickle 後端目前無法使用（OpenCode 未設定或無法連線）。",
                    mode=MODE_CHAT,
                )
            message = client.generate(prompt, temperature=0.7)
            metadata = self._model_metadata_for_backend(
                req.chat_backend,
                (ModelAttempt("opencode", self._big_pickle_model(), _MODEL_STATUS_OK),),
                "opencode",
                self._big_pickle_model(),
            )
        elif req.chat_backend == CHAT_BACKEND_CLOUD_MISTRAL:
            client = self._build_mistral_chat_client()
            if client is None:
                return WebCommandResponse(
                    status=STATUS_ERROR,
                    message="Mistral 後端目前無法使用（未設定 MISTRAL_API_KEY）。",
                    mode=MODE_CHAT,
                )
            message = client.generate(prompt, temperature=0.7)
            metadata = self._model_metadata_for_backend(
                req.chat_backend,
                (ModelAttempt("mistral", self._mistral_model(), _MODEL_STATUS_OK),),
                "mistral",
                self._mistral_model(),
            )
        elif req.chat_backend == CHAT_BACKEND_GEMINI:
            message, metadata = self._generate_gemini_with_fallback(prompt, temperature=0.7)
        elif req.chat_backend == CHAT_BACKEND_CLOUD_POOL:
            message, metadata = self._handle_cloud_pool_blocking(prompt)
        else:
            message = self._ollama_generate_blocking(prompt)
            metadata = self._model_metadata_for_backend(
                req.chat_backend,
                (ModelAttempt("local", self._local_model(), _MODEL_STATUS_OK),),
                "local",
                self._local_model(),
            )
        return WebCommandResponse(
            status=STATUS_OK, message=message, mode=MODE_CHAT, model_metadata=metadata
        )

    def _stream_chat(self, req: WebCommandRequest) -> Iterator[dict]:
        text = (req.input or "").strip()
        if not text:
            yield stream_error("請輸入訊息。")
            return
        # Workflow / schedule creation redirect (web#8 B2, web#9). Three layers so
        # the feature works even when the bge-m3 embedder is unavailable:
        #   1. Embedding fast-path (primary): bge-m3 cosine over phrasings JSON.
        #   3a. Bridge "工作流 + verb" catch (before telegram_nl to avoid misclassification).
        #   3b. Bridge "排程 + creation verb" catch for schedule creation.
        #   4. NL-rule fallback (secondary): telegram_nl keyword rules (create workflow only).
        # All layers emit stream_redirect and return before the LLM router runs.
        fp = self._get_intent_fast_path()
        if fp is not None:
            wf_intent = fp.route(text)
            if wf_intent is not None and wf_intent.intent == "create_workflow":
                yield stream_redirect(
                    "create_workflow",
                    wf_intent.workflow_description or text,
                )
                return
            if wf_intent is not None and wf_intent.intent == "create_schedule":
                yield stream_redirect(
                    "create_schedule", text,
                    workflow_id=self._extract_wf_slug(text),
                )
                return
        # Layer 3a: bridge "工作流 + verb" catch.
        if "工作流" in text and _WF_BRIDGE_VERB_RE.search(text):
            yield stream_redirect("create_workflow", text)
            return
        # Layer 3b: bridge "排程 + creation verb" catch.
        if _SH_BRIDGE_RE.search(text):
            yield stream_redirect(
                "create_schedule", text,
                workflow_id=self._extract_wf_slug(text),
            )
            return
        from .natural_language import fallback_route_openclaw_natural_language
        nl_intent = fallback_route_openclaw_natural_language(text)
        if nl_intent is not None and nl_intent.intent == "create_workflow":
            yield stream_redirect(
                "create_workflow",
                nl_intent.workflow_description or text,
            )
            return
        decision = self._route_chat_decision(req)
        if decision is not None and decision.decision == ROUTER_DECISION_TOOL:
            yield from self._stream_chat_tool(req, decision)
            return
        prompt = build_chat_prompt(req.input, req.history)
        if req.chat_backend == CHAT_BACKEND_CLOUD_PICKLE:
            yield from self._stream_cloud_chat(prompt)
        elif req.chat_backend == CHAT_BACKEND_CLOUD_MISTRAL:
            yield from self._stream_mistral_chat(prompt)
        elif req.chat_backend == CHAT_BACKEND_GEMINI:
            yield from self._stream_gemini_chat(prompt)
        elif req.chat_backend == CHAT_BACKEND_CLOUD_POOL:
            yield from self._stream_cloud_pool_chat(prompt)
        else:
            yield from self._stream_ollama_chat(prompt)

    # --- bounded music plan (#50) --------------------------------------------
    def _exec_music_intent(
        self, req: WebCommandRequest, intent: MusicIntent
    ) -> WebCommandResponse:
        """Dispatch a bounded music plan intent.

        The PLAN action (#50) runs a bounded multi-step flow that inspects the
        local library and searches for external popularity context before playing.
        Never executes arbitrary slash commands — only the closed MUSIC_ACTION_*
        set is reachable here."""
        logger.info(
            "[music-intent] action=%s query=%r qualifier=%r",
            intent.action, intent.query, intent.qualifier,
        )
        if intent.action == MUSIC_ACTION_PLAN:
            return self._exec_music_plan(req, intent)
        raise ValueError(f"unknown music action: {intent.action!r}")

    def _exec_music_plan(
        self, req: WebCommandRequest, intent: MusicIntent
    ) -> WebCommandResponse:
        """Bounded multi-tool plan (#50), now driven through the #51 BoundedTaskLoop.

        The four allowlisted steps — inspect → search → match → play — run under a
        hard step budget. When the match is unambiguous the loop plays and ends;
        when it is ambiguous (zero or several web-confirmed matches) the loop stops
        without playing and the bridge emits a resumable :class:`ContinuationState`
        (persisted per-conversation) plus track-choice buttons, so a follow-up turn
        resumes *at the play step* without re-running inspect/search/match.

        Only local songs confirmed by web results are ever played; arbitrary slash
        commands are unreachable — the play step calls ``run_music_command`` with a
        local title only."""
        artist = intent.query
        qualifier = intent.qualifier
        scratch: dict = {
            "artist": artist,
            "qualifier": qualifier,
            "trace": [f"Goal: play a {qualifier} song by {artist!r} available locally"],
            "selection": None,
            "matched": [],
            "local_candidates": [],
            "abort": None,
            "play_result": None,
        }
        loop = BoundedTaskLoop(
            f"play a {qualifier} song by {artist!r} available locally",
            steps=self._music_plan_steps(scratch),
            decider=self._music_plan_decider(scratch),
            max_steps=4,
            constraints="play only web-confirmed local tracks; no arbitrary commands",
        )
        result = loop.run()
        trace = scratch["trace"]

        def with_trace(body: str) -> str:
            return body + "\n\n" + "\n".join(trace)

        if scratch["abort"]:
            return WebCommandResponse(
                status=STATUS_OK, message=with_trace(scratch["abort"]), mode=MODE_CHAT
            )
        if result.done:
            play_result = scratch["play_result"] or {}
            return WebCommandResponse(
                status=play_result.get("status", STATUS_OK),
                message=with_trace(f"🎵 {play_result.get('message', '')}"),
                mode=MODE_CHAT,
            )
        # Loop stopped before playing → disambiguation needed. Persist a resumable
        # continuation (next action = play) and offer the candidate tracks.
        matched = scratch["matched"]
        local_candidates = scratch["local_candidates"]
        if not matched:
            candidates = local_candidates[:5]
            head = (
                f"找到以下「{artist}」歌曲，但無法從搜尋結果確認哪首最{qualifier}：\n"
                + "、".join(c.get("name", "?") for c in candidates)
                + "\n\n請問您想播哪一首？"
            )
        else:
            candidates = matched[:5]
            head = (
                f"找到多首「{artist}」{qualifier}候選歌曲：\n"
                + "、".join(c.get("name", "?") for c in candidates)
                + "\n\n請問您想播哪一首？"
            )
        names = [c.get("name", "?") for c in candidates]
        state = self._music_continuation_state(scratch, result, names)
        self._store_music_continuation(req, state, names)
        # Resume is driven by the user's next message naming a track (see
        # _maybe_resume_music_plan), so no buttons are attached here.
        return WebCommandResponse(
            status=STATUS_OK, message=with_trace(head), mode=MODE_CHAT
        )

    def _music_plan_steps(self, scratch: dict) -> dict:
        """Allowlisted steps for the music plan loop, sharing ``scratch`` for the
        data the linear StepOutcome string cannot carry between steps."""
        from .music_command import _search, load_or_build_index
        from .web_search import DEFAULT_WEB_SEARCH_LIMIT, web_search

        artist = scratch["artist"]
        qualifier = scratch["qualifier"]
        trace = scratch["trace"]

        def inspect(ctx: LoopContext) -> StepOutcome:
            trace.append(f"Task 1: inspect local music candidates for {artist!r}")
            try:
                index = load_or_build_index(
                    self.settings.openclaw_music_dir,
                    self.settings.openclaw_music_index_path,
                )
                local_sr = _search(index.entries, artist)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[music-plan] index load failed: %s", exc)
                index = None
                local_sr = None
            if index is None or not index.entries:
                trace.append("Task 1 result: local music library is empty or unavailable")
                scratch["abort"] = f"本地音樂庫中找不到「{artist}」的歌曲。"
                return StepOutcome(observation="local library empty", failed=True)
            if local_sr is not None and local_sr.kind != "none":
                local_candidates = (
                    [local_sr.entry]
                    if local_sr.kind in ("exact", "single") and local_sr.entry
                    else list(local_sr.candidates)
                )
            else:
                local_candidates = list(index.entries)
            scratch["local_candidates"] = local_candidates
            trace.append(
                f"Task 1 result: {len(local_candidates)} local candidate(s): "
                + "、".join(c.get("name", "?") for c in local_candidates[:5])
            )
            return StepOutcome(observation=f"{len(local_candidates)} local candidate(s)")

        def search(ctx: LoopContext) -> StepOutcome:
            query = f"{artist} {qualifier} シングル"
            trace.append(f"Task 2: search for {query!r}")
            try:
                web_results = web_search(
                    query, max_results=DEFAULT_WEB_SEARCH_LIMIT, reuse_browser=False
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[music-plan] web search failed: %s", exc)
                web_results = []
            scratch["web_results"] = web_results
            trace.append(f"Task 2 result: {len(web_results)} web result(s)")
            return StepOutcome(observation=f"{len(web_results)} web result(s)")

        def match(ctx: LoopContext) -> StepOutcome:
            trace.append("Task 3: match search results against local candidates")
            matched = self._rank_local_by_web_mentions(
                scratch["local_candidates"], scratch.get("web_results", [])
            )
            scratch["matched"] = matched
            trace.append(
                f"Task 3 result: {len(matched)} matched: "
                + "、".join(c.get("name", "?") for c in matched[:5])
            )
            return StepOutcome(observation=f"{len(matched)} matched")

        def play(ctx: LoopContext) -> StepOutcome:
            selection = scratch.get("selection") or scratch["matched"][0]["name"]
            trace.append(f"Task 4: play selected local match: {selection!r}")
            play_result = self.run_music_command(selection)
            scratch["play_result"] = play_result
            return StepOutcome(observation=f"played {selection}", done=True)

        return {"inspect": inspect, "search": search, "match": match, "play": play}

    @staticmethod
    def _music_plan_decider(scratch: dict):
        """Deterministic decider: walk inspect→search→match, then play only when a
        single track is resolvable (one web-confirmed match, or a user selection on
        resume). Returns ``""`` to pause for the user when the match is ambiguous."""
        def decide(ctx: LoopContext) -> str:
            if scratch.get("abort"):
                return ""
            done = {c.split(":", 1)[0] for c in ctx.completed}
            for action in ("inspect", "search", "match"):
                if action not in done:
                    return action
            if "play" in done:
                return ""
            if scratch.get("selection") or len(scratch.get("matched", [])) == 1:
                return "play"
            return ""
        return decide

    @staticmethod
    def _music_continuation_state(
        scratch: dict, result, candidate_names: list[str]
    ) -> ContinuationState:
        """Build the resumable snapshot for a paused music plan: completed steps so
        far, next action = play, and the offered tracks as the stop anchor."""
        completed = list(result.state.completed) if result.state else []
        return ContinuationState(
            goal=scratch["trace"][0].removeprefix("Goal: "),
            constraints="play only an offered local track",
            completed=completed,
            current_status=f"{len(scratch.get('matched', []))} web-confirmed match(es); awaiting user choice",
            attempted_fixes=[],
            budget={"steps_used": len(completed), "steps_limit": 4},
            next_action="play",
            stop_condition="awaiting user track selection from: " + "、".join(candidate_names),
        )

    @staticmethod
    def _conversation_key(req: WebCommandRequest) -> str:
        return req.conversation_id or req.session_id or "_default"

    def _store_music_continuation(
        self, req: WebCommandRequest, state: ContinuationState, candidates: list[str]
    ) -> None:
        with self._music_cont_lock:
            self._music_continuations[self._conversation_key(req)] = {
                "state": state.to_dict(),
                "candidates": list(candidates),
            }

    def _maybe_resume_music_plan(
        self, req: WebCommandRequest, text: str
    ) -> WebCommandResponse | None:
        """If a paused music plan exists for this conversation and ``text`` names an
        offered track, resume the loop at the play step. Returns ``None`` otherwise
        so normal routing proceeds untouched."""
        key = self._conversation_key(req)
        with self._music_cont_lock:
            entry = self._music_continuations.get(key)
        if not entry or text not in entry["candidates"]:
            return None
        return self._resume_music_plan(req, text)

    def _resume_music_plan(
        self, req: WebCommandRequest, selection: str
    ) -> WebCommandResponse:
        """Resume a paused music plan: play the user-chosen track via the bounded
        loop's resume path, so inspect/search/match are NOT re-run. The selection
        is validated against the offered candidates (guardrail: only an offered
        local title is playable)."""
        key = self._conversation_key(req)
        with self._music_cont_lock:
            entry = self._music_continuations.get(key)
        if not entry:
            return WebCommandResponse(
                status=STATUS_ERROR, message="沒有可續播的音樂計畫。", mode=MODE_CHAT
            )
        if selection not in entry["candidates"]:
            return WebCommandResponse(
                status=STATUS_ERROR,
                message=f"「{selection}」不在候選清單中，無法播放。",
                mode=MODE_CHAT,
            )
        state = ContinuationState.from_dict(entry["state"])
        scratch: dict = {
            "artist": "",
            "qualifier": "",
            "selection": selection,
            "matched": [{"name": selection}],
            "local_candidates": [],
            "abort": None,
            "trace": [f"Goal: {state.goal}"],
            "play_result": None,
        }
        loop = BoundedTaskLoop(
            state.goal,
            steps=self._music_plan_steps(scratch),
            decider=self._music_plan_decider(scratch),
            max_steps=4,
        )
        resume_loop(loop, state)
        with self._music_cont_lock:
            self._music_continuations.pop(key, None)
        play_result = scratch["play_result"] or {}
        return WebCommandResponse(
            status=play_result.get("status", STATUS_OK),
            message=f"🎵 {play_result.get('message', '')}",
            mode=MODE_CHAT,
        )

    @staticmethod
    def _rank_local_by_web_mentions(
        local_candidates: list[dict], web_results: list
    ) -> list[dict]:
        """Rank local songs by mentions in web search result titles/snippets.

        Only songs that appear at least once in the web text are returned, ordered
        by mention count descending (most-mentioned = most-popular proxy)."""
        from .music_command import _normalize

        web_text = " ".join(
            (_normalize(r.title or "") + " " + _normalize(r.snippet or ""))
            for r in web_results
        )
        scored: list[tuple[int, dict]] = []
        for c in local_candidates:
            norm_name = _normalize(c.get("name", ""))
            if norm_name and norm_name in web_text:
                count = web_text.count(norm_name)
                scored.append((count, c))
        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored]

    # --- chat tool routing (#45) -----------------------------------------
    def _route_chat_decision(self, req: WebCommandRequest) -> RouterDecision | None:
        """Ask the local router LLM whether this chat turn needs a tool.

        Returns ``None`` (→ direct chat) when the router is unavailable, times
        out, or emits anything untrusted — routing must never block a plain
        answer. A trusted ``direct`` or ``tool`` decision is logged for
        debugging and returned."""
        try:
            raw = self._generate_router_json(self._build_router_prompt(req))
        except Exception:  # noqa: BLE001 — router is best-effort; fall back to direct
            logger.warning(
                "[chat-route] router LLM unavailable; direct fallback", exc_info=True
            )
            return None
        decision = parse_router_decision(raw)
        if decision is None:
            logger.info(
                "[chat-route] untrusted router output; direct fallback raw=%r",
                (raw or "")[:200],
            )
            return None
        logger.info(
            "[chat-route] decision=%s tool=%s query=%r reason=%s",
            decision.decision, decision.tool, decision.query, decision.reason_summary,
        )
        return decision

    def _build_router_prompt(self, req: WebCommandRequest) -> str:
        lines = [self._router_system_prompt(), "", "對話紀錄："]
        for turn in req.history:
            label = _CHAT_ROLE_LABELS.get(turn.role, turn.role)
            lines.append(f"{label}：{turn.content}")
        lines += ["", f"使用者最新訊息：{(req.input or '').strip()}", "", "JSON："]
        return "\n".join(lines)

    def _router_system_prompt(self) -> str:
        tools = [CHAT_TOOL_SEARCH]
        tool_lines = [
            "- /search：當回答需要即時、最新或你不確定的事實資訊時使用；query 是適合搜尋引擎的完整查詢。",
        ]
        for tool in (CHAT_TOOL_MUSIC, CHAT_TOOL_BLUETOOTH, CHAT_TOOL_IR):
            line = self._registered_chat_tool_prompt_line(tool)
            if not line:
                continue
            tools.append(tool)
            tool_lines.append(line)
        tool_choices = "|".join(t.split("/", 1)[-1] for t in tools)
        tool_choices = "/" + "|/".join(tool_choices.split("|"))
        return _ROUTER_SYSTEM_PROMPT_TEMPLATE.format(
            tool_lines="\n".join(tool_lines),
            tool_choices=tool_choices,
        )

    def _registered_command_usage(self, command: str) -> str:
        try:
            registered = self._handlers().get(command)
        except Exception:  # noqa: BLE001
            logger.debug("router prompt: command registry unavailable for %s", command, exc_info=True)
            return ""
        return str(getattr(registered, "usage", "") or "").strip()

    def _registered_chat_tool_prompt_line(self, command: str) -> str:
        try:
            registered = self._handlers().get(command)
        except Exception:  # noqa: BLE001
            logger.debug("router prompt: command registry unavailable for %s", command, exc_info=True)
            return ""
        if registered is None:
            return ""
        usage = str(getattr(registered, "usage", "") or "").strip()
        if not usage:
            return ""
        purpose = str(getattr(registered, "chat_tool_purpose", "") or "").strip()
        query_hint = str(getattr(registered, "chat_tool_query_hint", "") or "").strip()
        parts = [f"- {command}：{usage}"]
        if purpose:
            parts.append(purpose)
        if query_hint:
            parts.append(query_hint)
        else:
            parts.append(f"query 只輸出 {command} 後面的參數")
        return "。".join(parts)

    def _generate_router_json(self, prompt: str) -> str:
        from .dynamic_tools import OllamaTextClient

        # Routing is mechanical classification → always local Ollama, low temp
        # for stable JSON, and a capped timeout so a slow router can't hold the
        # whole chat turn hostage (it just falls back to direct).
        client = OllamaTextClient(
            endpoint=self.settings.openclaw_local_text_endpoint,
            model=self._local_model(),
            timeout_seconds=min(
                self.settings.openclaw_local_text_timeout_seconds,
                _ROUTER_TIMEOUT_CAP_SECONDS,
            ),
        )
        return client.generate(prompt, temperature=0.0)

    def _run_chat_tool(self, req: WebCommandRequest, decision: RouterDecision) -> ChatToolResult:
        """Dispatch a router decision to the appropriate tool executor via the registry.

        Raises ``ValueError`` for any tool not in the registry (should not
        happen — parse_router_decision already guards the whitelist)."""
        policy_map: dict[str, tuple[ChatToolPolicy, object]] = {
            CHAT_TOOL_SEARCH: (_SEARCH_TOOL_POLICY, self._exec_grounded_search),
            CHAT_TOOL_MUSIC: (_MUSIC_TOOL_POLICY, self._exec_registered_command_chat_tool),
            CHAT_TOOL_BLUETOOTH: (
                _BLUETOOTH_TOOL_POLICY,
                self._exec_registered_command_chat_tool,
            ),
            CHAT_TOOL_IR: (_IR_TOOL_POLICY, self._exec_registered_command_chat_tool),
        }
        entry = policy_map.get(decision.tool)
        if entry is None:
            raise ValueError(f"unknown chat tool: {decision.tool!r}")
        policy, executor = entry
        tool_req = make_chat_tool_request(
            tool=decision.tool,
            raw_query=decision.query,
            user_question=req.input or "",
            policy=policy,
        )
        return executor(req, tool_req)  # type: ignore[operator]

    def _stream_chat_tool(
        self, req: WebCommandRequest, decision: RouterDecision
    ) -> Iterator[dict]:
        """Run the tool off-thread, surfacing a live "正在調用…工具中" notice up
        front (so the user can see a tool is being invoked) and heartbeats while
        it works (so the connection stays alive), then deliver the grounded
        answer as the ``done`` event. The finished answer still carries its own
        persistent "已使用工具" banner from the executor.

        If the client disconnects (GeneratorExit) before the worker finishes,
        the completed result is pushed into server-side session memory so it
        appears automatically when the user reconnects."""
        yield stream_delta(_tool_calling_notice(decision.tool))
        result: dict[str, object] = {}
        done = threading.Event()
        abandoned = threading.Event()

        def _worker() -> None:
            try:
                tool_result: ChatToolResult = self._run_chat_tool(req, decision)
                result["text"] = tool_result.answer
                result["model_metadata"] = tool_result.model_metadata
                logger.info(
                    "[chat-tool] tool=%s sources=%d summary=%r",
                    decision.tool, tool_result.source_count, tool_result.result_summary,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("chat tool failed tool=%s", decision.tool)
                result["error"] = str(exc)
            finally:
                done.set()
                if abandoned.is_set() and "text" in result:
                    try:
                        self._push_orphaned_result(str(result["text"]))
                    except Exception:  # noqa: BLE001
                        logger.exception("command bridge: failed to push orphaned tool result")

        threading.Thread(target=_worker, daemon=True).start()
        try:
            while not done.wait(timeout=_HEARTBEAT_SECONDS):
                yield stream_heartbeat()
        except GeneratorExit:
            abandoned.set()
            raise
        if "error" in result:
            yield stream_error(f"工具執行失敗：{result['error']}")
            return
        metadata = result.get("model_metadata")
        yield stream_done(
            str(result.get("text") or "").strip(),
            model_metadata=metadata if isinstance(metadata, ModelMetadata) else None,
        )

    def _push_orphaned_result(self, text: str) -> None:
        """Push a completed assistant message into session memory when the
        streaming client disconnected before delivery. The user sees it on
        the next reconnect/session load."""
        from uuid import uuid4
        snapshot = self._sessions().load()
        messages = list(snapshot.get("messages") or [])
        messages.append({
            "id": uuid4().hex,
            "role": "assistant",
            "text": text,
            "status": "ok",
        })
        snapshot["messages"] = messages
        self._sessions().save(snapshot)
        logger.info(
            "command bridge: pushed orphaned tool result to session memory (%d chars)", len(text)
        )

    def _exec_grounded_search(
        self, req: WebCommandRequest, tool_req: ChatToolRequest
    ) -> ChatToolResult:
        """Grounded web search executor: retrieve sources, then synthesize an
        answer with the user's chosen chat backend. Returns a typed
        ``ChatToolResult``; the ``answer`` field carries the user-visible text
        (banner + synthesis + source block). Logs the backing function so every
        tool call is traceable to code."""
        from .web_search import DEFAULT_WEB_SEARCH_LIMIT, web_search

        query = tool_req.query  # already sanitized + budget-enforced by make_chat_tool_request
        logger.info(
            "[chat-tool] tool=%s fn=openclaw_adapter.web_search.web_search query=%r",
            tool_req.tool, query,
        )
        banner = f"{_TOOL_USED_PREFIX}網路搜尋（{tool_req.tool}）｜查詢：{query}"
        results = web_search(
            query, max_results=DEFAULT_WEB_SEARCH_LIMIT, reuse_browser=False
        )
        if not results:
            return ChatToolResult(
                answer=(
                    f"{banner}\n\n"
                    f"我搜尋了「{query}」，但目前找不到可用的網路來源，"
                    "請稍後再試或換個說法。"
                ),
                source_count=0,
                result_summary="no results",
            )
        source_pack = self._format_search_source_pack(results, tool_req.policy)
        prompt = "\n".join([
            _SEARCH_SYNTHESIS_PROMPT, "",
            f"使用者問題：{tool_req.user_question}",
            f"搜尋查詢：{query}", "",
            "搜尋結果：", source_pack, "", "回答：",
        ])
        answer, model_label, model_metadata = self._synthesize_with_chat_backend(
            req.chat_backend, prompt
        )
        message = (
            f"{banner}\n\n{answer.strip()}\n\n"
            f"{self._format_search_sources_block(results)}"
        )
        if self.settings.openclaw_web_chat_tool_debug:
            message += f"\n\n（合成模型：{model_label}）"
        return ChatToolResult(
            answer=message,
            source_count=len(results),
            result_summary=f"query={query!r} sources={len(results)} model={model_label}",
            model_metadata=model_metadata,
        )

    def _exec_music_chat_tool(
        self, req: WebCommandRequest, tool_req: ChatToolRequest
    ) -> ChatToolResult:
        return self._exec_registered_command_chat_tool(req, tool_req)

    def _exec_registered_command_chat_tool(
        self, req: WebCommandRequest, tool_req: ChatToolRequest
    ) -> ChatToolResult:
        query = tool_req.query
        runner_map = {
            CHAT_TOOL_MUSIC: ("CommandBridge.run_music_command", self.run_music_command),
            CHAT_TOOL_BLUETOOTH: (
                "CommandBridge.run_bluetooth_command",
                self.run_bluetooth_command,
            ),
            CHAT_TOOL_IR: ("CommandBridge.run_ir_command", self.run_ir_command),
        }
        entry = runner_map.get(tool_req.tool)
        if entry is None:
            raise ValueError(f"unknown registered command chat tool: {tool_req.tool!r}")
        fn_name, runner = entry
        logger.info("[chat-tool] tool=%s fn=%s query=%r", tool_req.tool, fn_name, query)
        result = runner(query)
        status = str(result.get("status", STATUS_OK))
        message = str(result.get("message", "")).strip()
        banner = f"{_TOOL_USED_PREFIX}{tool_req.policy.display_name}（{tool_req.tool}）｜指令：{query}"
        if status == STATUS_ERROR:
            message = message or f"{tool_req.policy.display_name}失敗。"
        return ChatToolResult(
            answer=f"{banner}\n\n{message}",
            source_count=0,
            result_summary=f"query={query!r} status={status}",
        )

    @staticmethod
    def _format_search_source_pack(results, policy: ChatToolPolicy) -> str:
        lines: list[str] = []
        total = 0
        for i, r in enumerate(results, 1):
            title = _clip(r.title, _SOURCE_PACK_TITLE_CAP)
            url = (r.url or "").strip()
            snippet = _clip(r.snippet, policy.max_source_field_chars)
            entry = f"[{i}] 標題：{title}\n    網址：{url}\n    摘要：{snippet}"
            # Keep at least the first source even if it alone busts the budget.
            if lines and total + len(entry) > policy.max_source_pack_chars:
                break
            lines.append(entry)
            total += len(entry)
        return "\n".join(lines)

    @staticmethod
    def _format_search_sources_block(results) -> str:
        lines = ["資料來源："]
        for i, r in enumerate(results, 1):
            lines.append(f"[{i}] {r.title} — {r.url}")
        return "\n".join(lines)

    def _synthesize_with_chat_backend(
        self, chat_backend: str, prompt: str
    ) -> tuple[str, str, ModelMetadata]:
        """Compose the final grounded answer with the user's chat backend.
        Returns (text, model_label, metadata). If cloud is requested but
        unavailable, fall back to local synthesis so the search result is still
        usable and visible."""
        if chat_backend == CHAT_BACKEND_CLOUD_PICKLE:
            client = self._build_cloud_chat_client()
            if client is None:
                text = self._ollama_generate_blocking(prompt)
                metadata = self._model_metadata_for_backend(
                    chat_backend,
                    (
                        ModelAttempt(
                            "opencode",
                            self._big_pickle_model(),
                            _MODEL_STATUS_NOT_CONFIGURED,
                            "Big Pickle unavailable",
                        ),
                        ModelAttempt("local", self._local_model(), _MODEL_STATUS_OK),
                    ),
                    "local",
                    self._local_model(),
                    fallback_reason="Big Pickle unavailable",
                )
                return text, f"本地 {self._local_model()}（雲端不可用，已改用本地）", metadata
            text = client.generate(prompt, temperature=0.3)
            model = self._big_pickle_model()
            metadata = self._model_metadata_for_backend(
                chat_backend,
                (ModelAttempt("opencode", model, _MODEL_STATUS_OK),),
                "opencode",
                model,
            )
            return text, f"雲端 {model}", metadata
        if chat_backend == CHAT_BACKEND_CLOUD_MISTRAL:
            client = self._build_mistral_chat_client()
            if client is None:
                text = self._ollama_generate_blocking(prompt)
                metadata = self._model_metadata_for_backend(
                    chat_backend,
                    (
                        ModelAttempt(
                            "mistral",
                            self._mistral_model(),
                            _MODEL_STATUS_NOT_CONFIGURED,
                            "Mistral API key missing",
                        ),
                        ModelAttempt("local", self._local_model(), _MODEL_STATUS_OK),
                    ),
                    "local",
                    self._local_model(),
                    fallback_reason="Mistral API key missing",
                )
                return text, f"本地 {self._local_model()}（Mistral 不可用，已改用本地）", metadata
            text = client.generate(prompt, temperature=0.3)
            model = self._mistral_model()
            metadata = self._model_metadata_for_backend(
                chat_backend,
                (ModelAttempt("mistral", model, _MODEL_STATUS_OK),),
                "mistral",
                model,
            )
            return text, f"Mistral {model}", metadata
        if chat_backend == CHAT_BACKEND_GEMINI:
            text, metadata = self._generate_gemini_with_fallback(prompt, temperature=0.3)
            return text, f"{metadata.final_provider} {metadata.final_model}", metadata
        if chat_backend == CHAT_BACKEND_CLOUD_POOL:
            text, metadata = self._handle_cloud_pool_blocking(prompt)
            return text, f"{metadata.final_provider} {metadata.final_model}", metadata
        text = self._ollama_generate_blocking(prompt)
        metadata = self._model_metadata_for_backend(
            chat_backend,
            (ModelAttempt("local", self._local_model(), _MODEL_STATUS_OK),),
            "local",
            self._local_model(),
        )
        return text, f"本地 {self._local_model()}", metadata

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

    def now_playing(self) -> dict:
        """Name of the song OpenClaw is currently playing (``null`` when idle),
        so the web 生活 mode can show a small now-playing strip and hide it when
        nothing is playing. Reads the live-verified player state via the same
        music module the handlers use."""
        from . import music_command

        try:
            name = music_command.now_playing(self.settings)
        except Exception:  # noqa: BLE001
            logger.exception("now_playing lookup failed")
            name = None
        return {"status": STATUS_OK, "name": name}

    # --- workflow surface: NL draft + editable card (issue #53, web) -------
    def _workflow_surface(self) -> tuple[object, object]:
        """Lazily build the shared (handler, editor) pair for web workflows.

        One editor instance is kept for the bridge's lifetime so a draft created
        by ``run_workflow_command`` survives the later button-press requests
        (reorder / delete / save) that hit ``run_workflow_action``."""
        if self._workflow_handler is None:
            with self._workflow_lock:
                if self._workflow_handler is None:
                    from .workflow_command import build_workflow_handler, _workflow_store
                    from .workflow_editor import WorkflowEditor

                    runner = _WorkflowShimRunner(self.settings)
                    # Pass the full command registry so the web command picker
                    # lists every allowed registered command and _cmd_run can
                    # dispatch any registered handler (not just the fallbacks).
                    command_registry = self._handlers()
                    editor = WorkflowEditor(
                        _workflow_store(runner),
                        command_registry=command_registry,
                        catalog=runner.catalog,
                    )
                    self._workflow_editor = editor
                    self._workflow_handler = build_workflow_handler(
                        self.settings, runner, workflow_editor=editor,
                        command_registry=command_registry,
                    )
        return self._workflow_handler, self._workflow_editor  # type: ignore[return-value]

    def run_workflow_command(self, text: str) -> dict:
        """Run the ``/workflow`` handler for the web console. ``text`` is the
        remainder after the command (e.g. ``create 先查天氣再念出來…``); a
        natural-language ``create`` drafts a workflow via the cloud-preferred LLM
        and lands it in an editable card (the same flow the Telegram bot uses, so
        tool_call steps reuse real generated-tool slugs).

        When the editor is in capture mode (collecting a field value such as the
        workflow id/goal or a step tool name), the text is routed to
        ``handle_text_capture`` instead of being dispatched as a new subcommand.
        This mirrors the Telegram path in
        ``TelegramCommandProcessor._build_workflow_capture_plan``."""
        handler, editor = self._workflow_surface()
        raw = (text or "").strip()

        # Escape hatch (mirrors the Telegram path): a slash command is never
        # swallowed by capture mode, so /workflow cancel (or any command to start
        # over) always reaches the dispatcher instead of being eaten as field text.
        if editor.is_capturing(_WF_WEB_CHAT_ID) and not raw.startswith("/"):
            try:
                captured = editor.handle_text_capture(raw, _WF_WEB_CHAT_ID)
            except Exception as exc:  # noqa: BLE001
                logger.exception("workflow capture failed text=%r", raw)
                return {"status": STATUS_ERROR, "message": f"工作流欄位輸入失敗：{exc}", "actions": []}
            if captured is not None:
                message, markup = captured
                return {
                    "status": STATUS_OK,
                    "message": str(message) if message is not None else "",
                    "actions": self._markup_to_actions(markup),
                }

        remainder = raw
        if remainder.startswith("/workflow"):
            remainder = remainder[len("/workflow"):].strip()
        try:
            result = handler(remainder, _WF_WEB_CHAT_ID)
        except Exception as exc:  # noqa: BLE001
            logger.exception("workflow command failed text=%r", text)
            return {"status": STATUS_ERROR, "message": f"工作流指令失敗：{exc}", "actions": []}
        if isinstance(result, tuple):
            message = result[0]
            markup = result[1] if len(result) > 1 else None
        else:
            message, markup = result, None
        return {
            "status": STATUS_OK,
            "message": str(message) if message is not None else "",
            "actions": self._markup_to_actions(markup),
        }

    def run_workflow_action(self, callback_data: str) -> dict:
        """Re-invoke a ``wfe:`` workflow-editor button for the web console
        (reorder / delete / save / cancel a draft step). The same editor handler
        the Telegram bot dispatches, so step validation on save is identical."""
        _, editor = self._workflow_surface()
        prefix, _, payload = (callback_data or "").partition(":")
        if prefix != "wfe":
            return {"status": STATUS_ERROR,
                    "message": f"未知的工作流動作：{callback_data}", "actions": []}
        handler = editor.callback_handlers().get("wfe")
        if handler is None:
            return {"status": STATUS_ERROR, "message": "工作流編輯器尚未啟用。", "actions": []}
        try:
            result = handler(payload, "", _WF_WEB_CHAT_ID)
        except Exception as exc:  # noqa: BLE001
            logger.exception("workflow action failed cb=%s", callback_data)
            return {"status": STATUS_ERROR, "message": f"動作執行失敗：{exc}", "actions": []}
        toast, new_text, markup = (list(result) + [None, None, None])[:3] \
            if isinstance(result, tuple) else (result, None, None)
        message = new_text if new_text else toast
        return {
            "status": STATUS_OK,
            "message": str(message) if message is not None else "",
            "actions": self._markup_to_actions(markup),
        }

    # --- schedule surface (web#9) -------------------------------------------
    def _schedulehome_surface(self) -> tuple[object, object, object]:
        """Lazily build the (command_handler, cb_handler, store) triple for web schedules.

        Shares the same HomeScheduleStore singleton the Telegram bot and scheduler
        use, so a schedule created via the web console is visible everywhere."""
        if self._sh_handler is None:
            with self._sh_lock:
                if self._sh_handler is None:
                    from .home_schedule_command import (
                        build_schedulehome_handler,
                        build_schedulehome_callback_handler,
                    )
                    from .home_schedule import get_home_schedule_store, make_run_slash_command
                    store = get_home_schedule_store(
                        self.settings.openclaw_home_schedules_path
                    )
                    run_cmd = make_run_slash_command(self._handlers())
                    self._sh_store = store
                    self._sh_cb_handler = build_schedulehome_callback_handler(store, run_cmd)
                    self._sh_handler = build_schedulehome_handler(store, run_cmd)
        return self._sh_handler, self._sh_cb_handler, self._sh_store  # type: ignore[return-value]

    @staticmethod
    def _extract_wf_slug(text: str) -> str:
        """Return the last workflow slug (underscore or wf- kebab-case) in text, or ''."""
        matches = [m for m in _WF_SLUG_RE.findall(text) if "_" in m or m.startswith("wf-")]
        return matches[-1] if matches else ""

    def run_schedulehome_command(self, text: str) -> dict:
        """Run /schedulehome for the web console.

        Three sub-cases:
        1. Capture mode active (collecting slash commands for a new schedule): route
           text to capture handler.  ``完成``/``done``/``結束`` finalises the schedule.
        2. ``add_for_wf <id>`` — start add flow with workflow ID pre-filled; recurrence
           ok auto-creates the schedule without entering capture mode.
        3. Normal subcommands: list (empty), add, run/on/off/delete <id>."""
        handler, _, store = self._schedulehome_surface()
        raw = (text or "").strip()

        sid = store.capture_target(_SH_WEB_CHAT_ID)
        if sid is not None:
            if raw in {"完成", "done", "結束"}:
                store.end_capture(_SH_WEB_CHAT_ID)
                entry = store.get(sid)
                n = len(entry.get("commands") or []) if entry else 0
                return {
                    "status": STATUS_OK,
                    "message": f"✅ 排程設定完成，已加入 {n} 個指令。",
                    "actions": [],
                }
            if raw.startswith("/"):
                store.add_command(sid, raw)
                entry = store.get(sid)
                n = len(entry.get("commands") or []) if entry else 0
                return {
                    "status": STATUS_OK,
                    "message": (
                        f"已加入第 {n} 個指令：{raw}\n"
                        "繼續傳下一個指令，或輸入「完成」結束。"
                    ),
                    "actions": [{"label": "完成", "callback_data": "sh:done"}],
                }
            # Non-slash, non-done in capture: echo the hint back.
            entry = store.get(sid)
            hint = (
                f"排程設定中，請傳入斜線指令（如 /workflow run greeting_workflow）"
                f"或輸入「完成」。\n目前已加入 {len(entry.get('commands') or [])} 個指令。"
                if entry else "排程設定中，請傳入斜線指令或「完成」。"
            )
            return {
                "status": STATUS_OK,
                "message": hint,
                "actions": [{"label": "完成", "callback_data": "sh:done"}],
            }

        try:
            result = handler(raw, _SH_WEB_CHAT_ID)
        except Exception as exc:  # noqa: BLE001
            logger.exception("schedulehome command failed text=%r", text)
            return {"status": STATUS_ERROR, "message": f"排程指令失敗：{exc}", "actions": []}
        # Command handler returns str | (message, markup) 2-tuple.
        if isinstance(result, tuple):
            message = result[0]
            markup = result[1] if len(result) > 1 else None
            return {
                "status": STATUS_OK,
                "message": str(message) if message is not None else "",
                "actions": self._markup_to_actions(markup),
            }
        return {
            "status": STATUS_OK,
            "message": str(result) if result is not None else "",
            "actions": [],
        }

    def run_schedulehome_action(self, callback_data: str) -> dict:
        """Re-invoke a ``sh:`` schedulehome button for the web console (time/recurrence
        pickers, list management, capture done/cancel)."""
        _, cb_handler, _ = self._schedulehome_surface()
        prefix, _, payload = (callback_data or "").partition(":")
        if prefix != "sh":
            return {
                "status": STATUS_ERROR,
                "message": f"未知的排程動作：{callback_data}",
                "actions": [],
            }
        try:
            result = cb_handler(payload, "", _SH_WEB_CHAT_ID)
        except Exception as exc:  # noqa: BLE001
            logger.exception("schedulehome action failed cb=%s", callback_data)
            return {"status": STATUS_ERROR, "message": f"動作執行失敗：{exc}", "actions": []}
        toast, new_text, markup = (list(result) + [None, None, None])[:3] \
            if isinstance(result, tuple) else (result, None, None)
        message = new_text if new_text is not None else toast
        return {
            "status": STATUS_OK,
            "message": str(message) if message is not None else "",
            "actions": self._markup_to_actions(markup),
        }

    # --- 生活 mode: bluetooth control surface (aka_no_claw#38 / web#7) ------
    def run_bluetooth_command(self, text: str = "") -> dict:
        """Run the Telegram ``/bluetooth`` handler from the web console.

        Empty input (or ``scan``) keeps the existing scan behavior; any other
        text is passed through as the device name so Web Chat and the 生活 mode
        both hit the same command handler."""
        remainder = (text or "").strip()
        if remainder.startswith("/bluetooth"):
            remainder = remainder[len("/bluetooth"):].strip()
        if remainder.lower() == "scan":
            remainder = ""
        message, markup = self._run_command_raw("/bluetooth", remainder)
        return {
            "status": STATUS_OK,
            "message": message,
            "actions": self._markup_to_actions(markup),
        }

    def run_bluetooth_action(self, callback_data: str) -> dict:
        """Re-invoke a ``bt:`` callback button for the web 生活 mode (re-scan or
        connect a selected device) — the same handler the Telegram bot dispatches,
        so address-token resolution and MAC validation are enforced identically."""
        prefix, _, payload = (callback_data or "").partition(":")
        if prefix != "bt":
            return {"status": STATUS_ERROR, "message": f"未知的藍牙動作：{callback_data}", "actions": []}
        handler = self._callbacks().get("bt")
        if handler is None:
            return {"status": STATUS_ERROR, "message": "藍牙功能尚未啟用。", "actions": []}
        try:
            result = handler(payload, "", _BRIDGE_CHAT_ID)
        except Exception as exc:  # noqa: BLE001
            logger.exception("bluetooth action failed cb=%s", callback_data)
            return {"status": STATUS_ERROR, "message": f"動作執行失敗：{exc}", "actions": []}
        toast, new_text, markup = (list(result) + [None, None, None])[:3] \
            if isinstance(result, tuple) else (result, None, None)
        message = new_text if new_text else toast
        return {
            "status": STATUS_OK,
            "message": str(message) if message is not None else "",
            "actions": self._markup_to_actions(markup),
        }

    # --- 生活 mode: IR / home-appliance control surface --------------------
    def run_ir_command(self, text: str) -> dict:
        """Run the Telegram ``/ir`` handler from the web 生活 mode. The web UI may
        send either the full slash command (``/ir send ...``) or just the
        remainder (``send ...``); the bridge normalizes both forms."""
        remainder = (text or "").strip()
        if remainder.startswith("/ir"):
            remainder = remainder[3:].strip()
        message, markup = self._run_command_raw("/ir", remainder)
        return {
            "status": STATUS_OK,
            "message": message,
            "actions": self._markup_to_actions(markup),
        }

    def run_ir_action(self, callback_data: str) -> dict:
        """Re-invoke an ``ir:`` callback button for the web 生活 mode."""
        prefix, _, payload = (callback_data or "").partition(":")
        if prefix != "ir":
            return {"status": STATUS_ERROR, "message": f"未知的 IR 動作：{callback_data}", "actions": []}
        handler = self._callbacks().get("ir")
        if handler is None:
            return {"status": STATUS_ERROR, "message": "IR 功能尚未啟用。", "actions": []}
        try:
            result = handler(payload, "", _BRIDGE_CHAT_ID)
        except Exception as exc:  # noqa: BLE001
            logger.exception("ir action failed cb=%s", callback_data)
            return {"status": STATUS_ERROR, "message": f"動作執行失敗：{exc}", "actions": []}
        toast, new_text, markup = (list(result) + [None, None, None])[:3] \
            if isinstance(result, tuple) else (result, None, None)
        message = new_text if new_text else toast
        return {
            "status": STATUS_OK,
            "message": str(message) if message is not None else "",
            "actions": self._markup_to_actions(markup),
        }

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

    def model_routes(self) -> dict:
        """Return the concrete model chain behind each web Chat model tab."""
        local = self._local_model()
        gemini_chain = [
            {"provider": "gemini", "model": model}
            for model in self._gemini_route_models()
        ]
        gemini_chain.append({"provider": "local", "model": local})
        cp_providers = [
            {"provider": "gemini", "model": self._gemini_primary_model()},
            {"provider": "mistral", "model": self._mistral_model()},
            {"provider": "opencode", "model": self._big_pickle_model()},
        ]
        # Pick the first actually usable provider as the preview (no probe).
        cp_preview_provider, cp_preview_model = self._cloud_pool_preview()
        return {
            "status": STATUS_OK,
            "routes": [
                {
                    "backend": CHAT_BACKEND_CLOUD_POOL,
                    "label": "雲端池",
                    "requested_provider": cp_preview_provider,
                    "requested_model": cp_preview_model,
                    "chain": cp_providers,
                    "configured": True,
                },
                {
                    "backend": CHAT_BACKEND_LOCAL,
                    "label": "本地",
                    "requested_provider": "local",
                    "requested_model": local,
                    "chain": [{"provider": "local", "model": local}],
                    "configured": True,
                },
                {
                    "backend": CHAT_BACKEND_CLOUD_MISTRAL,
                    "label": "Mistral",
                    "requested_provider": "mistral",
                    "requested_model": self._mistral_model(),
                    "chain": [{"provider": "mistral", "model": self._mistral_model()}],
                    "configured": bool(getattr(self.settings, "openclaw_mistral_api_key", None)),
                },
                {
                    "backend": CHAT_BACKEND_GEMINI,
                    "label": "Gemini",
                    "requested_provider": "gemini",
                    "requested_model": self._gemini_primary_model(),
                    "chain": gemini_chain,
                    "configured": bool(getattr(self.settings, "openclaw_gemini_api_key", None)),
                },
                {
                    "backend": CHAT_BACKEND_CLOUD_PICKLE,
                    "label": "Big Pickle",
                    "requested_provider": "opencode",
                    "requested_model": self._big_pickle_model(),
                    "chain": [{"provider": "opencode", "model": self._big_pickle_model()}],
                    "configured": True,
                },
            ],
        }

    def _requested_model_for_backend(self, chat_backend: str) -> tuple[str, str]:
        if chat_backend == CHAT_BACKEND_CLOUD_PICKLE:
            return "opencode", self._big_pickle_model()
        if chat_backend == CHAT_BACKEND_CLOUD_MISTRAL:
            return "mistral", self._mistral_model()
        if chat_backend == CHAT_BACKEND_GEMINI:
            return "gemini", self._gemini_primary_model()
        if chat_backend == CHAT_BACKEND_CLOUD_POOL:
            return self._cloud_pool_chain()[0][0], self._cloud_pool_chain()[0][1]
        return "local", self._local_model()

    def _model_metadata_for_backend(
        self,
        chat_backend: str,
        attempted: tuple[ModelAttempt, ...],
        final_provider: str,
        final_model: str,
        *,
        fallback_reason: str | None = None,
    ) -> ModelMetadata:
        requested_provider, requested_model = self._requested_model_for_backend(chat_backend)
        return ModelMetadata(
            requested_provider=requested_provider,
            requested_model=requested_model,
            attempted_models=attempted,
            final_provider=final_provider,
            final_model=final_model,
            fallback_reason=fallback_reason,
        )

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
        metadata = self._model_metadata_for_backend(
            CHAT_BACKEND_LOCAL,
            (ModelAttempt("local", model, _MODEL_STATUS_OK),),
            "local",
            model,
        )
        yield stream_done("".join(full).strip(), model_metadata=metadata)

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
        metadata = self._model_metadata_for_backend(
            CHAT_BACKEND_CLOUD_PICKLE,
            (ModelAttempt("opencode", self._big_pickle_model(), _MODEL_STATUS_OK),),
            "opencode",
            self._big_pickle_model(),
        )
        yield stream_done(text, model_metadata=metadata)

    def _ollama_generate_blocking(self, prompt: str) -> str:
        from .dynamic_tools import OllamaTextClient

        client = OllamaTextClient(
            endpoint=self.settings.openclaw_local_text_endpoint,
            model=self._local_model(),
            timeout_seconds=self.settings.openclaw_local_text_timeout_seconds,
        )
        return client.generate(prompt, temperature=0.7)

    def _build_cloud_chat_client(self):
        """Big-pickle chat client via direct HTTP (zen/v1). No CLI fallback (#59)."""
        from .dynamic_tools import OpenCodeTextClient, probe_opencode

        base_url = self.settings.openclaw_opencode_base_url
        model = self._big_pickle_model()
        if probe_opencode(base_url, model=model, timeout=10.0):
            return OpenCodeTextClient(
                base_url=base_url,
                model=model,
                api_key=self.settings.openclaw_opencode_api_key,
                timeout_seconds=180,
            )
        return None

    def _build_mistral_chat_client(self):
        """Mistral cloud chat client; returns None when MISTRAL_API_KEY not set."""
        from .dynamic_tools import MistralTextClient

        key = getattr(self.settings, "openclaw_mistral_api_key", None)
        if not key:
            return None
        model = self._mistral_model()
        return MistralTextClient(api_key=key, model=model, timeout_seconds=180)

    def _build_gemini_chat_client(self, model: str):
        """Gemini cloud chat client; returns None when no Google API key is configured."""
        key = getattr(self.settings, "openclaw_gemini_api_key", None)
        if not key:
            return None
        ssl_ctx = build_ssl_context(self.settings)
        return _GeminiTextClient(
            api_key=key,
            model=model,
            timeout_seconds=180,
            ssl_context=ssl_ctx,
        )

    def _stream_mistral_chat(self, prompt: str) -> "Iterator[dict]":
        client = self._build_mistral_chat_client()
        if client is None:
            yield stream_error("Mistral 後端目前無法使用（未設定 MISTRAL_API_KEY）。")
            return
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
            abort = getattr(client, "abort", None)
            if callable(abort):
                abort()
            raise
        if "error" in result:
            yield stream_error(f"Mistral 後端失敗：{result['error']}")
            return
        text = str(result.get("text") or "").strip()
        if text:
            yield stream_delta(text)
        metadata = self._model_metadata_for_backend(
            CHAT_BACKEND_CLOUD_MISTRAL,
            (ModelAttempt("mistral", self._mistral_model(), _MODEL_STATUS_OK),),
            "mistral",
            self._mistral_model(),
        )
        yield stream_done(text, model_metadata=metadata)

    def _generate_gemini_with_fallback(
        self, prompt: str, *, temperature: float
    ) -> tuple[str, ModelMetadata]:
        attempts: list[ModelAttempt] = []
        gemini_models = self._gemini_route_models()
        primary_model = gemini_models[0]

        if not getattr(self.settings, "openclaw_gemini_api_key", None):
            attempts.append(
                ModelAttempt(
                    "gemini",
                    primary_model,
                    _MODEL_STATUS_NOT_CONFIGURED,
                    "Gemini API key missing",
                )
            )
            text = self._ollama_generate_blocking(prompt)
            attempts.append(ModelAttempt("local", self._local_model(), _MODEL_STATUS_OK))
            return text, self._model_metadata_for_backend(
                CHAT_BACKEND_GEMINI,
                tuple(attempts),
                "local",
                self._local_model(),
                fallback_reason="Gemini API key missing",
            )

        last_reason = ""
        for model in gemini_models:
            client = self._build_gemini_chat_client(model)
            if client is None:
                attempts.append(
                    ModelAttempt(
                        "gemini",
                        model,
                        _MODEL_STATUS_NOT_CONFIGURED,
                        "Gemini API key missing",
                    )
                )
                last_reason = "Gemini API key missing"
                break
            try:
                text = client.generate(prompt, temperature=temperature)
            except _GeminiRequestError as exc:
                attempts.append(ModelAttempt("gemini", model, exc.status, str(exc)))
                last_reason = str(exc)
                if _is_gemini_fallback_status(exc.status):
                    continue
                raise
            attempts.append(ModelAttempt("gemini", model, _MODEL_STATUS_OK))
            fallback_reason = attempts[0].reason if len(attempts) > 1 else None
            return text, self._model_metadata_for_backend(
                CHAT_BACKEND_GEMINI,
                tuple(attempts),
                "gemini",
                model,
                fallback_reason=fallback_reason,
            )

        text = self._ollama_generate_blocking(prompt)
        attempts.append(ModelAttempt("local", self._local_model(), _MODEL_STATUS_OK))
        return text, self._model_metadata_for_backend(
            CHAT_BACKEND_GEMINI,
            tuple(attempts),
            "local",
            self._local_model(),
            fallback_reason=last_reason or "Gemini quota or rate limit",
        )

    def _stream_gemini_chat(self, prompt: str) -> Iterator[dict]:
        result: dict[str, object] = {}
        done = threading.Event()

        def _worker() -> None:
            try:
                text, metadata = self._generate_gemini_with_fallback(prompt, temperature=0.7)
                result["text"] = text
                result["model_metadata"] = metadata
            except Exception as exc:  # noqa: BLE001
                result["error"] = str(exc)
            finally:
                done.set()

        threading.Thread(target=_worker, daemon=True).start()
        while not done.wait(timeout=_HEARTBEAT_SECONDS):
            yield stream_heartbeat()
        if "error" in result:
            yield stream_error(f"Gemini 後端失敗：{result['error']}")
            return
        text = str(result.get("text") or "").strip()
        if text:
            yield stream_delta(text)
        metadata = result.get("model_metadata")
        yield stream_done(
            text, model_metadata=metadata if isinstance(metadata, ModelMetadata) else None
        )

    def _cloud_pool_chain(self) -> list[tuple[str, str, object, object]]:
        """Return ordered list of (provider_label, model_name, build_fn, is_configured_fn)."""
        gemini_model = self._gemini_primary_model()
        mistral_model = self._mistral_model()
        bp_model = self._big_pickle_model()
        return [
            ("gemini", gemini_model, self._build_gemini_chat_client,
             lambda: bool(getattr(self.settings, "openclaw_gemini_api_key", None))),
            ("mistral", mistral_model, self._build_mistral_chat_client,
             lambda: bool(getattr(self.settings, "openclaw_mistral_api_key", None))),
            ("opencode", bp_model, self._build_cloud_chat_client,
             lambda: True),
        ]

    def _cloud_pool_preview(self) -> tuple[str, str]:
        """First actually usable (provider, model) for the cloud_pool tab preview.
        Checks settings only — no probing. Falls through to Big Pickle which is
        always considered configured."""
        for provider, model_name, _build_fn, configured_fn in self._cloud_pool_chain():
            if configured_fn():
                return provider, model_name
        return "local", self._local_model()

    def _handle_cloud_pool_blocking(
        self, prompt: str
    ) -> tuple[str, ModelMetadata]:
        """Try Gemini → Mistral → Big Pickle → local; return (text, metadata)."""
        attempts: list[ModelAttempt] = []

        for provider, model_name, build_fn, configured_fn in self._cloud_pool_chain():
            if not configured_fn():
                attempts.append(ModelAttempt(
                    provider, model_name, _MODEL_STATUS_NOT_CONFIGURED,
                    f"{provider} not configured",
                ))
                continue
            client = build_fn(model_name) if provider == "gemini" else build_fn()
            if client is None:
                attempts.append(ModelAttempt(
                    provider, model_name, _MODEL_STATUS_NOT_CONFIGURED,
                    f"{provider} unavailable",
                ))
                continue
            try:
                text = client.generate(prompt, temperature=0.7)
            except _GeminiRequestError as exc:
                attempts.append(ModelAttempt(provider, model_name, exc.status, str(exc)))
                continue
            except Exception as exc:
                attempts.append(ModelAttempt(provider, model_name, _MODEL_STATUS_ERROR, str(exc)))
                continue
            attempts.append(ModelAttempt(provider, model_name, _MODEL_STATUS_OK))
            fb = len(attempts) > 1
            first_provider = self._cloud_pool_chain()[0][0]
            first_model = self._cloud_pool_chain()[0][1]
            return text, ModelMetadata(
                requested_provider=first_provider,
                requested_model=first_model,
                attempted_models=tuple(attempts),
                final_provider=provider,
                final_model=model_name,
                fallback_reason=None if not fb else f"Fell back from {attempts[0].provider}",
                fallback_occurred=fb,
                requested_tab=CHAT_BACKEND_CLOUD_POOL,
            )

        local_model = self._local_model()
        text = self._ollama_generate_blocking(prompt)
        attempts.append(ModelAttempt("local", local_model, _MODEL_STATUS_OK))
        first_provider = self._cloud_pool_chain()[0][0]
        first_model = self._cloud_pool_chain()[0][1]
        return text, ModelMetadata(
            requested_provider=first_provider,
            requested_model=first_model,
            attempted_models=tuple(attempts),
            final_provider="local",
            final_model=local_model,
            fallback_reason="All cloud providers unavailable",
            fallback_occurred=True,
            requested_tab=CHAT_BACKEND_CLOUD_POOL,
        )

    def _stream_cloud_pool_chat(self, prompt: str) -> Iterator[dict]:
        """Try Gemini → Mistral → Big Pickle → local for streaming."""
        attempts: list[ModelAttempt] = []

        for provider, model_name, build_fn, configured_fn in self._cloud_pool_chain():
            if not configured_fn():
                attempts.append(ModelAttempt(
                    provider, model_name, _MODEL_STATUS_NOT_CONFIGURED,
                    f"{provider} not configured",
                ))
                continue
            client = build_fn(model_name) if provider == "gemini" else build_fn()
            if client is None:
                attempts.append(ModelAttempt(
                    provider, model_name, _MODEL_STATUS_NOT_CONFIGURED,
                    f"{provider} unavailable",
                ))
                continue

            result: dict[str, object] = {}
            done = threading.Event()

            def _worker(
                _client=client, _prompt=prompt,
            ) -> None:
                try:
                    result["text"] = _client.generate(_prompt, temperature=0.7)
                except _GeminiRequestError as exc:
                    result["error"] = str(exc)
                    result["error_status"] = exc.status
                except Exception as exc:
                    result["error"] = str(exc)
                finally:
                    done.set()

            worker = threading.Thread(target=_worker, daemon=True)
            worker.start()
            try:
                while not done.wait(timeout=_HEARTBEAT_SECONDS):
                    yield stream_heartbeat()
            except GeneratorExit:
                abort = getattr(client, "abort", None)
                if callable(abort):
                    abort()
                raise

            if "error" in result:
                error_status = str(result.get("error_status", _MODEL_STATUS_ERROR))
                attempts.append(ModelAttempt(
                    provider, model_name, error_status, str(result["error"]),
                ))
                continue

            attempts.append(ModelAttempt(provider, model_name, _MODEL_STATUS_OK))
            text = str(result.get("text") or "").strip()
            if text:
                yield stream_delta(text)
            fb = len(attempts) > 1
            first_provider = self._cloud_pool_chain()[0][0]
            first_model = self._cloud_pool_chain()[0][1]
            metadata = ModelMetadata(
                requested_provider=first_provider,
                requested_model=first_model,
                attempted_models=tuple(attempts),
                final_provider=provider,
                final_model=model_name,
                fallback_reason=None if not fb else f"Fell back from {attempts[0].provider}",
                fallback_occurred=fb,
                requested_tab=CHAT_BACKEND_CLOUD_POOL,
            )
            yield stream_done(text, model_metadata=metadata)
            return

        local_model = self._local_model()
        text = self._ollama_generate_blocking(prompt)
        attempts.append(ModelAttempt("local", local_model, _MODEL_STATUS_OK))
        first_provider = self._cloud_pool_chain()[0][0]
        first_model = self._cloud_pool_chain()[0][1]
        metadata = ModelMetadata(
            requested_provider=first_provider,
            requested_model=first_model,
            attempted_models=tuple(attempts),
            final_provider="local",
            final_model=local_model,
            fallback_reason="All cloud providers unavailable",
            fallback_occurred=True,
            requested_tab=CHAT_BACKEND_CLOUD_POOL,
        )
        if text:
            yield stream_delta(text)
        yield stream_done(text, model_metadata=metadata)

    def _local_model(self) -> str:
        return (
            getattr(self.settings, "openclaw_local_text_model", None) or "qwen3:14b"
        ).split(",")[0].strip()

    def _big_pickle_model(self) -> str:
        raw = (getattr(self.settings, "openclaw_opencode_model", None) or "big-pickle").strip()
        return raw.split("/")[-1] if "/" in raw else raw

    def _mistral_model(self) -> str:
        return (getattr(self.settings, "openclaw_mistral_model", None) or "mistral-large-latest").strip()

    def _gemini_primary_model(self) -> str:
        return (
            getattr(self.settings, "openclaw_gemini_primary_model", None)
            or getattr(self.settings, "openclaw_gemini_pro_model", None)
            or "gemini-2.5-flash"
        ).strip()

    def _gemini_flash_model(self) -> str:
        return (
            getattr(self.settings, "openclaw_gemini_flash_model", None)
            or "gemini-2.5-flash"
        ).strip()

    def _gemini_route_models(self) -> tuple[str, ...]:
        seen: set[str] = set()
        ordered: list[str] = []
        for model in (self._gemini_primary_model(), self._gemini_flash_model()):
            if model and model not in seen:
                seen.add(model)
                ordered.append(model)
        return tuple(ordered)

    @staticmethod
    def _build_translation_prompt(text: str) -> str:
        return (
            "將下列文字翻譯成自然、通順的繁體中文（台灣用語）。"
            "只輸出譯文，不要解說，不要加引號，不要加前綴。"
            "保留 URL、專有名詞、產品名。\n\n"
            f"原文：\n{text}\n\n譯文："
        )

    # --- translation -----------------------------------------------------
    def _image_translate_renderer(self):
        """Lazily build (and cache) the shared OCR+繁中翻譯 renderer from settings.
        Returns None when the local vision/text models are not configured — the
        same gate the Telegram photo path uses."""
        if self._image_renderer_built:
            return self._image_renderer
        with self._image_renderer_lock:
            if not self._image_renderer_built:
                from .image_translate import (
                    build_image_ocr_translate_renderer_from_settings,
                )

                self._image_renderer = build_image_ocr_translate_renderer_from_settings(
                    self.settings
                )
                self._image_renderer_built = True
        return self._image_renderer

    def _handle_translation(self, req: WebCommandRequest) -> WebCommandResponse:
        if req.submode == SUBMODE_IMAGE_TRANSLATION or req.has_image_attachment:
            return self._handle_image_translation(req)
        text = (req.input or "").strip()
        if not text:
            return WebCommandResponse(
                status=STATUS_ERROR,
                message="請輸入要翻譯的文字。",
                mode=MODE_TRANSLATION,
                submode=SUBMODE_TEXT_TRANSLATION,
            )
        message, metadata = self._translate_text_with_backend(
            text, req.chat_backend or CHAT_BACKEND_LOCAL
        )
        return WebCommandResponse(
            status=STATUS_OK,
            message=message,
            mode=MODE_TRANSLATION,
            submode=SUBMODE_TEXT_TRANSLATION,
            model_metadata=metadata,
        )

    def _translate_text_with_backend(
        self,
        text: str,
        chat_backend: str,
    ) -> tuple[str, ModelMetadata]:
        if chat_backend == CHAT_BACKEND_LOCAL:
            message = self._run_command("/zh", text)
            metadata = self._model_metadata_for_backend(
                chat_backend,
                (ModelAttempt("local", self._local_model(), _MODEL_STATUS_OK),),
                "local",
                self._local_model(),
            )
            return message, metadata

        prompt = self._build_translation_prompt(text)
        if chat_backend == CHAT_BACKEND_CLOUD_PICKLE:
            client = self._build_cloud_chat_client()
            if client is None:
                raise RuntimeError("cloud pickle 後端目前無法使用（OpenCode 未設定或無法連線）。")
            message = client.generate(prompt, temperature=0.2)
            metadata = self._model_metadata_for_backend(
                chat_backend,
                (ModelAttempt("opencode", self._big_pickle_model(), _MODEL_STATUS_OK),),
                "opencode",
                self._big_pickle_model(),
            )
            return message, metadata
        if chat_backend == CHAT_BACKEND_CLOUD_MISTRAL:
            client = self._build_mistral_chat_client()
            if client is None:
                raise RuntimeError("Mistral 後端目前無法使用（未設定 MISTRAL_API_KEY）。")
            message = client.generate(prompt, temperature=0.2)
            metadata = self._model_metadata_for_backend(
                chat_backend,
                (ModelAttempt("mistral", self._mistral_model(), _MODEL_STATUS_OK),),
                "mistral",
                self._mistral_model(),
            )
            return message, metadata
        if chat_backend == CHAT_BACKEND_GEMINI:
            return self._generate_gemini_with_fallback(prompt, temperature=0.2)
        if chat_backend == CHAT_BACKEND_CLOUD_POOL:
            return self._handle_cloud_pool_blocking(prompt)

        message = self._run_command("/zh", text)
        metadata = self._model_metadata_for_backend(
            CHAT_BACKEND_LOCAL,
            (ModelAttempt("local", self._local_model(), _MODEL_STATUS_OK),),
            "local",
            self._local_model(),
        )
        return message, metadata

    def _handle_image_translation(self, req: WebCommandRequest) -> WebCommandResponse:
        """Run the uploaded image through the same OCR + 繁體中文 translation
        pipeline the Telegram photo path uses (#43). Image bytes are written to a
        throwaway temp file (the renderer takes a path), then unlinked."""
        def _err(message: str, status: str = STATUS_ERROR) -> WebCommandResponse:
            return WebCommandResponse(
                status=status,
                message=message,
                mode=MODE_TRANSLATION,
                submode=SUBMODE_IMAGE_TRANSLATION,
            )

        image = next((a for a in req.attachments if a.type == "image"), None)
        if image is None or not image.data:
            return _err(_NO_IMAGE_MSG)
        if not _is_supported_image(image):
            return _err(_BAD_IMAGE_TYPE_MSG)

        renderer = self._image_translate_renderer()
        if renderer is None:
            from .image_translate import NOT_CONFIGURED_MESSAGE

            return _err(NOT_CONFIGURED_MESSAGE, status=STATUS_UNSUPPORTED)

        import os
        import tempfile
        from pathlib import Path

        tmp_path: Path | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                prefix="akaweb_imgtr_", suffix=_image_temp_suffix(image)
            )
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "wb") as fh:
                fh.write(image.data)
            result = renderer(tmp_path, (req.input or "").strip() or None)
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except OSError:
                    logger.warning("image translation temp cleanup failed path=%s", tmp_path)

        if not result.ok:
            return _err(result.message)
        message = (
            f"🌐→🇹🇼 圖片文字翻譯（偵測語言：{result.source_language}）\n\n"
            f"{result.translation}\n\n【原文】\n{result.ocr_text}"
        )
        return WebCommandResponse(
            status=STATUS_OK,
            message=message,
            mode=MODE_TRANSLATION,
            submode=SUBMODE_IMAGE_TRANSLATION,
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
