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
    CHAT_TOOL_SEARCH,
    ChatToolPolicy,
    ChatToolRequest,
    ChatToolResult,
    ChatTurn,
    MODE_CHAT,
    MODE_INVESTMENT,
    MODE_TRANSLATION,
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
    stream_start,
)

logger = logging.getLogger(__name__)

_BRIDGE_CHAT_ID = "web-bridge"
_HEARTBEAT_SECONDS = 10.0
# Finished jobs linger this long so a phone that reconnects after a screen-lock
# can still fetch the final report, then they are garbage-collected.
_JOB_TTL_SECONDS = 1800.0

_SELLER_UNSUPPORTED_MSG = "賣家信譽快照目前尚未由本地 command bridge 支援。"

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

# Web Chat contextual tool routing (#45). A local router LLM decides, per chat
# turn, whether to answer directly or call the one whitelisted tool (/search).
# The router gets the recent history so it can resolve pronouns into a
# self-contained search query (e.g. 「她的歌」→「初音未來 歌曲」). It must emit a
# single strict-JSON object; anything else is treated as "direct" (fail soft).
_ROUTER_TIMEOUT_CAP_SECONDS = 30
_ROUTER_SYSTEM_PROMPT = (
    "你是 aka_no_claw 聊天助理的路由器。根據對話判斷要不要使用工具來回答使用者的『最新訊息』。\n"
    "可用工具：\n"
    "- /search：當回答需要『即時、最新或你不確定的事實資訊』（新聞、價格、商品規格、人物近況、"
    "賽事結果等）時使用。\n"
    "其他情況（閒聊、改寫、翻譯、一般常識、可由上文直接回答）一律 direct，不要用工具。\n"
    "只輸出一個 JSON 物件，不要加任何多餘文字或說明：\n"
    '{"decision":"direct|tool","tool":"/search","query":"...","reason_summary":"..."}\n'
    "當 decision=tool 時，query 必須是適合丟給搜尋引擎、語意完整的查詢；"
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
_TOOL_FRIENDLY_NAMES = {CHAT_TOOL_SEARCH: "網路搜尋"}

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
        if not (req.input or "").strip():
            return WebCommandResponse(
                status=STATUS_ERROR, message="請輸入訊息。", mode=MODE_CHAT
            )
        decision = self._route_chat_decision(req)
        if decision is not None and decision.decision == ROUTER_DECISION_TOOL:
            try:
                tool_result = self._run_chat_tool(req, decision)
                logger.info(
                    "[chat-tool] tool=%s sources=%d summary=%r",
                    decision.tool, tool_result.source_count, tool_result.result_summary,
                )
                return WebCommandResponse(
                    status=STATUS_OK, message=tool_result.answer, mode=MODE_CHAT
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
        else:
            message = self._ollama_generate_blocking(prompt)
        return WebCommandResponse(status=STATUS_OK, message=message, mode=MODE_CHAT)

    def _stream_chat(self, req: WebCommandRequest) -> Iterator[dict]:
        if not (req.input or "").strip():
            yield stream_error("請輸入訊息。")
            return
        decision = self._route_chat_decision(req)
        if decision is not None and decision.decision == ROUTER_DECISION_TOOL:
            yield from self._stream_chat_tool(req, decision)
            return
        prompt = build_chat_prompt(req.input, req.history)
        if req.chat_backend == CHAT_BACKEND_CLOUD_PICKLE:
            yield from self._stream_cloud_chat(prompt)
        else:
            yield from self._stream_ollama_chat(prompt)

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
        lines = [_ROUTER_SYSTEM_PROMPT, "", "對話紀錄："]
        for turn in req.history:
            label = _CHAT_ROLE_LABELS.get(turn.role, turn.role)
            lines.append(f"{label}：{turn.content}")
        lines += ["", f"使用者最新訊息：{(req.input or '').strip()}", "", "JSON："]
        return "\n".join(lines)

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
        persistent "已使用工具" banner from the executor."""
        yield stream_delta(_tool_calling_notice(decision.tool))
        result: dict[str, object] = {}
        done = threading.Event()

        def _worker() -> None:
            try:
                tool_result: ChatToolResult = self._run_chat_tool(req, decision)
                result["text"] = tool_result.answer
                logger.info(
                    "[chat-tool] tool=%s sources=%d summary=%r",
                    decision.tool, tool_result.source_count, tool_result.result_summary,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("chat tool failed tool=%s", decision.tool)
                result["error"] = str(exc)
            finally:
                done.set()

        threading.Thread(target=_worker, daemon=True).start()
        while not done.wait(timeout=_HEARTBEAT_SECONDS):
            yield stream_heartbeat()
        if "error" in result:
            yield stream_error(f"工具執行失敗：{result['error']}")
            return
        yield stream_done(str(result.get("text") or "").strip())

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
        answer, model_label = self._synthesize_with_chat_backend(
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
    ) -> tuple[str, str]:
        """Compose the final grounded answer with the user's chat backend.
        Returns (text, model_label). If cloud is requested but unavailable, fall
        back to local synthesis so the search result is still usable."""
        if chat_backend == CHAT_BACKEND_CLOUD_PICKLE:
            client = self._build_cloud_chat_client()
            if client is None:
                text = self._ollama_generate_blocking(prompt)
                return text, f"本地 {self._local_model()}（雲端不可用，已改用本地）"
            text = client.generate(prompt, temperature=0.3)
            model = (self.settings.openclaw_opencode_model or "big-pickle").strip()
            return text, f"雲端 {model}"
        text = self._ollama_generate_blocking(prompt)
        return text, f"本地 {self._local_model()}"

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

    # --- 生活 mode: bluetooth control surface (aka_no_claw#38 / web#7) ------
    def run_bluetooth_command(self) -> dict:
        """Scan Bluetooth devices for the web 生活 mode — returns the device list
        (text + connect buttons). Same handler the Telegram ``/bluetooth`` uses."""
        message, markup = self._run_command_raw("/bluetooth", "")
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
        message = self._run_command("/zh", text)
        return WebCommandResponse(
            status=STATUS_OK,
            message=message,
            mode=MODE_TRANSLATION,
            submode=SUBMODE_TEXT_TRANSLATION,
        )

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
