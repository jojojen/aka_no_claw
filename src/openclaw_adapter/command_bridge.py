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

import dataclasses
import json
import logging
import queue
import re
import threading
import time
from contextvars import ContextVar
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Callable
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from assistant_runtime import AssistantSettings, build_ssl_context

from .job_store import JobStore
from .session_memory import SessionMemoryStore, SessionWriteError, empty_session
from .session_event_service import SessionEventService
from .session_event_journal import CursorExpiredError
from .run_recorder import RunRecorder
from .command_bridge_conversation import ConversationSession, ConversationState
from .command_bridge_music import MusicCapability
from .command_bridge_workflow import WorkflowCapability
from .command_bridge_home import HomeCapability
from .service_restart import RESTART_MESSAGE, trigger_restart_all
from .llm_pool_settings import (
    LLM_PROVIDER_BIG_PICKLE,
    LLM_PROVIDER_GEMINI,
    LLM_PROVIDER_LOCAL,
    LLM_PROVIDER_MISTRAL,
    LLM_PROVIDER_NVIDIA,
    ChatLlmPoolWriteError,
    CloudPoolRotation,
    chat_backend_configured,
    chat_backend_enabled,
    chat_llm_pool_payload,
    cloud_pool_order,
    default_chat_backend,
    enabled_cloud_pool_providers,
    enabled_vision_pool_providers,
    normalize_chat_llm_pool_settings,
    provider_enabled,
    resolve_provider_model,
    save_chat_llm_pool_settings,
)
from .command_bridge_models import (
    Action,
    CHAT_BACKEND_CLOUD_MISTRAL,
    CHAT_BACKEND_CLOUD_NVIDIA,
    CHAT_BACKEND_CLOUD_PICKLE,
    CHAT_BACKEND_CLOUD_POOL,
    CHAT_BACKEND_GEMINI,
    CHAT_BACKEND_LOCAL,
    CHAT_TOOL_BLUETOOTH,
    CHAT_TOOL_CREATE_WORKFLOW,
    CHAT_TOOL_GOAL,
    CHAT_TOOL_IR,
    CHAT_TOOL_MUSIC,
    CHAT_TOOL_MUSICQUEUE,
    CHAT_TOOL_NO_TOOL,
    CHAT_TOOL_RESEARCH,
    CHAT_TOOL_SEARCH,
    CHAT_TOOL_VISION,
    MUSIC_ACTION_PLAN,
    ChatToolPlan,
    ChatToolPolicy,
    ChatToolRequest,
    ChatToolResult,
    MODE_CHAT,
    MODE_INVESTMENT,
    MODE_TRANSLATION,
    ModelAttempt,
    ModelMetadata,
    MusicIntent,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_UNSUPPORTED,
    SUBMODE_DEEP_PRODUCT_RESEARCH,
    SUBMODE_IMAGE_TRANSLATION,
    SUBMODE_SELLER_REPUTATION_SNAPSHOT,
    SUBMODE_TEXT_TRANSLATION,
    WebCommandRequest,
    WebCommandResponse,
    _CHAT_ROLE_LABELS,
    _clip,
    _encode_image_attachment,
    _image_temp_suffix,
    _is_supported_image,
    _seed_variable_name_for_tool,
    _tool_calling_notice,
    build_chat_prompt,
    make_chat_tool_request,
    markup_to_actions,
    stream_delta,
    stream_done,
    stream_error,
    stream_heartbeat,
    stream_job,
    stream_process,
    stream_redirect,
    stream_start,
)
from .command_bridge_planner import ChatToolPlanner
from .command_bridge_executor import (
    ChatToolExecutor,
    _BLUETOOTH_TOOL_POLICY,
    _IR_TOOL_POLICY,
    _MUSIC_TOOL_POLICY,
    _MUSICQUEUE_TOOL_POLICY,
    _RESEARCH_TOOL_POLICY,
    _SEARCH_TOOL_POLICY,
    _VISION_TOOL_POLICY,
)
from .command_bridge_providers import (
    ProviderRouter,
    _GeminiRequestError,
    _GeminiTextClient,
    _MODEL_STATUS_ERROR,
    _MODEL_STATUS_NOT_CONFIGURED,
    _MODEL_STATUS_OK,
    _pin_provider_chain,
    _walk_cloud_pool_chain,
)
from .continuation_policy import (
    ContinuationAction,
    classify_outcome,
    decide_continuation,
)
from .goal_loop import GoalLoop, GoalLoopContinuation, GoalLoopReport
from .goal_planner import GoalPlanner
from .task_loop import (
    BoundedTaskLoop,
    ContinuationState,
    LoopContext,
    StepOutcome,
    resume_loop,
)
from .voice import (
    CompositeVoiceActionRegistry,
    VoiceClarification,
    VoiceIntentGate,
    VoiceUserContext,
)
from .voice import policy as voice_policy
from .voice.action_registry import DISPATCH_IR_SEND, DISPATCH_MUSIC_CALLBACK
from .voice.intent_gate import resolve_direct_prototype_action
from .voice.learning import (
    LEARNING_ACTION_FAILED,
    LEARNING_SKIPPED_NO_TOKEN,
    LEARNING_STORE_ERROR,
    commit_prototype,
    issue_learning_token,
    redeem_learning_token,
)
from .voice.metrics import METRICS as VOICE_METRICS
from .voice.models import RISK_LOW
from .voice.prototype_store import VoiceStoreCorruptError, open_voice_store

logger = logging.getLogger(__name__)

_BRIDGE_CHAT_ID = "web-bridge"
# Fixed chat id for the single-user web workflow editor. The editor keys draft
# sessions by chat id; the web console is one user, so a constant id lets a
# draft survive across the separate HTTP requests of a draft → edit → save flow.
_WF_WEB_CHAT_ID = "web-workflow"
_SH_WEB_CHAT_ID = "web-schedule"
_HEARTBEAT_SECONDS = 10.0

# Chat-tool ledger: how many recent executions to keep per conversation and how
# much of each result to quote back to the tool-plan router.


# Finished jobs linger this long so a phone that reconnects after a screen-lock
# can still fetch the final report, then they are garbage-collected.
_JOB_TTL_SECONDS = 1800.0
_GOAL_RESUME_TOKENS = frozenset({"繼續", "continue", "繼續執行", "再繼續", "resume"})
_GOAL_PENDING_TTL_SECONDS = 600.0
_GOAL_CONFIRM_INPUT = "__goal_confirm__"
_GOAL_CONTINUE_INPUT = "__goal_continue__"
_GOAL_CONTINUE_SEARCH_INPUT = "__goal_continue_search__"
_GOAL_STOP_INPUT = "__goal_stop__"
_GOAL_SAVE_WORKFLOW_INPUT = "__goal_save_workflow__"
_GOAL_STEP_GRANT = 6
_GOAL_REPLAN_LIMIT = 2
_GOAL_SEARCH_GRANT = 5

_SELLER_UNSUPPORTED_MSG = "賣家信譽快照目前尚未由本地 command bridge 支援。"

# Match slug-like words (lowercase + digits + underscore) containing an underscore.
# Used to extract a workflow_id from a free-text schedule phrase.
_WF_SLUG_RE = re.compile(r"\b([a-z][a-z0-9_\-]{2,})\b")

_CHAT_TOOL_SATISFACTION_PROMPT = """你要判斷「工具回覆」是否已真正完成「使用者原始需求」。

規則：
1. 只看是否已完成原始需求，不要看工具有沒有被成功呼叫。
2. 使用者的最新需求可能是接續對話的追問，要先用「對話脈絡」還原完整意圖再判斷：
   若完整意圖是把新資訊與先前的結果整合成結論或建議，而工具回覆只提供了新資訊、
   沒有整合出對應的結論或建議，請判定 satisfied=false。
3. 如果工具回覆表示找不到、缺少必要資訊、只完成部分需求、或沒有回答到原始需求，請判定 satisfied=false。
4. 如果工具回覆已直接完成完整意圖，才判定 satisfied=true。
5. 若原始需求是「改變狀態的動作」（例如開關某裝置、調整某設定），只要工具回覆已回報
   該動作執行成功、或回報狀態已在極限無法再改變，就算達成，請判定 satisfied=true；
   不要因為回覆沒有附加額外資訊而判定未達成。
6. 另外判斷 environment_blocked：若工具回覆顯示失敗原因是執行環境本身的障礙
   （例如目標裝置或服務無法連線、硬體離線、網路／VPN／防火牆／權限阻擋），
   換一種做法或拆成多步驟流程也無法立刻繞過，請判定 environment_blocked=true；
   若只是內容面的不足（資料不完整、方向錯誤、可改用其他工具或來源補救），
   請判定 environment_blocked=false。satisfied=true 時一律輸出 false。
7. 只能輸出 JSON，不要加任何其他文字。

請輸出：
{{"satisfied": true 或 false, "environment_blocked": true 或 false, "reason": "一句極短理由"}}

對話脈絡（用來還原追問的完整意圖；可能為空）：
{context}

使用者原始需求：
{user_input}

工具類型：
{tool_name}

工具查詢：
{tool_query}

工具回覆：
{tool_answer}
"""
_GOAL_RESULT_SATISFACTION_PROMPT = """你要判斷「執行結果」是否已真正達成「任務目標」。

規則：
1. 只看目標是否已實際達成，不要看流程有沒有跑完。
2. 如果執行結果表示找不到、反問使用者、要求更多資訊、只完成部分目標、或沒有回應目標本身，請判定 satisfied=false。
3. 如果執行結果已直接達成目標，才判定 satisfied=true。
4. 若目標是「改變狀態的動作」（例如開關某裝置、調整某設定），只要執行結果已回報
   該動作執行成功、或回報狀態已在極限無法再改變，就算達成，請判定 satisfied=true；
   不要因為結果沒有附加額外資訊而判定未達成。
5. 只能輸出 JSON，不要加任何其他文字。

請輸出：
{{"satisfied": true 或 false, "reason": "一句極短理由"}}

任務目標：
{goal}

執行結果：
{final_result}
"""
_GOAL_CONSERVATIVE_SYNTHESIS_PROMPT = """已經盡力但沒能完全達成目標。請根據「目前已取得的證據」，
給使用者一個誠實、保守、可用的最終回答。

規則：
1. 只根據下方證據作答，不要編造沒有出現的數字或事實。
2. 先給出在現有證據下能給的最佳結論或建議（即使只是暫時、有條件的）。
3. 明確指出仍然缺少、無法確認、或需要進一步查證的部分（參考下方「未達成原因」）。
4. 用使用者的語言，自然地回答，不要輸出 JSON，也不要描述你的內部流程。

任務目標：
{goal}

未達成原因（最後一次判斷）：
{last_reason}

目前已取得的證據：
{evidence}
"""
# Conversation-context budget for the satisfaction judge / goal-loop seeds.
_CONTEXT_HISTORY_TURNS = 6
_CONTEXT_TURN_CHARS = 400
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

# Search snippets are external (search-engine) text fed into the final synthesis
# LLM, so they are budgeted before entering the prompt: per-field caps bound any
# single title/snippet, and a total cap bounds the whole pack. This protects
# prompt size and shrinks the prompt-injection surface from upstream snippets.
# URLs are NOT truncated (a clipped URL is useless) — the visible sources block
# always carries the full URL.
_SOURCE_PACK_TITLE_CAP = 200
_SOURCE_PACK_SNIPPET_CAP = 500
_SOURCE_PACK_TOTAL_CAP = 4000

_NO_IMAGE_MSG = "請附上要翻譯的圖片。"
_BAD_IMAGE_TYPE_MSG = "不支援的檔案類型，請改用 JPG / PNG / WEBP / GIF 等圖片格式。"

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
        # Set by an explicit cancel (e.g. /api/command/cancel). A running goal
        # loop polls this at each stage/step boundary and stops cooperatively
        # (issue #81). Distinct from the client merely disconnecting, which by
        # design lets the worker finish so the answer stays poll-recoverable.
        self.cancel_event = threading.Event()


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

    def cancel(self, job_id: str) -> str | None:
        """Signal cooperative cancel for a running job. Returns the job's
        status at signal time — only ``running`` jobs get the event set, so a
        completed-but-not-yet-GC'd job keeps its real terminal state instead of
        falsely reporting ``interrupted``. Returns None when the job is unknown
        (already GC'd / never existed)."""
        job = self.get(job_id)
        if job is None:
            return None
        with job.lock:
            if job.status == JOB_RUNNING:
                job.cancel_event.set()
            return job.status

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
            recorder = getattr(job, "recorder", None)
        if recorder is not None:
            recorder.progress("research", text)
        self._store.save({
            "job_id": self._job_id,
            "run_id": getattr(job, "run_id", None),
            "session_id": getattr(job, "session_id", None),
            "status": JOB_RUNNING,
            "progress": progress_snapshot,
            "message": "",
            "actions": [],
            "error": None,
            "created_at": wall_created_at,
            "updated_at": time.time(),
        })


