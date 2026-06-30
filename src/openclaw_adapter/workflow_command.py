"""/workflow command handler (#53, Phase B/B+).

Subcommands:
  /workflow list              — list all stored workflows
  /workflow show <id>         — show a workflow's steps
  /workflow run <id>          — execute a stored workflow
  /workflow delete <id>       — remove a stored workflow
  /workflow create <自然語言>  — LLM drafts a workflow → editable card
  /workflow create <JSON>     — create a workflow from a JSON definition (power-user)
  /workflow new               — open card editor to create a new workflow
  /workflow edit <id>         — open card editor to edit an existing workflow
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from .task_workspace import (
    is_command_sink_allowed,
    Workflow,
    WorkflowRunner,
    WorkflowStore,
)

logger = logging.getLogger(__name__)


# Fallback usage hints for command_sink grounding. The authoritative source is
# each command's RegisteredCommand.usage (set where the command is registered);
# this map covers commands that don't declare one yet, and the test/headless
# paths that build the prompt without the full registry. Keep entries short —
# they teach the drafting LLM the real argument shape so it fills `literal`
# correctly instead of hallucinating commands like /musiclistbest.
_COMMAND_USAGE: dict[str, str] = {
    "/ir": "send <裝置> <按鍵名>，如 `send ceiling_light power`（切換天花板燈電源）",
    "/music": "playbest=播放最愛清單；<關鍵字>=搜尋並播放",
    "/musicmute": "靜音（無參數）",
    "/musiclouder": "調高音量（無參數）",
    "/musiclower": "調低音量（無參數）",
    "/musicnowbest": "把目前歌曲加入最愛清單（無參數）",
    "/saynow": "立即用語音念出文字（參數＝要念的文字，通常用 input 變數帶入）",
    "/say": "用語音念出文字（參數＝要念的文字）",
    "/bluetooth": "連線／切換藍牙裝置（參數＝裝置名）",
    "/translateja": "把文字翻成日文（參數＝原文，通常用 input 變數帶入）",
    "/translatezh": "把文字翻成中文（參數＝原文，通常用 input 變數帶入）",
}


def _command_usage(command: str, command_registry=None) -> str:
    """Resolve a command's usage hint: prefer the RegisteredCommand.usage that
    the command declared at registration, fall back to the local _COMMAND_USAGE
    map. Returns '' when nothing is known."""
    if command_registry is not None:
        reg = command_registry.get(command)
        usage = getattr(reg, "usage", None) if reg is not None else None
        if usage:
            return str(usage).strip()
    return _COMMAND_USAGE.get(command, "")


def _workflow_store(runner) -> WorkflowStore:
    """Derive a WorkflowStore path from the runner's tools directory."""
    return WorkflowStore(Path(runner.tools_dir).parent / "workflow_store")


def build_workflow_handler(
    settings, runner, *, workflow_editor=None, command_registry=None
) -> Callable[[str, str], object]:
    """Return a ``handler(remainder, chat_id)`` for the ``/workflow`` command.

    ``runner`` must implement the ``ToolCallExecutor`` protocol
    (i.e. have ``run_tool_step`` and ``tools_dir``) — in production this is a
    ``DynamicToolRunner``. ``settings`` is used to build the ``/saynow``
    dispatcher and, if available, the LLM client for ``llm_transform`` steps.
    Pass ``workflow_editor`` to enable the ``new`` and ``edit`` subcommands.
    Pass ``command_registry`` (the full RegisteredCommand dict) to wire every
    allowlisted slash command into workflow execution without needing to build
    each handler individually.
    """
    from .voice_command import build_saynow_handler as _build_saynow

    _saynow_raw = _build_saynow(settings)
    try:
        from .music_command import build_music_handler as _build_music
        _music_raw = _build_music(settings)
    except Exception:
        _music_raw = None
    _catalog = getattr(runner, "catalog", None)

    def handler(remainder: str, chat_id: str) -> object:
        parts = (remainder or "").strip().split(maxsplit=1)
        subcmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        store = _workflow_store(runner)

        if subcmd == "new":
            if workflow_editor is None:
                return "Workflow 編輯器未啟用"
            text, markup = workflow_editor.start_new(chat_id)
            return text, markup or None
        if subcmd == "cancel":
            if workflow_editor is None:
                return "Workflow 編輯器未啟用"
            return workflow_editor.cancel_session(chat_id)
        if subcmd == "edit":
            if workflow_editor is None:
                return "Workflow 編輯器未啟用"
            if not arg:
                return "用法：/workflow edit <id>"
            text, markup = workflow_editor.start_edit(chat_id, arg)
            return text, markup or None
        if subcmd == "list":
            return _cmd_list(store)
        if subcmd == "show":
            return _cmd_show(arg, store)
        if subcmd == "delete":
            return _cmd_delete(arg, store)
        if subcmd == "create":
            _client, _fallback, _warning, _fb_warning = _resolve_draft_client(settings, runner)
            return _cmd_create(
                arg, store, chat_id,
                llm_client=_client,
                fallback_client=_fallback,
                catalog=_catalog,
                editor=workflow_editor,
                client_warning=_warning,
                fallback_warning=_fb_warning,
                command_registry=command_registry,
            )
        if subcmd == "run":
            return _cmd_run(arg, chat_id, store, runner, _saynow_raw, settings,
                            music_raw=_music_raw, command_registry=command_registry)
        if subcmd == "traces":
            return _cmd_traces(arg, store)
        return _help()

    return handler


