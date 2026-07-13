"""aka-specific command / callback / view / item-deleter registry assembly.

Moved out of telegram_bot.py in R2.2 (#75). This module owns the real payload
of the R2 decomposition: the large ``_build_registries`` factory that wires
every aka slash command, callback prefix, list view and item-deleter into the
four dictionaries injected into the generic telegram_core dispatcher, plus the
``ragkeep``/``ragdel`` callback helper it closes over. telegram_bot.py
re-imports both names so ``run_telegram_polling`` and ``command_bridge`` keep
their existing ``from .telegram_bot import _build_registries`` contract.

Behavior is unchanged: command/callback precedence, aliases, ack strings, and
the (command, callback, view, item_deleter) return tuple are identical to the
old in-telegram_bot implementation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from assistant_runtime import AssistantSettings

from price_monitor_bot.bot import (
    LookupRenderer,
    ReputationRenderer,
    TelegramCommandProcessor as _BaseTelegramCommandProcessor,
)
from telegram_core.contracts import RegisteredCommand
from telegram_core.list_view import LIST_VIEW_MODE_READ as _MB_READ

from .backup_command import build_backup_handler, build_recover_handler
from .bluetooth_command import build_bluetooth_callback_handler, build_bluetooth_handler
from .fix_command import FixPendingApplyCache, build_fix_callback_handler, build_fix_handler
from .home_schedule import get_home_schedule_store, make_run_slash_command
from .home_schedule_command import (
    build_schedulehome_callback_handler,
    build_schedulehome_handler,
)
from .ir_command import build_ir_callback_handler, build_ir_handler
from .item_condition import build_item_condition_assessor
from .knowledge_command import (
    build_knowledge_coding_view_fn,
    build_knowledge_handler,
    build_knowledge_item_deleters,
    build_knowledge_market_view_fn,
)
from .local_text import build_translate_handler, default_web_fetch_renderer
from .music_browser import build_music_callback_handler, build_musiclistall_handler
from .music_command import (
    build_music_handler,
    build_musicnowbest_handler,
    build_musicqueue_handler,
)
from .music_favorites import (
    MUSIC_BEST_LIST_KIND,
    FavoritesStore,
    build_music_best_item_deleter,
    build_music_best_view_fn,
)
from .music_volume import louder_music, lower_music, mute_music
from .opportunity_command import (
    build_hunt_callback_handler,
    build_hunt_handler,
    build_huntlist_item_deleter,
    build_huntlist_view_fn,
)
from .opportunity_scorecard import build_scorecard_handler
from .photo_render import (
    _IMAGE_TRANSLATE_ORIGINAL_CACHE,
    _build_image_translate_callback_handler,
    default_board_loader,
    default_lookup_renderer,
)
from .quiz_command import build_quiz_callback_handler, build_quiz_handler
from .rag_daily_digest import handle_ragdel_callback, handle_ragkeep_callback
from .reputation_render import default_reputation_renderer
from .research_command import (
    MercariItemAdapter,
    ResearchNotifier,
    build_ollama_entity_recognizer,
    build_ollama_sellable_unit_gate,
    build_research_handler,
    build_research_item_fetch_html,
)
from .research_telegram import (
    _ResearchReplyCache,
    _build_research_appreciation_enricher,
    _build_research_callback_handler,
    _build_research_ip_heat_lookup,
    _build_research_reply_formatter,
    _build_research_seller_snapshot_followup,
    _build_research_seller_snapshot_lookup,
    _build_yuyutei_code_resolver,
    _run_research_worker_call,
    default_web_research_renderer,
)
from .service_restart import build_restart_all_handler
from .sns_commands import (
    build_sns_add_handler,
    build_sns_buzz_handler,
    build_sns_clear_filter_handler,
    build_sns_delete_handler,
    build_sns_rule_deleter,
    build_snsaddok_callback_handler,
    build_snsdel_callback_handler,
    build_snsfb_callback_handler,
    build_snslist_handler,
    build_snslist_view_fn,
)
from .source_command import build_source_handler
from .vpn_command import VpnConfigStore, VpnRotationScheduler, build_vpn_handler
from .voice_command import (
    build_generateaudio_handler,
    build_saynow_handler,
    build_voice_callback_handler,
    build_voice_handler,
)
from .web_search import web_search
from .workflow_command import build_workflow_handler, command_metadata


def _build_registries(
    settings: AssistantSettings,
    dynamic_tool_runner,
    sns_db=None,
    buzz_fn=None,
    sns_inbox=None,
    knowledge_inbox=None,
    opportunity_inbox=None,
    watch_db=None,
    watch_inbox=None,
    lookup_renderer: LookupRenderer | None = None,
    board_loader=None,
    reputation_renderer: ReputationRenderer | None = None,
    research_notifier_factory: "Callable[[str], ResearchNotifier] | None" = None,
    research_cancel_probe_factory: "Callable[[str], Callable[[], bool]] | None" = None,
    start_schedulers: bool = True,
) -> "tuple[dict, dict, dict, dict]":
    """Build registries injected into the base dispatcher.

    Returns (command_handlers, callback_handlers, view_handlers, item_deleter_handlers).
    Registering as data means adding a new command never requires editing bot.py.

    When sns_inbox / knowledge_inbox are provided, write operations go through
    the respective inbox (single-writer-per-file pattern for Task 3+).
    """
    quiz_handler = build_quiz_handler(settings)
    backup_handler = build_backup_handler(settings)
    recover_handler = build_recover_handler(settings)
    scorecard_handler = build_scorecard_handler(settings)
    research_cache = _ResearchReplyCache()
    fix_pending_cache = FixPendingApplyCache()
    fix_handler = build_fix_handler(
        settings, fix_pending_cache, notifier_factory=research_notifier_factory
    )
    vpn_store = VpnConfigStore(
        Path(settings.monitor_db_path).resolve().parent / "vpn_rotation.json"
    )
    vpn_handler = build_vpn_handler(settings, vpn_store)
    if start_schedulers:
        # 只在 poller 行程跑輪替排程；bridge 也會建 registries，兩邊都跑會雙倍輪替
        VpnRotationScheduler(
            vpn_store, notifier_factory=research_notifier_factory
        ).start()
    def research_search_fn(q, limit):
        return _run_research_worker_call(
            lambda: web_search(q, max_results=limit, reuse_browser=False)
        )
    _yuyutei_resolver = _build_yuyutei_code_resolver(settings, research_search_fn)
    research_handler = build_research_handler(
        notifier_factory=research_notifier_factory,
        cancel_probe_factory=research_cancel_probe_factory,
        search_fn=research_search_fn,
        item_fetcher=MercariItemAdapter(fetch_html_fn=build_research_item_fetch_html()),
        knowledge_db_path=settings.knowledge_db_path,
        seller_snapshot_lookup_fn=_build_research_seller_snapshot_lookup(settings),
        seller_snapshot_followup_fn=_build_research_seller_snapshot_followup(settings),
        game_code_resolver_fn=_yuyutei_resolver.resolve if _yuyutei_resolver else None,
        cache_enricher_fn=_yuyutei_resolver.enrich_cache if _yuyutei_resolver else None,
        ip_heat_lookup_fn=_build_research_ip_heat_lookup(settings),
        entity_recognizer_fn=build_ollama_entity_recognizer(
            endpoint=settings.openclaw_local_text_endpoint,
            model=settings.openclaw_local_text_model or "qwen3:14b",
            knowledge_db_path=settings.knowledge_db_path,
        ),
        appreciation_enricher_fn=_build_research_appreciation_enricher(settings),
        semantic_gate_fn=build_ollama_sellable_unit_gate(
            endpoint=settings.openclaw_local_text_endpoint,
            model=settings.openclaw_local_text_model or "qwen3:14b",
        ),
        condition_assessor_fn=build_item_condition_assessor(settings),
        final_formatter=_build_research_reply_formatter(research_cache),
    )

    def _quizlikesong_handler(remainder: str, chat_id: str):
        return quiz_handler("like song " + (remainder or "").strip(), chat_id)

    def _new_handler(remainder: str, chat_id: str) -> str:
        if dynamic_tool_runner is None:
            return "/new 尚未啟用（需有本地 text model）。"
        return dynamic_tool_runner.run(remainder)

    music_favorites_store = FavoritesStore(settings.openclaw_music_best_path)
    _music_best_view_fn = build_music_best_view_fn(music_favorites_store)

    def _musiclistbest_handler(remainder: str, chat_id: str):
        text, markup, _ = _music_best_view_fn(page=0, mode=_MB_READ)
        return text, markup

    _web_research_renderer = default_web_research_renderer(settings)
    _web_fetch_renderer = default_web_fetch_renderer(settings)
    _base_processor = _BaseTelegramCommandProcessor(
        lookup_renderer=lookup_renderer or default_lookup_renderer(settings),
        board_loader=board_loader or (lambda: default_board_loader(settings)),
        catalog_renderer=lambda: "",
        reputation_renderer=reputation_renderer or default_reputation_renderer(settings),
        research_renderer=_web_research_renderer,
        fetch_renderer=_web_fetch_renderer,
        watch_db=watch_db,
        watch_inbox=watch_inbox,
    )

    def _search_handler(remainder: str, chat_id: str):
        return _base_processor._handle_web_research(remainder)

    def _fetch_handler(remainder: str, chat_id: str):
        return _base_processor._handle_web_fetch(remainder)

    def _lookup_handler(remainder: str, chat_id: str):
        return _base_processor._handle_lookup(remainder)

    def _trend_handler(remainder: str, chat_id: str):
        return _base_processor._handle_liquidity(remainder)

    def _snapshot_handler(remainder: str, chat_id: str):
        return _base_processor._handle_reputation_snapshot(remainder)

    def _watch_handler(remainder: str, chat_id: str):
        return _base_processor._handle_watch(remainder, chat_id)

    def _watchlist_handler(remainder: str, chat_id: str):
        return _base_processor.render_watchlist_view()

    def _unwatch_handler(remainder: str, chat_id: str):
        return _base_processor._handle_unwatch(remainder)

    def _setprice_handler(remainder: str, chat_id: str):
        return _base_processor._handle_set_price(remainder)

    def _scan_help_handler(remainder: str, chat_id: str):
        return "Send a card photo with the caption /scan pokemon or /scan ws, and I will parse it and then look up the price."

    command_handlers: dict[str, RegisteredCommand] = {
        "/quiz": RegisteredCommand(
            quiz_handler,
            ack="收到，正在出題（地端模型，可能要一點時間）…",
            background=True,
            **command_metadata("/quiz"),
        ),
        "/quizlikesong": RegisteredCommand(
            _quizlikesong_handler, ack="收到，正在收藏歌曲…", background=True,
            **command_metadata("/quizlikesong"),
        ),
        "/voice": RegisteredCommand(
            build_voice_handler(settings),
            **command_metadata("/voice"),
        ),
        "/generateaudio": RegisteredCommand(
            build_generateaudio_handler(settings),
            ack="收到，正在產生音訊檔案…",
            background=True,
            **command_metadata("/generateaudio"),
        ),
        "/saynow": RegisteredCommand(
            build_saynow_handler(settings),
            ack="收到，正在合成並於 Mac mini 播放語音…",
            background=True,
            **command_metadata("/saynow"),
        ),
        "/translateja": RegisteredCommand(
            build_translate_handler(settings, target="ja"),
            ack="收到，正在翻譯成日文…",
            background=True,
            **command_metadata("/translateja"),
        ),
        "/ja": RegisteredCommand(
            build_translate_handler(settings, target="ja"),
            ack="收到，正在翻譯成日文…",
            background=True,
            **command_metadata("/ja"),
        ),
        "/jp": RegisteredCommand(
            build_translate_handler(settings, target="ja"),
            ack="收到，正在翻譯成日文…",
            background=True,
            **command_metadata("/jp"),
        ),
        "/translatezh": RegisteredCommand(
            build_translate_handler(settings, target="zh"),
            ack="收到，正在翻譯成繁體中文…",
            background=True,
            **command_metadata("/translatezh"),
        ),
        "/zh": RegisteredCommand(
            build_translate_handler(settings, target="zh"),
            ack="收到，正在翻譯成繁體中文…",
            background=True,
            **command_metadata("/zh"),
        ),
        "/new": RegisteredCommand(
            _new_handler,
            ack="收到，正在找/生成工具並執行（地端模型，可能要 1-2 分鐘）…",
            background=True,
            **command_metadata("/new"),
        ),
        "/backupclaw": RegisteredCommand(
            lambda r, c: backup_handler(r),
            ack="收到，正在備份龍蝦的資料庫與自學工具規格…",
            background=True,
            **command_metadata("/backupclaw"),
        ),
        "/backup": RegisteredCommand(
            lambda r, c: backup_handler(r),
            ack="收到，正在備份龍蝦的資料庫與自學工具規格…",
            background=True,
            **command_metadata("/backup"),
        ),
        "/clawrecover": RegisteredCommand(
            lambda r, c: recover_handler(r),
            ack="收到，正在從備份還原龍蝦的資料庫…",
            background=True,
            **command_metadata("/clawrecover"),
        ),
        "/recoverclaw": RegisteredCommand(
            lambda r, c: recover_handler(r),
            ack="收到，正在從備份還原龍蝦的資料庫…",
            background=True,
            **command_metadata("/recoverclaw"),
        ),
        "/restartall": RegisteredCommand(
            build_restart_all_handler(settings),
            **command_metadata("/restartall"),
        ),
        "/stats": RegisteredCommand(lambda r, c: scorecard_handler(r), **command_metadata("/stats")),
        "/scorecard": RegisteredCommand(lambda r, c: scorecard_handler(r), **command_metadata("/scorecard")),
        "/knowledge": RegisteredCommand(
            build_knowledge_handler(settings, knowledge_inbox=knowledge_inbox),
            **command_metadata("/knowledge"),
        ),
        "/kb": RegisteredCommand(
            build_knowledge_handler(settings, knowledge_inbox=knowledge_inbox),
            **command_metadata("/kb"),
        ),
        "/source": RegisteredCommand(build_source_handler(settings), **command_metadata("/source")),
        "/lookup": RegisteredCommand(
            _lookup_handler,
            ack="收到，正在查詢卡牌價格…",
            background=True,
            **command_metadata("/lookup"),
        ),
        "/price": RegisteredCommand(
            _lookup_handler,
            ack="收到，正在查詢卡牌價格…",
            background=True,
            **command_metadata("/price"),
        ),
        "/trend": RegisteredCommand(
            _trend_handler,
            ack="收到，正在整理榜單…",
            background=True,
            **command_metadata("/trend"),
        ),
        "/trending": RegisteredCommand(
            _trend_handler,
            ack="收到，正在整理榜單…",
            background=True,
            **command_metadata("/trending"),
        ),
        "/hot": RegisteredCommand(
            _trend_handler,
            ack="收到，正在整理榜單…",
            background=True,
            **command_metadata("/hot"),
        ),
        "/heat": RegisteredCommand(
            _trend_handler,
            ack="收到，正在整理榜單…",
            background=True,
            **command_metadata("/heat"),
        ),
        "/liquidity": RegisteredCommand(
            _trend_handler,
            ack="收到，正在整理流動性排名…",
            background=True,
            **command_metadata("/liquidity"),
        ),
        "/snapshot": RegisteredCommand(
            _snapshot_handler,
            ack="收到，正在建立信譽快照…",
            background=True,
            **command_metadata("/snapshot"),
        ),
        "/proof": RegisteredCommand(
            _snapshot_handler,
            ack="收到，正在建立信譽快照…",
            background=True,
            **command_metadata("/proof"),
        ),
        "/repcheck": RegisteredCommand(
            _snapshot_handler,
            ack="收到，正在建立信譽快照…",
            background=True,
            **command_metadata("/repcheck"),
        ),
        "/reputation": RegisteredCommand(
            _snapshot_handler,
            ack="收到，正在建立信譽快照…",
            background=True,
            **command_metadata("/reputation"),
        ),
        "/scan": RegisteredCommand(
            _scan_help_handler,
            **command_metadata("/scan"),
        ),
        "/image": RegisteredCommand(
            _scan_help_handler,
            **command_metadata("/image"),
        ),
        "/photo": RegisteredCommand(
            _scan_help_handler,
            **command_metadata("/photo"),
        ),
        "/search": RegisteredCommand(
            _search_handler,
            ack="收到，正在搜尋並整理網路來源…",
            background=True,
            **command_metadata("/search"),
        ),
        "/web": RegisteredCommand(
            _search_handler,
            ack="收到，正在搜尋並整理網路來源…",
            background=True,
            **command_metadata("/web"),
        ),
        "/fetch": RegisteredCommand(
            _fetch_handler,
            ack="收到，正在讀取網頁並回答…",
            background=True,
            **command_metadata("/fetch"),
        ),
        "/read": RegisteredCommand(
            _fetch_handler,
            ack="收到，正在讀取網頁並回答…",
            background=True,
            **command_metadata("/read"),
        ),
        "/music": RegisteredCommand(
            build_music_handler(settings),
            **command_metadata("/music"),
        ),
        "/musicqueue": RegisteredCommand(
            build_musicqueue_handler(settings),
            **command_metadata("/musicqueue"),
        ),
        "/musiclistall": RegisteredCommand(
            build_musiclistall_handler(settings),
            **command_metadata("/musiclistall"),
        ),
        "/musiclistbest": RegisteredCommand(
            _musiclistbest_handler,
            **command_metadata("/musiclistbest"),
        ),
        "/musicnowbest": RegisteredCommand(
            build_musicnowbest_handler(settings),
            **command_metadata("/musicnowbest"),
        ),
        "/musicmute": RegisteredCommand(
            lambda r, c: mute_music(settings), **command_metadata("/musicmute"),
        ),
        "/musiclouder": RegisteredCommand(
            lambda r, c: louder_music(settings), **command_metadata("/musiclouder"),
        ),
        "/musiclower": RegisteredCommand(
            lambda r, c: lower_music(settings), **command_metadata("/musiclower"),
        ),
        "/bluetooth": RegisteredCommand(
            build_bluetooth_handler(settings),
            **command_metadata("/bluetooth"),
        ),
        "/ir": RegisteredCommand(
            build_ir_handler(settings),
            **command_metadata("/ir"),
        ),
        "/visionlook": RegisteredCommand(
            lambda r, c: "此功能僅支援網頁聊天中上傳圖片使用。",
            **command_metadata("/visionlook"),
        ),
        "/research": RegisteredCommand(
            research_handler,
            ack="收到，正在進行深度商品研究（會分階段回報進度）…",
            background=True,
            **command_metadata("/research"),
        ),
        "/fix": RegisteredCommand(
            fix_handler,
            ack="收到，開始 benchmark 修復迴圈（會分階段回報進度）…",
            background=True,
            **command_metadata("/fix"),
        ),
        "/resaerch": RegisteredCommand(
            research_handler,
            ack="收到，正在進行深度商品研究（會分階段回報進度）…",
            background=True,
            **command_metadata("/resaerch"),
        ),
        "/vpn": RegisteredCommand(
            vpn_handler,
            ack="收到，VPN 指令處理中…",
            background=True,
            **command_metadata("/vpn"),
        ),
        "/watch": RegisteredCommand(
            _watch_handler,
            ack="收到追蹤指令，正在設定…",
            background=True,
            **command_metadata("/watch"),
        ),
        "/watchlist": RegisteredCommand(
            _watchlist_handler,
            **command_metadata("/watchlist"),
        ),
        "/watches": RegisteredCommand(
            _watchlist_handler,
            **command_metadata("/watches"),
        ),
        "/unwatch": RegisteredCommand(
            _unwatch_handler,
            **command_metadata("/unwatch"),
        ),
        "/stopwatch": RegisteredCommand(
            _unwatch_handler,
            **command_metadata("/stopwatch"),
        ),
        "/setprice": RegisteredCommand(
            _setprice_handler,
            **command_metadata("/setprice"),
        ),
        "/updatewatch": RegisteredCommand(
            _setprice_handler,
            **command_metadata("/updatewatch"),
        ),
        "/snsadd": RegisteredCommand(
            build_sns_add_handler(sns_db, sns_inbox=sns_inbox),
            ack="收到 X 追蹤指令，正在設定…", background=True,
            **command_metadata("/snsadd"),
        ),
        "/sns_add": RegisteredCommand(
            build_sns_add_handler(sns_db, sns_inbox=sns_inbox),
            ack="收到 X 追蹤指令，正在設定…", background=True,
            **command_metadata("/sns_add"),
        ),
        "/snslist": RegisteredCommand(build_snslist_handler(sns_db), **command_metadata("/snslist")),
        "/sns_list": RegisteredCommand(build_snslist_handler(sns_db), **command_metadata("/sns_list")),
        "/snsdelete": RegisteredCommand(
            build_sns_delete_handler(sns_db, sns_inbox=sns_inbox),
            **command_metadata("/snsdelete"),
        ),
        "/sns_delete": RegisteredCommand(
            build_sns_delete_handler(sns_db, sns_inbox=sns_inbox),
            **command_metadata("/sns_delete"),
        ),
        "/snsbuzz": RegisteredCommand(
            build_sns_buzz_handler(buzz_fn),
            ack="收到，正在掃描 4chan 收藏/IP 討論並交給 LLM 整理…",
            background=True,
            **command_metadata("/snsbuzz"),
        ),
        "/sns_buzz": RegisteredCommand(
            build_sns_buzz_handler(buzz_fn),
            ack="收到，正在掃描 4chan 收藏/IP 討論並交給 LLM 整理…",
            background=True,
            **command_metadata("/sns_buzz"),
        ),
        "/snsclearfilter": RegisteredCommand(
            build_sns_clear_filter_handler(sns_db, sns_inbox=sns_inbox),
            **command_metadata("/snsclearfilter"),
        ),
        "/hunt": RegisteredCommand(
            build_hunt_handler(settings, opportunity_inbox=opportunity_inbox),
            **command_metadata("/hunt"),
        ),
        "/opportunity": RegisteredCommand(
            build_hunt_handler(settings, opportunity_inbox=opportunity_inbox),
            **command_metadata("/opportunity"),
        ),
    }

    # /schedulehome (issue #39): scheduled runs re-dispatch existing slash
    # commands through this same registry, so the runner must close over the
    # finished command_handlers dict (defined just above).
    _home_schedule_store = get_home_schedule_store(settings.openclaw_home_schedules_path)
    _run_slash_command = make_run_slash_command(command_handlers)
    command_handlers["/schedulehome"] = RegisteredCommand(
        build_schedulehome_handler(_home_schedule_store, _run_slash_command),
        **command_metadata("/schedulehome"),
    )

    if dynamic_tool_runner is not None:
        command_handlers["/workflow"] = RegisteredCommand(
            build_workflow_handler(settings, dynamic_tool_runner,
                                   command_registry=command_handlers),
            ack="⚙️",
            background=True,
            **command_metadata("/workflow"),
        )

    _rag_cb = _build_rag_callback_handler(settings, knowledge_inbox=knowledge_inbox)

    def _rag_keep_adapter(payload: str, original_text: str, chat_id: str):
        new_text, markup = _rag_cb("ragkeep", payload, original_text)
        return "✅ 已保留", new_text, markup

    def _rag_del_adapter(payload: str, original_text: str, chat_id: str):
        new_text, markup = _rag_cb("ragdel", payload, original_text)
        return "🗑️ 已刪除", new_text, markup

    def _workflow_list_adapter(payload: str, original_text: str, chat_id: str):
        action, _, wf_id = (payload or "").partition(":")
        wf_id = wf_id.strip()
        if not wf_id:
            return None, "缺少 workflow id。", None
        if action == "run":
            wf_spec = command_handlers.get("/workflow")
            if wf_spec is None:
                return None, "/workflow 指令尚未啟用。", None
            result = wf_spec.handler(f"run {wf_id}", str(chat_id))
            if isinstance(result, tuple):
                return None, result[0], result[1] if len(result) > 1 else None
            return None, result, None
        if action == "schedule":
            sh_spec = command_handlers.get("/schedulehome")
            if sh_spec is None:
                return None, "/schedulehome 指令尚未啟用。", None
            result = sh_spec.handler(f"add_for_wf {wf_id}", str(chat_id))
            if isinstance(result, tuple):
                return None, result[0], result[1] if len(result) > 1 else None
            return None, result, None
        if action == "delete":
            wf_spec = command_handlers.get("/workflow")
            if wf_spec is None:
                return None, "/workflow 指令尚未啟用。", None
            result = wf_spec.handler(f"delete {wf_id}", str(chat_id))
            if isinstance(result, tuple):
                return None, result[0], result[1] if len(result) > 1 else None
            return None, result, None
        if action == "rename":
            wf_spec = command_handlers.get("/workflow")
            if wf_spec is None:
                return None, "/workflow 指令尚未啟用。", None
            result = wf_spec.handler(f"rename {wf_id}", str(chat_id))
            if isinstance(result, tuple):
                return None, result[0], result[1] if len(result) > 1 else None
            return None, result, None
        if action == "renameid":
            wf_spec = command_handlers.get("/workflow")
            if wf_spec is None:
                return None, "/workflow 指令尚未啟用。", None
            result = wf_spec.handler(f"renameid {wf_id}", str(chat_id))
            if isinstance(result, tuple):
                return None, result[0], result[1] if len(result) > 1 else None
            return None, result, None
        return None, f"未知的 workflow 動作：{action}", None

    callback_handlers: dict[str, Callable[[str, str, str], tuple[object, str, object]]] = {
        "quiz": build_quiz_callback_handler(settings),
        "voice": build_voice_callback_handler(settings),
        "ragkeep": _rag_keep_adapter,
        "ragdel": _rag_del_adapter,
        "snsdel": build_snsdel_callback_handler(sns_db, sns_inbox=sns_inbox),
        "snsaddok": build_snsaddok_callback_handler(sns_db, sns_inbox=sns_inbox),
        "snsfb": build_snsfb_callback_handler(sns_db, sns_inbox=sns_inbox),
        "oppfb": build_hunt_callback_handler(settings, opportunity_inbox=opportunity_inbox),
        "rs": _build_research_callback_handler(research_cache),
        "fix": build_fix_callback_handler(fix_pending_cache),
        "imgtr": _build_image_translate_callback_handler(_IMAGE_TRANSLATE_ORIGINAL_CACHE),
        "music": build_music_callback_handler(settings),
        "bt": build_bluetooth_callback_handler(settings),
        "ir": build_ir_callback_handler(settings),
        "wf": _workflow_list_adapter,
        "sh": build_schedulehome_callback_handler(_home_schedule_store, _run_slash_command),
    }

    view_handlers = {
        "km": build_knowledge_market_view_fn(settings),
        "kc": build_knowledge_coding_view_fn(settings),
        "sl": build_snslist_view_fn(sns_db),
        "hl": build_huntlist_view_fn(settings),
        MUSIC_BEST_LIST_KIND: _music_best_view_fn,
    }
    item_deleter_handlers = {
        **build_knowledge_item_deleters(settings),
        "sl": build_sns_rule_deleter(sns_db, sns_inbox=sns_inbox),
        "hl": build_huntlist_item_deleter(settings, opportunity_inbox=opportunity_inbox),
        MUSIC_BEST_LIST_KIND: build_music_best_item_deleter(music_favorites_store),
    }

    return command_handlers, callback_handlers, view_handlers, item_deleter_handlers


def _build_rag_callback_handler(settings, knowledge_inbox=None) -> "Callable[[str, str, str], tuple[str, object]]":
    """Return a handler for ragkeep/ragdel callbacks."""
    from pathlib import Path as _Path
    db_path = _Path(settings.knowledge_db_path)

    def handler(prefix: str, entry_id: str, original_text: str) -> tuple[str, object]:
        if prefix == "ragkeep":
            return handle_ragkeep_callback(entry_id=entry_id, original_text=original_text)
        if prefix == "ragdel":
            return handle_ragdel_callback(
                entry_id=entry_id, original_text=original_text,
                db_path=db_path, knowledge_inbox=knowledge_inbox,
            )
        return original_text, None

    return handler