class _CallbackNotifier:
    """ResearchNotifier that forwards staged progress straight to a live
    callback (e.g. an NDJSON stream's narration queue). Used while a chat
    stream is open so long-running tools stay visible; exceptions in the
    callback must never kill the tool run itself."""

    def __init__(self, callback: Callable[[str], None]) -> None:
        self._callback = callback

    def send(self, text: str) -> None:
        try:
            self._callback(text)
        except Exception:  # noqa: BLE001
            logger.exception("live progress callback failed")


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
            # Gear-settings LOCAL provider model, not the raw env default — the
            # user can change this in the UI without redeploying, and previous
            # code silently ignored that by hardcoding "qwen3:14b" here.
            model=resolve_provider_model(settings, LLM_PROVIDER_LOCAL),
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
        # Live progress callbacks keyed by chat_id: while a chat NDJSON stream
        # is open, staged tool milestones (e.g. /research 的 ⏳/✅ 進度) are
        # forwarded to the stream instead of being dropped (no job exists for
        # the chat path, so _JobNotifier alone would swallow them).
        self._live_notifiers: dict[str, Callable[[str], None]] = {}
        # chat_id → cancel probe for the duration of a run whose commands are
        # dispatched under that chat_id (streaming goal-loop runs use
        # _BRIDGE_CHAT_ID). Same shape as _live_notifiers; consulted by
        # long-command pipelines at their cooperative checkpoints (#81).
        self._cancel_probes: dict[str, Callable[[], bool]] = {}
        self._cancel_probe_lock = threading.Lock()
        self._live_notifier_lock = threading.Lock()
        self._conversation_session: ConversationSession | None = None
        self._conversation_session_lock = threading.Lock()
        self._session_store: SessionMemoryStore | None = None
        self._event_sessions_inst: SessionEventService | None = None
        self._event_sessions_lock = threading.Lock()
        self._active_run_recorder: ContextVar[RunRecorder | None] = ContextVar(
            "active_run_recorder", default=None
        )
        self._image_renderer = None
        self._image_renderer_built = False
        self._image_renderer_lock = threading.Lock()
        # #51 PR3: per-conversation paused music plans awaiting a track choice.
        # Maps a conversation key -> {"state": ContinuationState dict, "candidates": [names]}.
        # In-process only: a resume is a follow-up turn within the same bridge run.
        self._conversation_state = ConversationState()
        self._music_continuations = self._conversation_state.music_continuations
        self._music_cont_lock = self._conversation_state.music_lock
        self._goal_continuations = self._conversation_state.goal_continuations
        self._goal_cont_lock = self._conversation_state.goal_lock
        # Provider routing collaborator (#74 R1.2): sticky pins, pool chains,
        # model metadata. Client builders stay on the bridge (ChatClientDeps).
        self._providers = ProviderRouter(self)
        self._planner = ChatToolPlanner(self, self._providers)
        self._executor = ChatToolExecutor(self)
        self._music = MusicCapability(self)
        self._workflow_capability = WorkflowCapability(self)
        self._home = HomeCapability(self)
        # Voice-intent gate (#82 PR1): clarify before open-ended chat tools
        # when a short voice utterance may be a misrecognized control command.
        # Lambdas keep the providers late-bound so instance monkeypatching of
        # the _voice_* methods still intercepts (same pattern as ChatClientDeps).
        self._voice_registry = CompositeVoiceActionRegistry(
            music_available=lambda: self._voice_music_available(),
            ir_buttons=lambda: self._voice_ir_buttons(),
        )
        self._voice_gate = VoiceIntentGate(self._voice_registry)
        # #82 PR3: personalization store, opened lazily on first voice turn.
        self._voice_store_cached: object | None = None
        self._voice_store_checked = False
        self._goal_pending_confirms = self._conversation_state.goal_pending_confirms
        self._goal_pending_lock = self._conversation_state.goal_pending_lock
        # A goal run that finished successfully offers a "💾 存為工作流" button
        # instead of auto-saving -- most completed goals are one-off asks, not
        # something the user wants cluttering the workflow list. This holds the
        # most recent completed-but-not-yet-saved workflow per conversation so
        # that button click has something to persist.
        self._goal_completed_workflows = self._conversation_state.goal_completed_workflows
        self._goal_completed_lock = self._conversation_state.goal_completed_lock
        # Per-conversation record of every chat tool / goal-loop execution
        # (success AND failure). Shown to the tool-plan router so it stops
        # re-running tools whose results (or failures) this conversation
        # already has. In-process only, bounded per conversation.
        # #53: workflow surface for web chat (NL draft + editable card buttons).
        # Built lazily — the shared editor must persist draft sessions across the
        # separate HTTP requests that a draft → reorder → save flow spans.
        self._workflow_handler: object | None = None
        self._workflow_editor: object | None = None
        self._workflow_lock = threading.Lock()
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
                        research_notifier_factory=lambda chat_id: self._research_notifier(
                            str(chat_id)
                        ),
                        research_cancel_probe_factory=lambda chat_id: self._cancel_probe(
                            str(chat_id)
                        ),
                        # poller 已經跑 VpnRotationScheduler；bridge 只要 handlers
                        start_schedulers=False,
                    )
                    self._callback_handlers = callback_handlers
                    self._command_handlers = command_handlers
                    self._view_handlers = view_handlers
                    self._item_deleter_handlers = item_deleter_handlers

                    # _build_registries was called with dynamic_tool_runner=None
                    # above (the bridge avoids a full codegen runner — see
                    # _WorkflowShimRunner), so it never wired up "/workflow".
                    # Without this, any home schedule containing a stored
                    # "/workflow run <id>" command fails with 找不到指令：/workflow
                    # when dispatched through the bridge's schedule surface
                    # (_schedulehome_surface, which runs commands through this
                    # same self._command_handlers dict).
                    #
                    # DEADLOCK GUARD: never call _workflow_surface() here.
                    # _workflow_surface() itself calls _handlers() →
                    # _ensure_registries(); when a workflow request is the FIRST
                    # request on a fresh bridge, that thread holds _workflow_lock
                    # while building registries, and an eager _workflow_surface()
                    # call from this line would re-enter the non-reentrant
                    # _workflow_lock → self-deadlock (every later /workflow call
                    # then hangs forever). Wire a lazy proxy instead: the surface
                    # is resolved at dispatch time, outside _registry_lock.
                    from telegram_core.contracts import RegisteredCommand
                    from .workflow_command import command_metadata

                    def _lazy_workflow_handler(remainder: str, chat_id: str):
                        workflow_handler, _ = self._workflow_surface()
                        return workflow_handler(remainder, chat_id)

                    command_handlers["/workflow"] = RegisteredCommand(
                        _lazy_workflow_handler,
                        ack="⚙️",
                        background=True,
                        **command_metadata("/workflow"),
                    )

    def _research_notifier(self, chat_id: str):
        """Notifier for a /research run: a live stream callback when one is
        registered for this chat_id (web chat NDJSON stream open), otherwise
        the job-backed notifier (async job + poll path)."""
        with self._live_notifier_lock:
            callback = self._live_notifiers.get(chat_id)
        if callback is not None:
            return _CallbackNotifier(callback)
        return _JobNotifier(self._jobs, chat_id, self._get_job_store())

    def _cancel_probe(self, chat_id: str) -> Callable[[], bool]:
        """Cancel probe for a long command run keyed by ``chat_id`` (#81).

        Prefers a scope probe registered via :meth:`_cancel_scope` (streaming
        goal-loop runs dispatch commands under ``_BRIDGE_CHAT_ID``); otherwise
        falls back to the job whose id IS the chat_id (the async job path,
        where ``start_async`` runs /research with ``chat_id=job.id``)."""

        def _probe() -> bool:
            with self._cancel_probe_lock:
                registered = self._cancel_probes.get(chat_id)
            if registered is not None:
                return bool(registered())
            job = self._jobs.get(chat_id)
            return job.cancel_event.is_set() if job is not None else False

        return _probe

    @contextmanager
    def _cancel_scope(
        self, probe: Callable[[], bool], chat_id: str = _BRIDGE_CHAT_ID
    ):
        """Register a cancel probe for ``chat_id`` for the duration of a run,
        saving/restoring any previous registration (mirrors _live_progress)."""
        with self._cancel_probe_lock:
            previous = self._cancel_probes.get(chat_id)
            self._cancel_probes[chat_id] = probe
        try:
            yield
        finally:
            with self._cancel_probe_lock:
                if previous is None:
                    self._cancel_probes.pop(chat_id, None)
                else:
                    self._cancel_probes[chat_id] = previous

    @contextmanager
    def _live_progress(
        self, callback: Callable[[str], None], chat_id: str = _BRIDGE_CHAT_ID
    ):
        """Register a live progress callback for ``chat_id`` for the duration
        of a streaming run. Saves and restores any previous registration so
        nested scopes (tool run upgrading into a goal loop) stay correct."""
        with self._live_notifier_lock:
            previous = self._live_notifiers.get(chat_id)
            self._live_notifiers[chat_id] = callback
        try:
            yield
        finally:
            with self._live_notifier_lock:
                if previous is None:
                    self._live_notifiers.pop(chat_id, None)
                else:
                    self._live_notifiers[chat_id] = previous

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
        recorder = self._active_run_recorder.get()
        if recorder is not None:
            recorder.tool_started(command)
        registered = self._handlers()[command]
        try:
            result = registered.handler(remainder, chat_id)
        except Exception:
            if recorder is not None:
                recorder.tool_completed(command, ok=False)
            raise
        if recorder is not None:
            recorder.tool_completed(command, ok=True)
        if isinstance(result, tuple):
            text = result[0]
            markup = result[1] if len(result) > 1 else None
            return (str(text) if text is not None else "", markup)
        return (str(result) if result is not None else "", None)

    _markup_to_actions = staticmethod(markup_to_actions)

    # --- blocking entrypoint ---------------------------------------------
    def handle(self, req: WebCommandRequest) -> WebCommandResponse:
        session_id = getattr(req, "session_id", None)
        recorder = self._event_sessions().recorder(session_id)
        recorder.accepted((req.input or "").strip())
        recorder.started()
        token = self._active_run_recorder.set(recorder)
        try:
            response = self._handle_unrecorded(req)
            recorder.planner_completed("command_bridge" if req.mode == MODE_CHAT else req.mode)
            recorder.judge_completed(
                satisfied=response.status != STATUS_ERROR,
                reason_code="response_ok" if response.status != STATUS_ERROR else "response_error",
            )
            recorder.assistant_message(response.message)
            recorder.terminal("completed" if response.status != STATUS_ERROR else "failed", message=response.message)
            return response
        except Exception as exc:  # noqa: BLE001 — surface as structured error
            logger.exception("command bridge failed mode=%s", req.mode)
            response = WebCommandResponse(
                status=STATUS_ERROR,
                message=f"後端處理失敗：{exc}",
                mode=req.mode,
                submode=req.submode,
            )
            recorder.assistant_message(response.message)
            recorder.terminal("failed", message=response.message)
            return response
        finally:
            self._active_run_recorder.reset(token)

    def _handle_unrecorded(self, req: WebCommandRequest) -> WebCommandResponse:
        """The pre-event-spine router; callers must own lifecycle recording."""
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
        except Exception:
            raise

    # --- streaming entrypoint (chat) -------------------------------------
    def stream(self, req: WebCommandRequest, request_id: str) -> Iterator[dict]:
        """Yield streaming event dicts. Chat streams token-by-token (local) or
        in one block with heartbeats (cloud); non-chat modes run blocking and
        emit a single done event so the frontend can use one code path."""
        yield stream_start(request_id)
        if req.mode != MODE_CHAT:
            # ``handle`` is already the durable blocking compatibility adapter.
            try:
                response = self.handle(req)
                if response.status == STATUS_ERROR:
                    yield stream_error(response.message)
                else:
                    yield stream_done(response.message, model_metadata=response.model_metadata)
            except Exception as exc:  # noqa: BLE001
                yield stream_error(f"後端處理失敗：{exc}")
            return
        recorder = self._event_sessions().recorder(req.session_id)
        recorder.accepted((req.input or "").strip())
        recorder.started()
        token = self._active_run_recorder.set(recorder)
        deltas: list[str] = []
        terminal = False
        try:
            for event in self._stream_chat(req):
                event_type = event.get("type")
                if event_type == "delta":
                    text = event.get("text")
                    if isinstance(text, str):
                        deltas.append(text)
                elif event_type == "process":
                    text = event.get("text")
                    if isinstance(text, str):
                        recorder.progress("stream", text)
                elif event_type == "done":
                    message = event.get("message")
                    recorder.planner_completed("stream_chat")
                    recorder.judge_completed(satisfied=True, reason_code="stream_done")
                    recorder.assistant_message(str(message or "".join(deltas)))
                    recorder.terminal("completed")
                    terminal = True
                elif event_type == "error":
                    message = str(event.get("message") or "")
                    recorder.assistant_message(message)
                    recorder.terminal("failed", message=message)
                    terminal = True
                yield event
        except Exception as exc:  # noqa: BLE001
            logger.exception("command bridge stream failed mode=%s", req.mode)
            recorder.assistant_message(f"後端處理失敗：{exc}")
            recorder.terminal("failed", message=str(exc))
            terminal = True
            yield stream_error(f"後端處理失敗：{exc}")
        finally:
            if not terminal:
                partial = "".join(deltas)
                recorder.assistant_message(partial, partial=True)
                recorder.terminal("interrupted")
            self._active_run_recorder.reset(token)

    # --- chat ------------------------------------------------------------
    def _handle_chat_blocking(self, req: WebCommandRequest) -> WebCommandResponse:
        text = (req.input or "").strip()
        if not text:
            return WebCommandResponse(
                status=STATUS_ERROR, message="請輸入訊息。", mode=MODE_CHAT
            )
        goal_control = self._handle_goal_control_input(req, text)
        if goal_control is not None:
            return goal_control
        # #51 PR3: if this conversation has a paused music plan and the user's
        # message names one of the offered tracks, resume the loop (play that
        # track) instead of routing — the live resume client for the bounded loop.
        resumed = self._maybe_resume_music_plan(req, text)
        if resumed is not None:
            return resumed
        resumed_goal = self._maybe_resume_goal_loop(req, text)
        if resumed_goal is not None:
            return resumed_goal
        if not chat_backend_enabled(self.settings, req.chat_backend):
            return WebCommandResponse(
                status=STATUS_ERROR,
                message=self._chat_backend_disabled_message(req.chat_backend),
                mode=MODE_CHAT,
            )
        # #82 PR4: mature-prototype direct fast path — must run BEFORE the
        # router so a confident control match never enters /search planning
        # (§15.2: Chat router call count = 0 for mature prototypes).
        direct = self._maybe_voice_direct_action(req)
        if direct is not None:
            return direct
        prompt = build_chat_prompt(req.input, req.history)
        plan, metadata = self._select_chat_tool_plan(req)
        if plan is not None and plan.tool == CHAT_TOOL_CREATE_WORKFLOW:
            # No streaming connection to carry a "redirect" event here, so run
            # the same dedicated workflow-creation entrypoint the frontend
            # hits after a streamed redirect (run_workflow_command) directly.
            result = self.run_workflow_command(
                f"create {plan.query}", chat_backend=req.chat_backend
            )
            return WebCommandResponse(
                status=str(result.get("status") or STATUS_OK),
                message=str(result.get("message") or ""),
                mode=MODE_CHAT,
            )
        if plan is not None and plan.tool == CHAT_TOOL_GOAL:
            return self._run_goal_loop_blocking(req, plan.query, planner_metadata=metadata)
        if plan is not None and plan.tool != CHAT_TOOL_NO_TOOL:
            # #82 PR1 voice guard: must run BEFORE the tool executes so a
            # misrecognized control utterance never triggers /search.
            clarification = self._maybe_voice_clarification(req, plan)
            if clarification is not None:
                return WebCommandResponse(
                    status=STATUS_OK,
                    message=self._voice_clarification_message(clarification),
                    mode=MODE_CHAT,
                    clarification=clarification.to_dict(),
                )
            try:
                tool_result = self._run_chat_tool(req, plan)
                logger.info(
                    "[chat-tool] tool=%s sources=%d summary=%r",
                    plan.tool, tool_result.source_count, tool_result.result_summary,
                )
                upgraded = self._maybe_upgrade_tool_result_to_goal_loop(
                    req,
                    plan,
                    tool_result,
                    planner_metadata=metadata,
                )
                if upgraded is not None:
                    return upgraded
                return WebCommandResponse(
                    status=STATUS_OK,
                    message=tool_result.answer,
                    mode=MODE_CHAT,
                    model_metadata=tool_result.model_metadata,
                )
            except Exception as exc:  # noqa: BLE001 — surface, don't crash the turn
                logger.exception("chat tool failed tool=%s", plan.tool)
                return WebCommandResponse(
                    status=STATUS_ERROR,
                    message=f"工具執行失敗：{exc}",
                    mode=MODE_CHAT,
                )
        if plan is not None and plan.tool == CHAT_TOOL_NO_TOOL:
            message = plan.answer
        else:
            message, metadata = self._generate_chat_response_blocking(
                prompt, req.chat_backend, conversation_key=self._conversation_key(req)
            )
        return WebCommandResponse(
            status=STATUS_OK, message=message, mode=MODE_CHAT, model_metadata=metadata
        )

    def _stream_chat(self, req: WebCommandRequest) -> Iterator[dict]:
        text = (req.input or "").strip()
        if not text:
            yield stream_error("請輸入訊息。")
            return
        if self._is_goal_control_input(text):
            yield from self._stream_goal_control_input(req, text)
            return
        if self._should_resume_goal_loop(req, text):
            yield from self._stream_resume_goal_loop(req)
            return
        # Intent fast-paths intentionally do not run here. Web Chat should let
        # the selected model choose direct answer / registered tool / __goal__,
        # so cloud-model capability can be evaluated without regex or embedding
        # shortcuts masking the model's decision.
        if not chat_backend_enabled(self.settings, req.chat_backend):
            yield stream_error(self._chat_backend_disabled_message(req.chat_backend))
            return

        # #82 PR4 voice direct fast path (streaming twin of the blocking path).
        direct = self._maybe_voice_direct_action(req)
        if direct is not None:
            yield stream_done(direct.message, direct_action=direct.direct_action)
            return

        # WP-5a: vision observe step — if the current turn has an image attachment,
        # run the vision pool and inject a textual observation before the planner.
        observation: str | None = None
        if req.has_image_attachment:
            obs_result = yield from self._stream_vision_observe(req)
            if obs_result is not None:
                observation = obs_result
                yield stream_process(f"🔍 圖片觀察：{observation}")

        prompt = build_chat_prompt(req.input, req.history)
        # The observation also reaches the router-failed fallback prompt below.
        if observation:
            prompt = f"【圖片觀察】\n{observation}\n\n{prompt}"

        if observation:
            plan, metadata = yield from self._stream_chat_tool_plan(req, observation)
        else:
            plan, metadata = yield from self._stream_chat_tool_plan(req)
        if plan is not None and plan.tool == CHAT_TOOL_CREATE_WORKFLOW:
            yield stream_redirect("create_workflow", plan.query)
            return
        if plan is not None and plan.tool == CHAT_TOOL_GOAL:
            goal_seeds = {"image_observation": observation} if observation else None
            yield from self._stream_goal_loop(
                req, plan.query, planner_metadata=metadata, seed_variables=goal_seeds
            )
            return
        if plan is not None and plan.tool != CHAT_TOOL_NO_TOOL:
            # #82 PR1 voice guard (streaming twin of the blocking path).
            clarification = self._maybe_voice_clarification(req, plan)
            if clarification is not None:
                yield stream_done(
                    self._voice_clarification_message(clarification),
                    clarification=clarification.to_dict(),
                )
                return
            yield from self._stream_chat_tool(req, plan)
            return
        if plan is not None and plan.tool == CHAT_TOOL_NO_TOOL:
            if plan.answer:
                yield stream_delta(plan.answer)
            yield stream_done(plan.answer, model_metadata=metadata)
        else:
            # plan is None only when the router failed or emitted untrusted
            # output — say so, instead of silently degrading to plain chat.
            yield stream_delta("（工具路由暫時不可用，改以一般模式直接回答）\n")
            yield from self._stream_chat_response(
                prompt, req.chat_backend, conversation_key=self._conversation_key(req)
            )

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
            query = " ".join(part for part in (artist, qualifier, "シングル") if part)
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

    # --- chat tool ledger --------------------------------------------------
    def _record_chat_tool_run(
        self,
        req: WebCommandRequest,
        tool: str,
        query: str,
        *,
        status: str,
        summary: str,
    ) -> None:
        self._executor.record_run(req, tool, query, status=status, summary=summary)

    def _chat_tool_ledger_entries(self, req: WebCommandRequest) -> list[dict]:
        return self._executor.ledger_entries(req)

    def _record_goal_loop_run(self, req: WebCommandRequest, goal: str, report) -> None:
        try:
            steps = ""
            workflow = getattr(report, "workflow", None)
            if workflow is not None and getattr(workflow, "steps", None):
                from .task_workspace import describe_workflow_step

                steps = "；".join(describe_workflow_step(s) for s in workflow.steps)
            summary = str(getattr(report, "final_result", "") or "")
            if steps:
                summary = f"步驟：{steps}｜結果：{summary}"
            self._record_chat_tool_run(
                req,
                CHAT_TOOL_GOAL,
                goal,
                status="ok" if getattr(report, "done", False) else "partial",
                summary=summary,
            )
        except Exception:  # noqa: BLE001
            logger.exception("chat tool ledger: failed to record goal loop run")

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

    def _store_goal_continuation(
        self,
        req: WebCommandRequest,
        continuation: GoalLoopContinuation,
        *,
        chat_backend: str,
        planner_metadata: ModelMetadata | None,
    ) -> None:
        with self._goal_cont_lock:
            self._goal_continuations[self._conversation_key(req)] = {
                "continuation": continuation.to_dict(),
                "chat_backend": chat_backend,
                "planner_metadata": planner_metadata.to_dict() if planner_metadata is not None else None,
                "created_at": time.time(),
            }

    def _goal_pending_confirm_entry(self, req: WebCommandRequest) -> dict | None:
        with self._goal_pending_lock:
            entry = self._goal_pending_confirms.get(self._conversation_key(req))
            if entry is None:
                return None
            if time.time() - float(entry.get("created_at") or 0.0) > _GOAL_PENDING_TTL_SECONDS:
                self._goal_pending_confirms.pop(self._conversation_key(req), None)
                return None
            return entry

    def _clear_goal_pending_confirm(self, req: WebCommandRequest) -> None:
        with self._goal_pending_lock:
            self._goal_pending_confirms.pop(self._conversation_key(req), None)

    @staticmethod
    def _is_goal_control_input(text: str) -> bool:
        return text in {
            _GOAL_CONFIRM_INPUT,
            _GOAL_CONTINUE_INPUT,
            _GOAL_CONTINUE_SEARCH_INPUT,
            _GOAL_STOP_INPUT,
            _GOAL_SAVE_WORKFLOW_INPUT,
        }

    def _save_goal_workflow(self, req: WebCommandRequest) -> WebCommandResponse:
        with self._goal_completed_lock:
            workflow = self._goal_completed_workflows.pop(self._conversation_key(req), None)
        if workflow is None:
            return WebCommandResponse(
                status=STATUS_ERROR,
                message="沒有可儲存的工作流，請重新執行一次目標。",
                mode=MODE_CHAT,
            )
        from .task_workspace import WorkflowStore

        runner = _WorkflowShimRunner(self.settings)
        store = WorkflowStore(Path(runner.tools_dir).parent / "workflow_store")
        store.save(workflow)
        return WebCommandResponse(
            status=STATUS_OK,
            message=f"✅ 已存為工作流：{workflow.id}，可在「📋 工作流列表」找到。",
            mode=MODE_CHAT,
        )

    def _handle_goal_control_input(
        self, req: WebCommandRequest, text: str
    ) -> WebCommandResponse | None:
        if text == _GOAL_CONFIRM_INPUT:
            return self._confirm_goal_loop(req)
        if text == _GOAL_CONTINUE_INPUT:
            entry = self._goal_continuation_entry(req)
            if entry is None:
                return WebCommandResponse(
                    status=STATUS_ERROR,
                    message="目標續跑已逾時，請重新描述一次需求。",
                    mode=MODE_CHAT,
                )
            return self._resume_goal_loop(req, entry)
        if text == _GOAL_CONTINUE_SEARCH_INPUT:
            return self._continue_goal_loop_with_search_extension(req)
        if text == _GOAL_STOP_INPUT:
            return self._stop_goal_loop(req)
        if text == _GOAL_SAVE_WORKFLOW_INPUT:
            return self._save_goal_workflow(req)
        return None

    def _stream_goal_control_input(
        self, req: WebCommandRequest, text: str
    ) -> Iterator[dict]:
        if text == _GOAL_CONFIRM_INPUT:
            yield from self._stream_confirm_goal_loop(req)
            return
        if text == _GOAL_CONTINUE_INPUT:
            yield from self._stream_resume_goal_loop(req)
            return
        if text == _GOAL_CONTINUE_SEARCH_INPUT:
            yield from self._stream_continue_goal_loop_with_search_extension(req)
            return
        if text == _GOAL_STOP_INPUT:
            response = self._stop_goal_loop(req)
            if response.status == STATUS_ERROR:
                yield stream_error(response.message)
            else:
                yield stream_done(
                    response.message,
                    model_metadata=response.model_metadata,
                    actions=self._stream_actions(response),
                )
            return
        if text == _GOAL_SAVE_WORKFLOW_INPUT:
            response = self._save_goal_workflow(req)
            if response.status == STATUS_ERROR:
                yield stream_error(response.message)
            else:
                yield stream_done(
                    response.message,
                    model_metadata=response.model_metadata,
                    actions=self._stream_actions(response),
                )
            return
        yield stream_error("未知的目標控制指令。")

    def _goal_continuation_entry(self, req: WebCommandRequest) -> dict | None:
        with self._goal_cont_lock:
            entry = self._goal_continuations.get(self._conversation_key(req))
            if entry is None:
                return None
            if time.time() - float(entry.get("created_at") or 0.0) > _GOAL_PENDING_TTL_SECONDS:
                self._goal_continuations.pop(self._conversation_key(req), None)
                return None
            return entry

    def _should_resume_goal_loop(self, req: WebCommandRequest, text: str) -> bool:
        return text.strip().lower() in _GOAL_RESUME_TOKENS and self._goal_continuation_entry(req) is not None

    def _maybe_resume_goal_loop(
        self, req: WebCommandRequest, text: str
    ) -> WebCommandResponse | None:
        entry = self._goal_continuation_entry(req) if text.strip().lower() in _GOAL_RESUME_TOKENS else None
        if not entry:
            return None
        return self._resume_goal_loop(req, entry)

    def _stream_resume_goal_loop(self, req: WebCommandRequest) -> Iterator[dict]:
        result: dict[str, object] = {}
        done = threading.Event()
        entry = self._goal_continuation_entry(req)
        if not entry:
            yield stream_error("目標續跑已逾時，請重新描述一次需求。")
            return

        narration_queue: queue.Queue[str] = queue.Queue()

        def _worker() -> None:
            try:
                with self._live_progress(narration_queue.put):
                    result["response"] = self._resume_goal_loop(
                        req, entry, narrator=narration_queue.put
                    )
            except Exception as exc:  # noqa: BLE001
                result["error"] = str(exc)
            finally:
                done.set()

        threading.Thread(target=_worker, daemon=True).start()
        last_beat = time.time()
        while not done.is_set() or not narration_queue.empty():
            try:
                line = narration_queue.get(timeout=0.5)
            except queue.Empty:
                if time.time() - last_beat >= _HEARTBEAT_SECONDS:
                    yield stream_heartbeat()
                    last_beat = time.time()
                continue
            yield stream_delta(f"{line}\n")
            last_beat = time.time()
        if "error" in result:
            yield stream_error(f"目標續跑失敗：{result['error']}")
            return
        response = result.get("response")
        if not isinstance(response, WebCommandResponse):
            yield stream_error("目標續跑失敗：缺少結果。")
            return
        if response.status == STATUS_ERROR:
            yield stream_error(response.message)
            return
        yield stream_done(
            response.message,
            model_metadata=response.model_metadata,
            actions=self._stream_actions(response),
        )

    def _continue_goal_loop_with_search_extension(
        self,
        req: WebCommandRequest,
        narrator: Callable[[str], None] | None = None,
    ) -> WebCommandResponse:
        entry = self._goal_continuation_entry(req)
        if entry is None:
            return WebCommandResponse(
                status=STATUS_ERROR,
                message="目標續跑已逾時，請重新描述一次需求。",
                mode=MODE_CHAT,
            )
        continuation_data = entry.get("continuation")
        if not isinstance(continuation_data, dict):
            return WebCommandResponse(
                status=STATUS_ERROR,
                message="目標續跑狀態已損壞，請重新描述一次需求。",
                mode=MODE_CHAT,
            )
        continuation = GoalLoopContinuation.from_dict(continuation_data)
        budget = continuation.state.budget or {}
        search_limit = int(budget.get("search_limit") or 0)
        search_used = int(budget.get("search_used") or 0)
        search_hard_limit = int(budget.get("search_hard_limit") or 0)
        if search_hard_limit and search_used >= search_hard_limit:
            return WebCommandResponse(
                status=STATUS_OK,
                message=f"今日搜尋硬上限已達 ({search_used}/{search_hard_limit})，明天重置。",
                mode=MODE_CHAT,
            )
        granted = self._grant_goal_search_extension(_GOAL_SEARCH_GRANT)
        if granted <= 0:
            hard = search_hard_limit or search_limit
            return WebCommandResponse(
                status=STATUS_OK,
                message=f"今日搜尋硬上限已達 ({search_used}/{hard})，明天重置。",
                mode=MODE_CHAT,
            )
        return self._resume_goal_loop(req, entry, narrator=narrator)

    def _stream_continue_goal_loop_with_search_extension(
        self, req: WebCommandRequest
    ) -> Iterator[dict]:
        result: dict[str, object] = {}
        done = threading.Event()
        narration_queue: queue.Queue[str] = queue.Queue()

        def _worker() -> None:
            try:
                with self._live_progress(narration_queue.put):
                    result["response"] = self._continue_goal_loop_with_search_extension(
                        req, narrator=narration_queue.put
                    )
            except Exception as exc:  # noqa: BLE001
                result["error"] = str(exc)
            finally:
                done.set()

        threading.Thread(target=_worker, daemon=True).start()
        last_beat = time.time()
        while not done.is_set() or not narration_queue.empty():
            try:
                line = narration_queue.get(timeout=0.5)
            except queue.Empty:
                if time.time() - last_beat >= _HEARTBEAT_SECONDS:
                    yield stream_heartbeat()
                    last_beat = time.time()
                continue
            yield stream_delta(f"{line}\n")
            last_beat = time.time()
        if "error" in result:
            yield stream_error(f"目標續跑失敗：{result['error']}")
            return
        response = result.get("response")
        if not isinstance(response, WebCommandResponse):
            yield stream_error("目標續跑失敗：缺少結果。")
            return
        if response.status == STATUS_ERROR:
            yield stream_error(response.message)
            return
        yield stream_done(
            response.message,
            model_metadata=response.model_metadata,
            actions=self._stream_actions(response),
        )

    def _confirm_goal_loop(
        self,
        req: WebCommandRequest,
        narrator: Callable[[str], None] | None = None,
    ) -> WebCommandResponse:
        entry = self._goal_pending_confirm_entry(req)
        if entry is None:
            return WebCommandResponse(
                status=STATUS_ERROR,
                message="目標確認已逾時，請重新描述一次需求。",
                mode=MODE_CHAT,
            )
        self._clear_goal_pending_confirm(req)
        continuation_data = entry.get("continuation")
        if not isinstance(continuation_data, dict):
            return WebCommandResponse(
                status=STATUS_ERROR,
                message="目標確認狀態已損壞，請重新描述一次需求。",
                mode=MODE_CHAT,
            )
        chat_backend = str(entry.get("chat_backend") or req.chat_backend or CHAT_BACKEND_LOCAL)
        try:
            report = self._execute_goal_loop(
                goal="",
                chat_backend=chat_backend,
                resume=GoalLoopContinuation.from_dict(continuation_data),
                narrator=narrator,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("goal loop confirm failed")
            return WebCommandResponse(
                status=STATUS_ERROR,
                message=f"目標執行失敗：{exc}",
                mode=MODE_CHAT,
            )
        if report.continuation is not None:
            self._store_goal_continuation(
                req,
                report.continuation,
                chat_backend=chat_backend,
                planner_metadata=None,
            )
        else:
            with self._goal_cont_lock:
                self._goal_continuations.pop(self._conversation_key(req), None)
        return WebCommandResponse(
            status=STATUS_OK,
            message=self._goal_loop_response_message(
                report, narrated_live=narrator is not None
            ),
            mode=MODE_CHAT,
            actions=self._goal_web_actions(req, report),
        )

    def _stream_confirm_goal_loop(self, req: WebCommandRequest) -> Iterator[dict]:
        result: dict[str, object] = {}
        done = threading.Event()
        narration_queue: queue.Queue[str] = queue.Queue()

        def _worker() -> None:
            try:
                with self._live_progress(narration_queue.put):
                    result["response"] = self._confirm_goal_loop(
                        req, narrator=narration_queue.put
                    )
            except Exception as exc:  # noqa: BLE001
                result["error"] = str(exc)
            finally:
                done.set()

        threading.Thread(target=_worker, daemon=True).start()
        last_beat = time.time()
        while not done.is_set() or not narration_queue.empty():
            try:
                line = narration_queue.get(timeout=0.5)
            except queue.Empty:
                if time.time() - last_beat >= _HEARTBEAT_SECONDS:
                    yield stream_heartbeat()
                    last_beat = time.time()
                continue
            yield stream_delta(f"{line}\n")
            last_beat = time.time()
        if "error" in result:
            yield stream_error(f"目標執行失敗：{result['error']}")
            return
        response = result.get("response")
        if not isinstance(response, WebCommandResponse):
            yield stream_error("目標執行失敗：缺少結果。")
            return
        if response.status == STATUS_ERROR:
            yield stream_error(response.message)
            return
        yield stream_done(
            response.message,
            model_metadata=response.model_metadata,
            actions=self._stream_actions(response),
        )

    def _resume_goal_loop(
        self,
        req: WebCommandRequest,
        entry: dict,
        narrator: Callable[[str], None] | None = None,
    ) -> WebCommandResponse:
        continuation_data = entry.get("continuation")
        if not isinstance(continuation_data, dict):
            return WebCommandResponse(
                status=STATUS_ERROR,
                message="目標續跑狀態已損壞，請重新描述一次需求。",
                mode=MODE_CHAT,
            )
        chat_backend = str(entry.get("chat_backend") or req.chat_backend or CHAT_BACKEND_LOCAL)
        try:
            report = self._execute_goal_loop(
                goal="",
                chat_backend=chat_backend,
                resume=GoalLoopContinuation.from_dict(continuation_data),
                narrator=narrator,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("goal loop resume failed")
            return WebCommandResponse(
                status=STATUS_ERROR,
                message=f"目標續跑失敗：{exc}",
                mode=MODE_CHAT,
            )
        with self._goal_cont_lock:
            if report.continuation is None:
                self._goal_continuations.pop(self._conversation_key(req), None)
            else:
                self._goal_continuations[self._conversation_key(req)] = {
                    "continuation": report.continuation.to_dict(),
                    "chat_backend": chat_backend,
                    "planner_metadata": entry.get("planner_metadata"),
                    "created_at": time.time(),
                }
        return WebCommandResponse(
            status=STATUS_OK,
            message=self._goal_loop_response_message(
                report, narrated_live=narrator is not None
            ),
            mode=MODE_CHAT,
            actions=self._goal_web_actions(req, report),
        )

    def _stop_goal_loop(self, req: WebCommandRequest) -> WebCommandResponse:
        entry = self._goal_continuation_entry(req)
        if entry is None:
            entry = self._goal_pending_confirm_entry(req)
            if entry is None:
                return WebCommandResponse(
                    status=STATUS_ERROR,
                    message="沒有可停止的目標。",
                    mode=MODE_CHAT,
                )
            self._clear_goal_pending_confirm(req)
            continuation_data = entry.get("continuation")
            if not isinstance(continuation_data, dict):
                return WebCommandResponse(
                    status=STATUS_ERROR,
                    message="目標狀態已損壞，請重新描述一次需求。",
                    mode=MODE_CHAT,
                )
            continuation = GoalLoopContinuation.from_dict(continuation_data)
        else:
            with self._goal_cont_lock:
                self._goal_continuations.pop(self._conversation_key(req), None)
            continuation_data = entry.get("continuation")
            if not isinstance(continuation_data, dict):
                return WebCommandResponse(
                    status=STATUS_ERROR,
                    message="目標狀態已損壞，請重新描述一次需求。",
                    mode=MODE_CHAT,
                )
            continuation = GoalLoopContinuation.from_dict(continuation_data)
        chat_backend = str(
            entry.get("chat_backend") or req.chat_backend or CHAT_BACKEND_LOCAL
        )
        synthesized = self._synthesize_goal_stop_answer(continuation, chat_backend)
        if synthesized:
            message = f"已停止目前目標。\n\n{synthesized}"
        else:
            message = self._format_goal_stop_summary(continuation)
        return WebCommandResponse(
            status=STATUS_OK,
            message=message,
            mode=MODE_CHAT,
        )

    def _synthesize_goal_stop_answer(
        self, continuation: GoalLoopContinuation, chat_backend: str
    ) -> str:
        """Best-effort hedged answer when the user stops a goal mid-run (#81):
        relay whatever evidence the run already gathered through the generic
        conservative synthesizer instead of returning a bare progress dump.
        Fail-soft: any problem (no trace, no evidence, LLM error) returns ""
        so the caller falls back to the plain stop summary."""
        trace = continuation.trace
        if trace is None:
            return ""
        seeds = {
            name: str(var.value)
            for name, var in trace.variables.items()
            if str(var.value).strip()
        }
        if not seeds:
            return ""
        try:
            synthesize = self._goal_conservative_synthesizer(chat_backend)
            return synthesize(
                continuation.state.goal,
                seeds,
                continuation.state.stop_condition or "使用者停止",
            ).strip()
        except Exception:  # noqa: BLE001
            logger.exception("goal stop synthesis failed")
            return ""

    @staticmethod
    def _format_goal_stop_summary(continuation: GoalLoopContinuation) -> str:
        parts = [
            "已停止目前目標。",
            "\n".join(continuation.narration).strip(),
        ]
        if continuation.trace is not None and continuation.trace.final_result:
            parts.append(f"目前最後結果：{continuation.trace.final_result}")
        elif continuation.state.current_status:
            parts.append(f"目前進度：{continuation.state.current_status}")
        return "\n\n".join(part for part in parts if part)

    def _run_goal_loop_blocking(
        self,
        req: WebCommandRequest,
        goal: str,
        *,
        planner_metadata: ModelMetadata | None,
        narrator: Callable[[str], None] | None = None,
        seed_variables: dict[str, str] | None = None,
        seed_operations: dict[str, str] | None = None,
    ) -> WebCommandResponse:
        try:
            report = self._execute_goal_loop(
                goal=goal,
                chat_backend=req.chat_backend,
                narrator=narrator,
                seed_variables=seed_variables,
                seed_operations=seed_operations,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("goal loop failed goal=%r", goal)
            self._record_chat_tool_run(
                req, CHAT_TOOL_GOAL, goal, status="error", summary=str(exc)
            )
            return WebCommandResponse(
                status=STATUS_ERROR,
                message=f"目標執行失敗：{exc}",
                mode=MODE_CHAT,
            )
        self._record_goal_loop_run(req, goal, report)
        self._sync_goal_continuation(req, report, planner_metadata=planner_metadata)
        return WebCommandResponse(
            status=STATUS_OK,
            message=self._goal_loop_response_message(
                report, narrated_live=narrator is not None
            ),
            mode=MODE_CHAT,
            model_metadata=None,
            actions=self._goal_web_actions(req, report),
        )

    def _sync_goal_continuation(
        self,
        req: WebCommandRequest,
        report,
        *,
        planner_metadata: ModelMetadata | None,
    ) -> None:
        if report.continuation is not None:
            self._store_goal_continuation(
                req,
                report.continuation,
                chat_backend=req.chat_backend,
                planner_metadata=planner_metadata,
            )
        else:
            with self._goal_cont_lock:
                self._goal_continuations.pop(self._conversation_key(req), None)

    def _stream_goal_loop(
        self,
        req: WebCommandRequest,
        goal: str,
        *,
        planner_metadata: ModelMetadata | None,
        seed_variables: dict[str, str] | None = None,
    ) -> Iterator[dict]:
        result: dict[str, object] = {}
        done = threading.Event()
        abandoned = threading.Event()
        narration_queue: queue.Queue[str] = queue.Queue()

        # Back this long run with a job (issue #81 PR3). A mobile stream that
        # drops mid-research (screen-lock/backgrounding kills the held NDJSON
        # connection — heartbeats can't stop the OS) can then poll this job id
        # for the final answer, instead of the answer only surviving as a
        # session-memory reload. The worker persists the terminal state whether
        # or not the client is still attached.
        job = self._jobs.create()
        recorder = self._active_run_recorder.get() or self._event_sessions().recorder(req.session_id)
        job.run_id = recorder.run_id
        job.session_id = getattr(req, "session_id", None)
        job.recorder = recorder
        store = self._get_job_store()
        store.save({
            "job_id": job.id,
            "run_id": job.run_id,
            "session_id": job.session_id,
            "status": JOB_RUNNING,
            "progress": [],
            "message": "",
            "actions": [],
            "error": None,
            "created_at": job.wall_created_at,
            "updated_at": job.wall_created_at,
        })

        def _emit(line: str) -> None:
            narration_queue.put(line)
            self._jobs.append_progress(job.id, line)

        def _persist_job(*, status: str, message: str, error: str | None) -> None:
            with job.lock:
                job.status = status
                job.message = message
                job.error = error
                progress_snapshot = list(job.progress)
            store.save({
                "job_id": job.id,
                "run_id": job.run_id,
                "session_id": job.session_id,
                "status": status,
                "progress": progress_snapshot,
                "message": message,
                "actions": [],
                "error": error,
                "created_at": job.wall_created_at,
                "updated_at": time.time(),
            })
            # Journal injection happens after the authoritative terminal job
            # snapshot. A later reconnect sees one final message and one
            # terminal state even if no stream stayed open.
            if message:
                recorder.assistant_message(message)
            recorder.terminal(
                "cancelled" if status == JOB_INTERRUPTED else ("completed" if status == JOB_DONE else "failed"),
                message=message or (error or ""),
            )

        def _worker() -> None:
            try:
                # Workflow steps that run registered commands (e.g. /research)
                # emit staged milestones; surface them on this stream live.
                # The cancel scope lets those commands' own pipelines observe
                # this job's cancel flag MID-step (they dispatch under
                # _BRIDGE_CHAT_ID), not just the loop's stage boundaries.
                with self._live_progress(_emit), \
                        self._cancel_scope(job.cancel_event.is_set):
                    result["report"] = self._execute_goal_loop(
                        goal=goal,
                        chat_backend=req.chat_backend,
                        narrator=_emit,
                        seed_variables=seed_variables,
                        cancel_check=job.cancel_event.is_set,
                    )
            except Exception as exc:  # noqa: BLE001
                result["error"] = str(exc)
            finally:
                done.set()
                report = result.get("report")
                # Persist the terminal outcome to the job unconditionally so a
                # reconnecting/polling client recovers it deterministically.
                try:
                    if job.cancel_event.is_set():
                        # Explicit cancel: terminal interrupted state, with any
                        # progress gathered so far, so a poll shows a clean stop
                        # rather than a phantom "done" or a hard error.
                        _persist_job(
                            status=JOB_INTERRUPTED,
                            message=(
                                self._format_goal_loop_final_answer(report)
                                if isinstance(report, GoalLoopReport)
                                else "任務已取消。"
                            ),
                            error=None,
                        )
                    elif isinstance(report, GoalLoopReport):
                        _persist_job(
                            status=JOB_DONE,
                            message=self._format_goal_loop_final_answer(report),
                            error=None,
                        )
                    elif "error" in result:
                        _persist_job(
                            status=JOB_ERROR, message="",
                            error=f"目標執行失敗：{result['error']}",
                        )
                except Exception:  # noqa: BLE001
                    logger.exception("command bridge: failed to persist goal job result")
                # Client gone (page closed / phone locked): the run still
                # finished, so persist the continuation for the next page load.
                # The final answer itself is recovered via the job poll above,
                # so we no longer push it into session memory (which would
                # double-render alongside the polled result).
                if abandoned.is_set() and isinstance(report, GoalLoopReport):
                    try:
                        self._record_goal_loop_run(req, goal, report)
                        self._sync_goal_continuation(
                            req, report, planner_metadata=planner_metadata
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "command bridge: failed to persist abandoned goal result"
                        )

        threading.Thread(target=_worker, daemon=True).start()
        # Announce the recovery job id before the long work so the client has
        # it well before any ~26s mobile drop.
        yield stream_job(job.id)
        last_beat = time.time()
        try:
            while not done.is_set() or not narration_queue.empty():
                try:
                    line = narration_queue.get(timeout=0.5)
                except queue.Empty:
                    if time.time() - last_beat >= _HEARTBEAT_SECONDS:
                        yield stream_heartbeat()
                        last_beat = time.time()
                    continue
                yield stream_delta(f"{line}\n")
                last_beat = time.time()
        except GeneratorExit:
            abandoned.set()
            raise
        if "error" in result:
            self._record_chat_tool_run(
                req, CHAT_TOOL_GOAL, goal, status="error", summary=str(result["error"])
            )
            yield stream_error(f"目標執行失敗：{result['error']}")
            return
        report = result.get("report")
        if not isinstance(report, GoalLoopReport):
            yield stream_error("目標執行失敗：缺少結果。")
            return
        self._record_goal_loop_run(req, goal, report)
        self._sync_goal_continuation(req, report, planner_metadata=planner_metadata)
        actions = [a.to_dict() for a in self._goal_web_actions(req, report)]
        yield stream_done(
            self._format_goal_loop_final_answer(report),
            model_metadata=None,
            actions=actions or None,
        )

    def _execute_goal_loop(
        self,
        *,
        goal: str,
        chat_backend: str,
        resume: GoalLoopContinuation | None = None,
        narrator: Callable[[str], None] | None = None,
        seed_variables: dict[str, str] | None = None,
        seed_operations: dict[str, str] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ):
        runner = _WorkflowShimRunner(self.settings)
        # One rotation cursor shared by every LLM call this run makes (draft,
        # each replan, the result judge, each llm_transform step) so a long
        # multi-step goal spreads load across the cloud pool instead of
        # retrying provider[0] first on every single call.
        pool_rotation = CloudPoolRotation()
        loop = GoalLoop(
            goal=resume.state.goal if resume is not None else goal,
            planner=self._build_goal_planner(
                chat_backend, runner, progress=narrator, pool_rotation=pool_rotation
            ),
            executor=runner,
            command_registry=self._handlers(),
            llm_client=self._goal_llm_transform_client(chat_backend, runner, pool_rotation),
            trace_saver=self._goal_trace_saver(runner),
            chat_id=_BRIDGE_CHAT_ID,
            max_steps=_GOAL_STEP_GRANT,
            replan_limit=_GOAL_REPLAN_LIMIT,
            narrator=narrator,
            result_judge=self._goal_result_judge(chat_backend, pool_rotation=pool_rotation),
            seed_variables=seed_variables,
            seed_operations=seed_operations,
            conservative_synthesizer=self._goal_conservative_synthesizer(
                chat_backend, pool_rotation=pool_rotation
            ),
            cancel_check=cancel_check,
        )
        report = loop.run(resume=resume)
        logger.info(
            "[chat-goal-plan] done=%s replans=%s final=%r",
            report.done,
            report.replans_used,
            report.final_result,
        )
        return report

    def _build_goal_planner(
        self,
        chat_backend: str,
        runner: _WorkflowShimRunner,
        progress: Callable[[str], None] | None = None,
        pool_rotation: "CloudPoolRotation | None" = None,
    ) -> GoalPlanner:
        return GoalPlanner(
            catalog=runner.catalog,
            llm_client=self._goal_planner_client(chat_backend),
            command_registry=self._handlers(),
            command_usage_resolver=lambda command, _registry: self._registered_command_usage(command),
            progress=progress,
            pool_rotation=pool_rotation,
        )

    def _goal_llm_transform_client(
        self,
        chat_backend: str,
        runner: "_WorkflowShimRunner",
        pool_rotation: "CloudPoolRotation | None",
    ):
        """Adapter for ``WorkflowRunner``'s llm_transform steps.

        Previously this always used ``runner.client`` (local Ollama), no
        matter which backend the user picked for the goal loop — the cause of
        llm_transform hallucinating with a weak local model even when cloud
        was selected. Routes through the selected backend instead, sharing
        ``pool_rotation`` with draft/replan/judge for the cloud pool; local
        Ollama stays as the last-resort fallback so a transform step never
        hard-fails just because the cloud is unreachable.
        """
        bridge = self

        class _GoalTransformClient:
            def generate(self, prompt: str, *, temperature: float = 0.7) -> str:
                if chat_backend == CHAT_BACKEND_LOCAL:
                    return runner.client.generate(prompt, temperature=temperature)
                if chat_backend == CHAT_BACKEND_CLOUD_POOL:
                    chain = bridge._cloud_pool_chain()
                    if pool_rotation is not None:
                        chain = pool_rotation.rotate(chain)
                    text, _provider, _model, _attempts = _walk_cloud_pool_chain(
                        chain, prompt, temperature=temperature
                    )
                    if text is not None:
                        return text
                    logger.warning(
                        "[goal-loop] llm_transform: cloud pool exhausted, falling back to local"
                    )
                    return runner.client.generate(prompt, temperature=temperature)
                single_backend = {
                    CHAT_BACKEND_GEMINI: (
                        lambda: bridge._build_gemini_chat_client(bridge._gemini_primary_model())
                    ),
                    CHAT_BACKEND_CLOUD_MISTRAL: bridge._build_mistral_chat_client,
                    CHAT_BACKEND_CLOUD_PICKLE: bridge._build_cloud_chat_client,
                    CHAT_BACKEND_CLOUD_NVIDIA: bridge._build_nvidia_chat_client,
                }.get(chat_backend)
                if single_backend is not None:
                    client = single_backend()
                    if client is not None:
                        try:
                            return client.generate(prompt, temperature=temperature)
                        except Exception:  # noqa: BLE001
                            logger.warning(
                                "[goal-loop] llm_transform: backend=%s failed, falling back to local",
                                chat_backend,
                                exc_info=True,
                            )
                return runner.client.generate(prompt, temperature=temperature)

        return _GoalTransformClient()

    def _grant_goal_search_extension(self, extra_queries: int) -> int:
        runner = _WorkflowShimRunner(self.settings)
        before = runner._tool_runner._current_search_limit()
        granted_extra = runner._tool_runner.grant_search_extension(extra_queries)
        after = runner._tool_runner._current_search_limit()
        logger.info(
            "[chat-goal-plan] granted search extension extra=%s granted_extra=%s limit=%s->%s",
            extra_queries,
            granted_extra,
            before,
            after,
        )
        return max(0, after - before)

    def _goal_planner_client(self, chat_backend: str):
        bridge = self

        class _PlannerClient:
            def __init__(self, backend: str) -> None:
                self.backend = backend
                self.last_metadata: ModelMetadata | None = None

            def generate(self, prompt: str, *, temperature: float = 0.0) -> str:
                text, metadata = bridge._generate_chat_tool_plan_with_chat_backend(
                    self.backend,
                    prompt,
                )
                self.last_metadata = metadata
                return text

        if chat_backend == CHAT_BACKEND_CLOUD_POOL:
            provider_backend = {
                LLM_PROVIDER_GEMINI: CHAT_BACKEND_GEMINI,
                LLM_PROVIDER_MISTRAL: CHAT_BACKEND_CLOUD_MISTRAL,
                LLM_PROVIDER_BIG_PICKLE: CHAT_BACKEND_CLOUD_PICKLE,
                LLM_PROVIDER_LOCAL: CHAT_BACKEND_LOCAL,
                LLM_PROVIDER_NVIDIA: CHAT_BACKEND_CLOUD_NVIDIA,
            }
            return [
                _PlannerClient(provider_backend[provider])
                for provider in enabled_cloud_pool_providers(self.settings)
                if provider in provider_backend
            ]

        return _PlannerClient(chat_backend)

    @staticmethod
    def _format_goal_loop_message(report) -> str:
        parts = ["\n".join(report.narration).strip()]
        if report.final_result:
            parts.append(report.final_result.strip())
        if report.continuation is not None:
            parts.append(CommandBridge._format_goal_budget_status(report.continuation))
        return "\n\n".join(part for part in parts if part)

    @staticmethod
    def _format_goal_loop_final_answer(report) -> str:
        """Final message for channels that already delivered the narration live
        (stream deltas / job progress list): the answer itself plus, when the
        run paused, the budget status. Planner narration must not be repeated
        inside the final answer (#81)."""
        parts = []
        if report.final_result:
            parts.append(report.final_result.strip())
        if report.continuation is not None:
            parts.append(CommandBridge._format_goal_budget_status(report.continuation))
        return "\n\n".join(part for part in parts if part) or "（無輸出）"

    def _goal_loop_response_message(self, report, *, narrated_live: bool) -> str:
        if narrated_live:
            return self._format_goal_loop_final_answer(report)
        return self._format_goal_loop_message(report)

    @staticmethod
    def _format_goal_budget_status(continuation: GoalLoopContinuation) -> str:
        budget = continuation.state.budget or {}
        bits = []
        if "steps_used" in budget and "steps_limit" in budget:
            bits.append(f"steps {budget.get('steps_used')}/{budget.get('steps_limit')}")
        if "replans_used" in budget and "replans_limit" in budget:
            bits.append(f"replans {budget.get('replans_used')}/{budget.get('replans_limit')}")
        if "search_used" in budget and "search_limit" in budget:
            search_text = f"search {budget.get('search_used')}/{budget.get('search_limit')}"
            if budget.get("search_hard_limit"):
                search_text += f"（今日硬上限 {budget.get('search_hard_limit')}）"
            bits.append(search_text)
        return (
            "⏸ 已達執行上限\n"
            f"目標：{continuation.state.goal}\n"
            f"已完成：{len(continuation.state.completed)} 步；"
            f"已重試 {len(continuation.state.attempted_fixes)} 次\n"
            f"額度：{' · '.join(bits) if bits else 'n/a'}\n"
            f"下一步（若繼續）：{continuation.state.next_action or '（無）'}"
        )

    def _goal_web_actions(self, req: WebCommandRequest, report: GoalLoopReport) -> tuple[Action, ...]:
        if report.continuation is None:
            if report.done and report.workflow is not None:
                with self._goal_completed_lock:
                    self._goal_completed_workflows[self._conversation_key(req)] = report.workflow
                return (
                    Action(label="💾 存為工作流", command="chat", input=_GOAL_SAVE_WORKFLOW_INPUT),
                )
            return ()
        budget = report.continuation.state.budget or {}
        search_used = int(budget.get("search_used") or 0)
        search_hard_limit = int(budget.get("search_hard_limit") or 0)
        if "search" in (report.continuation.state.stop_condition or ""):
            actions = []
            if not search_hard_limit or search_used < search_hard_limit:
                actions.append(
                    Action(
                        label=f"繼續（再 {_GOAL_SEARCH_GRANT} 次搜尋）",
                        command="chat",
                        input=_GOAL_CONTINUE_SEARCH_INPUT,
                    )
                )
            actions.append(Action(label="停止並總結", command="chat", input=_GOAL_STOP_INPUT))
            return tuple(actions)
        return (
            Action(label=f"繼續（再 {_GOAL_STEP_GRANT} 步）", command="chat", input=_GOAL_CONTINUE_INPUT),
            Action(label="停止並總結", command="chat", input=_GOAL_STOP_INPUT),
        )

    @staticmethod
    def _stream_actions(response: WebCommandResponse) -> list[dict] | None:
        if not response.actions:
            return None
        return [action.to_dict() for action in response.actions]

    @staticmethod
    def _goal_trace_saver(runner: _WorkflowShimRunner):
        from .task_workspace import WorkflowStore

        store = WorkflowStore(Path(runner.tools_dir).parent / "workflow_store")
        return store.save_trace

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

    # --- chat tool planning ----------------------------------------------
    def _select_chat_tool_plan(
        self, req: WebCommandRequest, observation: str | None = None
    ) -> tuple[ChatToolPlan | None, ModelMetadata | None]:
        plan, metadata = self._planner.select_plan(req, observation)
        recorder = self._active_run_recorder.get()
        if recorder is not None:
            recorder.planner_completed(plan.tool if plan is not None else "fallback")
        return plan, metadata

    def _stream_chat_tool_plan(
        self, req: WebCommandRequest, observation: str | None = None
    ) -> Iterator[dict]:
        result: dict[str, object] = {}
        done = threading.Event()

        def _worker() -> None:
            try:
                # Keep the 1-arg call when there is no observation so existing
                # monkeypatched planners (tests) keep working unchanged.
                if observation:
                    plan, metadata = self._select_chat_tool_plan(req, observation)
                else:
                    plan, metadata = self._select_chat_tool_plan(req)
                result["plan"] = plan
                result["metadata"] = metadata
            except Exception as exc:  # noqa: BLE001
                result["error"] = str(exc)
            finally:
                done.set()

        threading.Thread(target=_worker, daemon=True).start()
        while not done.wait(timeout=_HEARTBEAT_SECONDS):
            yield stream_heartbeat()
        if "error" in result:
            logger.warning(
                "[chat-tool-plan] planner worker failed backend=%s error=%s",
                req.chat_backend,
                result["error"],
            )
            return None, None
        return (
            result.get("plan") if isinstance(result.get("plan"), ChatToolPlan) else None,
            result.get("metadata") if isinstance(result.get("metadata"), ModelMetadata) else None,
        )

    def _stream_vision_observe(self, req: WebCommandRequest) -> Iterator[dict]:
        image = next((a for a in req.attachments if a.type == "image"), None)
        if image is None or not image.data:
            return None
        b64 = _encode_image_attachment(image)
        if b64 is None:
            logger.warning("vision observe: failed to encode attachment")
            yield stream_process("（圖片編碼失敗，略過圖片觀察）")
            return None

        chain = self._vision_pool_chain()
        if not chain:
            logger.warning("vision observe: no vision pool members available")
            yield stream_process("（無可用視覺模型，略過圖片觀察）")
            return None

        from .vision_pool import _OBSERVE_PROMPT, walk_vision_pool_chain

        result: dict[str, object] = {}
        done = threading.Event()

        def _worker() -> None:
            try:
                text, provider, model_name, attempts = walk_vision_pool_chain(
                    chain, _OBSERVE_PROMPT, [b64], temperature=0.2,
                )
                result["text"] = text
                result["provider"] = provider
            except Exception as exc:
                result["error"] = str(exc)
            finally:
                done.set()

        threading.Thread(target=_worker, daemon=True).start()
        while not done.wait(timeout=_HEARTBEAT_SECONDS):
            yield stream_heartbeat()
        if "error" in result:
            logger.warning("vision observe failed: %s", result["error"])
            yield stream_process("（圖片觀察失敗，繼續以文字模式回答）")
            return None
        text = result.get("text")
        if not isinstance(text, str) or not text.strip():
            yield stream_process("（圖片觀察未回傳有效內容，繼續以文字模式回答）")
            return None
        return text.strip()

    def _build_chat_tool_plan_prompt(
        self, req: WebCommandRequest, observation: str | None = None
    ) -> str:
        return self._planner.build_plan_prompt(req, observation)

    def _chat_tool_plan_system_prompt(self) -> str:
        return self._planner.plan_system_prompt()

    def _registered_command_usage(self, command: str) -> str:
        try:
            registered = self._handlers().get(command)
        except Exception:  # noqa: BLE001
            logger.debug("router prompt: command registry unavailable for %s", command, exc_info=True)
            return ""
        return str(getattr(registered, "usage", "") or "").strip()

    def _registered_chat_tool_prompt_line(self, command: str) -> str:
        return self._planner.registered_chat_tool_prompt_line(command)

    def _tool_display_name(self, command: str) -> str:
        """Human-facing tool name from the command registry (single source of
        truth on the RegisteredCommand row); falls back to the command token."""
        try:
            registered = self._handlers().get(command)
        except Exception:  # noqa: BLE001
            logger.debug(
                "tool display name: command registry unavailable for %s", command, exc_info=True
            )
            registered = None
        name = str(getattr(registered, "chat_tool_display_name", "") or "").strip()
        return name or command

    @staticmethod
    def _tool_stream_heartbeat_seconds() -> float:
        """Compatibility seam for streamed tool heartbeats.

        Tests and operational probes adjust the module-level cadence without
        needing to reach into the executor collaborator.
        """
        return _HEARTBEAT_SECONDS

    def _local_judgment_model(self) -> str:
        return self._planner.local_judgment_model()

    def _generate_local_chat_tool_plan(self, prompt: str) -> tuple[str, ModelMetadata]:
        return self._planner.generate_local_plan(prompt)

    def _generate_chat_tool_plan_with_chat_backend(
        self,
        chat_backend: str,
        prompt: str,
        *,
        pool_rotation: "CloudPoolRotation | None" = None,
        conversation_key: str | None = None,
    ) -> tuple[str, ModelMetadata]:
        return self._planner.generate_with_chat_backend(
            chat_backend,
            prompt,
            pool_rotation=pool_rotation,
            conversation_key=conversation_key,
        )

    def _generate_gemini_chat_tool_plan(self, prompt: str) -> tuple[str, ModelMetadata]:
        return self._planner.generate_gemini_plan(prompt)

    def _generate_cloud_pool_chat_tool_plan(
        self,
        prompt: str,
        *,
        pool_rotation: "CloudPoolRotation | None" = None,
        conversation_key: str | None = None,
    ) -> tuple[str, ModelMetadata]:
        return self._planner.generate_cloud_pool_plan(
            prompt, pool_rotation=pool_rotation, conversation_key=conversation_key
        )

    def _legacy_run_chat_tool(self, req: WebCommandRequest, plan: ChatToolPlan) -> ChatToolResult:
        """Dispatch a trusted plan to the appropriate tool executor via the registry."""
        policy_map: dict[str, tuple[ChatToolPolicy, object]] = {
            CHAT_TOOL_SEARCH: (_SEARCH_TOOL_POLICY, self._exec_grounded_search),
            CHAT_TOOL_RESEARCH: (_RESEARCH_TOOL_POLICY, self._exec_registered_command_chat_tool),
            CHAT_TOOL_MUSIC: (_MUSIC_TOOL_POLICY, self._exec_registered_command_chat_tool),
            CHAT_TOOL_MUSICQUEUE: (
                _MUSICQUEUE_TOOL_POLICY,
                self._exec_registered_command_chat_tool,
            ),
            CHAT_TOOL_BLUETOOTH: (
                _BLUETOOTH_TOOL_POLICY,
                self._exec_registered_command_chat_tool,
            ),
            CHAT_TOOL_IR: (_IR_TOOL_POLICY, self._exec_registered_command_chat_tool),
            CHAT_TOOL_VISION: (_VISION_TOOL_POLICY, self._exec_vision_chat_tool),
        }
        entry = policy_map.get(plan.tool)
        if entry is None:
            raise ValueError(f"unknown chat tool: {plan.tool!r}")
        policy, executor = entry
        display_name = self._tool_display_name(plan.tool)
        if display_name != plan.tool:
            policy = dataclasses.replace(policy, display_name=display_name)
        tool_req = make_chat_tool_request(
            tool=plan.tool,
            raw_query=plan.query,
            user_question=req.input or "",
            policy=policy,
        )
        try:
            tool_result: ChatToolResult = executor(req, tool_req)  # type: ignore[operator]
        except Exception as exc:
            # Failures go into the ledger too: the next turn's router must know
            # this was attempted and failed instead of silently redoing it.
            self._record_chat_tool_run(
                req, plan.tool, plan.query, status="error", summary=str(exc)
            )
            raise
        self._record_chat_tool_run(
            req, plan.tool, plan.query, status="ok", summary=tool_result.answer
        )
        return tool_result

    def _legacy_stream_chat_tool(
        self, req: WebCommandRequest, plan: ChatToolPlan
    ) -> Iterator[dict]:
        """Run the tool off-thread, surfacing a live "正在調用…工具中" notice up
        front (so the user can see a tool is being invoked) and heartbeats while
        it works (so the connection stays alive), then deliver the grounded
        answer as the ``done`` event. The finished answer still carries its own
        persistent "已使用工具" banner from the executor.

        If the client disconnects (GeneratorExit) before the worker finishes,
        the completed result is pushed into server-side session memory so it
        appears automatically when the user reconnects."""
        yield stream_delta(_tool_calling_notice(plan.tool, self._tool_display_name(plan.tool)))
        result: dict[str, object] = {}
        done = threading.Event()
        abandoned = threading.Event()
        narration_queue: queue.Queue[str] = queue.Queue()

        def _worker() -> None:
            try:
                # Staged tool milestones (e.g. /research 進度) surface live on
                # this stream for the whole run, including a goal-loop upgrade.
                with self._live_progress(narration_queue.put):
                    tool_result: ChatToolResult = self._run_chat_tool(req, plan)
                    logger.info(
                        "[chat-tool] tool=%s sources=%d summary=%r",
                        plan.tool, tool_result.source_count, tool_result.result_summary,
                    )
                    upgraded = self._maybe_upgrade_tool_result_to_goal_loop(
                        req,
                        plan,
                        tool_result,
                        planner_metadata=None,
                        narrator=narration_queue.put,
                    )
                if upgraded is not None:
                    result["response"] = upgraded
                    return
                result["text"] = tool_result.answer
                result["model_metadata"] = tool_result.model_metadata
            except Exception as exc:  # noqa: BLE001
                logger.exception("chat tool failed tool=%s", plan.tool)
                result["error"] = str(exc)
            finally:
                done.set()
                if abandoned.is_set():
                    orphan = result.get("text")
                    response = result.get("response")
                    if orphan is None and isinstance(response, WebCommandResponse):
                        orphan = response.message
                    if orphan is not None:
                        try:
                            self._push_orphaned_result(str(orphan))
                        except Exception:  # noqa: BLE001
                            logger.exception("command bridge: failed to push orphaned tool result")

        threading.Thread(target=_worker, daemon=True).start()
        # Drain goal-loop narration live (an unsatisfied tool result upgrades
        # to a goal loop mid-worker); heartbeat only while nothing arrives.
        last_beat = time.time()
        try:
            while not done.is_set() or not narration_queue.empty():
                try:
                    line = narration_queue.get(timeout=0.5)
                except queue.Empty:
                    if time.time() - last_beat >= _HEARTBEAT_SECONDS:
                        yield stream_heartbeat()
                        last_beat = time.time()
                    continue
                yield stream_delta(f"{line}\n")
                last_beat = time.time()
        except GeneratorExit:
            abandoned.set()
            raise
        if "error" in result:
            yield stream_error(f"工具執行失敗：{result['error']}")
            return
        response = result.get("response")
        if isinstance(response, WebCommandResponse):
            if response.status == STATUS_ERROR:
                yield stream_error(response.message)
                return
            yield stream_done(
                response.message,
                model_metadata=response.model_metadata,
                actions=self._stream_actions(response),
            )
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
        self._conversation_sessions().append_orphaned_result(text)
        self._event_sessions().ensure().append(
            "assistant.message", run_id="orphaned-result", payload={"text": text, "orphaned": True}
        )
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

    def _exec_vision_chat_tool(
        self, req: WebCommandRequest, tool_req: ChatToolRequest
    ) -> ChatToolResult:
        from .vision_pool import (
            _OBSERVE_PROMPT,
            acquire_url_images,
            fetch_page_image_urls,
            walk_vision_pool_chain,
        )

        query = tool_req.query.strip()
        images_b64: list[str] = []

        # First, use any current-turn image attachments.
        for att in req.attachments:
            if att.type == "image" and att.data:
                b64 = _encode_image_attachment(att)
                if b64:
                    images_b64.append(b64)

        # If the query contains URLs, try to acquire images from them.
        url_pattern = re.compile(r'https?://[^\s]+')
        urls = url_pattern.findall(query)
        if urls:
            for url in urls[:3]:
                page_urls = fetch_page_image_urls(url)
                if page_urls:
                    acquired = acquire_url_images(page_urls[:3])
                    for _url, b64 in acquired:
                        images_b64.append(b64)

        # Cap at 3 images.
        images_b64 = images_b64[:3]

        if not images_b64:
            msg = "沒有可用的圖片來源。請直接上傳圖片或在查詢中附上圖片網址。"
            banner = f"{_TOOL_USED_PREFIX}{tool_req.policy.display_name}（{tool_req.tool}）｜指令：{query}"
            return ChatToolResult(answer=f"{banner}\n\n{msg}")

        chain = self._vision_pool_chain()
        if not chain:
            msg = "目前沒有可用的視覺模型。請先在設定中啟用至少一個視覺提供者。"
            banner = f"{_TOOL_USED_PREFIX}{tool_req.policy.display_name}（{tool_req.tool}）｜指令：{query}"
            return ChatToolResult(answer=f"{banner}\n\n{msg}")

        prompt = f"{_OBSERVE_PROMPT}\n\n使用者問題：{query}"
        text, provider, model_name, attempts = walk_vision_pool_chain(
            chain, prompt, images_b64, temperature=0.2,
        )
        if not text:
            msg = "視覺模型無法處理此圖片。"
            banner = f"{_TOOL_USED_PREFIX}{tool_req.policy.display_name}（{tool_req.tool}）｜指令：{query}"
            return ChatToolResult(answer=f"{banner}\n\n{msg}")

        banner = f"{_TOOL_USED_PREFIX}{tool_req.policy.display_name}（{tool_req.tool}）｜指令：{query}"
        result_text = f"{banner}\n\n{text}"
        metadata = ModelMetadata(
            requested_provider="vision_pool",
            requested_model=model_name or "",
            attempted_models=attempts,
            final_provider=provider or "",
            final_model=model_name or "",
        )
        return ChatToolResult(
            answer=result_text,
            result_summary=f"vision tool via {provider}/{model_name}",
            model_metadata=metadata,
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
            CHAT_TOOL_MUSICQUEUE: (
                "CommandBridge.run_musicqueue_command",
                self.run_musicqueue_command,
            ),
            CHAT_TOOL_BLUETOOTH: (
                "CommandBridge.run_bluetooth_command",
                self.run_bluetooth_command,
            ),
            CHAT_TOOL_IR: ("CommandBridge.run_ir_command", self.run_ir_command),
            CHAT_TOOL_RESEARCH: (
                "CommandBridge._run_command(/research)",
                lambda text: {
                    "status": STATUS_OK,
                    "message": self._run_command("/research", text),
                },
            ),
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

    def _maybe_upgrade_tool_result_to_goal_loop(
        self,
        req: WebCommandRequest,
        plan: ChatToolPlan,
        tool_result: ChatToolResult,
        *,
        planner_metadata: ModelMetadata | None,
        narrator: Callable[[str], None] | None = None,
    ) -> WebCommandResponse | None:
        user_input = (req.input or "").strip()
        if not user_input:
            return None
        try:
            verdict = self._chat_tool_result_satisfies_intent(req, plan, tool_result)
        except Exception:  # noqa: BLE001
            logger.exception("chat tool satisfaction check failed tool=%s", plan.tool)
            return None
        outcome = classify_outcome(
            verdict,
            tool=plan.tool,
            query=plan.query,
            answer=tool_result.answer,
            source_count=tool_result.source_count,
        )
        action = decide_continuation(outcome)
        if action is ContinuationAction.ANSWER:
            return None
        if action is ContinuationAction.SURFACE_FAILURE:
            # Replanning cannot route around an unreachable device / blocked
            # network: surface the tool's own failure reply (it carries the
            # recovery hint) instead of drafting a doomed workflow.
            logger.info(
                "[chat-tool] tool=%s blocked (%s) -> report failure, skip goal loop",
                plan.tool,
                outcome.missing_evidence,
            )
            return None
        logger.info(
            "[chat-tool] tool=%s partial -> goal loop (missing=%r)",
            plan.tool,
            outcome.missing_evidence,
        )
        if narrator is not None:
            try:
                narrator("直接指令沒有完成，我改規劃成多步驟流程並直接執行：")
            except Exception:  # noqa: BLE001
                logger.exception("chat tool upgrade narrator failed")
        # The tool DID produce an answer (it was just judged incomplete): hand
        # it to the goal loop as a pre-bound variable so the drafted workflow
        # builds on it instead of re-running the same expensive tool. The
        # conversation context rides along so an elliptical follow-up（e.g.
        # 「再加上…呢」）can be integrated with earlier results, not answered
        # in isolation.
        seeds = {_seed_variable_name_for_tool(plan.tool): tool_result.answer}
        context = self._conversation_context_block(req)
        if context:
            seeds["conversation_context"] = context
        # The judge's own reason for "not yet complete" — passed forward so a
        # replan targets only what's missing, and so conservative synthesis on
        # exhaustion can state plainly what stayed unknown (no keyword rules).
        if outcome.missing_evidence:
            seeds["missing_evidence"] = outcome.missing_evidence
        # Tell the goal loop this tool already ran (keyed structurally) so its
        # workflow can't spend a second identical /research on the same query —
        # a duplicate step gets the answer we already hold, not a fresh run.
        seed_operations = {outcome.operation_key: tool_result.answer}
        response = self._run_goal_loop_blocking(
            req,
            user_input,
            planner_metadata=planner_metadata,
            narrator=narrator,
            seed_variables=seeds,
            seed_operations=seed_operations,
        )
        if response.status == STATUS_ERROR:
            logger.warning("goal loop upgrade failed after unsatisfied tool tool=%s", plan.tool)
            return None
        return WebCommandResponse(
            status=response.status,
            message=(
                "直接指令沒有完成，我改規劃成多步驟流程並直接執行：\n\n"
                f"{response.message}"
            ),
            mode=response.mode,
            model_metadata=response.model_metadata,
            actions=response.actions,
        )

    def _goal_result_judge(
        self,
        chat_backend: str,
        *,
        pool_rotation: "CloudPoolRotation | None" = None,
    ) -> Callable[[str, str], tuple[bool, str]]:
        """LLM judge for GoalLoop: does the workflow's final result actually
        achieve the goal? Reuses the chat-tool satisfaction backend chain
        (chosen backend → local fallback)."""

        def judge(goal: str, final_result: str) -> tuple[bool, str]:
            prompt = _GOAL_RESULT_SATISFACTION_PROMPT.format(
                goal=json.dumps(goal, ensure_ascii=False),
                final_result=json.dumps(final_result.strip(), ensure_ascii=False),
            )
            raw = self._generate_chat_tool_satisfaction_text(
                chat_backend, prompt, pool_rotation=pool_rotation
            ).strip()
            parsed = self._parse_chat_tool_satisfaction(raw)
            logger.info(
                "[goal-loop] result judge satisfied=%s reason=%r",
                parsed.get("satisfied"),
                parsed.get("reason"),
            )
            return bool(parsed.get("satisfied")), str(parsed.get("reason") or "")

        return judge

    def _goal_conservative_synthesizer(
        self,
        chat_backend: str,
        *,
        pool_rotation: "CloudPoolRotation | None" = None,
    ) -> Callable[[str, dict[str, str], str], str]:
        """Best-effort answer builder for a goal loop that exhausted its replan
        budget. Reuses the satisfaction backend chain (chosen → local fallback).
        Generic: it only relays goal + gathered evidence + last judge reason to
        the LLM; no domain rules about what "complete" means."""

        def synthesize(goal: str, seeds: dict[str, str], last_reason: str) -> str:
            evidence_lines = [
                f"- {name}：{_clip(str(value), 1200)}"
                for name, value in seeds.items()
                if str(value).strip()
            ]
            evidence = "\n".join(evidence_lines) or "（無）"
            prompt = _GOAL_CONSERVATIVE_SYNTHESIS_PROMPT.format(
                goal=goal.strip(),
                last_reason=(last_reason or "（無說明）").strip(),
                evidence=evidence,
            )
            text = self._generate_chat_tool_satisfaction_text(
                chat_backend, prompt, pool_rotation=pool_rotation
            ).strip()
            logger.info(
                "[goal-loop] conservative synthesis produced %d chars", len(text)
            )
            return text

        return synthesize

    def _conversation_context_block(self, req: WebCommandRequest) -> str:
        """Compact conversational context (recent visible turns + the tool
        ledger) so follow-up turns can be judged/planned against their full
        intent instead of the elliptical latest message alone."""
        lines: list[str] = []
        history = list(req.history)[-_CONTEXT_HISTORY_TURNS:]
        if history:
            lines.append("對話紀錄（由舊到新）：")
            for turn in history:
                label = _CHAT_ROLE_LABELS.get(turn.role, turn.role)
                lines.append(f"{label}：{_clip(turn.content, _CONTEXT_TURN_CHARS)}")
        ledger = self._chat_tool_ledger_entries(req)
        if ledger:
            lines.append("本次對話先前已執行過的工具紀錄（由舊到新）：")
            for entry in ledger:
                status_label = {"ok": "成功", "partial": "部分完成"}.get(
                    str(entry.get("status")), "失敗"
                )
                lines.append(
                    f"- {entry.get('tool')}（參數：{entry.get('query')}）→ "
                    f"{status_label}｜{entry.get('summary')}"
                )
        return "\n".join(lines)

    def _legacy_chat_tool_result_satisfies_intent(
        self,
        req: WebCommandRequest,
        plan: ChatToolPlan,
        tool_result: ChatToolResult,
    ) -> dict[str, object]:
        context = self._conversation_context_block(req) or "（無）"
        prompt = _CHAT_TOOL_SATISFACTION_PROMPT.format(
            context=context,
            user_input=json.dumps((req.input or "").strip(), ensure_ascii=False),
            tool_name=json.dumps(plan.tool, ensure_ascii=False),
            tool_query=json.dumps(plan.query, ensure_ascii=False),
            tool_answer=json.dumps(tool_result.answer.strip(), ensure_ascii=False),
        )
        raw = self._generate_chat_tool_satisfaction_text(req.chat_backend, prompt).strip()
        parsed = self._parse_chat_tool_satisfaction(raw)
        logger.info(
            "[chat-tool] satisfaction tool=%s satisfied=%s environment_blocked=%s reason=%r",
            plan.tool,
            parsed.get("satisfied"),
            parsed.get("environment_blocked"),
            parsed.get("reason"),
        )
        return parsed

    def _legacy_generate_chat_tool_satisfaction_text(
        self,
        chat_backend: str,
        prompt: str,
        *,
        pool_rotation: "CloudPoolRotation | None" = None,
    ) -> str:
        backends = [chat_backend]
        if chat_backend != CHAT_BACKEND_LOCAL:
            backends.append(CHAT_BACKEND_LOCAL)
        last_exc: Exception | None = None
        for backend in backends:
            try:
                text, _metadata = self._generate_chat_tool_plan_with_chat_backend(
                    backend, prompt, pool_rotation=pool_rotation
                )
                logger.info("[chat-tool] satisfaction backend=%s ok", backend)
                return text
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[chat-tool] satisfaction backend=%s failed: %s",
                    backend,
                    exc,
                )
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("no available backend for chat tool satisfaction check")

    @staticmethod
    def _legacy_parse_chat_tool_satisfaction(raw: str) -> dict[str, object]:
        text = (raw or "").strip()
        if not text:
            raise ValueError("empty chat tool satisfaction response")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                raise
            data = json.loads(match.group(0))
        if not isinstance(data, dict):
            raise ValueError("chat tool satisfaction response must be a JSON object")
        satisfied = data.get("satisfied")
        if not isinstance(satisfied, bool):
            raise ValueError("chat tool satisfaction response missing boolean satisfied")
        reason = str(data.get("reason", "")).strip()
        environment_blocked = data.get("environment_blocked")
        if not isinstance(environment_blocked, bool):
            environment_blocked = False
        return {
            "satisfied": satisfied,
            "environment_blocked": environment_blocked and not satisfied,
            "reason": reason,
        }

    # R1.3b compatibility delegates.  The executor calls back through these
    # exact bridge names, preserving existing instance monkeypatch seams.
    def _run_chat_tool(self, req: WebCommandRequest, plan: ChatToolPlan) -> ChatToolResult:
        recorder = self._active_run_recorder.get()
        if recorder is not None:
            recorder.tool_started(plan.tool)
        try:
            result = self._executor.run(req, plan)
        except Exception:
            if recorder is not None:
                recorder.tool_completed(plan.tool, ok=False)
            raise
        if recorder is not None:
            recorder.tool_completed(plan.tool, ok=True)
        return result

    def _stream_chat_tool(
        self, req: WebCommandRequest, plan: ChatToolPlan
    ) -> Iterator[dict]:
        yield from self._executor.stream(req, plan)

    def _chat_tool_result_satisfies_intent(
        self, req: WebCommandRequest, plan: ChatToolPlan, tool_result: ChatToolResult
    ) -> dict[str, object]:
        return self._executor.result_satisfies_intent(req, plan, tool_result)

    def _generate_chat_tool_satisfaction_text(
        self,
        chat_backend: str,
        prompt: str,
        *,
        pool_rotation: "CloudPoolRotation | None" = None,
    ) -> str:
        return self._executor.generate_satisfaction_text(
            chat_backend, prompt, pool_rotation=pool_rotation
        )

    @staticmethod
    def _parse_chat_tool_satisfaction(raw: str) -> dict[str, object]:
        return ChatToolExecutor.parse_satisfaction(raw)

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
        if chat_backend == CHAT_BACKEND_CLOUD_NVIDIA:
            client = self._build_nvidia_chat_client()
            if client is None:
                text = self._ollama_generate_blocking(prompt)
                metadata = self._model_metadata_for_backend(
                    chat_backend,
                    (
                        ModelAttempt(
                            "nvidia",
                            self._nvidia_model(),
                            _MODEL_STATUS_NOT_CONFIGURED,
                            "NVIDIA API key missing",
                        ),
                        ModelAttempt("local", self._local_model(), _MODEL_STATUS_OK),
                    ),
                    "local",
                    self._local_model(),
                    fallback_reason="NVIDIA API key missing",
                )
                return text, f"本地 {self._local_model()}（Nvidia 不可用，已改用本地）", metadata
            text = client.generate(prompt, temperature=0.3)
            model = self._nvidia_model()
            metadata = self._model_metadata_for_backend(
                chat_backend,
                (ModelAttempt("nvidia", model, _MODEL_STATUS_OK),),
                "nvidia",
                model,
            )
            return text, f"NVIDIA {model}", metadata
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
        session_id = getattr(req, "session_id", None)
        recorder = self._event_sessions().recorder(session_id)
        recorder.accepted(text)
        recorder.started()
        job.run_id = recorder.run_id
        job.session_id = session_id
        job.recorder = recorder
        store = self._get_job_store()
        store.save({
            "job_id": job.id,
            "run_id": job.run_id,
            "session_id": session_id,
            "status": JOB_RUNNING,
            "progress": [],
            "message": "",
            "actions": [],
            "error": None,
            "created_at": job.wall_created_at,
            "updated_at": job.wall_created_at,
        })
        store.purge_expired()

        def _persist_terminal(*, status: str, message: str,
                              actions: list[dict], error: str | None) -> None:
            # Explicit cancel wins over whatever the worker produced: once the
            # user asked to stop, a late-finishing run must not resurrect a
            # "done" (or "error") over the interrupted terminal state (#81).
            with job.lock:
                if job.cancel_event.is_set():
                    status, message, actions, error = (
                        JOB_INTERRUPTED, "任務已取消。", [], None
                    )
                progress_snapshot = list(job.progress)
                # This is the sole terminal compare-and-set. Keep the job in
                # ``running`` until its snapshot and event history are both
                # durable, so poll cannot observe a finished job without the
                # replayable final message.
                store.save({
                    "job_id": job.id,
                    "run_id": job.run_id,
                    "session_id": job.session_id,
                    "status": status,
                    "progress": progress_snapshot,
                    "message": message,
                    "actions": list(actions),
                    "error": error,
                    "created_at": job.wall_created_at,
                    "updated_at": time.time(),
                })
                if message:
                    recorder.assistant_message(message)
                recorder.terminal(
                    "cancelled" if status == JOB_INTERRUPTED else ("completed" if status == JOB_DONE else "failed"),
                    message=message or (error or ""),
                )
                job.status = status
                job.message = message
                job.actions = actions
                job.error = error

        def _worker() -> None:
            try:
                message, markup = self._run_command_raw(
                    "/research", text, chat_id=job.id
                )
                _persist_terminal(
                    status=JOB_DONE, message=message,
                    actions=self._markup_to_actions(markup), error=None,
                )
            except Exception as exc:  # noqa: BLE001
                if job.cancel_event.is_set():
                    logger.info("async research cancelled job=%s", job.id)
                else:
                    logger.exception("async research failed job=%s", job.id)
                _persist_terminal(
                    status=JOB_ERROR, message="", actions=[], error=str(exc),
                )

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
                    "run_id": getattr(job, "run_id", None),
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
                "run_id": persisted.get("run_id"),
                "progress": persisted.get("progress") or [],
                "message": persisted.get("message") or "",
                "actions": persisted.get("actions") or [],
                "error": None,
            }
        if status == JOB_ERROR:
            return {
                "job_status": JOB_ERROR,
                "run_id": persisted.get("run_id"),
                "progress": persisted.get("progress") or [],
                "message": "",
                "actions": [],
                "error": persisted.get("error") or "任務失敗",
            }
        # status == running but in-memory worker gone: bridge was restarted.
        run_id = persisted.get("run_id")
        session_id = persisted.get("session_id")
        if isinstance(run_id, str):
            recorder = RunRecorder(self._event_sessions().ensure(session_id), run_id=run_id)
            recorder.terminal("interrupted", message="bridge_restart")
        return {
            "job_status": JOB_INTERRUPTED,
            "run_id": run_id,
            "message": "研究任務因系統重啟而中斷，請重新執行 /research。",
            "progress": persisted.get("progress") or [],
            "actions": [],
            "error": None,
        }

    def cancel_job(self, job_id: str) -> dict:
        """Request cooperative cancellation of a running job (issue #81).

        Sets the job's cancel flag; the goal-loop worker observes it at its next
        stage/step boundary, stops all remaining sub-stages, and persists an
        ``interrupted`` terminal state. Idempotent: cancelling an already-gone
        or already-finished job is reported, not treated as an error.
        """
        status = self._jobs.cancel(job_id)
        if status is None:
            persisted = self._get_job_store().load(job_id)
            if persisted is None:
                return {"status": STATUS_ERROR, "not_found": True,
                        "message": "找不到此任務（可能已結束或過期）。"}
            return {"status": STATUS_OK, "job_status": persisted.get("status"),
                    "message": "任務已結束，無需取消。"}
        if status != JOB_RUNNING:
            return {"status": STATUS_OK, "job_status": status,
                    "message": "任務已結束，無需取消。"}
        logger.info("[job] cancel requested job=%s", job_id)
        job = self._jobs.get(job_id)
        recorder = getattr(job, "recorder", None) if job is not None else None
        if recorder is not None:
            recorder.emit("run.cancel_requested", {})
        return {"status": STATUS_OK, "job_status": JOB_INTERRUPTED,
                "message": "已要求取消，將於下一個安全點停止。"}

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
    # --- voice-intent gate (#82 PR1) ---------------------------------------
    def _voice_music_available(self) -> bool:
        return self._callbacks().get("music") is not None

    def _voice_ir_buttons(self) -> list[tuple[str, str]]:
        from .ir_command import IrStore

        return [
            (b.device, b.button)
            for b in IrStore(self.settings.openclaw_ir_devices_path).list_buttons()
        ]

    def _voice_user_context(self, req: WebCommandRequest) -> VoiceUserContext:
        return VoiceUserContext(conversation_id=req.conversation_id)

    def _voice_store(self):
        """Lazy personalization store (#82 PR3). A corrupt DB is reported
        loudly and the feature stays off — never silently rebuilt, never a
        reason for chat to fail (design §13.4)."""
        if not self._voice_store_checked:
            try:
                self._voice_store_cached = open_voice_store(self.settings)
            except VoiceStoreCorruptError:
                logger.exception(
                    "voice store is CORRUPT — voice learning disabled; "
                    "inspect/back up the DB before deleting it"
                )
                self._voice_store_cached = None
            self._voice_store_checked = True
        return self._voice_store_cached

    def _maybe_voice_clarification(
        self, req: WebCommandRequest, plan: ChatToolPlan
    ) -> VoiceClarification | None:
        """First-use unresolved-control gate (design §9.3): a short voice
        utterance about to hit an open-ended tool gets a clarification card
        instead. MUST run before the tool executes; returns None whenever the
        request should proceed unchanged (all non-voice input, long-form
        speech, declined clarification, or no available candidates)."""
        if not req.is_voice:
            return None
        if plan.tool not in (CHAT_TOOL_SEARCH, CHAT_TOOL_RESEARCH):
            return None
        voice = req.voice
        context = self._voice_user_context(req)
        should = self._voice_gate.should_clarify_before_open_tool(
            transcript=req.input,
            plan_query=plan.query,
            duration_ms=voice.duration_ms if voice else None,
            clarification_declined=bool(voice and voice.clarification_declined),
            user_context=context,
        )
        if not should:
            return None
        clarification = self._voice_gate.build_first_use_clarification(
            transcript=req.input, user_context=context
        )
        if not clarification.candidates:
            return None
        # #82 PR3: bind a single-use learning token to this utterance and
        # candidate set so a confirmed successful action becomes a prototype.
        store = self._voice_store()
        if store is not None and voice is not None and voice.utterance_id:
            token = issue_learning_token(
                store,
                utterance_id=voice.utterance_id,
                candidate_action_ids=tuple(
                    c.action_id for c in clarification.candidates
                ),
            )
            if token:
                clarification = dataclasses.replace(
                    clarification, learning_token=token
                )
        logger.info(
            "[voice-gate] clarify before tool=%s transcript=%r candidates=%d token=%s",
            plan.tool, req.input, len(clarification.candidates),
            "yes" if clarification.learning_token else "no",
        )
        VOICE_METRICS.record_resolution("clarify", clarification.reason_code)
        return clarification

    @staticmethod
    def _voice_clarification_message(clarification: VoiceClarification) -> str:
        return (
            f"我聽到：「{clarification.transcript}」\n"
            "你是要執行哪一個？（也可以選「都不是」當一般問題處理）"
        )

    def confirm_voice_action(
        self, action_id: str, *, learning_token: str | None = None
    ) -> dict:
        """Execute a clarification candidate. The client submits only the
        ``action_id`` (+ the opaque learning token); the registry is re-read
        and availability/risk re-validated server-side (design §5.5) — labels,
        payloads and risk levels from the client are never trusted.

        Learning transaction (#82 PR3, §5.4): a provided token is consumed
        BEFORE execution (single-use — a replayed token is rejected without
        re-executing), the confirmed action must be inside the token's
        candidate set, and a prototype is committed only after the action
        reports success. No token = execute without learning."""
        aid = (action_id or "").strip()
        if not aid:
            return {"status": STATUS_ERROR, "message": "缺少 action_id。", "actions": []}
        store = self._voice_store()
        redeemed = None
        if learning_token:
            if store is not None:
                redeemed = redeem_learning_token(store, learning_token)
                if redeemed is None:
                    return {
                        "status": STATUS_ERROR,
                        "message": "確認已失效（逾時或已使用過），請重新語音操作一次。",
                        "actions": [],
                    }
                if aid not in redeemed.candidate_action_ids:
                    return {
                        "status": STATUS_ERROR,
                        "message": "選擇與候選清單不一致，已取消執行。",
                        "actions": [],
                    }
        try:
            actions = self._voice_registry.list_actions(
                user_context=VoiceUserContext()
            )
        except Exception:  # noqa: BLE001
            logger.exception("voice confirm: registry listing failed")
            return {"status": STATUS_ERROR, "message": "語音操作清單暫時不可用。", "actions": []}
        descriptor = next((a for a in actions if a.action_id == aid), None)
        if descriptor is None or not descriptor.available:
            return {"status": STATUS_ERROR, "message": "此操作目前不可用。", "actions": []}
        if descriptor.risk != RISK_LOW:
            return {
                "status": STATUS_ERROR,
                "message": "此操作風險等級需要進一步確認，尚不支援語音直接執行。",
                "actions": [],
            }
        result = self._dispatch_voice_descriptor(descriptor)
        if result is None:
            return {"status": STATUS_ERROR, "message": "未知的操作類型。", "actions": []}

        success = str(result.get("status")) == STATUS_OK
        out = dict(result)
        if store is not None:
            try:
                store.record_action_outcome(aid, success=success)
            except Exception:  # noqa: BLE001
                logger.exception("voice action stats update failed (fail-soft)")
        if redeemed is not None and store is not None:
            out["learning_status"] = (
                commit_prototype(
                    store, utterance_id=redeemed.utterance_id, action_id=aid
                )
                if success
                else LEARNING_ACTION_FAILED
            )
        elif learning_token:
            out["learning_status"] = LEARNING_STORE_ERROR
        else:
            out["learning_status"] = LEARNING_SKIPPED_NO_TOKEN
        VOICE_METRICS.record_learning_commit(str(out["learning_status"]))
        return out

    def _dispatch_voice_descriptor(self, descriptor) -> dict | None:
        """Execute a registry descriptor via its surface; None = unknown kind."""
        if descriptor.dispatch_kind == DISPATCH_MUSIC_CALLBACK:
            callback = str(descriptor.dispatch_payload.get("callback_data") or "")
            return self.run_music_action(callback)
        if descriptor.dispatch_kind == DISPATCH_IR_SEND:
            device = str(descriptor.dispatch_payload.get("device") or "")
            button = str(descriptor.dispatch_payload.get("button") or "")
            return self.run_ir_command(f"send {device} {button}")
        return None

    def _maybe_voice_direct_action(
        self, req: WebCommandRequest
    ) -> WebCommandResponse | None:
        """Prototype direct fast path (#82 PR4, design §8.3): a short voice
        utterance whose embedding confidently matches a mature low-risk
        reversible prototype is dispatched immediately — the Chat router
        never runs. Every miss returns None and the request proceeds
        unchanged; store/registry failures are fail-soft (§13.4)."""
        if not req.is_voice:
            return None
        if not getattr(self.settings, "openclaw_voice_direct_action_enabled", False):
            return None
        voice = req.voice
        if voice is None or not voice.utterance_id or voice.clarification_declined:
            return None
        if not voice_policy.is_short_form(req.input, duration_ms=voice.duration_ms):
            return None
        store = self._voice_store()
        if store is None:
            return None
        started = time.monotonic()
        try:
            utterance = store.get_utterance(voice.utterance_id)
            if (
                utterance is None
                or not utterance.embedding
                or not utterance.embedding_model_version
            ):
                return None
            prototypes = store.list_prototypes(
                embedding_model_version=utterance.embedding_model_version
            )
        except Exception:  # noqa: BLE001 — fall back to normal routing
            logger.exception("voice direct path: store lookup failed")
            return None
        if not prototypes:
            return None
        try:
            actions = self._voice_registry.list_actions(
                user_context=self._voice_user_context(req)
            )
        except Exception:  # noqa: BLE001
            logger.exception("voice direct path: registry listing failed")
            return None
        direct = resolve_direct_prototype_action(
            embedding=utterance.embedding,
            prototypes=prototypes,
            actions=actions,
        )
        VOICE_METRICS.observe_stage("gate_ms", (time.monotonic() - started) * 1000.0)
        if direct is None:
            return None
        descriptor = next(
            (a for a in actions if a.action_id == direct.action_id), None
        )
        if descriptor is None:
            return None
        dispatch_started = time.monotonic()
        result = self._dispatch_voice_descriptor(descriptor)
        VOICE_METRICS.observe_stage(
            "dispatch_ms", (time.monotonic() - dispatch_started) * 1000.0
        )
        if result is None:
            return None
        success = str(result.get("status")) == STATUS_OK
        VOICE_METRICS.record_resolution("direct_action", direct.reason_code)
        VOICE_METRICS.record_direct_action(
            direct.action_id, "ok" if success else "error"
        )
        try:
            store.record_action_outcome(direct.action_id, success=success)
            # §10.4: direct execution updates usage stats only — it must not
            # create or reinforce prototypes (no self-confirmation loop).
            store.mark_utterance_consumed(voice.utterance_id)
        except Exception:  # noqa: BLE001
            logger.exception("voice direct path: stats update failed (fail-soft)")
        logger.info(
            "[voice-direct] action=%s confidence=%.3f margin=%.3f ok=%s",
            direct.action_id, direct.confidence, direct.margin, success,
        )
        outcome = str(result.get("message") or "")
        return WebCommandResponse(
            status=str(result.get("status") or STATUS_ERROR),
            message=f"已辨識：{direct.display_label}\n{outcome}".rstrip(),
            mode=MODE_CHAT,
            direct_action=direct.to_dict(),
        )

    def report_voice_direct_rejection(self, prototype_id: str) -> dict:
        """Negative feedback「不是這個」(#82 PR4, §7.6): lower trust in the
        prototype that triggered a direct dispatch; repeated rejections
        disable it. Never auto-transfers the utterance to another action —
        the user re-confirms via a fresh clarification instead."""
        pid = (prototype_id or "").strip()
        if not pid:
            return {"status": STATUS_ERROR, "message": "缺少 prototype_id。"}
        store = self._voice_store()
        if store is None:
            return {"status": STATUS_ERROR, "message": "語音個人化未啟用。"}
        try:
            store.record_rejection(pid)
        except Exception:  # noqa: BLE001
            logger.exception("voice direct rejection failed")
            return {"status": STATUS_ERROR, "message": "回饋記錄失敗。"}
        VOICE_METRICS.record_negative_correction(pid)
        return {"status": STATUS_OK, "message": "已記錄回饋，將降低此語音對應的信任度。"}

    def list_voice_prototypes(self) -> dict:
        """Personalization inspection surface (#82 §17: 檢視)."""
        store = self._voice_store()
        if store is None:
            return {"status": STATUS_OK, "prototypes": [], "message": "語音個人化未啟用。"}
        try:
            prototypes = store.list_prototypes(status=None)
        except Exception:  # noqa: BLE001
            logger.exception("voice prototype listing failed")
            return {"status": STATUS_ERROR, "prototypes": [], "message": "無法讀取語音個人化資料。"}
        return {
            "status": STATUS_OK,
            "prototypes": [
                {
                    "prototype_id": p.prototype_id,
                    "action_id": p.action_id,
                    "status": p.status,
                    "confirmed_count": p.confirmed_count,
                    "rejected_count": p.rejected_count,
                    "embedding_model_version": p.embedding_model_version,
                    "updated_at": p.updated_at,
                }
                for p in prototypes
            ],
        }

    def reset_voice_personalization(self) -> dict:
        """Delete prototypes, utterances, tokens and stats (#82 §13.3)."""
        store = self._voice_store()
        if store is None:
            return {"status": STATUS_OK, "message": "語音個人化未啟用，無資料可清除。"}
        try:
            store.reset_profile()
        except Exception:  # noqa: BLE001
            logger.exception("voice personalization reset failed")
            return {"status": STATUS_ERROR, "message": "清除失敗，請檢查儲存空間狀態。"}
        return {"status": STATUS_OK, "message": "已清除語音個人化資料。"}

    def run_music_command(self, text: str) -> dict:
        """Run the ``/music`` handler for the 生活 mode text box — an empty box
        returns the music menu (text + control buttons); a query plays/searches
        a song. The same handler the Telegram bot uses, so no logic is duped."""
        return self._music.run_command(text)

    def run_musicqueue_command(self, text: str) -> dict:
        """Run the ``/musicqueue`` handler (ordered multi-song play-once queue)
        — the same registered handler the Telegram bot uses."""
        return self._music.run_queue_command(text)

    def run_music_action(self, callback_data: str) -> dict:
        """Re-invoke a music callback button for the web 生活 mode. Handles the
        ``music:`` family (browse / play / favorite / volume) plus the generic
        list callbacks (``pg`` / ``del`` / ``close``) the favorites list uses —
        the very same handlers the Telegram bot dispatches, so playback safety
        (path re-validation under the music root) is enforced identically."""
        return self._music.run_action(callback_data)

    def _legacy_run_music_action(self, callback_data: str) -> dict:
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
        return self._music.now_playing()

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

                    def _bridge_on_id_renamed(old_id: str, new_id: str) -> None:
                        sh_store = self._sh_store
                        if sh_store is None:
                            return
                        old_cmd = f"/workflow run {old_id}"
                        new_cmd = f"/workflow run {new_id}"
                        for entry in sh_store.list():
                            sid = entry.get("id")
                            cmds = entry.get("commands") or []
                            if not any(c == old_cmd for c in cmds):
                                continue
                            sh_store.clear_commands(sid)
                            for cmd in cmds:
                                sh_store.add_command(sid, new_cmd if cmd == old_cmd else cmd)

                    editor = WorkflowEditor(
                        _workflow_store(runner),
                        command_registry=command_registry,
                        catalog=runner.catalog,
                        on_id_renamed=_bridge_on_id_renamed,
                    )
                    self._workflow_editor = editor
                    self._workflow_handler = build_workflow_handler(
                        self.settings, runner, workflow_editor=editor,
                        command_registry=command_registry,
                    )
        return self._workflow_handler, self._workflow_editor  # type: ignore[return-value]

    def run_workflow_command(self, text: str, *, chat_backend: str | None = None) -> dict:
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
        return self._workflow_capability_for_compat().run_command(
            text, chat_backend=chat_backend
        )

    def _workflow_capability_for_compat(self) -> WorkflowCapability:
        """Lazy fallback for minimal bridge fixtures that intentionally bypass
        ``__init__`` while injecting workflow seams."""
        capability = getattr(self, "_workflow_capability", None)
        if capability is None:
            capability = WorkflowCapability(self)
            self._workflow_capability = capability
        return capability

    def _legacy_run_workflow_command(self, text: str, *, chat_backend: str | None = None) -> dict:
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
        create_arg = self._workflow_create_arg(remainder)
        if chat_backend and create_arg is not None and not create_arg.lstrip().startswith("{"):
            return self._run_workflow_create_with_chat_backend(
                create_arg,
                chat_backend=chat_backend,
            )
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

    @staticmethod
    def _workflow_create_arg(remainder: str) -> str | None:
        parts = (remainder or "").strip().split(maxsplit=1)
        if not parts or parts[0].lower() != "create":
            return None
        return parts[1].strip() if len(parts) > 1 else ""

    def _run_workflow_create_with_chat_backend(
        self,
        description: str,
        *,
        chat_backend: str | None,
    ) -> dict:
        if not description.strip():
            handler, _editor = self._workflow_surface()
            result = handler("create", _WF_WEB_CHAT_ID)
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

        _handler, editor = self._workflow_surface()
        backend = (chat_backend or default_chat_backend(self.settings) or CHAT_BACKEND_LOCAL).strip().lower()
        if backend not in {
            CHAT_BACKEND_LOCAL,
            CHAT_BACKEND_GEMINI,
            CHAT_BACKEND_CLOUD_MISTRAL,
            CHAT_BACKEND_CLOUD_PICKLE,
            CHAT_BACKEND_CLOUD_NVIDIA,
            CHAT_BACKEND_CLOUD_POOL,
        }:
            backend = default_chat_backend(self.settings)
        if not chat_backend_enabled(self.settings, backend):
            return {
                "status": STATUS_ERROR,
                "message": self._chat_backend_disabled_message(backend),
                "actions": [],
            }

        runner = _WorkflowShimRunner(self.settings)
        planner = self._build_goal_planner(backend, runner)
        workflow, err, used_fallback = planner.draft(description.strip())
        if workflow is None:
            return {
                "status": STATUS_ERROR,
                "message": f"❌ 無法生成草稿：{err}\n可改用 /workflow new 手動建立。",
                "actions": [],
            }
        text, markup = editor.start_from_draft(_WF_WEB_CHAT_ID, workflow)
        prefix = self._workflow_draft_model_prefix(
            backend,
            used_fallback=used_fallback,
            metadata=self._goal_planner_metadata(planner),
        )
        if err:
            text = "⚠️ 草稿已開啟，但仍有待修正：\n" f"{err}\n\n{text}"
        if prefix:
            text = prefix + text
        return {
            "status": STATUS_OK,
            "message": str(text) if text is not None else "",
            "actions": self._markup_to_actions(markup),
        }

    @staticmethod
    def _goal_planner_metadata(planner: GoalPlanner) -> ModelMetadata | None:
        llm_client = getattr(planner, "llm_client", None)
        clients = llm_client if isinstance(llm_client, list) else [llm_client]
        for client in clients:
            metadata = getattr(client, "last_metadata", None)
            if metadata is not None:
                return metadata
        return None

    def _workflow_draft_model_prefix(
        self,
        chat_backend: str,
        *,
        used_fallback: bool,
        metadata: ModelMetadata | None,
    ) -> str:
        requested_provider, requested_model = self._requested_model_for_backend(chat_backend)
        final_provider = metadata.final_provider if metadata is not None else requested_provider
        final_model = metadata.final_model if metadata is not None else requested_model
        provider_label = {
            "gemini": "Gemini",
            "mistral": "Mistral",
            "opencode": "OpenCode",
            "local": "本地模型",
        }.get(final_provider, final_provider)
        requested_label = {
            "gemini": "Gemini",
            "mistral": "Mistral",
            "opencode": "OpenCode",
            "local": "本地模型",
        }.get(requested_provider, requested_provider)
        if used_fallback or (metadata is not None and metadata.fallback_reason):
            return (
                f"⚠️ 已從 {requested_label}（{requested_model}）改用 "
                f"{provider_label}（{final_model}）生成草稿。"
                f"{'原因：' + metadata.fallback_reason if metadata and metadata.fallback_reason else ''}\n\n"
            )
        return f"🤖 已使用 {provider_label}（{final_model}）生成草稿。\n\n"

    def run_workflow_action(self, callback_data: str) -> dict:
        """Re-invoke a ``wfe:`` workflow-editor button for the web console
        (reorder / delete / save / cancel a draft step). The same editor handler
        the Telegram bot dispatches, so step validation on save is identical."""
        return self._workflow_capability_for_compat().run_action(callback_data)

    def _legacy_run_workflow_action(self, callback_data: str) -> dict:
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
        return self._home.command("bluetooth", text)
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
        return self._home.action("bt", callback_data)
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
        return self._home.command("ir", text)
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
        return self._home.action("ir", callback_data)
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
        from telegram_core.list_view import LIST_VIEW_MODE_EDIT

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
    def _conversation_sessions(self) -> ConversationSession:
        if self._conversation_session is None:
            with self._conversation_session_lock:
                if self._conversation_session is None:
                    self._conversation_session = ConversationSession(
                        getattr(self.settings, "openclaw_web_memory_dir", None),
                        store=self._session_store,
                    )
                if self._session_store is not None:
                    self._conversation_session.adopt_store(self._session_store)
        return self._conversation_session

    def _sessions(self) -> SessionMemoryStore:
        store = self._conversation_sessions().store()
        self._session_store = store
        return store

    def _event_sessions(self) -> SessionEventService:
        if self._event_sessions_inst is None:
            with self._event_sessions_lock:
                if self._event_sessions_inst is None:
                    self._event_sessions_inst = SessionEventService(self.settings, self._sessions)
        return self._event_sessions_inst

    def load_session(self) -> dict:
        """GET — journal projection with the historical snapshot wire shape."""
        try:
            return {"status": STATUS_OK, "session": self._event_sessions().projection()}
        except Exception as exc:  # noqa: BLE001 — must never crash the API
            logger.exception("session memory: load failed")
            return {"status": STATUS_ERROR, "message": f"讀取 session 失敗：{exc}",
                    "session": empty_session()}

    def read_session_events(
        self, *, session_id: str | None = None, after: int | None = None, limit: int = 500
    ) -> dict:
        """Return exact cursor pages from the durable session authority."""
        try:
            journal = self._event_sessions().ensure(session_id)
            page = journal.read(after=after, limit=limit)
            return {
                "status": STATUS_OK,
                "event_version": 1,
                "session_id": journal.session_id,
                "events": [event.to_dict() for event in page.events],
                "server_cursor": page.server_cursor,
                "has_more": page.has_more,
            }
        except CursorExpiredError:
            return {
                "status": "cursor_expired", "event_version": 1,
                "session_id": session_id or "web-default", "projection": self._event_sessions().projection(session_id),
            }
        except (TypeError, ValueError) as exc:
            return {"status": STATUS_ERROR, "message": f"無效的 cursor：{exc}"}

    def save_session(self, snapshot: object) -> dict:
        """POST — preserve legacy compatibility without overwriting event history."""
        try:
            stored = self._sessions().save(snapshot)
            self._event_sessions().save_compat_snapshot(snapshot)
        except SessionWriteError as exc:
            return {"status": STATUS_ERROR, "message": f"儲存 session 失敗：{exc}"}
        except Exception as exc:  # noqa: BLE001
            logger.exception("session memory: save failed")
            return {"status": STATUS_ERROR, "message": f"儲存 session 失敗：{exc}"}
        return {"status": STATUS_OK, "updated_at": stored.get("updated_at")}

    def clear_session(self) -> dict:
        """DELETE — clear projection through an event, then remove fallback state."""
        try:
            self._event_sessions().clear()
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

    def load_chat_settings(self) -> dict:
        return {"status": STATUS_OK, "settings": chat_llm_pool_payload(self.settings)}

    def save_chat_settings(self, payload: object) -> dict:
        current = chat_llm_pool_payload(self.settings)
        normalized = normalize_chat_llm_pool_settings(self.settings, payload)
        next_local = resolve_provider_model(self.settings, LLM_PROVIDER_LOCAL)
        local_changed = normalized.providers[LLM_PROVIDER_LOCAL].model != current["providers"]["local"]["model"]
        local_reload = {"status": "skipped", "model": normalized.providers[LLM_PROVIDER_LOCAL].model}
        to_persist = normalized.to_dict()
        status = STATUS_OK
        message = "模型設定已儲存。"

        if local_changed and normalized.providers[LLM_PROVIDER_LOCAL].enabled:
            target_model = normalized.providers[LLM_PROVIDER_LOCAL].model
            try:
                self._warm_local_model(target_model)
                local_reload = {
                    "status": "ok",
                    "model": target_model,
                    "message": f"本地模型已載入：{target_model}",
                }
                next_local = target_model
            except Exception as exc:  # noqa: BLE001
                logger.warning("llm pool: local warmup failed target=%s err=%s", target_model, exc)
                to_persist["providers"]["local"]["model"] = current["providers"]["local"]["model"]
                local_reload = {
                    "status": "error",
                    "model": target_model,
                    "previous_model": current["providers"]["local"]["model"],
                    "message": f"本地模型載入失敗：{exc}",
                }
                status = "partial"
                message = (
                    f"雲端設定已儲存，但本地模型載入失敗，已保留原模型："
                    f"{current['providers']['local']['model']}"
                )
        try:
            save_chat_llm_pool_settings(self.settings, to_persist)
        except ChatLlmPoolWriteError as exc:
            return {"status": STATUS_ERROR, "message": f"儲存模型設定失敗：{exc}"}
        if status == STATUS_OK and local_changed and normalized.providers[LLM_PROVIDER_LOCAL].enabled:
            message = f"本地模型已載入：{next_local}"
        return {
            "status": status,
            "message": message,
            "settings": chat_llm_pool_payload(self.settings),
            "local_reload": local_reload,
        }

    def model_routes(self) -> dict:
        """Return the concrete model chain behind each web Chat model tab."""
        local = self._local_model()
        gemini_chain = [
            {"provider": "gemini", "model": model}
            for model in self._gemini_route_models()
        ]
        gemini_chain.append({"provider": "local", "model": local})
        provider_map = {
            LLM_PROVIDER_GEMINI: ("gemini", self._gemini_primary_model()),
            LLM_PROVIDER_MISTRAL: ("mistral", self._mistral_model()),
            LLM_PROVIDER_BIG_PICKLE: ("opencode", self._big_pickle_model()),
            LLM_PROVIDER_NVIDIA: ("nvidia", self._nvidia_model()),
        }
        cp_providers = [
            {"provider": provider_map[provider][0], "model": provider_map[provider][1]}
            for provider in cloud_pool_order(self.settings)
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
                    "configured": chat_backend_configured(self.settings, CHAT_BACKEND_CLOUD_POOL),
                },
                {
                    "backend": CHAT_BACKEND_LOCAL,
                    "label": "本地",
                    "requested_provider": "local",
                    "requested_model": local,
                    "chain": [{"provider": "local", "model": local}],
                    "configured": chat_backend_configured(self.settings, CHAT_BACKEND_LOCAL),
                },
                {
                    "backend": CHAT_BACKEND_CLOUD_MISTRAL,
                    "label": "Mistral",
                    "requested_provider": "mistral",
                    "requested_model": self._mistral_model(),
                    "chain": [{"provider": "mistral", "model": self._mistral_model()}],
                    "configured": chat_backend_configured(self.settings, CHAT_BACKEND_CLOUD_MISTRAL),
                },
                {
                    "backend": CHAT_BACKEND_GEMINI,
                    "label": "Gemini",
                    "requested_provider": "gemini",
                    "requested_model": self._gemini_primary_model(),
                    "chain": gemini_chain,
                    "configured": chat_backend_configured(self.settings, CHAT_BACKEND_GEMINI),
                },
                {
                    "backend": CHAT_BACKEND_CLOUD_PICKLE,
                    "label": "OpenCode",
                    "requested_provider": "opencode",
                    "requested_model": self._big_pickle_model(),
                    "chain": [{"provider": "opencode", "model": self._big_pickle_model()}],
                    "configured": chat_backend_configured(self.settings, CHAT_BACKEND_CLOUD_PICKLE),
                },
                {
                    "backend": CHAT_BACKEND_CLOUD_NVIDIA,
                    "label": "NVIDIA",
                    "requested_provider": "nvidia",
                    "requested_model": self._nvidia_model(),
                    "chain": [{"provider": "nvidia", "model": self._nvidia_model()}],
                    "configured": chat_backend_configured(self.settings, CHAT_BACKEND_CLOUD_NVIDIA),
                },
            ],
            "vision": self._vision_pool_route(),
        }

    def _vision_pool_route(self) -> dict[str, object] | None:
        """Preview of the vision pool for the UI banner: full enabled chain plus
        the first provider whose settings are configured (no probing)."""
        chain = self._vision_pool_chain()
        if not chain:
            return None
        preview = next(
            ((provider, model) for provider, model, _build, configured in chain if configured()),
            (chain[0][0], chain[0][1]),
        )
        return {
            "label": "視覺池",
            "requested_provider": preview[0],
            "requested_model": preview[1],
            "chain": [{"provider": provider, "model": model} for provider, model, _b, _c in chain],
        }

    def _requested_model_for_backend(self, chat_backend: str) -> tuple[str, str]:
        return self._providers.requested_model_for_backend(chat_backend)

    def _model_metadata_for_backend(
        self,
        chat_backend: str,
        attempted: tuple[ModelAttempt, ...],
        final_provider: str,
        final_model: str,
        *,
        fallback_reason: str | None = None,
    ) -> ModelMetadata:
        return self._providers.model_metadata_for_backend(
            chat_backend,
            attempted,
            final_provider,
            final_model,
            fallback_reason=fallback_reason,
        )

    def _generate_chat_response_blocking(
        self, prompt: str, chat_backend: str, *, conversation_key: str | None = None
    ) -> tuple[str, ModelMetadata]:
        if chat_backend == CHAT_BACKEND_CLOUD_PICKLE:
            client = self._build_cloud_chat_client()
            if client is None:
                raise RuntimeError("cloud pickle 後端目前無法使用（OpenCode 未設定或無法連線）。")
            message = client.generate(prompt, temperature=0.7)
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
            message = client.generate(prompt, temperature=0.7)
            metadata = self._model_metadata_for_backend(
                chat_backend,
                (ModelAttempt("mistral", self._mistral_model(), _MODEL_STATUS_OK),),
                "mistral",
                self._mistral_model(),
            )
            return message, metadata
        if chat_backend == CHAT_BACKEND_CLOUD_NVIDIA:
            client = self._build_nvidia_chat_client()
            if client is None:
                raise RuntimeError("NVIDIA 後端目前無法使用（未設定 NVIDIA_KEY）。")
            message = client.generate(prompt, temperature=0.7)
            metadata = self._model_metadata_for_backend(
                chat_backend,
                (ModelAttempt("nvidia", self._nvidia_model(), _MODEL_STATUS_OK),),
                "nvidia",
                self._nvidia_model(),
            )
            return message, metadata
        if chat_backend == CHAT_BACKEND_GEMINI:
            return self._generate_gemini_with_fallback(prompt, temperature=0.7)
        if chat_backend == CHAT_BACKEND_CLOUD_POOL:
            return self._handle_cloud_pool_blocking(prompt, conversation_key=conversation_key)
        message = self._ollama_generate_blocking(prompt)
        metadata = self._model_metadata_for_backend(
            CHAT_BACKEND_LOCAL,
            (ModelAttempt("local", self._local_model(), _MODEL_STATUS_OK),),
            "local",
            self._local_model(),
        )
        return message, metadata

    def _stream_chat_response(
        self, prompt: str, chat_backend: str, *, conversation_key: str | None = None
    ) -> Iterator[dict]:
        if chat_backend == CHAT_BACKEND_CLOUD_PICKLE:
            yield from self._stream_cloud_chat(prompt)
            return
        if chat_backend == CHAT_BACKEND_CLOUD_MISTRAL:
            yield from self._stream_mistral_chat(prompt)
            return
        if chat_backend == CHAT_BACKEND_CLOUD_NVIDIA:
            yield from self._stream_nvidia_chat(prompt)
            return
        if chat_backend == CHAT_BACKEND_GEMINI:
            yield from self._stream_gemini_chat(prompt)
            return
        if chat_backend == CHAT_BACKEND_CLOUD_POOL:
            yield from self._stream_cloud_pool_chat(prompt, conversation_key=conversation_key)
            return
        yield from self._stream_ollama_chat(prompt)

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
        from .dynamic_tools import OpenCodeTextClient

        base_url = self.settings.openclaw_opencode_base_url
        model = self._big_pickle_model()
        # The actual generation is the health check. A preflight generation
        # doubled hot-path model calls and could consume the full probe timeout;
        # cloud-pool callers already fail over when this client's request fails.
        return OpenCodeTextClient(
            base_url=base_url,
            model=model,
            api_key=getattr(self.settings, "openclaw_opencode_api_key", None),
            timeout_seconds=180,
        )

    def _build_mistral_chat_client(self):
        """Mistral cloud chat client; returns None when MISTRAL_API_KEY not set."""
        from .dynamic_tools import MistralTextClient

        key = getattr(self.settings, "openclaw_mistral_api_key", None)
        if not key:
            return None
        model = self._mistral_model()
        return MistralTextClient(api_key=key, model=model, timeout_seconds=180)

    def _build_nvidia_chat_client(self):
        """NVIDIA NIM cloud chat client; returns None when NVIDIA_KEY not set."""
        from .dynamic_tools import NvidiaTextClient

        key = getattr(self.settings, "openclaw_nvidia_api_key", None)
        if not key:
            return None
        model = self._nvidia_model()
        return NvidiaTextClient(api_key=key, model=model, timeout_seconds=180)

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

    def _stream_nvidia_chat(self, prompt: str) -> "Iterator[dict]":
        client = self._build_nvidia_chat_client()
        if client is None:
            yield stream_error("NVIDIA 後端目前無法使用（未設定 NVIDIA_KEY）。")
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
            yield stream_error(f"NVIDIA 後端失敗：{result['error']}")
            return
        text = str(result.get("text") or "").strip()
        if text:
            yield stream_delta(text)
        metadata = self._model_metadata_for_backend(
            CHAT_BACKEND_CLOUD_NVIDIA,
            (ModelAttempt("nvidia", self._nvidia_model(), _MODEL_STATUS_OK),),
            "nvidia",
            self._nvidia_model(),
        )
        yield stream_done(text, model_metadata=metadata)

    def _generate_gemini_with_fallback(
        self, prompt: str, *, temperature: float
    ) -> tuple[str, ModelMetadata]:
        return self._providers.generate_gemini_with_fallback(prompt, temperature=temperature)

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
        return self._providers.cloud_pool_chain()

    def _vision_pool_chain(self) -> list[tuple[str, str, Callable[[], object], Callable[[], bool]]]:
        return self._providers.vision_pool_chain()

    def _cloud_pool_preview(self) -> tuple[str, str]:
        return self._providers.cloud_pool_preview()

    def _handle_cloud_pool_blocking(
        self,
        prompt: str,
        *,
        pool_rotation: "CloudPoolRotation | None" = None,
        conversation_key: str | None = None,
    ) -> tuple[str, ModelMetadata]:
        return self._providers.generate_cloud_pool_blocking(
            prompt, pool_rotation=pool_rotation, conversation_key=conversation_key
        )

    def _stream_cloud_pool_chat(
        self, prompt: str, *, conversation_key: str | None = None
    ) -> Iterator[dict]:
        """Try Gemini → Mistral → Big Pickle → local for streaming."""
        attempts: list[ModelAttempt] = []

        pinned = self._providers.pinned_provider(conversation_key)
        chain = (
            _pin_provider_chain(self._cloud_pool_chain(), pinned)
            if pinned is not None
            else self._cloud_pool_chain()
        )

        for provider, model_name, build_fn, configured_fn in chain:
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
            self._providers.record_pin(conversation_key, provider)
            text = str(result.get("text") or "").strip()
            if text:
                yield stream_delta(text)
            fb = len(attempts) > 1
            first_provider = chain[0][0]
            first_model = chain[0][1]
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

        if provider_enabled(self.settings, LLM_PROVIDER_LOCAL):
            local_model = self._local_model()
            text = self._ollama_generate_blocking(prompt)
            attempts.append(ModelAttempt("local", local_model, _MODEL_STATUS_OK))
            first_provider, first_model = self._cloud_pool_preview()
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
            return
        yield stream_error("雲端池目前沒有可用模型。")

    def _local_model(self) -> str:
        return self._providers.local_model()

    def _big_pickle_model(self) -> str:
        return self._providers.big_pickle_model()

    def _mistral_model(self) -> str:
        return self._providers.mistral_model()

    def _nvidia_model(self) -> str:
        return self._providers.nvidia_model()

    def _gemini_primary_model(self) -> str:
        return self._providers.gemini_primary_model()

    def _gemini_flash_model(self) -> str:
        return self._providers.gemini_flash_model()

    def _gemini_route_models(self) -> tuple[str, ...]:
        return self._providers.gemini_route_models()

    def _warm_local_model(self, model: str) -> None:
        from .dynamic_tools import OllamaTextClient

        client = OllamaTextClient(
            endpoint=self.settings.openclaw_local_text_endpoint,
            model=model,
            timeout_seconds=min(max(10, self.settings.openclaw_local_text_timeout_seconds), 60),
        )
        client.generate("Reply with exactly: ok", temperature=0.0)

    def _chat_backend_disabled_message(self, chat_backend: str) -> str:
        if chat_backend == CHAT_BACKEND_CLOUD_POOL:
            return "雲端池目前已停用，請先在設定中啟用至少一個 provider。"
        labels = {
            CHAT_BACKEND_LOCAL: "本地模型",
            CHAT_BACKEND_GEMINI: "Gemini",
            CHAT_BACKEND_CLOUD_MISTRAL: "Mistral",
            CHAT_BACKEND_CLOUD_PICKLE: "OpenCode",
            CHAT_BACKEND_CLOUD_NVIDIA: "NVIDIA",
        }
        return f"{labels.get(chat_backend, '此模型')}目前已停用，請先到設定中重新啟用。"

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
        if chat_backend == CHAT_BACKEND_CLOUD_NVIDIA:
            client = self._build_nvidia_chat_client()
            if client is None:
                raise RuntimeError("NVIDIA 後端目前無法使用（未設定 NVIDIA_KEY）。")
            message = client.generate(prompt, temperature=0.2)
            metadata = self._model_metadata_for_backend(
                chat_backend,
                (ModelAttempt("nvidia", self._nvidia_model(), _MODEL_STATUS_OK),),
                "nvidia",
                self._nvidia_model(),
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
        """Run the uploaded image through the vision pool first (when configured),
        falling back to the local OCR + 繁體中文 translation pipeline the Telegram
        photo path uses (#43)."""
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

        # --- WP-7: try vision pool first ---
        b64 = _encode_image_attachment(image)
        if b64 is not None and enabled_vision_pool_providers(self.settings):
            from .vision_pool import walk_vision_pool_chain

            chain = self._vision_pool_chain()
            if chain:
                user_hint = (req.input or "").strip()
                vision_prompt = (
                    "請將這張圖片中的文字提取並翻譯成繁體中文。"
                    "先輸出偵測到的原文語言，然後輸出翻譯結果，最後附上原文。"
                )
                if user_hint:
                    vision_prompt += f"\n\n使用者補充說明：{user_hint}"
                text, provider, model_name, _attempts = walk_vision_pool_chain(
                    chain, vision_prompt, [b64], temperature=0.2,
                )
                if text and isinstance(text, str) and text.strip():
                    message = (
                        f"🌐→🇹🇼 圖片翻譯（視覺模型：{provider}/{model_name}）\n\n"
                        f"{text.strip()}"
                    )
                    return WebCommandResponse(
                        status=STATUS_OK,
                        message=message,
                        mode=MODE_TRANSLATION,
                        submode=SUBMODE_IMAGE_TRANSLATION,
                    )

        # --- fallback: local OCR pipeline ---
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
