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
  /workflow rename <id>       — rename a stored workflow (prompts for new name)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from .goal_planner import (
    build_goal_workflow_prompt as _shared_build_goal_workflow_prompt,
    extract_json_object as _shared_extract_json_object,
    generate_workflow_from_goal as _shared_generate_workflow_from_goal,
    resolve_goal_draft_client as _shared_resolve_goal_draft_client,
)
from .task_workspace import (
    is_command_sink_allowed,
    Workflow,
    WorkflowRunner,
    WorkflowStore,
)

logger = logging.getLogger(__name__)


# Shared command metadata for Telegram registry wiring, Web Chat tool routing,
# and workflow drafting. Keep semantics in one table so the three surfaces
# cannot drift.
_COMMAND_METADATA: dict[str, dict[str, str]] = {
    "/quiz": {
        "usage": "JLPT 日文測驗；常用：random、wrong、stats、vocab、grammar、review。",
    },
    "/quizlikesong": {
        "usage": "收藏 YouTube 歌曲並建立題庫；參數＝YouTube URL。",
    },
    "/voice": {
        "usage": "語音合成預覽；參數＝要合成的日文文字。",
    },
    "/generateaudio": {
        "usage": "產生音訊檔案；把文字轉成語音 WAV 並傳回目前 Telegram 對話（參數＝要轉成語音的文字，"
        "通常用 input 變數帶入）。與 /saynow 是同一需求的兩種互斥實作方式，只能擇一："
        "使用者要的是「傳一個音檔給我」時才用這個；若只是想「念出來/說出來/播放語音」，"
        "改用 /saynow。此指令依賴目前的 Telegram 對話，排程或非對話情境下會失敗。",
    },
    "/saynow": {
        "usage": "立即於 Mac mini 喇叭念出文字（參數＝要念的文字，通常用 input 變數帶入）。"
        "與 /generateaudio 是同一需求的兩種互斥實作方式，只能擇一："
        "使用者要「念出來/說出來/播放語音」時用這個；不依賴 Telegram 對話，排程或無對話情境也能執行。",
    },
    "/new": {
        "usage": "動態建立並執行新工具；高風險，不可作為自動 workflow sink。",
    },
    "/backupclaw": {
        "usage": "備份 OpenClaw 資料庫與工具規格；可帶目的資料夾。",
    },
    "/backup": {
        "usage": "同 /backupclaw；備份 OpenClaw 資料。",
    },
    "/clawrecover": {
        "usage": "從備份還原 OpenClaw 資料；高風險，不可作為自動 workflow sink。",
    },
    "/recoverclaw": {
        "usage": "同 /clawrecover；從備份還原 OpenClaw 資料。",
    },
    "/restartall": {
        "usage": "重啟本機 OpenClaw 服務；高風險，不可作為自動 workflow sink。",
    },
    "/stats": {
        "usage": "查看作答／系統統計（無參數）。",
    },
    "/scorecard": {
        "usage": "同 /stats；查看統計（無參數）。",
    },
    "/knowledge": {
        "usage": "查詢知識庫；常用參數：market、coding 或搜尋關鍵字。",
    },
    "/kb": {
        "usage": "同 /knowledge；查詢知識庫。",
    },
    "/source": {
        "usage": "查詢或管理知識來源；參數依 source 子命令。",
    },
    "/lookup": {
        "usage": "查詢卡牌價格；格式同 /price，例如 pokemon | Pikachu ex | 132/106 | SAR | sv08。",
    },
    "/price": {
        "usage": "查詢卡牌價格；參數＝遊戲與卡名／編號／稀有度／系列。",
    },
    "/trend": {
        "usage": "查詢指定遊戲熱門／流動性榜；格式：<game> [數量]，例如 pokemon 5。",
    },
    "/trending": {
        "usage": "同 /trend；格式：<game> [數量]。",
    },
    "/hot": {
        "usage": "同 /trend；格式：<game> [數量]。",
    },
    "/heat": {
        "usage": "同 /trend；格式：<game> [數量]。",
    },
    "/liquidity": {
        "usage": "查詢指定遊戲流動性排名；格式：<game> [數量]。",
    },
    "/snapshot": {
        "usage": "建立賣家／商品信譽快照；參數＝Mercari 商品或店鋪網址。",
    },
    "/proof": {
        "usage": "同 /snapshot；參數＝Mercari 商品或店鋪網址。",
    },
    "/repcheck": {
        "usage": "同 /snapshot；參數＝Mercari 商品或店鋪網址。",
    },
    "/reputation": {
        "usage": "同 /snapshot；參數＝Mercari 商品或店鋪網址。",
    },
    "/search": {
        "usage": (
            "網路搜尋並回傳摘要與來源；參數＝搜尋查詢。"
            "需要熱門、最新、排名或外部事實時先用這個。"
        ),
        "chat_tool_purpose": "當回答需要即時、最新、外部來源或不確定的事實資訊時使用",
        "chat_tool_query_hint": "query 是適合搜尋引擎的完整查詢，不要包含 /search",
        "chat_tool_display_name": "網路搜尋",
    },
    "/fetch": {
        "usage": (
            "讀取指定網頁並針對問題回答；格式：<網址> <問題>。"
            "已有明確來源網址、需要讀頁面內容時使用；一般文章、公告、說明頁優先用這個。"
        ),
    },
    "/read": {
        "usage": "同 /fetch；格式：<網址> <問題>。",
    },
    "/web": {
        "usage": "同 /search；參數＝搜尋查詢。",
    },
    "/research": {
        "usage": (
            "深度商品研究與投資判斷；參數＝商品網址或商品描述。"
            "當使用者問商品能不能買、是否適合投資、估價、行情、流動性、賣家風險，"
            "或提供 Mercari/拍賣/商品頁網址時用這個。"
            "例：/research https://jp.mercari.com/item/m123456789 以投資為考量這個商品能買嗎？"
        ),
        "chat_tool_purpose": (
            "當使用者要評估商品能否購買、是否值得投資、估價、行情、流動性、"
            "賣家風險，或貼出 Mercari/拍賣/商品頁網址時使用"
        ),
        "chat_tool_query_hint": "query 保留商品 URL 或商品描述，並保留使用者的投資／購買判斷問題",
        "chat_tool_display_name": "商品研究",
    },
    "/resaerch": {
        "usage": "同 /research（歷史拼字相容別名）。",
    },
    "/fix": {
        "usage": (
            "benchmark 自我修復迴圈；無參數＝列出可修復的 benchmark，"
            "參數＝benchmark 名稱（如 price_reference_sources）開始修復。"
            "只修 docs/fix_benchmarks/ 內的 benchmark parser，不碰 production 程式碼；"
            "高風險，不可作為自動 workflow sink。"
        ),
    },
    "/vpn": {
        "usage": (
            "NordVPN 出口控制；無參數＝狀態，switch [國家]＝換伺服器（換 IP），"
            "auto on [小時]/auto off＝定期自動輪替，pool 國家,國家＝設定輪替池。"
            "會改變全機網路出口；高風險，不可作為自動 workflow sink。"
        ),
    },
    "/scan": {
        "usage": "圖片辨識命令；需搭配 Telegram 圖片，不適合作為純文字 workflow 步驟。",
    },
    "/image": {
        "usage": "同 /scan；需搭配 Telegram 圖片。",
    },
    "/photo": {
        "usage": "同 /scan；需搭配 Telegram 圖片。",
    },
    "/watch": {
        "usage": "新增 marketplace 追蹤；格式：<關鍵字> on <價格> [markets:mercari,rakuma,yuyutei]。",
    },
    "/watchlist": {
        "usage": "列出 marketplace 追蹤清單（無參數）。",
    },
    "/watches": {
        "usage": "同 /watchlist（無參數）。",
    },
    "/unwatch": {
        "usage": "移除 marketplace 追蹤；參數＝追蹤 ID。",
    },
    "/stopwatch": {
        "usage": "同 /unwatch；參數＝追蹤 ID。",
    },
    "/setprice": {
        "usage": "更新追蹤價格；格式：<追蹤 ID> <新價格>。",
    },
    "/updatewatch": {
        "usage": "同 /setprice；格式：<追蹤 ID> <新價格>。",
    },
    "/ir": {
        "usage": (
            "discover=掃描可用紅外線裝置；devices=列出已註冊裝置；"
            "send <裝置> <按鍵名>=發送紅外線指令，如 `send ceiling_light power`。"
            "send 的裝置與按鍵可直接用使用者說的自然語言名稱"
            "（如 `send 電風扇 on`），指令內部會自動對應到已學習的按鍵，"
            "不需要先查 devices。"
        ),
        "chat_tool_purpose": "當使用者要控制紅外線家電時使用",
        "chat_tool_query_hint": (
            "query 只輸出 /ir 後面的參數，例如 discover、devices 或 "
            "send <使用者說的裝置> <動作>（一步完成，免先查 devices）"
        ),
        "chat_tool_display_name": "紅外線控制",
    },
    "/music": {
        "usage": (
            "playbest=播放最愛清單；random=隨機播放；stop=停止；"
            "pause=暫停；resume=繼續；next/previous=切歌；"
            "louder=調高音量；lower=調低音量；mute=靜音；"
            "<本地歌曲關鍵字>=搜尋並播放本地曲目。"
            "一次只播一首，且呼叫時會停掉正在播放的歌；"
            "要依序連播多首請改用 /musicqueue，不要連續呼叫 /music。"
            "不負責判斷熱門／最新；需要外部判斷時先用 /search，"
            "需要確認本地可播曲目時先用 /musiclistall，再用 llm_transform 比對。"
        ),
        "chat_tool_purpose": "當使用者要控制本機音樂播放時使用",
        "chat_tool_query_hint": (
            "query 只輸出 /music 後面的參數，例如 stop、pause、resume、"
            "next、previous、louder、lower、mute、random、playbest 或歌曲關鍵字"
        ),
        "chat_tool_display_name": "音樂控制",
    },
    "/musicqueue": {
        "usage": (
            "依序連續播放多首本地歌曲，每首播完自動接下一首；"
            "參數＝歌名清單（以「、」或換行分隔）。"
            "要一次播放多首指定歌曲時用這個。"
        ),
        "chat_tool_purpose": (
            "當使用者要依序連續播放多首本地歌曲、且歌曲已可直接列出時使用；"
            "若還需要先查資料或挑選才能決定歌單，屬於多步驟目標，改用 __goal__"
        ),
        "chat_tool_query_hint": (
            "query 只輸出 /musicqueue 後面的參數：以「、」分隔的歌名清單"
        ),
        "chat_tool_display_name": "音樂連播",
    },
    "/musicmute": {"usage": "音樂靜音（無參數）"},
    "/musiclouder": {"usage": "調高音量（無參數）"},
    "/musiclower": {"usage": "調低音量（無參數）"},
    "/musicnowbest": {"usage": "把目前播放的歌曲加入最愛清單（無參數）"},
    "/musiclistall": {
        "usage": (
            "列出全部本地可播曲目清單（不播放，無參數）。"
            "規劃需要從本機音樂庫挑歌時，先用這個取得候選清單。"
        )
    },
    "/musiclistbest": {
        "usage": "列出最愛曲目清單（不播放，無參數）；要『播放』最愛請改用 /music playbest",
    },
    "/bluetooth": {
        "usage": "scan=掃描藍牙裝置；<裝置名>=連線／切換藍牙裝置",
        "chat_tool_purpose": "當使用者要掃描、查看、連線或切換藍牙裝置時使用",
        "chat_tool_query_hint": "query 只輸出 /bluetooth 後面的參數；掃描時輸出 scan，連線時輸出裝置名",
        "chat_tool_display_name": "藍牙控制",
    },
    "/translateja": {
        "usage": "把文字翻成日文（參數＝原文，通常用 input 變數帶入）",
    },
    "/ja": {
        "usage": "同 /translateja；把文字翻成日文。",
    },
    "/jp": {
        "usage": "同 /translateja；把文字翻成日文。",
    },
    "/translatezh": {
        "usage": "把文字翻成繁體中文（參數＝原文，通常用 input 變數帶入）",
    },
    "/zh": {
        "usage": "同 /translatezh；把文字翻成繁體中文。",
    },
    "/snsadd": {
        "usage": "新增 X/Twitter 監控；格式：@帳號、keyword:<關鍵字> 或 trend:<分類>。",
    },
    "/sns_add": {
        "usage": "同 /snsadd；新增 X/Twitter 監控。",
    },
    "/snslist": {
        "usage": "列出 X/Twitter 監控規則（無參數）。",
    },
    "/sns_list": {
        "usage": "同 /snslist；列出 X/Twitter 監控規則。",
    },
    "/snsdelete": {
        "usage": "刪除 X/Twitter 監控規則；參數＝rule_id。",
    },
    "/sns_delete": {
        "usage": "同 /snsdelete；刪除 X/Twitter 監控規則。",
    },
    "/snsbuzz": {
        "usage": "查詢 4chan 收藏品／IP 熱度；參數＝關鍵字。",
    },
    "/sns_buzz": {
        "usage": "同 /snsbuzz；查詢 4chan 收藏品／IP 熱度。",
    },
    "/snsclearfilter": {
        "usage": "清除 SNS 監控規則的關鍵字過濾；參數＝rule_id。",
    },
    "/hunt": {
        "usage": "Opportunity Agent 目標清單與操作；常用：status、remove <id>。",
    },
    "/opportunity": {
        "usage": "同 /hunt；Opportunity Agent 操作入口。",
    },
    "/schedulehome": {
        "usage": "建立居家排程，排程會重新派發既有 slash command；不作為 workflow sink。",
    },
    "/workflow": {
        "usage": "管理 workflow；常用：list、show、run、create、edit、delete。",
    },
    "/visionlook": {
        "usage": "查看圖片並回答相關問題；參數＝要查看的目標（URL或描述）。當使用者需要看商品照片評估品況、或圖片在網頁上需要進一步檢視時使用。",
        "chat_tool_purpose": "當需要實際查看圖片或商品照片來回答問題時使用",
        "chat_tool_query_hint": "query 保留要查看的目標與想確認的重點；若圖片在網頁上，附上網址",
        "chat_tool_display_name": "圖片查看",
    },
}

