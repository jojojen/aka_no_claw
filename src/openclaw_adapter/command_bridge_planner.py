"""Trusted chat-tool planning for the command bridge (P1 R1.3a, #74).

The planner owns the hidden no-tool/tool routing decision for Web Chat:
prompt assembly (system prompt + conversation history + tool ledger +
optional vision observation), per-backend plan generation, and strict-JSON
validation via ``parse_chat_tool_plan``. Untrusted model output never
selects a tool directly — an unparseable or failed plan falls back to the
plain chat path (``select_plan`` returns ``(None, None)``).

``CommandBridge`` satisfies :class:`PlannerDeps` and keeps thin same-name
delegates for every moved method, so existing instance monkeypatches and
consumers are unaffected (same pattern as R1.2b's ``ProviderRouter``).
The seams the planner calls *back through deps* (rather than on itself)
are exactly the bridge methods tests monkeypatch:
``_build_chat_tool_plan_prompt`` / ``_chat_tool_plan_system_prompt`` /
``_local_judgment_model`` / ``_generate_*_chat_tool_plan*``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from .command_bridge_models import (
    CHAT_BACKEND_CLOUD_MISTRAL,
    CHAT_BACKEND_CLOUD_NVIDIA,
    CHAT_BACKEND_CLOUD_PICKLE,
    CHAT_BACKEND_CLOUD_POOL,
    CHAT_BACKEND_GEMINI,
    CHAT_BACKEND_LOCAL,
    CHAT_TOOL_BLUETOOTH,
    CHAT_TOOL_IR,
    CHAT_TOOL_MUSIC,
    CHAT_TOOL_MUSICQUEUE,
    CHAT_TOOL_RESEARCH,
    CHAT_TOOL_SEARCH,
    CHAT_TOOL_VISION,
    ChatToolPlan,
    ModelAttempt,
    ModelMetadata,
    WebCommandRequest,
    _CHAT_ROLE_LABELS,
    parse_chat_tool_plan,
)
from .command_bridge_providers import (
    ProviderRouter,
    _GeminiRequestError,
    _MODEL_STATUS_NOT_CONFIGURED,
    _MODEL_STATUS_OK,
    _is_gemini_fallback_status,
    _pin_provider_chain,
    _walk_cloud_pool_chain,
)

if TYPE_CHECKING:
    from .llm_pool_settings import CloudPoolRotation

logger = logging.getLogger(__name__)

# Web Chat tool planning (#45 follow-up). The selected chat backend decides, in
# one strict-JSON response, whether to answer directly or call an allowlisted
# tool. Direct answers are represented as a hidden no-tool plan so Web Chat and
# Telegram-style tool use share the same main path.
_ROUTER_TIMEOUT_CAP_SECONDS = 30
_CHAT_TOOL_PLAN_PROMPT_TEMPLATE = (
    "你是 aka_no_claw 聊天助理。請根據對話決定："
    "直接回答使用者，或使用一個工具處理『最新訊息』。\n"
    "可用工具：\n{tool_lines}\n"
    "若是閒聊、改寫、翻譯、一般常識、人物介紹、解釋、摘要，"
    "或任何你可直接回答的情況，就不要用工具，直接回答。\n"
    "只輸出一個 JSON 物件，不要加任何多餘文字或說明：\n"
    '{{"tool":"__no_tool__","answer":"...","reason_summary":"..."}}\n'
    "或\n"
    '{{"tool":"{tool_choices}","query":"...","reason_summary":"..."}}\n'
    "例如：\n"
    '- 問「米津玄師是誰」→ {{"tool":"__no_tool__","answer":"...","reason_summary":"一般知識"}}\n'
    '- 問「今天東京天氣」→ {{"tool":"/search","query":"東京 今天天氣","reason_summary":"需要最新資訊"}}\n'
    '- 問「先幫我查一下東京天氣，再用日文念出來」→ {{"tool":"__goal__","query":"查東京天氣後用日文念出來","reason_summary":"想現在就完成的多步驟任務"}}\n'
    '- 問「建立工作流：查東京天氣，用女僕口吻以日文報告」→ '
    '{{"tool":"__create_workflow__","query":"查東京天氣，用女僕口吻以日文報告","reason_summary":"要求建立可重複使用的工作流程"}}\n'
    "當 tool=__no_tool__ 時，answer 必須是給使用者看的最終答案，"
    "用繁體中文自然回答。\n"
    "當使用者是明確要求「建立/新增/設定一個工作流程」這件事本身（例如訊息以"
    "「建立工作流：」、「幫我建立一個工作流」開頭），要用 __create_workflow__，"
    "不要用 __goal__——工作流是要保存起來、之後可以重複執行的流程定義，"
    "不是現在就執行一次的任務；query 必須是工作流的完整內容描述。\n"
    "當 tool=__goal__ 時，代表使用者是在描述一個想要『現在就完成』的多步驟"
    "任務或目標（不是要求建立一個之後可重複執行的工作流程本身），"
    "query 必須是使用者想完成的多步驟目標描述，不要回答成最終執行結果。\n"
    "當 tool 是真實工具時，query 必須是該工具可直接執行的參數；"
    "若使用者用代名詞（她／它／這個），請用對話紀錄把主詞補回 query。"
)


class PlannerDeps(Protocol):
    """What the planner needs from the bridge facade.

    The ``_generate_*`` / ``_build_chat_tool_plan_prompt`` /
    ``_chat_tool_plan_system_prompt`` / ``_local_judgment_model`` members are
    deliberately called back through this protocol (the bridge delegates
    forward to the planner implementations) so existing instance
    monkeypatches on the bridge keep intercepting the calls.
    """

    def _build_gemini_chat_client(self, model: str) -> object | None: ...

    def _build_mistral_chat_client(self) -> object | None: ...

    def _build_cloud_chat_client(self) -> object | None: ...

    def _build_nvidia_chat_client(self) -> object | None: ...

    def _handlers(self): ...

    def _conversation_key(self, req: WebCommandRequest) -> str: ...

    def _chat_tool_ledger_entries(self, req: WebCommandRequest) -> list[dict]: ...

    def _local_judgment_model(self) -> str: ...

    def _build_chat_tool_plan_prompt(
        self, req: WebCommandRequest, observation: str | None = None
    ) -> str: ...

    def _chat_tool_plan_system_prompt(self) -> str: ...

    def _generate_chat_tool_plan_with_chat_backend(
        self,
        chat_backend: str,
        prompt: str,
        *,
        pool_rotation: "CloudPoolRotation | None" = None,
        conversation_key: str | None = None,
    ) -> tuple[str, ModelMetadata]: ...

    def _generate_local_chat_tool_plan(self, prompt: str) -> tuple[str, ModelMetadata]: ...

    def _generate_gemini_chat_tool_plan(self, prompt: str) -> tuple[str, ModelMetadata]: ...

    def _generate_cloud_pool_chat_tool_plan(
        self,
        prompt: str,
        *,
        pool_rotation: "CloudPoolRotation | None" = None,
        conversation_key: str | None = None,
    ) -> tuple[str, ModelMetadata]: ...


class ChatToolPlanner:
    """Produces validated typed chat-tool plans; never executes anything."""

    def __init__(self, deps: PlannerDeps, providers: ProviderRouter) -> None:
        self._deps = deps
        self._providers = providers

    def select_plan(
        self, req: WebCommandRequest, observation: str | None = None
    ) -> tuple[ChatToolPlan | None, ModelMetadata | None]:
        """Ask the selected backend for a single hidden no-tool/tool plan.

        If the plan call fails or returns untrusted JSON, fall back to the
        plain chat path instead of risking a wrong tool invocation.
        """
        try:
            raw, metadata = self._deps._generate_chat_tool_plan_with_chat_backend(
                req.chat_backend,
                self._deps._build_chat_tool_plan_prompt(req, observation),
                conversation_key=self._deps._conversation_key(req),
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "[chat-tool-plan] planner unavailable backend=%s; plain-answer fallback",
                req.chat_backend,
                exc_info=True,
            )
            return None, None
        plan = parse_chat_tool_plan(raw)
        if plan is None:
            logger.info(
                "[chat-tool-plan] untrusted output; plain-answer fallback raw=%r",
                (raw or "")[:200],
            )
            return None, None
        logger.info(
            "[chat-tool-plan] tool=%s query=%r direct_answer=%s reason=%s",
            plan.tool,
            plan.query,
            bool(plan.answer),
            plan.reason_summary,
        )
        return plan, metadata

    def build_plan_prompt(
        self, req: WebCommandRequest, observation: str | None = None
    ) -> str:
        lines = [self._deps._chat_tool_plan_system_prompt(), "", "對話紀錄："]
        for turn in req.history:
            label = _CHAT_ROLE_LABELS.get(turn.role, turn.role)
            lines.append(f"{label}：{turn.content}")
        ledger = self._deps._chat_tool_ledger_entries(req)
        if ledger:
            lines += ["", "本次對話先前已執行過的工具紀錄（由舊到新）："]
            for entry in ledger:
                status_label = {"ok": "成功", "partial": "部分完成"}.get(
                    str(entry.get("status")), "失敗"
                )
                lines.append(
                    f"- {entry.get('tool')}（參數：{entry.get('query')}）→ "
                    f"{status_label}｜{entry.get('summary')}"
                )
            lines.append(
                "以上是已完成（或已失敗）的工作，不要為同樣的需求重複執行同一個工具："
                "若既有結果足以回答（包括使用者在問你做過什麼），直接用 no_tool 統整回答；"
                "若先前失敗，優先改用其他工具或利用已有資訊，而不是原樣重試。"
                "「同樣的需求」只指使用者重複要求同一件事；"
                "使用者提出新的動作要求（例如先前是播放、現在要停止）不是重複，要照常執行。"
            )
        if observation:
            lines += [
                "",
                "使用者這則訊息附帶了圖片，視覺模型已先觀察過，觀察結果如下：",
                observation,
                "若觀察結果足以回答，請直接回答（no_tool）；"
                "只有在需要針對問題重新細看圖片時才使用圖片查看工具。",
            ]
        # Ledger summaries describe the past, and async goal-loop results land
        # in the ledger only after completion — the planner once refused a
        # stop request "because nothing is playing" while a goal-started track
        # was audible. The planner must never veto a requested action based on
        # environment state it inferred from history; the tool is the source
        # of truth and reports reality itself.
        lines += [
            "",
            "當使用者的最新訊息是要求執行一個動作（停止、暫停、繼續、開、關等），"
            "一律以使用者的要求為準，直接呼叫對應工具執行；"
            "不要根據對話紀錄或工具紀錄推測目前的裝置／播放狀態，"
            "再以推測的狀態為理由拒絕執行——紀錄只描述過去，實際狀態由工具執行後回報。",
        ]
        lines += ["", f"使用者最新訊息：{(req.input or '').strip()}", "", "JSON："]
        return "\n".join(lines)

    def plan_system_prompt(self) -> str:
        tools = []
        tool_lines = [
            "- __goal__：當使用者是在描述一個想現在就完成的多步驟目標或任務時使用"
            "（不是要求建立一個之後可重複執行的工作流程本身）；"
            "query 是精簡後的目標描述。",
            "- __create_workflow__：當使用者明確要求「建立/新增/設定一個工作流程」"
            "這件事本身時使用（例如訊息以「建立工作流：」開頭）；"
            "query 是工作流的完整內容描述。",
        ]
        for tool in (
            CHAT_TOOL_SEARCH,
            CHAT_TOOL_RESEARCH,
            CHAT_TOOL_MUSIC,
            CHAT_TOOL_MUSICQUEUE,
            CHAT_TOOL_BLUETOOTH,
            CHAT_TOOL_IR,
            CHAT_TOOL_VISION,
        ):
            line = self.registered_chat_tool_prompt_line(tool)
            if not line:
                continue
            tools.append(tool)
            tool_lines.append(line)
        tool_choices = "__goal__|__create_workflow__|" + "|".join(tools)
        return _CHAT_TOOL_PLAN_PROMPT_TEMPLATE.format(
            tool_lines="\n".join(tool_lines),
            tool_choices=tool_choices,
        )

    def registered_chat_tool_prompt_line(self, command: str) -> str:
        try:
            registered = self._deps._handlers().get(command)
        except Exception:  # noqa: BLE001
            logger.debug(
                "router prompt: command registry unavailable for %s", command, exc_info=True
            )
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

    def local_judgment_model(self) -> str:
        """Hidden judgment calls (tool plan / satisfaction / goal drafts) run
        on the dedicated local text model, NOT the chat-pool model: the pool
        choice tunes user-visible answers (speed/style) and may be a small
        code model, which live-probed 6/12 on planning vs 12/12 for the text
        model — including truncating multi-step requests to a single step."""
        raw = (self._providers.settings.openclaw_local_text_model or "").split(",")[0].strip()
        return raw or self._providers.local_model()

    def generate_local_plan(self, prompt: str) -> tuple[str, ModelMetadata]:
        from .dynamic_tools import OllamaTextClient

        settings = self._providers.settings
        model = self._deps._local_judgment_model()
        client = OllamaTextClient(
            endpoint=settings.openclaw_local_text_endpoint,
            model=model,
            timeout_seconds=min(
                settings.openclaw_local_text_timeout_seconds,
                _ROUTER_TIMEOUT_CAP_SECONDS,
            ),
            keep_alive="30m",
        )
        text = client.generate(prompt, temperature=0.2)
        metadata = self._providers.model_metadata_for_backend(
            CHAT_BACKEND_LOCAL,
            (ModelAttempt("local", model, _MODEL_STATUS_OK),),
            "local",
            model,
        )
        return text, metadata

    def generate_with_chat_backend(
        self,
        chat_backend: str,
        prompt: str,
        *,
        pool_rotation: "CloudPoolRotation | None" = None,
        conversation_key: str | None = None,
    ) -> tuple[str, ModelMetadata]:
        if chat_backend == CHAT_BACKEND_LOCAL:
            return self._deps._generate_local_chat_tool_plan(prompt)
        if chat_backend == CHAT_BACKEND_GEMINI:
            return self._deps._generate_gemini_chat_tool_plan(prompt)
        if chat_backend == CHAT_BACKEND_CLOUD_MISTRAL:
            client = self._deps._build_mistral_chat_client()
            if client is None:
                raise RuntimeError("Mistral planner unavailable")
            text = client.generate(prompt, temperature=0.2)
            metadata = self._providers.model_metadata_for_backend(
                chat_backend,
                (ModelAttempt("mistral", self._providers.mistral_model(), _MODEL_STATUS_OK),),
                "mistral",
                self._providers.mistral_model(),
            )
            return text, metadata
        if chat_backend == CHAT_BACKEND_CLOUD_PICKLE:
            client = self._deps._build_cloud_chat_client()
            if client is None:
                raise RuntimeError("OpenCode planner unavailable")
            text = client.generate(prompt, temperature=0.2)
            metadata = self._providers.model_metadata_for_backend(
                chat_backend,
                (
                    ModelAttempt(
                        "opencode", self._providers.big_pickle_model(), _MODEL_STATUS_OK
                    ),
                ),
                "opencode",
                self._providers.big_pickle_model(),
            )
            return text, metadata
        if chat_backend == CHAT_BACKEND_CLOUD_NVIDIA:
            client = self._deps._build_nvidia_chat_client()
            if client is None:
                raise RuntimeError("NVIDIA planner unavailable")
            text = client.generate(prompt, temperature=0.2)
            metadata = self._providers.model_metadata_for_backend(
                chat_backend,
                (ModelAttempt("nvidia", self._providers.nvidia_model(), _MODEL_STATUS_OK),),
                "nvidia",
                self._providers.nvidia_model(),
            )
            return text, metadata
        if chat_backend == CHAT_BACKEND_CLOUD_POOL:
            return self._deps._generate_cloud_pool_chat_tool_plan(
                prompt, pool_rotation=pool_rotation, conversation_key=conversation_key
            )
        return self._deps._generate_local_chat_tool_plan(prompt)

    def generate_gemini_plan(self, prompt: str) -> tuple[str, ModelMetadata]:
        attempts: list[ModelAttempt] = []
        for model in self._providers.gemini_route_models():
            client = self._deps._build_gemini_chat_client(model)
            if client is None:
                attempts.append(
                    ModelAttempt(
                        "gemini",
                        model,
                        _MODEL_STATUS_NOT_CONFIGURED,
                        "Gemini API key missing",
                    )
                )
                continue
            try:
                text = client.generate(prompt, temperature=0.2)
            except _GeminiRequestError as exc:
                attempts.append(ModelAttempt("gemini", model, exc.status, str(exc)))
                if _is_gemini_fallback_status(exc.status):
                    continue
                raise
            attempts.append(ModelAttempt("gemini", model, _MODEL_STATUS_OK))
            fallback_reason = attempts[0].reason if len(attempts) > 1 else None
            return text, self._providers.model_metadata_for_backend(
                CHAT_BACKEND_GEMINI,
                tuple(attempts),
                "gemini",
                model,
                fallback_reason=fallback_reason,
            )
        raise RuntimeError("Gemini planner unavailable")

    def generate_cloud_pool_plan(
        self,
        prompt: str,
        *,
        pool_rotation: "CloudPoolRotation | None" = None,
        conversation_key: str | None = None,
    ) -> tuple[str, ModelMetadata]:
        chain = self._providers.cloud_pool_chain()
        pinned = self._providers.pinned_provider(conversation_key)
        if pinned is not None and any(entry[0] == pinned for entry in chain):
            chain = _pin_provider_chain(chain, pinned)
        elif pool_rotation is not None:
            chain = pool_rotation.rotate(chain)
        text, provider, model_name, attempts = _walk_cloud_pool_chain(
            chain, prompt, temperature=0.2
        )
        if text is None:
            raise RuntimeError("cloud-pool planner unavailable")
        self._providers.record_pin(conversation_key, provider)
        fb = len(attempts) > 1
        first_provider, first_model = (
            (chain[0][0], chain[0][1]) if chain else self._providers.cloud_pool_preview()
        )
        metadata = ModelMetadata(
            requested_provider=first_provider,
            requested_model=first_model,
            attempted_models=attempts,
            final_provider=provider,
            final_model=model_name,
            fallback_reason=None if not fb else f"Fell back from {attempts[0].provider}",
            fallback_occurred=fb,
            requested_tab=CHAT_BACKEND_CLOUD_POOL,
        )
        return text, metadata
