"""Chat-native card editor for workflow authoring (#53, Phase B+).

WorkflowEditor manages per-chat editor sessions. Each session holds a mutable
Workflow draft and a "collecting" cursor that routes the user's next plain-text
message to whichever step field is being filled in.

Callback prefix: ``wfe``.
  wfe:add         — open the step-kind picker
  wfe:add_cancel  — cancel mid-add, return to editor card
  wfe:kind:<k>    — choose step kind (tool_call|llm_transform|command_sink)
  wfe:cmd:<cmd>   — choose command for command_sink (e.g. /saynow)
  wfe:del:<idx>   — delete step at index
  wfe:save        — persist draft and close session
  wfe:cancel      — discard draft and close session
  wfe:noop        — step label button (does nothing)

Text capture: when session.collecting is set, the user's next plain message is
consumed by _advance_add() instead of reaching the main bot dispatcher.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .task_workspace import COMMAND_SINK_ALLOWLIST, Workflow, WorkflowStep, WorkflowStore

logger = logging.getLogger(__name__)

_SESSION_TTL = 600          # 10 minutes
_STEP_ID_PREFIX = "s"       # auto-generated step IDs


# ── Session state ─────────────────────────────────────────────────────────────

@dataclass
class _AddingStep:
    """State accumulated while adding (or editing) a step via the card editor."""
    kind: str                                # "tool_call" | "llm_transform" | "command_sink"
    fields: dict = field(default_factory=dict)
    collecting: str | None = None            # which field to request next from the user
    edit_index: int | None = None            # if set, replace step at this index instead of appending


@dataclass
class _EditorSession:
    chat_id: str
    workflow: Workflow
    adding: _AddingStep | None = None        # None = main editor view
    collecting: str | None = None            # "goal" (top-level field collection)
    created_at: float = field(default_factory=time.time)

    def is_collecting(self) -> bool:
        """True if the session is waiting for a plain-text message from the user."""
        return self.collecting is not None or (
            self.adding is not None and self.adding.collecting is not None
        )

    def next_step_id(self) -> str:
        return f"{_STEP_ID_PREFIX}{len(self.workflow.steps) + 1}"


# ── Rendering ─────────────────────────────────────────────────────────────────

def _step_label(step: WorkflowStep) -> str:
    if step.kind == "tool_call":
        args_str = ", ".join(f"{k}={v}" for k, v in (step.args or {}).items())
        return f"[tool] {step.tool}({args_str}) → {step.output}"
    if step.kind == "llm_transform":
        inputs = ", ".join(step.inputs or [])
        return f"[llm] [{inputs}] → {step.output}"
    if step.kind == "command_sink":
        return f"[{step.command}] {step.input} → {step.output}"
    return f"[{step.kind}] → {step.output}"


def _render_editor_card(session: _EditorSession) -> tuple[str, dict]:
    """Return (message_text, inline_keyboard_markup) for the editor card."""
    wf = session.workflow
    lines = [f"✏️ *{wf.id}*", f"目標：{wf.goal}", ""]
    if wf.steps:
        lines.append("步驟：")
        for i, step in enumerate(wf.steps):
            lines.append(f"  {i + 1}. {_step_label(step)}")
    else:
        lines.append("_（尚無步驟）_")

    keyboard: list[list[dict]] = []
    n = len(wf.steps)
    # Two rows per step: a label row (noop) + an action row (edit/up/down/delete).
    for i, step in enumerate(wf.steps):
        keyboard.append([
            {"text": f"{i + 1}. {_step_label(step)}", "callback_data": "wfe:noop"},
        ])
        action_row = [{"text": "✏️ 編輯", "callback_data": f"wfe:edit:{i}"}]
        if i > 0:
            action_row.append({"text": "🔼", "callback_data": f"wfe:up:{i}"})
        if i < n - 1:
            action_row.append({"text": "🔽", "callback_data": f"wfe:down:{i}"})
        action_row.append({"text": "🗑", "callback_data": f"wfe:del:{i}"})
        keyboard.append(action_row)
    keyboard.append([{"text": "➕ 新增步驟", "callback_data": "wfe:add"}])
    keyboard.append([
        {"text": "💾 儲存", "callback_data": "wfe:save"},
        {"text": "✖️ 取消", "callback_data": "wfe:cancel"},
    ])

    markup = {"inline_keyboard": keyboard}
    return "\n".join(lines), markup


def _render_kind_picker() -> tuple[str, dict]:
    markup = {"inline_keyboard": [
        [{"text": "🌤 Tool Call（呼叫已生成工具）", "callback_data": "wfe:kind:tool_call"}],
        [{"text": "🤖 LLM Transform（LLM 轉換）", "callback_data": "wfe:kind:llm_transform"}],
        [{"text": "🔊 Command Sink（/saynow）", "callback_data": "wfe:kind:command_sink"}],
        [{"text": "✖️ 取消新增", "callback_data": "wfe:add_cancel"}],
    ]}
    return "選擇新步驟的類型：", markup


def _render_command_picker() -> tuple[str, dict]:
    rows = [[{"text": cmd, "callback_data": f"wfe:cmd:{cmd}"}]
            for cmd in sorted(COMMAND_SINK_ALLOWLIST)]
    rows.append([{"text": "✖️ 取消", "callback_data": "wfe:add_cancel"}])
    return "選擇要呼叫的指令：", {"inline_keyboard": rows}


# ── Editor class ──────────────────────────────────────────────────────────────

class WorkflowEditor:
    """Per-chat card editor for building and modifying Workflows."""

    def __init__(self, store: WorkflowStore) -> None:
        self._store = store
        self._sessions: dict[str, _EditorSession] = {}

    # ── Public entry points ───────────────────────────────────────────────────

    def start_new(self, chat_id: str) -> tuple[str, dict]:
        """Begin a blank workflow editor session. Returns (prompt, markup)."""
        wf = Workflow(id="", goal="")
        session = _EditorSession(chat_id=str(chat_id), workflow=wf, collecting="goal")
        self._sessions[str(chat_id)] = session
        return (
            "📝 新建 Workflow\n請輸入 ID 和目標，格式：\n`<id> / <目標>`\n例：`wf-morning / 早安工作流`",
            {},
        )

    def start_edit(self, chat_id: str, workflow_id: str) -> tuple[str, dict]:
        """Load an existing workflow into the editor. Returns (text, markup)."""
        wf = self._store.get(workflow_id)
        if wf is None:
            return f"找不到 workflow '{workflow_id}'", {}
        session = _EditorSession(chat_id=str(chat_id), workflow=_clone_workflow(wf))
        self._sessions[str(chat_id)] = session
        text, markup = _render_editor_card(session)
        return text, markup

    def start_from_draft(self, chat_id: str, workflow: Workflow) -> tuple[str, dict]:
        """Open the editor card pre-populated with an LLM-generated draft.

        The user lands directly on the editable step cards (edit / add / delete /
        reorder / save / cancel), so a one-line natural-language description can
        replace field-by-field manual authoring."""
        session = _EditorSession(chat_id=str(chat_id), workflow=_clone_workflow(workflow))
        self._sessions[str(chat_id)] = session
        header = "🤖 AI 已生成草稿，請檢查後儲存（可編輯／新增／刪除／排序步驟）：\n\n"
        text, markup = _render_editor_card(session)
        return header + text, markup

    def has_session(self, chat_id: str) -> bool:
        self._gc()
        return str(chat_id) in self._sessions

    def is_capturing(self, chat_id: str) -> bool:
        """True if this chat is mid-field-collection (next text message is consumed)."""
        self._gc()
        session = self._sessions.get(str(chat_id))
        return session is not None and session.is_collecting()

    # ── Text capture ──────────────────────────────────────────────────────────

    def handle_text_capture(
        self, text: str, chat_id: str
    ) -> tuple[str, dict] | None:
        """Process a captured plain-text message. Returns (reply_text, markup) or None
        if nothing was captured (session gone or not collecting)."""
        self._gc()
        session = self._sessions.get(str(chat_id))
        if session is None:
            return None
        if not session.is_collecting():
            return None
        return self._dispatch_capture(text.strip(), session)

    def _dispatch_capture(
        self, text: str, session: _EditorSession
    ) -> tuple[str, dict]:
        # Top-level goal collection
        if session.collecting == "goal":
            return self._collect_goal(text, session)
        # Step-level field collection
        if session.adding is not None and session.adding.collecting is not None:
            return self._advance_add(text, session)
        return "（無預期輸入）", {}

    def _collect_goal(
        self, text: str, session: _EditorSession
    ) -> tuple[str, dict]:
        if "/" in text:
            parts = text.split("/", 1)
            wf_id = parts[0].strip().replace(" ", "-").lower()
            goal = parts[1].strip()
        else:
            wf_id = text.strip().replace(" ", "-").lower()
            goal = text.strip()
        if not wf_id:
            return "ID 不能為空，請重新輸入（格式：`<id> / <目標>`）：", {}
        session.workflow.id = wf_id
        session.workflow.goal = goal
        session.collecting = None
        return _render_editor_card(session)

    def _advance_add(
        self, text: str, session: _EditorSession
    ) -> tuple[str, dict]:
        adding = session.adding
        assert adding is not None
        f = adding.collecting

        if f == "tool":
            adding.fields["tool"] = text
            adding.collecting = "args"
            return "請輸入 args（JSON 格式，或傳空訊息跳過）：", {}

        if f == "args":
            if text:
                try:
                    parsed = json.loads(text)
                    if not isinstance(parsed, dict):
                        return "args 必須是 JSON 物件（`{...}`），請重新輸入：", {}
                    adding.fields["args"] = parsed
                except json.JSONDecodeError:
                    # Accept key=value format
                    d: dict = {}
                    for pair in text.split(","):
                        if "=" in pair:
                            k, _, v = pair.partition("=")
                            d[k.strip()] = v.strip()
                    if not d:
                        return "格式錯誤，請用 JSON `{\"key\":\"val\"}` 或 key=val，或傳空訊息跳過：", {}
                    adding.fields["args"] = d
            adding.collecting = "output"
            return "請輸入輸出變數名稱（e.g. `weather`）：", {}

        if f == "inputs":
            adding.fields["inputs"] = [v.strip() for v in text.split(",") if v.strip()]
            adding.collecting = "instructions"
            return "請輸入 LLM 指示（instructions）：", {}

        if f == "instructions":
            adding.fields["instructions"] = text
            adding.collecting = "output"
            return "請輸入輸出變數名稱（e.g. `greeting`）：", {}

        if f == "input":
            adding.fields["input"] = text
            adding.collecting = "output"
            return "請輸入輸出變數名稱（e.g. `speech_result`）：", {}

        if f == "output":
            adding.fields["output"] = text
            return self._finalize_step(session)

        return "（未知欄位）", {}

    def _finalize_step(self, session: _EditorSession) -> tuple[str, dict]:
        adding = session.adding
        assert adding is not None
        fields = adding.fields
        is_edit = (
            adding.edit_index is not None
            and 0 <= adding.edit_index < len(session.workflow.steps)
        )
        sid = (
            session.workflow.steps[adding.edit_index].id
            if is_edit
            else session.next_step_id()
        )

        if adding.kind == "tool_call":
            step = WorkflowStep(
                id=sid, kind="tool_call",
                tool=fields.get("tool", ""),
                args=fields.get("args") or {},
                output=fields.get("output", "out"),
            )
        elif adding.kind == "llm_transform":
            step = WorkflowStep(
                id=sid, kind="llm_transform",
                inputs=fields.get("inputs") or [],
                instructions=fields.get("instructions", ""),
                output=fields.get("output", "out"),
            )
        elif adding.kind == "command_sink":
            step = WorkflowStep(
                id=sid, kind="command_sink",
                command=fields.get("command", ""),
                input=fields.get("input", ""),
                output=fields.get("output", "out"),
            )
        else:
            session.adding = None
            return "未知步驟類型", {}

        if is_edit:
            session.workflow.steps[adding.edit_index] = step
        else:
            session.workflow.steps.append(step)
        session.adding = None
        return _render_editor_card(session)

    # ── Callback dispatch ─────────────────────────────────────────────────────

    def callback_handlers(self) -> dict[str, Callable]:
        return {"wfe": self._handle_callback}

    def _handle_callback(
        self, payload: str, original_text: str, chat_id: str
    ) -> tuple[object, object, object]:
        """Returns (toast, new_text, new_markup)."""
        self._gc()
        parts = payload.split(":", 1)
        action = parts[0]
        arg = parts[1] if len(parts) > 1 else ""
        chat_id = str(chat_id)
        session = self._sessions.get(chat_id)

        if action == "noop":
            return None, None, None

        if action == "cancel":
            self._sessions.pop(chat_id, None)
            return "✖️ 已取消", "Workflow 編輯已取消。", None

        if session is None:
            return "⚠️ 編輯階段已過期，請重新執行 /workflow new。", None, None

        if action == "add":
            session.adding = None   # reset any partial add
            text, markup = _render_kind_picker()
            return None, text, markup

        if action == "add_cancel":
            session.adding = None
            text, markup = _render_editor_card(session)
            return "取消", text, markup

        if action == "kind":
            kind = arg
            if kind == "tool_call":
                session.adding = _AddingStep(kind="tool_call", collecting="tool")
                return None, "請輸入工具 slug（可從 /new list 查看）：", None
            if kind == "llm_transform":
                session.adding = _AddingStep(kind="llm_transform", collecting="inputs")
                return None, "請輸入 inputs（逗號分隔的輸入變數名稱，e.g. `weather`）：", None
            if kind == "command_sink":
                text, markup = _render_command_picker()
                return None, text, markup
            return "未知步驟類型", None, None

        if action == "cmd":
            cmd = arg
            if cmd not in COMMAND_SINK_ALLOWLIST:
                return f"指令 '{cmd}' 不在許可清單中", None, None
            session.adding = _AddingStep(
                kind="command_sink",
                fields={"command": cmd},
                collecting="input",
            )
            return None, f"請輸入 {cmd} 的輸入變數名稱（e.g. `greeting`）：", None

        if action == "del":
            try:
                idx = int(arg)
            except ValueError:
                return "無效索引", None, None
            if 0 <= idx < len(session.workflow.steps):
                removed = session.workflow.steps.pop(idx)
                _renumber_steps(session.workflow.steps)
                text, markup = _render_editor_card(session)
                return f"已刪除步驟 {removed.id}", text, markup
            return "步驟不存在", None, None

        if action == "edit":
            try:
                idx = int(arg)
            except ValueError:
                return "無效索引", None, None
            if not (0 <= idx < len(session.workflow.steps)):
                return "步驟不存在", None, None
            step = session.workflow.steps[idx]
            if step.kind == "tool_call":
                session.adding = _AddingStep(
                    kind="tool_call", collecting="tool", edit_index=idx)
                return None, (
                    f"✏️ 編輯步驟 {idx + 1}（目前工具：{step.tool}）\n"
                    "請輸入工具 slug（可從 /new list 查看）："
                ), None
            if step.kind == "llm_transform":
                session.adding = _AddingStep(
                    kind="llm_transform", collecting="inputs", edit_index=idx)
                return None, (
                    f"✏️ 編輯步驟 {idx + 1}（目前 inputs：{', '.join(step.inputs or [])}）\n"
                    "請輸入 inputs（逗號分隔的輸入變數名稱）："
                ), None
            if step.kind == "command_sink":
                session.adding = _AddingStep(
                    kind="command_sink",
                    fields={"command": step.command or ""},
                    collecting="input",
                    edit_index=idx,
                )
                return None, (
                    f"✏️ 編輯步驟 {idx + 1}（{step.command}，目前 input：{step.input}）\n"
                    f"請輸入 {step.command} 的輸入變數名稱："
                ), None
            return "未知步驟類型", None, None

        if action in ("up", "down"):
            try:
                idx = int(arg)
            except ValueError:
                return "無效索引", None, None
            steps = session.workflow.steps
            target = idx - 1 if action == "up" else idx + 1
            if 0 <= idx < len(steps) and 0 <= target < len(steps):
                steps[idx], steps[target] = steps[target], steps[idx]
                _renumber_steps(steps)
                text, markup = _render_editor_card(session)
                return ("已上移" if action == "up" else "已下移"), text, markup
            return "無法移動", None, None

        if action == "save":
            wf = session.workflow
            if not wf.id:
                return "⚠️ 請先輸入 workflow ID", None, None
            errors = wf.validate_references()
            if errors:
                return "⚠️ 定義有誤：" + errors[0], None, None
            self._store.save(wf)
            self._sessions.pop(chat_id, None)
            return f"✅ 已儲存 '{wf.id}'（{len(wf.steps)} 步驟）", f"Workflow *{wf.id}* 已儲存。", None

        return "未知動作", None, None

    # ── GC ───────────────────────────────────────────────────────────────────

    def _gc(self) -> None:
        now = time.time()
        expired = [k for k, v in self._sessions.items()
                   if now - v.created_at > _SESSION_TTL]
        for k in expired:
            del self._sessions[k]


# ── Helper ─────────────────────────────────────────────────────────────────────

def _clone_workflow(wf: Workflow) -> Workflow:
    return Workflow.from_dict(wf.to_dict())


def _renumber_steps(steps: list[WorkflowStep]) -> None:
    """Re-assign sequential step IDs (s1, s2, …) after a reorder or delete."""
    for i, step in enumerate(steps):
        step.id = f"{_STEP_ID_PREFIX}{i + 1}"