# ── Subcommand implementations ────────────────────────────────────────────────

def _cmd_list(store: WorkflowStore) -> str | tuple:
    workflows = store.list()
    if not workflows:
        return "尚無已儲存的 workflow。\n用 /workflow create <JSON> 新增一個。"
    lines = [f"• {wf.id}：{wf.goal}（{len(wf.steps)} 步驟）" for wf in workflows]
    text = "📋 Workflows\n" + "\n".join(lines)
    rows = []
    for wf in workflows:
        rows.append([
            {"text": f"▶️ 執行 {wf.id}", "callback_data": f"wf:run:{wf.id}"},
            {"text": f"📅 排程執行 {wf.id}", "callback_data": f"wf:schedule:{wf.id}"},
        ])
        rows.append([
            {"text": f"🗑 刪除 {wf.id}", "callback_data": f"wf:delete:{wf.id}"},
        ])
    return text, {"inline_keyboard": rows}


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


def _cmd_create(
    arg: str,
    store: WorkflowStore,
    chat_id: str = "",
    *,
    llm_client=None,
    fallback_client=None,
    catalog=None,
    editor=None,
    client_warning: str | None = None,
    fallback_warning: str | None = None,
    command_registry=None,
):
    """Create a workflow.

    Two modes, auto-detected from ``arg``:
      • JSON (arg starts with ``{``) — power-user path, saved directly.
      • Natural language — an LLM drafts the whole workflow, which then lands in
        the editable card (edit / add / delete / reorder / save) so the user
        never has to author steps field-by-field.
    """
    if not arg:
        return (
            "用法：\n"
            "  /workflow create <一句話描述>   — AI 生成可編輯草稿\n"
            "  例：/workflow create 每天早上查東京天氣，用日文女僕口吻說早安，然後念出來\n"
            "  /workflow create <JSON>          — 直接用 JSON 定義（進階）"
        )

    stripped = arg.strip()
    if stripped.startswith("{"):
        return _cmd_create_json(stripped, store, command_registry=command_registry)

    # Natural-language mode → LLM draft → editable card.
    if editor is None or llm_client is None:
        return (
            "自然語言生成需要卡片編輯器與 LLM（目前未啟用）。\n"
            "請改用 /workflow create <JSON>，或 /workflow new 手動建立。"
        )
    wf, err, used_fallback = _generate_workflow_from_nl(
        stripped, llm_client, catalog,
        command_registry=command_registry, fallback_client=fallback_client,
    )
    if wf is None:
        return f"❌ 無法生成草稿：{err}\n可改用 /workflow new 手動建立。"
    text, markup = editor.start_from_draft(chat_id, wf)
    if client_warning:
        text = client_warning + text
    elif used_fallback and fallback_warning:
        text = fallback_warning + text
    return text, markup


