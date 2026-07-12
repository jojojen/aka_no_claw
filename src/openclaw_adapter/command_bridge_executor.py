"""Trusted chat-tool execution for the command bridge (P1 R1.3b, #74).

The executor receives only a validated :class:`ChatToolPlan`, maps it to a
small allowlist, records every attempted operation, and owns the stream and
satisfaction mechanics around that execution.  Concrete integrations remain
callbacks on ``ExecutorDeps``: this deliberately preserves CommandBridge's
existing monkeypatch seams while the compatibility facade is being reduced.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import queue
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Protocol

from .command_bridge_models import (
    CHAT_BACKEND_LOCAL,
    CHAT_TOOL_BLUETOOTH,
    CHAT_TOOL_IR,
    CHAT_TOOL_MUSIC,
    CHAT_TOOL_MUSICQUEUE,
    CHAT_TOOL_RESEARCH,
    CHAT_TOOL_SEARCH,
    CHAT_TOOL_VISION,
    STATUS_ERROR,
    ChatToolPlan,
    ChatToolPolicy,
    ChatToolRequest,
    ChatToolResult,
    ModelMetadata,
    WebCommandRequest,
    WebCommandResponse,
    _tool_calling_notice,
    make_chat_tool_request,
    stream_delta,
    stream_done,
    stream_error,
    stream_heartbeat,
)

if TYPE_CHECKING:
    from .llm_pool_settings import CloudPoolRotation

logger = logging.getLogger(__name__)

_HEARTBEAT_SECONDS = 10.0
_CHAT_TOOL_LEDGER_LIMIT = 8
_CHAT_TOOL_LEDGER_SUMMARY_CHARS = 400

_SEARCH_TOOL_POLICY = ChatToolPolicy(
    display_name="網路搜尋",
    max_query_chars=256,
    max_source_field_chars=500,
    max_source_pack_chars=4000,
)
_RESEARCH_TOOL_POLICY = ChatToolPolicy(display_name="商品研究", max_query_chars=512)
_MUSIC_TOOL_POLICY = ChatToolPolicy(display_name="音樂控制", max_query_chars=128)
_MUSICQUEUE_TOOL_POLICY = ChatToolPolicy(display_name="音樂連播", max_query_chars=256)
_BLUETOOTH_TOOL_POLICY = ChatToolPolicy(display_name="藍牙控制", max_query_chars=128)
_IR_TOOL_POLICY = ChatToolPolicy(display_name="紅外線控制", max_query_chars=128)
_VISION_TOOL_POLICY = ChatToolPolicy(display_name="圖片查看", max_query_chars=512)

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


class ExecutorDeps(Protocol):
    def _conversation_key(self, req: WebCommandRequest) -> str: ...

    def _record_chat_tool_run(
        self, req: WebCommandRequest, tool: str, query: str, *, status: str, summary: str
    ) -> None: ...

    def _tool_display_name(self, command: str) -> str: ...

    def _tool_stream_heartbeat_seconds(self) -> float: ...

    def _exec_grounded_search(
        self, req: WebCommandRequest, tool_req: ChatToolRequest
    ) -> ChatToolResult: ...

    def _exec_registered_command_chat_tool(
        self, req: WebCommandRequest, tool_req: ChatToolRequest
    ) -> ChatToolResult: ...

    def _exec_vision_chat_tool(
        self, req: WebCommandRequest, tool_req: ChatToolRequest
    ) -> ChatToolResult: ...

    def _live_progress(self, callback: Callable[[str], None]): ...

    def _run_chat_tool(self, req: WebCommandRequest, plan: ChatToolPlan) -> ChatToolResult: ...

    def _maybe_upgrade_tool_result_to_goal_loop(
        self,
        req: WebCommandRequest,
        plan: ChatToolPlan,
        tool_result: ChatToolResult,
        *,
        planner_metadata: ModelMetadata | None,
        narrator: Callable[[str], None] | None = None,
    ) -> WebCommandResponse | None: ...

    def _push_orphaned_result(self, text: str) -> None: ...

    def _conversation_context_block(self, req: WebCommandRequest) -> str: ...

    def _generate_chat_tool_satisfaction_text(
        self,
        chat_backend: str,
        prompt: str,
        *,
        pool_rotation: "CloudPoolRotation | None" = None,
    ) -> str: ...

    def _generate_chat_tool_plan_with_chat_backend(
        self,
        chat_backend: str,
        prompt: str,
        *,
        pool_rotation: "CloudPoolRotation | None" = None,
    ) -> tuple[str, ModelMetadata]: ...


class ChatToolExecutor:
    """Run validated tool plans while preserving the bridge's public seams."""

    def __init__(self, deps: ExecutorDeps) -> None:
        self._deps = deps
        self._ledgers: dict[str, deque] = {}
        self._ledger_lock = threading.Lock()

    def record_run(
        self, req: WebCommandRequest, tool: str, query: str, *, status: str, summary: str
    ) -> None:
        entry = {
            "tool": tool,
            "query": " ".join(str(query or "").split())[:200],
            "status": status,
            "summary": " ".join(str(summary or "").split())[:_CHAT_TOOL_LEDGER_SUMMARY_CHARS],
        }
        with self._ledger_lock:
            ledger = self._ledgers.setdefault(
                self._deps._conversation_key(req), deque(maxlen=_CHAT_TOOL_LEDGER_LIMIT)
            )
            ledger.append(entry)

    def ledger_entries(self, req: WebCommandRequest) -> list[dict]:
        with self._ledger_lock:
            ledger = self._ledgers.get(self._deps._conversation_key(req))
            return list(ledger) if ledger else []

    def run(self, req: WebCommandRequest, plan: ChatToolPlan) -> ChatToolResult:
        policy_map: dict[str, tuple[ChatToolPolicy, Callable]] = {
            CHAT_TOOL_SEARCH: (_SEARCH_TOOL_POLICY, self._deps._exec_grounded_search),
            CHAT_TOOL_RESEARCH: (_RESEARCH_TOOL_POLICY, self._deps._exec_registered_command_chat_tool),
            CHAT_TOOL_MUSIC: (_MUSIC_TOOL_POLICY, self._deps._exec_registered_command_chat_tool),
            CHAT_TOOL_MUSICQUEUE: (_MUSICQUEUE_TOOL_POLICY, self._deps._exec_registered_command_chat_tool),
            CHAT_TOOL_BLUETOOTH: (_BLUETOOTH_TOOL_POLICY, self._deps._exec_registered_command_chat_tool),
            CHAT_TOOL_IR: (_IR_TOOL_POLICY, self._deps._exec_registered_command_chat_tool),
            CHAT_TOOL_VISION: (_VISION_TOOL_POLICY, self._deps._exec_vision_chat_tool),
        }
        entry = policy_map.get(plan.tool)
        if entry is None:
            raise ValueError(f"unknown chat tool: {plan.tool!r}")
        policy, executor = entry
        display_name = self._deps._tool_display_name(plan.tool)
        if display_name != plan.tool:
            policy = dataclasses.replace(policy, display_name=display_name)
        tool_req = make_chat_tool_request(
            tool=plan.tool,
            raw_query=plan.query,
            user_question=req.input or "",
            policy=policy,
        )
        try:
            result = executor(req, tool_req)
        except Exception as exc:
            self._deps._record_chat_tool_run(
                req, plan.tool, plan.query, status="error", summary=str(exc)
            )
            raise
        self._deps._record_chat_tool_run(
            req, plan.tool, plan.query, status="ok", summary=result.answer
        )
        return result

    def stream(self, req: WebCommandRequest, plan: ChatToolPlan) -> Iterator[dict]:
        yield stream_delta(_tool_calling_notice(plan.tool, self._deps._tool_display_name(plan.tool)))
        result: dict[str, object] = {}
        done = threading.Event()
        abandoned = threading.Event()
        narration_queue: queue.Queue[str] = queue.Queue()

        def worker() -> None:
            try:
                with self._deps._live_progress(narration_queue.put):
                    tool_result = self._deps._run_chat_tool(req, plan)
                    logger.info(
                        "[chat-tool] tool=%s sources=%d summary=%r",
                        plan.tool, tool_result.source_count, tool_result.result_summary,
                    )
                    upgraded = self._deps._maybe_upgrade_tool_result_to_goal_loop(
                        req, plan, tool_result, planner_metadata=None, narrator=narration_queue.put
                    )
                if upgraded is not None:
                    result["response"] = upgraded
                else:
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
                            self._deps._push_orphaned_result(str(orphan))
                        except Exception:  # noqa: BLE001
                            logger.exception("command bridge: failed to push orphaned tool result")

        threading.Thread(target=worker, daemon=True).start()
        last_beat = time.time()
        try:
            while not done.is_set() or not narration_queue.empty():
                try:
                    line = narration_queue.get(timeout=0.5)
                except queue.Empty:
                    if time.time() - last_beat >= self._deps._tool_stream_heartbeat_seconds():
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
            yield stream_done(response.message, model_metadata=response.model_metadata,
                              actions=[a.to_dict() for a in response.actions] or None)
            return
        metadata = result.get("model_metadata")
        yield stream_done(str(result.get("text") or "").strip(),
                          model_metadata=metadata if isinstance(metadata, ModelMetadata) else None)

    def result_satisfies_intent(
        self, req: WebCommandRequest, plan: ChatToolPlan, tool_result: ChatToolResult
    ) -> dict[str, object]:
        prompt = _CHAT_TOOL_SATISFACTION_PROMPT.format(
            context=self._deps._conversation_context_block(req) or "（無）",
            user_input=json.dumps((req.input or "").strip(), ensure_ascii=False),
            tool_name=json.dumps(plan.tool, ensure_ascii=False),
            tool_query=json.dumps(plan.query, ensure_ascii=False),
            tool_answer=json.dumps(tool_result.answer.strip(), ensure_ascii=False),
        )
        # Call back through the bridge compatibility seam.  Tests and
        # integrations patch this method to select a deterministic judge.
        parsed = self.parse_satisfaction(
            self._deps._generate_chat_tool_satisfaction_text(req.chat_backend, prompt).strip()
        )
        logger.info("[chat-tool] satisfaction tool=%s satisfied=%s environment_blocked=%s reason=%r",
                    plan.tool, parsed.get("satisfied"), parsed.get("environment_blocked"), parsed.get("reason"))
        return parsed

    def generate_satisfaction_text(
        self, chat_backend: str, prompt: str, *, pool_rotation: "CloudPoolRotation | None" = None
    ) -> str:
        backends = [chat_backend] + ([] if chat_backend == CHAT_BACKEND_LOCAL else [CHAT_BACKEND_LOCAL])
        last_exc: Exception | None = None
        for backend in backends:
            try:
                text, _ = self._deps._generate_chat_tool_plan_with_chat_backend(
                    backend, prompt, pool_rotation=pool_rotation
                )
                logger.info("[chat-tool] satisfaction backend=%s ok", backend)
                return text
            except Exception as exc:  # noqa: BLE001
                logger.warning("[chat-tool] satisfaction backend=%s failed: %s", backend, exc)
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("no available backend for chat tool satisfaction check")

    @staticmethod
    def parse_satisfaction(raw: str) -> dict[str, object]:
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
        environment_blocked = data.get("environment_blocked")
        return {
            "satisfied": satisfied,
            "environment_blocked": bool(environment_blocked) and not satisfied,
            "reason": str(data.get("reason", "")).strip(),
        }
