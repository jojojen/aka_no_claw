"""Telegram bot orchestration — bridges AssistantSettings to price_monitor_bot.bot."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from assistant_runtime import AssistantSettings, build_ssl_context

from market_monitor.storage import MonitorDatabase
from price_monitor_bot.bot import (
    CatalogRenderer,
    LookupRenderer,
    PhotoLookupRenderer,
    TelegramCommandProcessor as _BaseTelegramCommandProcessor,
)
from telegram_core.contracts import RegisteredCommand, TelegramTextReplyPlan
from telegram_core.polling import run_telegram_polling as _core_run_telegram_polling
from telegram_core.transport import (
    send_telegram_test_message as _base_send_telegram_test_message,
)
from .telegram_compat import (  # noqa: F401 - legacy re-export surface (R2.1)
    BoardLoader,
    TelegramFileAttachment,
    TelegramLookupQuery,
    TelegramReputationDelivery,
    TelegramReputationQuery,
    TelegramResearchQuery,
    build_processing_ack,
    format_liquidity_board,
    format_photo_lookup_result,
    handle_telegram_message,
    parse_lookup_command,
    parse_reputation_snapshot_command,
)

from .catalog_planner import CatalogPlanner
from .dynamic_tools import (
    build_dynamic_tool_runner_from_settings,
)
from .image_translate import (
    build_image_translate_caption_recognizer,
)
from .home_schedule import (
    get_home_schedule_store,
)
from .home_schedule_command import (
    render_list as render_home_schedule_list,
)
from .workflow_command import build_workflow_handler, command_metadata, iter_command_metadata, _workflow_store
from .workflow_editor import WorkflowEditor
from .llm_pool_settings import (  # noqa: F401 - _select_text_generation_model is a legacy re-export (R2.2)
    _select_text_generation_model,
)
from .research_telegram import (  # noqa: F401 - _build_research_* re-export surface (R2.2)
    _ResearchReplyCache,
    _build_research_appreciation_enricher,
    _build_research_callback_handler,
    _build_research_notifier_factory,
    _build_research_reply_formatter,
    _build_research_seller_snapshot_lookup,
    default_web_research_renderer,
)
from .telegram_env import require_telegram_chat_id, require_telegram_token
from .photo_render import (  # noqa: F401 - legacy re-export surface (R2.2)
    _IMAGE_TRANSLATE_ORIGINAL_CACHE,
    _ImageTranslateOriginalCache,
    _build_image_translate_callback_handler,
    _caption_requests_image_translation,
    build_photo_renderer,
    default_board_loader,
    default_lookup_renderer,
    default_photo_intent_analyzer,
    default_photo_renderer,
)
from .reputation_render import (  # noqa: F401 - legacy re-export surface (R2.2)
    _chromium_launch_options,
    _resolve_chromium_executable,
    default_reputation_renderer,
    format_reputation_snapshot_delivery_text,
    format_reputation_snapshot_result,
    render_reputation_snapshot_artifacts,
)
from .local_text import (  # noqa: F401 - legacy re-export surface (R2.2)
    _call_local_text_model,
    build_translate_handler,
    default_web_fetch_renderer,
)
from .local_stt import LocalWhisperTranscriber
from .media_ingest import AUDIO_INTAKE_ACK_TEXT, transcribe_telegram_audio
from .background_jobs import (  # noqa: F401 - legacy re-export surface (R2.4)
    _bootstrap_inboxes,
    _bootstrap_opportunity_inbox,
    _bootstrap_watch_db,
    _bootstrap_watch_inbox,
    _build_backup_notify,
    _start_backup_scheduler,
    _start_card_image_crawler,
    _start_home_schedule_scheduler,
    _start_rag_daily_digest,
    _start_title_corpus_rebuilder,
    _start_watch_monitor,
)
from .goal_bridge_telegram import (  # noqa: F401 - legacy re-export surface (R2.5)
    ensure_goal_bridge,
    execute_goal_bridge,
    goal_reply_markup,
    handle_goal_callback,
    route_goal_loop_intent,
    run_goal_bridge,
    run_goal_callback_async,
)
from .telegram_registry import (  # noqa: F401 - legacy re-export surface (R2.2)
    _build_rag_callback_handler,
    _build_registries,
)
from .command_bridge_models import WebCommandResponse
from .sns_commands import (
    PendingTelegramSnsBulkUpdate,
    build_sns_bulk_add_filter_plan,
    build_sns_bulk_remove_filter_plan,
    build_sns_bulk_update_schedule_plan,
    handle_sns_bulk_update_callback,
)
from .quiz_command import (
    build_like_song_confirmation,
    start_quiz_daily_scheduler,
)
from .natural_language import build_telegram_natural_language_router_from_settings
from .natural_language import fallback_route_openclaw_natural_language
from telegram_nl.natural_language import TelegramNaturalLanguageIntent
from .quiz_favorite_songs import extract_first_youtube_url

logger = logging.getLogger(__name__)

PRICE_LOOKUP_COMMANDS = {"/lookup", "/price"}
TREND_BOARD_COMMANDS = {"/trend", "/trending", "/hot", "/heat", "/liquidity"}
PHOTO_SCAN_COMMANDS = {"/scan", "/image", "/photo"}
REPUTATION_SNAPSHOT_COMMANDS = {"/snapshot", "/proof", "/repcheck", "/reputation"}
HEAVY_COMMANDS = PRICE_LOOKUP_COMMANDS | TREND_BOARD_COMMANDS | REPUTATION_SNAPSHOT_COMMANDS


def _looks_like_foreign_text_for_translation(text: str) -> bool:
    """Cheap, deterministic check for "this bare message is foreign text the user
    pasted to read in Chinese" — used to auto-route to translation WITHOUT a slow
    LLM intent-router round-trip, so recognising the intent is effectively free.

    Fires on Japanese (any kana) or pure-English (Latin letters, zero Han) text.
    This is safe because the user always issues commands with a leading「/」(already
    excluded by the caller), so a bare non-Chinese message is never a command — it
    is something they want translated. The "zero Han" gate for English is the key:
    Chinese commands routinely embed English product names ("幫我查 pokemon Pikachu
    ex"), so any Han ideograph means it's a Chinese query and goes to the normal
    router, not translation. Script detection by unicode range is a fact about
    codepoints, not open-world entity recognition, so it does not fall under the
    LLM+RAG rule. The length guard stops tiny tokens like「はい」/ "ok" from being
    hijacked."""
    s = text.strip()
    if len(s) < 4:
        return False
    has_kana = any(
        (0x3040 <= ord(ch) <= 0x30FF)
        or (0x31F0 <= ord(ch) <= 0x31FF)
        or (0xFF66 <= ord(ch) <= 0xFF9D)
        for ch in s
    )
    if has_kana:
        return True
    has_han = any(0x4E00 <= ord(ch) <= 0x9FFF for ch in s)
    if has_han:
        return False
    return sum(1 for ch in s if "a" <= ch.lower() <= "z") >= 2


class TelegramCommandProcessor(_BaseTelegramCommandProcessor):
    """OpenClaw compatibility wrapper around the reusable Telegram processor."""

    def __init__(
        self,
        *,
        settings: AssistantSettings | None = None,
        allowed_chat_ids: frozenset[str] | None = None,
        workflow_editor=None,
        goal_bridge=None,
        stt_transcriber: LocalWhisperTranscriber | None = None,
        **kwargs,
    ) -> None:
        self._settings = settings
        self._workflow_editor = workflow_editor
        self._goal_bridge = goal_bridge
        self._stt_transcriber = stt_transcriber
        if self._stt_transcriber is None and settings is not None:
            self._stt_transcriber = LocalWhisperTranscriber.from_settings(settings)
        if allowed_chat_ids is None and settings is not None and settings.openclaw_telegram_chat_id:
            allowed_chat_ids = frozenset({settings.openclaw_telegram_chat_id})
        super().__init__(allowed_chat_ids=allowed_chat_ids, **kwargs)
        self._callback_registry.setdefault("goal", self._handle_goal_callback)
        self._pending_sns_bulk_updates: dict[str, PendingTelegramSnsBulkUpdate] = {}
        self._callback_registry.setdefault("bulk", self._bulk_callback)

    def prewarm_stt(self) -> None:
        """Load the whisper model in the background so the first voice message
        doesn't pay the multi-second model-load cost. Call from the polling
        entrypoint only — tests construct this processor directly and must not
        spawn real model loads."""
        if self._stt_transcriber is None:
            return
        threading.Thread(target=self._stt_transcriber.prewarm, daemon=True).start()

    def build_audio_intake_ack_text(self) -> str:
        return AUDIO_INTAKE_ACK_TEXT

    def handle_audio_message(
        self,
        *,
        client,
        chat_id: str | int,
        message: dict[str, object],
    ) -> tuple[str | None, str | None] | None:
        """Transcribe Telegram voice/audio, then let telegram_core redispatch it.

        Returning the transcript (instead of routing here) preserves the exact
        pending-reply, pre-dispatch, and natural-language path used by text.
        """
        return transcribe_telegram_audio(
            client=client,
            message=message,
            stt_transcriber=self._stt_transcriber,
            stt_language=getattr(self._settings, "openclaw_stt_language", None),
        )

    def get_pending_sns_bulk_update(self, chat_id: str | int) -> PendingTelegramSnsBulkUpdate | None:
        key = str(chat_id)
        pending = self._pending_sns_bulk_updates.get(key)
        if pending is None:
            return None
        if not pending.is_expired():
            return pending
        self._pending_sns_bulk_updates.pop(key, None)
        return None

    def set_pending_sns_bulk_update(self, pending: PendingTelegramSnsBulkUpdate) -> None:
        self._pending_sns_bulk_updates[pending.chat_id] = pending

    def pop_pending_sns_bulk_update(self, chat_id: str | int) -> PendingTelegramSnsBulkUpdate | None:
        return self._pending_sns_bulk_updates.pop(str(chat_id), None)

    def _bulk_callback(
        self, payload: str, original_text: str, chat_id: str
    ) -> tuple[str | None, str | None, dict[str, object] | None]:
        # Preserves the pre-Phase-3 quirk that `toast` defaults to "未知按鈕"
        # unless the underlying handler explicitly overrides it.
        if payload not in ("c", "x"):
            logger.warning("Unknown callback_query prefix=%r data=%r", "bulk", f"bulk:{payload}")
            return "未知按鈕", None, None
        toast_out, edit_text, edit_kb = handle_sns_bulk_update_callback(
            sns_db=self._sns_db,
            pop_pending=self.pop_pending_sns_bulk_update,
            action=payload,
            chat_id=chat_id,
            original_text=original_text,
        )
        return (toast_out if toast_out is not None else "未知按鈕"), edit_text, edit_kb

    def _build_sns_bulk_add_filter_plan(
        self,
        *,
        chat_id: str | int,
        target_domain: str,
        keywords: tuple[str, ...],
    ) -> TelegramTextReplyPlan:
        return build_sns_bulk_add_filter_plan(
            self._sns_db,
            chat_id=chat_id,
            target_domain=target_domain,
            keywords=keywords,
            set_pending=self.set_pending_sns_bulk_update,
        )

    def _build_sns_bulk_remove_filter_plan(
        self,
        *,
        chat_id: str | int,
        target_domain: str,
        keywords: tuple[str, ...],
    ) -> TelegramTextReplyPlan:
        return build_sns_bulk_remove_filter_plan(
            self._sns_db,
            chat_id=chat_id,
            target_domain=target_domain,
            keywords=keywords,
            set_pending=self.set_pending_sns_bulk_update,
        )

    def _build_sns_bulk_update_schedule_plan(
        self,
        *,
        chat_id: str | int,
        minutes: int | None,
        target_domain: str,
    ) -> TelegramTextReplyPlan:
        return build_sns_bulk_update_schedule_plan(
            self._sns_db,
            chat_id=chat_id,
            target_domain=target_domain,
            minutes=minutes,
            set_pending=self.set_pending_sns_bulk_update,
        )

    def _help_text(self) -> str:
        return _build_openclaw_help_text(getattr(self, "_command_registry", None))

    def _build_youtube_like_song_plan(
        self,
        *,
        chat_id: str | int,
        text: str | None,
    ) -> TelegramTextReplyPlan | None:
        if not self.is_allowed_chat(chat_id):
            return None
        if text is None or text.strip().startswith("/"):
            return None
        if self._settings is None:
            return None
        youtube_url = extract_first_youtube_url(text)
        if not youtube_url:
            return None
        proposal = build_like_song_confirmation(self._settings, youtube_url)
        if proposal is None:
            return None
        self.clear_pending_text_clarification(chat_id)
        proposal_text, proposal_markup = proposal
        return TelegramTextReplyPlan(
            ack=None,
            reply=proposal_text,
            reply_markup=proposal_markup,
        )

    def build_pending_text_reply_plan(
        self,
        *,
        chat_id: str | int,
        text: str | None,
    ) -> TelegramTextReplyPlan | None:
        youtube_plan = self._build_youtube_like_song_plan(chat_id=chat_id, text=text)
        if youtube_plan is not None:
            return youtube_plan
        return super().build_pending_text_reply_plan(chat_id=chat_id, text=text)

    def _zh_translate_handler(self) -> "Callable[[str, str], str] | None":
        if self._settings is None:
            return None
        handler = getattr(self, "_cached_zh_translate_handler", None)
        if handler is None:
            handler = build_translate_handler(self._settings, target="zh")
            self._cached_zh_translate_handler = handler
        return handler

    def _build_auto_translate_plan(
        self,
        *,
        chat_id: str | int,
        text: str | None,
    ) -> TelegramTextReplyPlan | None:
        if not self.is_allowed_chat(chat_id) or text is None:
            return None
        content = text.strip()
        if not content or content.startswith("/"):
            return None
        # Never hijack a reply the user is giving to a pending clarification.
        if self.get_pending_photo_clarification(chat_id) is not None:
            return None
        if self.get_pending_text_clarification(chat_id) is not None:
            return None
        if not _looks_like_foreign_text_for_translation(content):
            return None
        handler = self._zh_translate_handler()
        if handler is None:
            return None
        return TelegramTextReplyPlan(
            ack="收到，看起來是外文，直接翻成繁體中文…",
            reply=None,
            reply_factory=lambda: handler(content, str(chat_id)),
            run_in_background=True,
        )

    def _build_home_capture_plan(
        self, *, chat_id: str | int, text: str | None
    ) -> TelegramTextReplyPlan | None:
        """Capture-mode for /schedulehome (issue #39): after a schedule is created
        the user sends the slash commands to run, one per message, ending with
        「完成」. While a capture session is active for this chat, plain ``/``
        messages are appended to that schedule instead of being executed."""
        if text is None or not self.is_allowed_chat(chat_id):
            return None
        store = get_home_schedule_store(self._settings.openclaw_home_schedules_path)
        rename_sid = store.rename_target(chat_id)
        if rename_sid is not None:
            content = text.strip()
            # Escape hatch: let /schedulehome (and any slash) through so the user
            # can bail out instead of being trapped in rename mode.
            if content.startswith("/"):
                return None
            if content in {"取消", "cancel"}:
                store.end_rename(chat_id)
                list_text, markup = render_home_schedule_list(store)
                return TelegramTextReplyPlan(
                    ack=None, reply=f"已取消改名。\n\n{list_text}", reply_markup=markup
                )
            if not content:
                return None
            store.set_label(rename_sid, content)
            store.end_rename(chat_id)
            list_text, markup = render_home_schedule_list(store)
            return TelegramTextReplyPlan(
                ack=None,
                reply=f"✅ 已改名為「{content}」。\n\n{list_text}",
                reply_markup=markup,
            )
        sid = store.capture_target(chat_id)
        if sid is None:
            return None
        content = text.strip()
        if content in {"完成", "done", "結束"}:
            store.end_capture(chat_id)
            entry = store.get(sid)
            n = len(entry.get("commands") or []) if entry else 0
            list_text, markup = render_home_schedule_list(store)
            return TelegramTextReplyPlan(
                ack=None,
                reply=f"✅ 排程設定完成，已加入 {n} 個指令。\n\n{list_text}",
                reply_markup=markup,
            )
        # Let the user still manage schedules mid-capture without it being eaten.
        if content.startswith("/schedulehome"):
            return None
        if content.startswith("/"):
            store.add_command(sid, content)
            entry = store.get(sid)
            n = len(entry.get("commands") or []) if entry else 0
            return TelegramTextReplyPlan(
                ack=None,
                reply=f"已加入第 {n} 個指令：{content}\n繼續傳下一個指令，或輸入「完成」結束。",
            )
        return None

    def _build_workflow_capture_plan(
        self, *, chat_id: str | int, text: str | None
    ) -> TelegramTextReplyPlan | None:
        """Capture-mode for the workflow card editor (#53): while a user has an
        active editor session and is being asked for a field value, plain-text
        messages are routed here instead of the main dispatcher."""
        if text is None or self._workflow_editor is None:
            return None
        if not self.is_allowed_chat(chat_id):
            return None
        if not self._workflow_editor.is_capturing(str(chat_id)):
            return None
        # Escape hatch: never swallow a slash command. Otherwise the editor's
        # text-collection state becomes a roach motel — /workflow cancel (or any
        # command to restart) gets eaten before it reaches the dispatcher, leaving
        # the user with no way to cancel, save, or start over. No capture field
        # legitimately starts with "/", so letting these through is safe.
        if text.strip().startswith("/"):
            return None
        result = self._workflow_editor.handle_text_capture(text, str(chat_id))
        if result is None:
            return None
        reply_text, markup = result
        return TelegramTextReplyPlan(
            ack=None,
            reply=reply_text,
            reply_markup=markup or None,
        )

    def _build_app_natural_language_reply_plan(
        self,
        intent: TelegramNaturalLanguageIntent,
        *,
        chat_id: str | int = "",
    ) -> TelegramTextReplyPlan | None:
        cid = str(chat_id)
        if intent.intent == "sns_bulk_add_filter":
            return self._build_sns_bulk_add_filter_plan(
                chat_id=chat_id,
                target_domain=intent.bulk_target_domain or "",
                keywords=intent.bulk_filter_keywords,
            )
        if intent.intent == "sns_bulk_remove_filter":
            return self._build_sns_bulk_remove_filter_plan(
                chat_id=chat_id,
                target_domain=intent.bulk_target_domain or "",
                keywords=intent.bulk_filter_keywords,
            )
        if intent.intent == "sns_bulk_update_schedule":
            return self._build_sns_bulk_update_schedule_plan(
                chat_id=chat_id,
                target_domain=intent.bulk_target_domain or "",
                minutes=intent.sns_schedule_minutes,
            )
        if intent.intent == "create_workflow":
            desc = intent.workflow_description or ""
            wf_spec = self._command_registry.get("/workflow")
            if wf_spec is None:
                return TelegramTextReplyPlan(ack=None, reply="/workflow 指令尚未啟用。")
            logger.info("Telegram NL routed intent=create_workflow desc=%s", desc[:80])
            return TelegramTextReplyPlan(
                ack="收到，正在建立 workflow…",
                reply=None,
                reply_factory=lambda d=desc, c=cid: wf_spec.handler(f"create {d}", c),
                run_in_background=True,
            )
        if intent.intent == "play_music":
            query = intent.music_query or ""
            music_spec = self._command_registry.get("/music")
            if music_spec is None:
                return TelegramTextReplyPlan(ack=None, reply="/music 指令尚未啟用。")
            logger.info("Telegram NL routed intent=play_music query=%s", query or "(none)")
            return TelegramTextReplyPlan(
                ack=None,
                reply=None,
                reply_factory=lambda q=query, c=cid: music_spec.handler(q or "playbest", c),
            )
        if intent.intent == "home_action":
            target = intent.home_target or ""
            command = intent.home_command or "on"
            ir_spec = self._command_registry.get("/ir")
            if ir_spec is None:
                return TelegramTextReplyPlan(ack=None, reply="/ir 指令尚未啟用。")
            logger.info("Telegram NL routed intent=home_action target=%s cmd=%s", target, command)
            return TelegramTextReplyPlan(
                ack=None,
                reply=None,
                reply_factory=lambda t=target, cmd=command, c=cid: ir_spec.handler(f"send {t} {cmd}", c),
            )
        if intent.intent == "execute_goal":
            goal = (intent.workflow_description or "").strip()
            if not goal:
                return TelegramTextReplyPlan(ack=None, reply="我沒有抓到要執行的目標內容。")
            logger.info("Telegram NL routed intent=execute_goal goal=%s", goal[:120])
            return TelegramTextReplyPlan(
                ack="收到，先幫你規劃工作流並請你確認…",
                reply=None,
                reply_factory=lambda g=goal, c=cid: self._execute_goal_bridge_reply(g, c),
                run_in_background=True,
            )
        return None

    def _route_natural_language(self, text: str) -> TelegramNaturalLanguageIntent | None:
        intent = super()._route_natural_language(text)
        if intent is not None:
            return intent
        app_intent = fallback_route_openclaw_natural_language(text)
        if app_intent is not None and app_intent.intent != "unknown":
            logger.info(
                "Telegram openclaw fallback intent=%s confidence=%s",
                app_intent.intent,
                app_intent.confidence,
            )
            return app_intent
        goal_intent = self._route_goal_loop_intent(text)
        if goal_intent is not None:
            return goal_intent
        return None

    def build_reply_plan(self, *, chat_id: str | int, text: str | None) -> TelegramTextReplyPlan:
        workflow_capture_plan = self._build_workflow_capture_plan(chat_id=chat_id, text=text)
        if workflow_capture_plan is not None:
            return workflow_capture_plan
        home_capture_plan = self._build_home_capture_plan(chat_id=chat_id, text=text)
        if home_capture_plan is not None:
            return home_capture_plan
        youtube_plan = self._build_youtube_like_song_plan(chat_id=chat_id, text=text)
        if youtube_plan is not None:
            return youtube_plan
        translate_plan = self._build_auto_translate_plan(chat_id=chat_id, text=text)
        if translate_plan is not None:
            return translate_plan
        return super().build_reply_plan(chat_id=chat_id, text=text)

    def _get_goal_bridge(self):
        self._goal_bridge = ensure_goal_bridge(self._settings, self._goal_bridge)
        return self._goal_bridge

    def _route_goal_loop_intent(self, text: str) -> TelegramNaturalLanguageIntent | None:
        return route_goal_loop_intent(self._get_goal_bridge(), text, settings=self._settings)

    def _execute_goal_bridge(self, text: str, chat_id: str | int) -> WebCommandResponse:
        return execute_goal_bridge(self._get_goal_bridge(), text, chat_id, settings=self._settings)

    def _run_goal_bridge(self, goal: str, chat_id: str | int) -> WebCommandResponse:
        return run_goal_bridge(self._get_goal_bridge(), goal, chat_id, settings=self._settings)

    def _execute_goal_bridge_reply(
        self,
        text: str,
        chat_id: str | int,
    ) -> tuple[str, dict[str, object] | None]:
        response = self._run_goal_bridge(text, chat_id)
        return response.message, goal_reply_markup(response)

    def _handle_goal_callback(
        self,
        payload: str,
        original_text: str,
        chat_id: str,
    ) -> tuple[object, str | None, object]:
        return handle_goal_callback(
            self._get_goal_bridge(),
            payload,
            original_text,
            chat_id,
            settings=self._settings,
        )

    def handle_callback_query_async(
        self,
        *,
        client,
        callback_id: str,
        chat_id: str,
        message_id: int,
        prefix: str,
        payload: str,
        original_text: str,
    ) -> bool:
        if prefix != "goal":
            return super().handle_callback_query_async(
                client=client,
                callback_id=callback_id,
                chat_id=chat_id,
                message_id=message_id,
                prefix=prefix,
                payload=payload,
                original_text=original_text,
            )
        run_goal_callback_async(
            client=client,
            callback_id=callback_id,
            chat_id=chat_id,
            message_id=message_id,
            payload=payload,
            original_text=original_text,
            bridge=self._get_goal_bridge(),
            settings=self._settings,
        )
        return True


def _build_openclaw_help_text(command_registry: dict[str, RegisteredCommand] | None = None) -> str:
    registry = command_registry or {}
    command_lines = []
    for command in sorted(registry):
        usage = getattr(registry[command], "usage", None) or command_metadata(command).get("usage", "")
        if usage:
            command_lines.append(f"{command} — {usage}")
        else:
            command_lines.append(command)
    if not command_lines:
        for command, meta in sorted(_known_command_metadata_items()):
            usage = meta.get("usage", "")
            command_lines.append(f"{command} — {usage}" if usage else command)

    examples = [
        "/price pokemon | Pikachu ex | 132/106 | SAR | sv08",
        "/trend pokemon 5",
        "/snapshot https://jp.mercari.com/item/m123456789",
        "/search 初音未來哪年發明的？",
        "/fetch https://example.com 這篇文章的重點是什麼",
        "/research https://jp.mercari.com/item/m123456789",
        "傳圖片 + caption: /scan pokemon",
        "/watch 想いが重なる場所で 初音ミク SSP on 300000",
        "/snsadd @username",
        "/quiz grammar",
        "/translateja 你好，今天辛苦了",
        "/translatezh お疲れさま、今日は大変だったね",
        "/generateaudio こんにちは、今日もよろしくお願いします",
        "/music playbest",
        "/hunt status",
    ]
    return "\n".join(
        [
            "OpenClaw — 指令一覧",
            "",
            "--- 系統 ---",
            "/ping  /status  /tools  /help",
            "",
            "--- 常用範例 ---",
            *examples,
            "",
            "--- 已註冊指令 ---",
            *command_lines,
            "",
            "--- 自然語言也可以 ---",
            "pokemon 熱門前 5",
            "幫我查 pokemon Pikachu ex 132/106",
            "播放米津玄師的熱門歌曲",
        ]
    )


def _known_command_metadata_items():
    return iter_command_metadata()


def _wire_kb_embedder(settings: AssistantSettings) -> None:
    """Install the process-wide KB embedder once at boot so every
    ``KnowledgeDatabase(...)`` in this process gets semantic write/retrieval.
    Best-effort: any failure leaves the KB pure-lexical."""
    try:
        from .kb_embedder import build_kb_embedder
        from .knowledge_db import set_default_embedder

        embedder = build_kb_embedder(settings, ssl_context=build_ssl_context(settings))
        set_default_embedder(embedder)
        if embedder is not None:
            logger.info("KB embedder wired: model=%s dim=%s", embedder.model, embedder.dim)
    except Exception:
        logger.warning("KB embedder wiring failed — KB stays lexical", exc_info=True)


def _build_intent_fast_path(settings: AssistantSettings):
    """Build the embedding intent fast-path (zero-arg command short-circuit).
    Best-effort: any failure leaves routing to the LLM router alone."""
    try:
        from .intent_fast_path import build_intent_fast_path

        return build_intent_fast_path(settings)
    except Exception:
        logger.warning("intent fast-path build failed — using LLM router only", exc_info=True)
        return None


def run_telegram_polling(
    *,
    settings: AssistantSettings,
    lookup_renderer: LookupRenderer,
    board_loader,
    catalog_renderer: CatalogRenderer,
    photo_renderer: PhotoLookupRenderer | None = None,
    poll_timeout: int = 20,
    notify_startup: bool = False,
    drop_pending_updates: bool = True,
) -> int:
    token = require_telegram_token(settings)
    from .command_bridge import CommandBridge

    _wire_kb_embedder(settings)
    watch_db = _bootstrap_watch_db(settings)
    # Price monitor now runs in local.openclaw.price_monitor (separate process).
    # Telegram reads monitor.sqlite3 for watchlist queries; writes go through watch_inbox.
    watch_inbox = _bootstrap_watch_inbox(settings)
    # SNS background monitor now runs in local.openclaw.sns_monitor (separate process).
    # Telegram opens sns.sqlite3 read-only for /snslist queries; writes go through inbox.
    sns_db = _open_sns_db_readonly(settings)
    sns_buzz_fn = _build_buzz_fn_standalone(settings, ssl_context=build_ssl_context(settings))
    # Bootstrap inboxes — telegram is the producer; owner services are the consumers.
    sns_inbox, knowledge_inbox = _bootstrap_inboxes(settings)
    opportunity_inbox = _bootstrap_opportunity_inbox(settings)
    research_renderer = default_web_research_renderer(settings)
    feedback_service = _build_feedback_service(watch_db)
    _start_backup_scheduler(settings)
    _start_title_corpus_rebuilder(settings)
    _start_rag_daily_digest(settings)
    start_quiz_daily_scheduler(settings)
    dynamic_tool_runner = build_dynamic_tool_runner_from_settings(settings)
    command_handlers, callback_handlers, view_handlers, item_deleter_handlers = (
        _build_registries(settings, dynamic_tool_runner, sns_db=sns_db, buzz_fn=sns_buzz_fn,
                          sns_inbox=sns_inbox, knowledge_inbox=knowledge_inbox,
                          opportunity_inbox=opportunity_inbox,
                          watch_db=watch_db, watch_inbox=watch_inbox,
                          lookup_renderer=lookup_renderer,
                          board_loader=board_loader,
                          reputation_renderer=default_reputation_renderer(settings),
                          research_notifier_factory=_build_research_notifier_factory(settings))
    )
    _start_home_schedule_scheduler(settings, command_handlers)

    # Live Chat/planner integration (#52): a free-text message that matched no
    # built-in intent gets a shot at the growing generated-tool catalog. The
    # planner's inline-button confirmations route back through the callback
    # registry, so merge its handlers in.
    catalog_planner = CatalogPlanner(dynamic_tool_runner)
    callback_handlers.update(catalog_planner.callback_handlers())

    # Workflow card editor (#53): single shared WorkflowEditor instance handles
    # wfe: callbacks AND text capture via the processor's build_reply_plan.
    # Re-register /workflow to include the editor for `new`/`edit` subcommands.
    _wf_editor: WorkflowEditor | None = None
    if dynamic_tool_runner is not None:
        _tg_sh_store = get_home_schedule_store(settings.openclaw_home_schedules_path)

        def _tg_on_id_renamed(old_id: str, new_id: str) -> None:
            _rewrite_schedule_commands(_tg_sh_store, old_id, new_id)

        _wf_editor = WorkflowEditor(_workflow_store(dynamic_tool_runner),
                                      command_registry=command_handlers,
                                      catalog=dynamic_tool_runner.catalog,
                                      on_id_renamed=_tg_on_id_renamed)
        callback_handlers.update(_wf_editor.callback_handlers())
        command_handlers["/workflow"] = RegisteredCommand(
            build_workflow_handler(settings, dynamic_tool_runner, workflow_editor=_wf_editor,
                                   command_registry=command_handlers),
            ack="⚙️",
            background=True,
        )

    goal_bridge = CommandBridge(settings=settings)
    # P3 completion: build the app processor here and hand it to the generic
    # telegram_core loop — the price_monitor_bot processor_factory relay is
    # retired. Startup stdout marker is now core's
    # "Telegram bot polling as @…" (see CLAUDE.md ops section).
    processor = TelegramCommandProcessor(
        settings=settings,
        workflow_editor=_wf_editor,
        goal_bridge=goal_bridge,
        lookup_renderer=lookup_renderer,
        board_loader=board_loader,
        catalog_renderer=catalog_renderer,
        photo_intent_analyzer=default_photo_intent_analyzer(settings),
        reputation_renderer=default_reputation_renderer(settings),
        research_renderer=research_renderer,
        fetch_renderer=default_web_fetch_renderer(settings),
        natural_language_router=build_telegram_natural_language_router_from_settings(settings),
        intent_fast_path=_build_intent_fast_path(settings),
        image_translate_recognizer=build_image_translate_caption_recognizer(settings),
        allowed_chat_ids=frozenset(settings.openclaw_telegram_chat_ids),
        status_renderer=lambda: _build_status_text(settings, dynamic_tool_runner),
        command_handlers=command_handlers,
        callback_handlers=callback_handlers,
        view_handlers=view_handlers,
        item_deleter_handlers=item_deleter_handlers,
        unknown_text_handler=catalog_planner.handle_text,
        watch_db=watch_db,
        watch_inbox=watch_inbox,
        sns_db=sns_db,
        sns_buzz_fn=sns_buzz_fn,
        feedback_service=feedback_service,
    )
    processor.prewarm_stt()
    return _core_run_telegram_polling(
        token=token,
        processor=processor,
        photo_renderer=photo_renderer or build_photo_renderer(settings, research_renderer=research_renderer),
        ssl_context=build_ssl_context(settings),
        allowed_chat_ids=frozenset(settings.openclaw_telegram_chat_ids),
        poll_timeout=poll_timeout,
        notify_startup=notify_startup,
        drop_pending_updates=drop_pending_updates,
    )


def _rewrite_schedule_commands(store, old_id: str, new_id: str) -> None:
    """Replace `/workflow run <old_id>` with `/workflow run <new_id>` in every
    home-schedule entry that references the renamed workflow ID."""
    old_cmd = f"/workflow run {old_id}"
    new_cmd = f"/workflow run {new_id}"
    for entry in store.list():
        sid = entry.get("id")
        cmds = entry.get("commands") or []
        if not any(c == old_cmd for c in cmds):
            continue
        store.clear_commands(sid)
        for cmd in cmds:
            store.add_command(sid, new_cmd if cmd == old_cmd else cmd)


def _build_feedback_service(watch_db: MonitorDatabase):
    """Construct a TcgPriceFeedbackService bound to the shared watch_db.
    Returns None if the price_monitor_bot package isn't importable, so the
    rest of the bot keeps running."""
    try:
        from tcg_tracker.feedback import TcgPriceFeedbackService
    except Exception:
        return None
    return TcgPriceFeedbackService(database=watch_db)


def _open_sns_db_readonly(settings):
    """Open sns.sqlite3 read-only for telegram list queries.

    Returns None and logs a warning if the file doesn't exist yet (sns_monitor
    service not started). Telegram never writes sns.sqlite3 directly — writes
    go through sns_inbox.
    """
    from pathlib import Path as _Path
    from sns_monitor.storage import SnsDatabase
    path = _Path(settings.sns_db_path)
    if not path.exists():
        logger.warning(
            "_open_sns_db_readonly: %s not found — start local.openclaw.sns_monitor first",
            path,
        )
        return None
    try:
        return SnsDatabase(path)
    except Exception:
        logger.exception("_open_sns_db_readonly: failed to open %s", path)
        return None


def _build_buzz_fn_standalone(settings, ssl_context=None):
    """Build /snsbuzz using only the 4chan client — no full SNS monitor needed."""
    try:
        from sns_monitor.fourchan_buzz import FourchanBuzzClient
        from sns_monitor.x_client_web import XClientWeb as _XClient
        from .sns_tools import _build_sns_buzz_fn
        fourchan_client = FourchanBuzzClient()
        x_client = _XClient(buzz_search_backend=fourchan_client)
        buzz_fn = _build_sns_buzz_fn(settings, x_client, ssl_context=ssl_context,
                                     fourchan_client=fourchan_client)
        if buzz_fn is not None:
            logger.info("telegram: /snsbuzz enabled (4chan + LLM + IP-heat)")
        return buzz_fn
    except Exception:
        logger.exception("telegram: failed to build buzz_fn standalone")
        return None


def send_telegram_test_message(*, settings: AssistantSettings, message: str) -> int:
    token = require_telegram_token(settings)
    chat_id = require_telegram_chat_id(settings)
    return _base_send_telegram_test_message(
        token=token,
        chat_id=chat_id,
        message=message,
        ssl_context=build_ssl_context(settings),
    )


def _build_status_text(settings: AssistantSettings, dynamic_tool_runner=None) -> str:
    allowed_chats = ", ".join(settings.openclaw_telegram_chat_ids) if settings.openclaw_telegram_chat_ids else "not restricted"
    configured = _load_status_configuration_snapshot()
    tesseract = settings.openclaw_tesseract_path or configured.get("OPENCLAW_TESSERACT_PATH") or "PATH lookup"
    tessdata = settings.openclaw_tessdata_dir or configured.get("OPENCLAW_TESSDATA_DIR") or "auto"
    text_backend = (settings.openclaw_local_text_backend or "").strip().lower() or "none"
    text_model = _select_router_model_for_status(settings) if text_backend != "none" else None
    configured_text_backend = configured.get("OPENCLAW_LOCAL_TEXT_BACKEND") or "none"
    configured_text_model = configured.get("OPENCLAW_LOCAL_TEXT_MODEL")
    configured_text_timeout = configured.get("OPENCLAW_LOCAL_TEXT_TIMEOUT_SECONDS") or str(settings.openclaw_local_text_timeout_seconds)
    configured_text_endpoint = configured.get("OPENCLAW_LOCAL_TEXT_ENDPOINT") or settings.openclaw_local_text_endpoint
    vision_backend = (settings.openclaw_local_vision_backend or "").strip().lower() or "none"
    vision_models = _split_model_list(settings.openclaw_local_vision_model)
    configured_vision_backend = configured.get("OPENCLAW_LOCAL_VISION_BACKEND") or "none"
    configured_vision_models = _split_model_list(configured.get("OPENCLAW_LOCAL_VISION_MODEL"))
    configured_vision_timeout = configured.get("OPENCLAW_LOCAL_VISION_TIMEOUT_SECONDS") or str(settings.openclaw_local_vision_timeout_seconds)
    configured_vision_endpoint = configured.get("OPENCLAW_LOCAL_VISION_ENDPOINT") or settings.openclaw_local_vision_endpoint
    reputation_host = settings.reputation_agent_server_url or "not configured"
    return "\n".join(
        [
            "OpenClaw Telegram status",
            f"env: {settings.monitor_env}",
            f"db: {settings.monitor_db_path}",
            f"allowed chats: {allowed_chats}",
            "",
            "Features",
            _format_status_feature_line(
                "text routing",
                active_backend=text_backend,
                active_model_display=_format_model_display(text_model),
                configured_backend=configured_text_backend,
                configured_model_display=_format_model_display(configured_text_model),
                timeout_seconds=configured_text_timeout,
                endpoint=configured_text_endpoint,
            ),
            _format_status_feature_line(
                "image scan vision",
                active_backend=vision_backend,
                active_model_display=_format_model_list_display(vision_models),
                configured_backend=configured_vision_backend,
                configured_model_display=_format_model_list_display(configured_vision_models),
                timeout_seconds=configured_vision_timeout,
                endpoint=configured_vision_endpoint,
            ),
            f"/new codegen: {dynamic_tool_runner.backend_label if dynamic_tool_runner is not None else 'disabled'}",
            f"image scan OCR: engine=tesseract | binary={tesseract} | tessdata={tessdata}",
            "price lookup / trend / watch: model=none | source-driven matching and pricing rules",
            f"reputation snapshot: model=none | server={reputation_host} | poll={settings.reputation_agent_poll_secs}s | renderer=playwright chromium",
            (
                "opportunity agent: "
                f"{'enabled' if settings.opportunity_agent_enabled else 'disabled'}"
                f" | db={settings.opportunity_db_path}"
                f" | interval={settings.opportunity_interval_seconds}s"
                f" | llm_timeout={settings.opportunity_llm_timeout_seconds}s"
                f" | sns_lookback={settings.opportunity_sns_lookback_hours}h"
            ),
        ]
    )


def _select_router_model_for_status(settings: AssistantSettings) -> str | None:
    from .natural_language import _select_router_model

    return _select_router_model(settings)


def _split_model_list(raw_models: str | None) -> tuple[str, ...]:
    if not raw_models:
        return ()
    return tuple(part.strip() for part in raw_models.split(",") if part.strip())


def _format_model_list_display(models: tuple[str, ...]) -> str:
    if not models:
        return "none"
    return ", ".join(_format_model_display(model) for model in models)


def _format_model_display(model: str | None) -> str:
    if not model:
        return "none"
    size = _extract_model_size(model)
    if size is None:
        return model
    return f"{model} ({size})"


def _extract_model_size(model: str) -> str | None:
    for segment in reversed(model.split(":")):
        candidate = segment.strip()
        if not candidate:
            continue
        lowered = candidate.lower()
        if lowered.endswith("b") and any(ch.isdigit() for ch in lowered):
            return lowered.upper()
    return None


def _format_feature_runtime(backend: str, model_display: str) -> str:
    if backend == "none":
        return f"disabled / {model_display}"
    return f"{backend} / {model_display}"


def _format_status_feature_line(
    label: str,
    *,
    active_backend: str,
    active_model_display: str,
    configured_backend: str,
    configured_model_display: str,
    timeout_seconds: str,
    endpoint: str,
) -> str:
    active_runtime = _format_feature_runtime(active_backend, active_model_display)
    configured_runtime = _format_feature_runtime(configured_backend, configured_model_display)
    if active_runtime == configured_runtime:
        runtime_text = active_runtime
    else:
        runtime_text = f"active={active_runtime} | configured={configured_runtime}"
    return f"{label}: {runtime_text} | timeout={timeout_seconds}s | endpoint={endpoint}"


def _load_status_configuration_snapshot() -> dict[str, str]:
    merged: dict[str, str] = {}
    for file_name in (".env.example", ".env"):
        merged.update(_read_env_values(Path.cwd() / file_name))
    return merged


def _read_env_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values