def _cmd_create_json(arg: str, store: WorkflowStore, *, command_registry=None) -> str:
    try:
        data = json.loads(arg)
    except json.JSONDecodeError as exc:
        return f"JSON 格式錯誤：{exc}"
    try:
        wf = Workflow.from_dict(data)
    except (KeyError, TypeError) as exc:
        return f"工作流結構錯誤：{exc}"
    known = frozenset(command_registry.keys()) if command_registry else None
    errors = wf.validate_references(known_commands=known)
    if errors:
        return "工作流定義有誤：\n" + "\n".join(errors)
    store.save(wf)
    return f"✅ workflow '{wf.id}' 已儲存（{len(wf.steps)} 步驟）"


def _resolve_draft_client(settings, runner) -> tuple[object, object, str | None, str | None]:
    """Pick the LLM client(s) for natural-language workflow drafting.

    Drafting a whole workflow from one sentence is abstract reasoning, so we
    prefer the cloud big-pickle model and fall back to the runner's local Ollama
    client. The fallback is never silent — a warning string is returned so the
    user is told it happened.

    The cloud big-pickle endpoint (free, no-auth) is flaky: its HTTP probe can
    pass while the heavier generation request gets dropped mid-flight
    (``RemoteDisconnected``). So we hand back BOTH a primary and a request-time
    fallback, and let the caller retry locally when the cloud request itself
    fails — not only when the probe fails.

    Returns ``(primary, fallback, primary_warning, fallback_warning)``:
      - ``primary_warning`` is prepended unconditionally (set when we already had
        to downgrade to local at probe time).
      - ``fallback_warning`` is prepended only if the fallback is actually used."""
    local = getattr(runner, "client", None)
    reason = ""
    try:
        from .dynamic_tools import OpenCodeTextClient, probe_opencode

        base_url = (
            getattr(settings, "openclaw_opencode_base_url", None)
            or "https://opencode.ai/zen/v1"
        ).strip()
        raw_model = (getattr(settings, "openclaw_opencode_model", None) or "big-pickle").strip()
        model = raw_model.split("/")[-1] if "/" in raw_model else raw_model
        if probe_opencode(base_url, model=model, timeout=10.0):
            cloud = OpenCodeTextClient(
                base_url=base_url,
                model=model,
                api_key=getattr(settings, "openclaw_opencode_api_key", None),
                timeout_seconds=180,
            )
            fb_warning = None
            if local is not None:
                fb_warning = (
                    "⚠️ 雲端模型（big-pickle）連線中斷，已改用本地模型"
                    f"（{type(local).__name__}）生成草稿，品質可能較低。\n\n"
                )
            return cloud, local, None, fb_warning
        reason = "雲端端點 HTTP 探測失敗"
    except Exception as exc:  # noqa: BLE001 — any cloud-setup failure → local fallback
        logger.warning("workflow_command: cloud draft client setup failed, using local",
                       exc_info=True)
        reason = f"雲端模型設定失敗（{exc}）"

    if local is None:
        return None, None, None, None
    local_label = type(local).__name__
    logger.warning("workflow_command: cloud draft client unavailable (%s); using local %s",
                   reason, local_label)
    warning = (
        f"⚠️ 雲端模型（big-pickle）目前無法使用（{reason}），"
        f"已改用本地模型（{local_label}）生成草稿，品質可能較低。\n\n"
    )
    return local, None, warning, None


def _generate_workflow_from_nl(
    description: str, llm_client, catalog, *,
    command_registry=None, fallback_client=None,
):
    """Ask the LLM to draft a Workflow from a one-line description.

    Returns ``(Workflow, None, used_fallback)`` on success or
    ``(None, error_message, used_fallback)`` on failure. Tool steps are grounded
    on the live generated-tool catalog so the draft references real slugs.

    Tries ``llm_client`` first; if its request itself fails (the cloud endpoint
    drops the connection — ``RemoteDisconnected`` — even though the earlier probe
    passed), retries once with ``fallback_client``. ``used_fallback`` tells the
    caller whether to surface the local-fallback warning."""
    prompt = _build_nl_workflow_prompt(description, catalog, command_registry=command_registry)
    clients: list[tuple[object, bool]] = [(llm_client, False)]
    if fallback_client is not None and fallback_client is not llm_client:
        clients.append((fallback_client, True))

    last_err: object = "無可用的 LLM client"
    for client, is_fallback in clients:
        if client is None:
            continue
        try:
            raw = client.generate(prompt, temperature=0.2)
        except Exception as exc:  # noqa: BLE001 — transport failure → try next client
            logger.warning("workflow_command: draft via %s failed: %s",
                           type(client).__name__, exc)
            last_err = exc
            continue
        data = _extract_json_object(raw)
        if data is None:
            last_err = "LLM 未回傳有效的 JSON"
            continue
        # Backfill required top-level keys so a slightly-incomplete draft still
        # opens in the editor (the user can fix it there) rather than hard-failing.
        if not data.get("id"):
            data["id"] = "wf-draft"
        if not data.get("goal"):
            data["goal"] = description
        try:
            wf = Workflow.from_dict(data)
        except (KeyError, TypeError) as exc:
            return None, f"草稿結構錯誤：{exc}", is_fallback
        return wf, None, is_fallback
    return None, f"LLM 生成失敗：{last_err}", False


