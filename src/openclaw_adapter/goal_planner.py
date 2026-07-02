"""Shared goal/workflow drafting helpers for chat-goal loop work (#54).

Phase 2 keeps execution wiring off: this module only turns a natural-language
goal into a draft workflow JSON, reusing the same prompt and fallback rules as
``/workflow create`` so future chat-goal planning does not invent a second
drafting path.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable

from .task_workspace import Workflow, WorkflowTrace, is_command_sink_allowed

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GoalPlanner:
    catalog: object | None
    llm_client: object
    fallback_client: object | None = None
    command_registry: object | None = None
    allowed_commands: list[str] | None = None
    command_usage_resolver: Callable[[str, object | None], str] | None = None
    progress: Callable[[str], None] | None = None
    # Shared by one goal-loop run across draft + every replan call so
    # successive calls start the cloud-pool fail-over chain at a different
    # provider instead of always retrying provider[0] first. Only rotates when
    # ``llm_client`` is a list (the cloud-pool fallback chain); a single client
    # is returned as-is.
    pool_rotation: object | None = None

    def _client_for_call(self):
        if self.pool_rotation is not None and isinstance(self.llm_client, (list, tuple)):
            return self.pool_rotation.rotate(list(self.llm_client))
        return self.llm_client

    def draft(self, goal: str):
        return generate_workflow_from_goal(
            goal,
            self._client_for_call(),
            self.catalog,
            command_registry=self.command_registry,
            allowed_commands=self.allowed_commands,
            command_usage_resolver=self.command_usage_resolver,
            fallback_client=self.fallback_client,
            progress=self.progress,
        )

    def replan(self, goal: str, previous_workflow: Workflow, trace: WorkflowTrace):
        return replan_workflow_from_trace(
            goal,
            previous_workflow,
            trace,
            self._client_for_call(),
            self.catalog,
            command_registry=self.command_registry,
            allowed_commands=self.allowed_commands,
            command_usage_resolver=self.command_usage_resolver,
            fallback_client=self.fallback_client,
            progress=self.progress,
        )


def resolve_goal_draft_client(settings, runner) -> tuple[object, object, str | None, str | None]:
    """Pick the LLM client(s) for natural-language goal/workflow drafting."""
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("goal_planner: cloud draft client setup failed, using local", exc_info=True)
        reason = f"雲端模型設定失敗（{exc}）"

    if local is None:
        return None, None, None, None
    local_label = type(local).__name__
    logger.warning("goal_planner: cloud draft client unavailable (%s); using local %s",
                   reason, local_label)
    warning = (
        f"⚠️ 雲端模型（big-pickle）目前無法使用（{reason}），"
        f"已改用本地模型（{local_label}）生成草稿，品質可能較低。\n\n"
    )
    return local, None, warning, None


def _client_label(client) -> str:
    for attr in ("label", "model", "model_name"):
        value = getattr(client, attr, None)
        if isinstance(value, str) and value:
            return value
    return type(client).__name__


def generate_workflow_from_goal(
    description: str,
    llm_client,
    catalog,
    *,
    command_registry=None,
    allowed_commands=None,
    command_usage_resolver: Callable[[str, object | None], str] | None = None,
    fallback_client=None,
    prompt_override: str | None = None,
    strict: bool = True,
    progress: Callable[[str], None] | None = None,
):
    """Ask the LLM to draft a Workflow from a one-line goal description."""

    def _emit(line: str) -> None:
        if progress is None:
            return
        try:
            progress(line)
        except Exception:  # noqa: BLE001
            logger.exception("goal_planner: progress callback failed")

    prompt = prompt_override or build_goal_workflow_prompt(
        description,
        catalog,
        command_registry=command_registry,
        allowed_commands=allowed_commands,
        command_usage_resolver=command_usage_resolver,
    )
    raw_clients = list(llm_client) if isinstance(llm_client, (list, tuple)) else [llm_client]
    clients: list[tuple[object, bool]] = [
        (client, index > 0) for index, client in enumerate(raw_clients)
    ]
    if fallback_client is not None and fallback_client is not llm_client:
        clients.append((fallback_client, True))

    last_err: object = "無可用的 LLM client"
    last_workflow_error: str | None = None
    for client, is_fallback in clients:
        if client is None:
            continue
        label = _client_label(client)
        last_parseable_wf: Workflow | None = None
        last_parseable_errors: list[str] = []
        _emit(f"規劃中：請 {label} 起草…")
        try:
            raw = client.generate(prompt, temperature=0.2)
        except Exception as exc:  # noqa: BLE001
            logger.warning("goal_planner: draft via %s failed: %s",
                           type(client).__name__, exc)
            _emit(f"{label} 規劃失敗（{exc}），改用下一個模型")
            last_err = exc
            continue
        wf, errors, parse_err = _workflow_from_llm_output(
            raw,
            description,
            catalog,
            command_registry=command_registry,
        )
        if wf is not None:
            last_parseable_wf = wf
            last_parseable_errors = list(errors)
        if wf is not None and not errors:
            return wf, None, is_fallback
        if parse_err is not None:
            last_err = parse_err
            continue

        repair_prompt = _build_goal_workflow_repair_prompt(
            description,
            original_prompt=prompt,
            raw_output=raw,
            errors=errors,
        )
        _emit(f"{label} 草稿有結構問題，要求它修正中…")
        try:
            repaired_raw = client.generate(repair_prompt, temperature=0.2)
        except Exception as exc:  # noqa: BLE001
            logger.warning("goal_planner: repair via %s failed: %s",
                           type(client).__name__, exc)
            _emit(f"{label} 修正失敗（{exc}），改用下一個模型")
            last_err = exc
            continue
        repaired_wf, repaired_errors, repaired_parse_err = _workflow_from_llm_output(
            repaired_raw,
            description,
            catalog,
            command_registry=command_registry,
        )
        if repaired_wf is not None:
            last_parseable_wf = repaired_wf
            last_parseable_errors = list(repaired_errors)
        if repaired_wf is not None and not repaired_errors:
            return repaired_wf, None, is_fallback
        if not strict and last_parseable_wf is not None:
            warning_errors = (
                repaired_errors
                if repaired_wf is not None
                else last_parseable_errors
            )
            if warning_errors:
                return (
                    last_parseable_wf,
                    "工作流草稿驗證失敗：\n" + "\n".join(warning_errors),
                    is_fallback,
            )
            return last_parseable_wf, None, is_fallback
        if repaired_parse_err is not None:
            last_workflow_error = f"工作流草稿修正失敗：{repaired_parse_err}"
            continue
        last_workflow_error = "工作流草稿驗證失敗：\n" + "\n".join(repaired_errors)
        continue
    if last_workflow_error is not None:
        return None, last_workflow_error, False
    return None, f"LLM 生成失敗：{last_err}", False


def replan_workflow_from_trace(
    description: str,
    previous_workflow: Workflow,
    trace: WorkflowTrace,
    llm_client,
    catalog,
    *,
    command_registry=None,
    allowed_commands=None,
    command_usage_resolver: Callable[[str, object | None], str] | None = None,
    fallback_client=None,
    progress: Callable[[str], None] | None = None,
):
    prompt = build_goal_replan_prompt(
        description,
        previous_workflow,
        trace,
        catalog,
        command_registry=command_registry,
        allowed_commands=allowed_commands,
        command_usage_resolver=command_usage_resolver,
    )
    return generate_workflow_from_goal(
        description,
        llm_client,
        catalog,
        command_registry=command_registry,
        allowed_commands=allowed_commands,
        command_usage_resolver=command_usage_resolver,
        fallback_client=fallback_client,
        prompt_override=prompt,
        strict=True,
        progress=progress,
    )


def build_goal_workflow_prompt(
    description: str,
    catalog,
    *,
    command_registry=None,
    allowed_commands=None,
    command_usage_resolver: Callable[[str, object | None], str] | None = None,
) -> str:
    tool_lines = []
    if catalog is not None:
        try:
            for entry in catalog.entries()[:40]:
                desc = (entry.description or "").strip().replace("\n", " ")[:80]
                tool_lines.append(f"- {entry.slug}: {desc}")
        except Exception:  # noqa: BLE001
            tool_lines = []
    tool_block = "\n".join(tool_lines) if tool_lines else "（目前沒有已生成的工具可參考）"

    if command_registry is not None:
        allowed_cmds = sorted(c for c in command_registry if is_command_sink_allowed(c))
    else:
        allowed_cmds = list(allowed_commands or [])
    cmd_lines = []
    for command in allowed_cmds:
        usage = _command_usage(command, command_registry, command_usage_resolver)
        cmd_lines.append(f"- {command}：{usage}" if usage else f"- {command}")
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
        "5. 若需求需要熱門、最新、排名、查證或其他外部事實，先用已登記的搜尋／讀取類指令取得根據，再做後續動作。\n"
        "6. 若最後動作依賴本機資源（例如本機音樂庫、已登記裝置、既有清單），先用已登記的列出／查詢類指令取得候選，再比對後執行。\n"
        "7. 需要從多個前置結果中選擇、比對、萃取參數時，用 llm_transform；不要把未查證的猜測直接塞進最終指令。\n"
        "8. id 用 kebab-case，並以 wf- 開頭（如 wf-morning-greeting）。\n"
        "9. 只輸出 JSON，不要任何說明文字或 markdown 圍欄。\n\n"
        "輸出格式：\n"
        '{"id":"wf-...","goal":"...","steps":[{"id":"s1","kind":"...","...":"..."}]}\n\n'
        f"使用者需求：{description}\n\n"
        "JSON："
    )


def build_goal_replan_prompt(
    description: str,
    previous_workflow: Workflow,
    trace: WorkflowTrace,
    catalog,
    *,
    command_registry=None,
    allowed_commands=None,
    command_usage_resolver: Callable[[str, object | None], str] | None = None,
) -> str:
    base = build_goal_workflow_prompt(
        description,
        catalog,
        command_registry=command_registry,
        allowed_commands=allowed_commands,
        command_usage_resolver=command_usage_resolver,
    )
    previous_json = json.dumps(previous_workflow.to_dict(), ensure_ascii=False)
    trace_json = json.dumps(trace.to_dict(), ensure_ascii=False)
    return (
        base
        + "\n\n你現在是在修正一份已執行失敗的工作流。"
        + "\n規則補充："
        + "\n1. 保留已成功步驟的成果，不要重複同一個失敗做法。"
        + "\n2. 優先修改失敗步驟及其之後需要調整的步驟。"
        + "\n3. 若原目標本身不可行，要輸出最接近且可執行的 workflow，而不是空白。"
        + "\n4. 若上一版的執行結果是在反問、要求澄清、或列出多個候選，"
        + "請由你依目標自行決定（多個候選都符合時任選其一或全部處理），"
        + "不要規劃出需要使用者回覆才能繼續的流程。"
        + f"\n\n上一版 workflow:\n{previous_json}"
        + f"\n\n上一版執行 trace:\n{trace_json}"
        + "\n\n請只輸出修正版 JSON。"
    )


def extract_json_object(text: str) -> dict | None:
    """Pull the first top-level JSON object out of an LLM response."""
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


def _workflow_from_llm_output(
    raw: str,
    description: str,
    catalog,
    *,
    command_registry=None,
) -> tuple[Workflow | None, list[str], str | None]:
    data = extract_json_object(raw)
    if data is None:
        return None, [], "LLM 未回傳有效的 JSON"
    if not data.get("id"):
        data["id"] = "wf-draft"
    if not data.get("goal"):
        data["goal"] = description
    try:
        wf = Workflow.from_dict(data)
    except (KeyError, TypeError) as exc:
        return None, [], f"草稿結構錯誤：{exc}"
    return wf, _validate_goal_workflow(
        wf,
        catalog,
        command_registry=command_registry,
    ), None


def _validate_goal_workflow(
    workflow: Workflow,
    catalog,
    *,
    command_registry=None,
) -> list[str]:
    known_commands = None
    if command_registry is not None:
        known_commands = frozenset(command_registry.keys())
    errors = workflow.validate_references(known_commands=known_commands)
    known_tools = _catalog_tool_slugs(catalog)
    if known_tools is not None:
        for step in workflow.steps:
            if step.kind == "tool_call" and step.tool and step.tool not in known_tools:
                errors.append(
                    f"Step {step.id}: tool '{step.tool}' does not exist in the generated-tool catalog"
                )
    return errors


def _catalog_tool_slugs(catalog) -> frozenset[str] | None:
    if catalog is None:
        return None
    try:
        entries = list(catalog.entries())
    except Exception:  # noqa: BLE001
        logger.exception("goal_planner: failed to read generated-tool catalog")
        return None
    slugs = {
        str(getattr(entry, "slug", "")).strip()
        for entry in entries
        if str(getattr(entry, "slug", "")).strip()
    }
    return frozenset(slugs)


def _build_goal_workflow_repair_prompt(
    description: str,
    *,
    original_prompt: str,
    raw_output: str,
    errors: list[str],
) -> str:
    return (
        original_prompt
        + "\n\n你剛剛輸出的草稿沒有通過硬性驗證，請直接修正成一份新的 workflow JSON。\n"
        + "驗證錯誤：\n"
        + "\n".join(f"- {err}" for err in errors)
        + "\n\n你上一次輸出的內容：\n"
        + raw_output.strip()
        + "\n\n請只輸出修正後的 JSON，不要解說。"
    )


def _command_usage(
    command: str,
    command_registry=None,
    command_usage_resolver: Callable[[str, object | None], str] | None = None,
) -> str:
    if command_usage_resolver is not None:
        return str(command_usage_resolver(command, command_registry) or "").strip()
    if command_registry is not None:
        reg = command_registry.get(command)
        usage = getattr(reg, "usage", None) if reg is not None else None
        if usage:
            return str(usage).strip()
    return ""