# Backward-compatible usage view for tests/callers that only care about the
# command argument shape.
_COMMAND_USAGE: dict[str, str] = {
    command: meta["usage"] for command, meta in _COMMAND_METADATA.items() if meta.get("usage")
}


def command_metadata(command: str) -> dict[str, str]:
    return dict(_COMMAND_METADATA.get(command, {}))


def iter_command_metadata() -> tuple[tuple[str, dict[str, str]], ...]:
    return tuple((command, dict(meta)) for command, meta in sorted(_COMMAND_METADATA.items()))


def _command_usage(command: str, command_registry=None) -> str:
    """Resolve a command's usage hint: prefer the RegisteredCommand.usage that
    the command declared at registration, fall back to the local _COMMAND_USAGE
    map. Returns '' when nothing is known."""
    if command_registry is not None:
        reg = command_registry.get(command)
        usage = getattr(reg, "usage", None) if reg is not None else None
        if usage:
            return str(usage).strip()
    return command_metadata(command).get("usage", "")


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
        if subcmd == "rename":
            if workflow_editor is None:
                return "Workflow 編輯器未啟用"
            if not arg:
                return "用法：/workflow rename <id>"
            text, markup = workflow_editor.start_rename(chat_id, arg)
            return text, markup or None
        if subcmd == "renameid":
            if workflow_editor is None:
                return "Workflow 編輯器未啟用"
            if not arg:
                return "用法：/workflow renameid <id>"
            text, markup = workflow_editor.start_renameid(chat_id, arg)
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
            {"text": f"✏️ 改名 {wf.id}", "callback_data": f"wf:rename:{wf.id}"},
            {"text": f"✏️ 改代號 {wf.id}", "callback_data": f"wf:renameid:{wf.id}"},
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
    if err:
        text = (
            "⚠️ 草稿已開啟，但仍有待修正：\n"
            f"{err}\n\n{text}"
        )
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
    return _shared_resolve_goal_draft_client(settings, runner)


def _generate_workflow_from_nl(
    description: str, llm_client, catalog, *,
    command_registry=None, fallback_client=None,
):
    return _shared_generate_workflow_from_goal(
        description,
        llm_client,
        catalog,
        command_registry=command_registry,
        allowed_commands=sorted(_COMMAND_USAGE),
        command_usage_resolver=_command_usage,
        fallback_client=fallback_client,
        strict=False,
    )


def _build_nl_workflow_prompt(description: str, catalog, *, command_registry=None) -> str:
    return _shared_build_goal_workflow_prompt(
        description,
        catalog,
        command_registry=command_registry,
        allowed_commands=sorted(_COMMAND_USAGE),
        command_usage_resolver=_command_usage,
    )


def _extract_json_object(text: str) -> dict | None:
    return _shared_extract_json_object(text)


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
        "  /workflow rename <id>       — 改名稱（顯示用）\n"
        "  /workflow renameid <id>     — 改代號（slug）\n"
        "  /workflow create <一句話>   — AI 生成可編輯草稿\n"
        "  /workflow create <JSON>     — 從 JSON 建立 workflow（進階）"
    )