def _build_nl_workflow_prompt(description: str, catalog, *, command_registry=None) -> str:
    tool_lines = []
    if catalog is not None:
        try:
            for entry in catalog.entries()[:40]:
                desc = (entry.description or "").strip().replace("\n", " ")[:80]
                tool_lines.append(f"- {entry.slug}: {desc}")
        except Exception:  # noqa: BLE001 — catalog is best-effort grounding
            tool_lines = []
    tool_block = "\n".join(tool_lines) if tool_lines else "（目前沒有已生成的工具可參考）"

    # Render each allowlisted command WITH its usage so the LLM knows the real
    # argument shape (e.g. /music playbest, /ir send ceiling_light power) and fills
    # `literal` correctly instead of inventing commands or fabricating
    # llm_transform steps to produce fixed parameters.
    if command_registry is not None:
        allowed_cmds = sorted(c for c in command_registry if is_command_sink_allowed(c))
    else:
        allowed_cmds = sorted(_COMMAND_USAGE)
    cmd_lines = []
    for c in allowed_cmds:
        usage = _command_usage(c, command_registry)
        cmd_lines.append(f"- {c}：{usage}" if usage else f"- {c}")
    command_block = "\n".join(cmd_lines) if cmd_lines else "（目前沒有可用的指令）"

    return (
        "你是工作流草稿生成器。把使用者的一句話需求轉成結構化的 workflow JSON。\n\n"
        "步驟種類（kind）：\n"
        "- tool_call：呼叫一個已生成的工具。欄位：tool（slug）、args（物件）、output（變數名）。\n"
        "- llm_transform：用 LLM 把輸入變數轉換成文字。欄位：inputs（變數名陣列）、"
        "instructions（指示）、output（變數名）。\n"
        "- command_sink：呼叫一個 slash 指令。欄位：command、output（變數名），參數二選一：\n"
        "    • literal（固定字串參數）：當參數是固定的、不依賴前面步驟時用這個，直接填指令後面要帶的字串。\n"
        "      例：開最愛音樂清單 → {\"kind\":\"command_sink\",\"command\":\"/music\",\"literal\":\"playbest\",\"output\":\"r1\"}\n"
        "      例：切換天花板燈電源 → {\"kind\":\"command_sink\",\"command\":\"/ir\",\"literal\":\"send ceiling_light power\",\"output\":\"r2\"}\n"
        "    • input（變數名）：只有當參數需要引用前面步驟產生的 output 變數時才用。\n"
        f"  command 只能是下列已登記的指令（請依其用法填 literal）：\n{command_block}\n\n"
        "可用的工具（tool_call 只能使用下列已存在的 slug；若沒有合適的，改用 llm_transform 或 command_sink，不可自行編造 slug）：\n"
        f"{tool_block}\n\n"
        "規則：\n"
        "1. 每個步驟都要有唯一的 output 變數名（英文小寫，如 weather、greeting）。\n"
        "2. 參數固定時一律用 command_sink 的 literal 直接填，**不要**為了產生固定參數而多插一個 llm_transform 步驟。\n"
        "3. 後面步驟的 inputs／input 只能引用前面步驟產生的 output 變數。\n"
        "4. command 只能用上面列出的指令，不可自行編造（如 /musiclistbest 不存在）。\n"
        "5. id 用 kebab-case，並以 wf- 開頭（如 wf-morning-greeting）。\n"
        "6. 只輸出 JSON，不要任何說明文字或 markdown 圍欄。\n\n"
        "輸出格式：\n"
        '{"id":"wf-...","goal":"...","steps":[{"id":"s1","kind":"...","...":"..."}]}\n\n'
        f"使用者需求：{description}\n\n"
        "JSON："
    )


