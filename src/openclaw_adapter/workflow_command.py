"""/workflow command handler (#53, Phase B).

Subcommands:
  /workflow list              — list all stored workflows
  /workflow show <id>         — show a workflow's steps
  /workflow run <id>          — execute a stored workflow
  /workflow delete <id>       — remove a stored workflow
  /workflow create <JSON>     — create a workflow from a JSON definition (power-user)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from .task_workspace import (
    COMMAND_SINK_ALLOWLIST,
    Workflow,
    WorkflowRunner,
    WorkflowStore,
)

logger = logging.getLogger(__name__)


def _workflow_store(runner) -> WorkflowStore:
    """Derive a WorkflowStore path from the runner's tools directory."""
    return WorkflowStore(Path(runner.tools_dir).parent / "workflow_store")


def build_workflow_handler(settings, runner) -> Callable[[str, str], object]:
    """Return a ``handler(remainder, chat_id)`` for the ``/workflow`` command.

    ``runner`` must implement the ``ToolCallExecutor`` protocol
    (i.e. have ``run_tool_step`` and ``tools_dir``) — in production this is a
    ``DynamicToolRunner``. ``settings`` is used to build the ``/saynow``
    dispatcher and, if available, the LLM client for ``llm_transform`` steps.
    """
    # Build the /saynow command sink lazily to avoid importing voice deps at
    # module level; the import lives only inside this closure scope.
    from .voice_command import build_saynow_handler as _build_saynow

    _saynow_raw = _build_saynow(settings)

    def handler(remainder: str, chat_id: str) -> object:
        parts = (remainder or "").strip().split(maxsplit=1)
        subcmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        store = _workflow_store(runner)

        if subcmd == "list":
            return _cmd_list(store)
        if subcmd == "show":
            return _cmd_show(arg, store)
        if subcmd == "delete":
            return _cmd_delete(arg, store)
        if subcmd == "create":
            return _cmd_create(arg, store)
        if subcmd == "run":
            return _cmd_run(arg, chat_id, store, runner, _saynow_raw, settings)
        return _help()

    return handler


# ── Subcommand implementations ────────────────────────────────────────────────

def _cmd_list(store: WorkflowStore) -> str:
    workflows = store.list()
    if not workflows:
        return "尚無已儲存的 workflow。\n用 /workflow create <JSON> 新增一個。"
    lines = [f"• {wf.id}：{wf.goal}（{len(wf.steps)} 步驟）" for wf in workflows]
    return "📋 Workflows\n" + "\n".join(lines)


def _cmd_show(workflow_id: str, store: WorkflowStore) -> str:
    if not workflow_id:
        return "用法：/workflow show <id>"
    wf = store.get(workflow_id)
    if wf is None:
        return f"找不到 workflow '{workflow_id}'"
    lines = [f"🔄 {wf.id}", f"目標：{wf.goal}", "步驟："]
    for i, step in enumerate(wf.steps, 1):
        if step.kind == "tool_call":
            args_str = ", ".join(f"{k}={v}" for k, v in (step.args or {}).items())
            lines.append(f"  {i}. [tool] {step.tool}({args_str}) → {step.output}")
        elif step.kind == "command_sink":
            lines.append(f"  {i}. [{step.command}] ←{step.input} → {step.output}")
        elif step.kind == "llm_transform":
            lines.append(
                f"  {i}. [llm] inputs={step.inputs} → {step.output}"
                + (f"\n      prompt：{step.instructions}" if step.instructions else "")
            )
        else:
            lines.append(f"  {i}. [{step.kind}] → {step.output}")
    return "\n".join(lines)


def _cmd_delete(workflow_id: str, store: WorkflowStore) -> str:
    if not workflow_id:
        return "用法：/workflow delete <id>"
    if store.delete(workflow_id):
        return f"✅ 已刪除 workflow '{workflow_id}'"
    return f"找不到 workflow '{workflow_id}'"


def _cmd_create(arg: str, store: WorkflowStore) -> str:
    if not arg:
        return (
            "用法：/workflow create <JSON>\n"
            "例：/workflow create {\"id\":\"wf-test\",\"goal\":\"測試\",\"steps\":[]}"
        )
    try:
        data = json.loads(arg)
    except json.JSONDecodeError as exc:
        return f"JSON 格式錯誤：{exc}"
    try:
        wf = Workflow.from_dict(data)
    except (KeyError, TypeError) as exc:
        return f"工作流結構錯誤：{exc}"
    errors = wf.validate_references()
    if errors:
        return "工作流定義有誤：\n" + "\n".join(errors)
    store.save(wf)
    return f"✅ workflow '{wf.id}' 已儲存（{len(wf.steps)} 步驟）"


def _cmd_run(
    workflow_id: str,
    chat_id: str,
    store: WorkflowStore,
    executor,          # ToolCallExecutor (DynamicToolRunner)
    saynow_raw,        # raw handler(text, chat_id) from build_saynow_handler
    settings,
) -> str:
    if not workflow_id:
        return "用法：/workflow run <id>"
    wf = store.get(workflow_id)
    if wf is None:
        return f"找不到 workflow '{workflow_id}'"

    # Build a /saynow dispatcher bound to the current chat_id.
    def _saynow(text: str) -> str:
        return str(saynow_raw(text, chat_id))

    dispatcher = {"/saynow": _saynow}

    # Use the runner's main LLM client for llm_transform steps (Big Pickle /
    # Mistral / local, whichever is active).  executor.client may not exist on
    # test fakes, so guard with getattr.
    llm_client = getattr(executor, "client", None)

    wf_runner = WorkflowRunner(
        executor=executor,
        command_dispatcher=dispatcher,
        llm_client=llm_client,
    )

    try:
        trace = wf_runner.run(wf)
    except Exception as exc:
        logger.exception("workflow_command: unexpected error running %s", workflow_id)
        return f"❌ workflow 執行異常：{exc}"

    try:
        store.save_trace(trace)
    except Exception:
        logger.warning("workflow_command: failed to save trace for %s", workflow_id)

    if trace.ok:
        result = trace.final_result or "（無輸出）"
        return f"✅ {wf.id} 完成\n{result}"
    return f"❌ {wf.id} 失敗\n{trace.final_result or '（無詳情）'}"


def _help() -> str:
    return (
        "用法：\n"
        "  /workflow list              — 列出所有 workflow\n"
        "  /workflow show <id>         — 顯示 workflow 的步驟\n"
        "  /workflow run <id>          — 執行 workflow\n"
        "  /workflow delete <id>       — 刪除 workflow\n"
        "  /workflow create <JSON>     — 從 JSON 建立 workflow"
    )