def _extract_json_object(text: str) -> dict | None:
    """Pull the first top-level JSON object out of an LLM response.

    Tolerates ```json fences and surrounding prose."""
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _cmd_run(
    workflow_id: str,
    chat_id: str,
    store: WorkflowStore,
    executor,
    saynow_raw,        # raw handler(text, chat_id) — used by tests / as fallback
    settings,
    music_raw=None,    # optional music handler — used when no command_registry
    command_registry=None,  # full RegisteredCommand dict; primary source in production
) -> str:
    if not workflow_id:
        return "用法：/workflow run <id>"
    wf = store.get(workflow_id)
    if wf is None:
        return f"找不到 workflow '{workflow_id}'"

    # Build command dispatchers bound to the current chat_id.
    # If a full command registry is available, wire every allowlisted command
    # that has a registered handler.  Explicit saynow_raw / music_raw are then
    # used as fallbacks for tests and headless environments.
    dispatcher: dict = {}

    if command_registry is not None:
        for cmd, reg in command_registry.items():
            if not is_command_sink_allowed(cmd):
                continue
            raw = reg.handler
            def _make_wrapper(h):
                def _wrapper(text: str) -> str:
                    result = h(text, chat_id)
                    return str(result[0] if isinstance(result, tuple) else result)
                return _wrapper
            dispatcher[cmd] = _make_wrapper(raw)

    # Always honour explicitly supplied handlers (tests, CI, or overrides).
    if saynow_raw is not None and "/saynow" not in dispatcher:
        def _saynow(text: str) -> str:
            return str(saynow_raw(text, chat_id))
        dispatcher["/saynow"] = _saynow

    if music_raw is not None and "/music" not in dispatcher:
        def _music(text: str) -> str:
            result = music_raw(text, chat_id)
            return str(result[0] if isinstance(result, tuple) else result)
        dispatcher["/music"] = _music

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


def _cmd_traces(workflow_id: str, store: WorkflowStore, limit: int = 5) -> str:
    if not workflow_id:
        return "用法：/workflow traces <id>"
    traces = store.list_traces(workflow_id)
    if not traces:
        return f"workflow '{workflow_id}' 尚無執行記錄。"
    recent = traces[-limit:][::-1]  # most recent first, up to limit
    lines = [f"📊 {workflow_id} — {len(traces)} 回執行記錄（顯示最近 {len(recent)} 回）"]
    for i, trace in enumerate(recent, 1):
        status = "✅" if trace.ok else "❌"
        summary = (trace.final_result or "（無輸出）")[:80]
        if len(trace.final_result or "") > 80:
            summary += "…"
        # Summarise failed steps
        failed = [st for st in trace.steps if st.status == "failed"]
        if failed:
            fail_info = f" | 失敗步驟：{failed[0].step_id}（{failed[0].error or ''}）"[:60]
        else:
            fail_info = ""
        lines.append(f"[{i}] {status} {summary}{fail_info}")
    return "\n".join(lines)


def _help() -> str:
    return (
        "用法：\n"
        "  /workflow new               — 開啟卡片編輯器新建 workflow\n"
        "  /workflow cancel            — 放棄目前編輯（卡住時用這個脫離）\n"
        "  /workflow edit <id>         — 開啟卡片編輯器編輯 workflow\n"
        "  /workflow list              — 列出所有 workflow\n"
        "  /workflow show <id>         — 顯示 workflow 的步驟\n"
        "  /workflow run <id>          — 執行 workflow\n"
        "  /workflow traces <id>       — 顯示執行記錄\n"
        "  /workflow delete <id>       — 刪除 workflow\n"
        "  /workflow create <一句話>   — AI 生成可編輯草稿\n"
        "  /workflow create <JSON>     — 從 JSON 建立 workflow（進階）"
    )
