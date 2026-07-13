"""Telegram bot orchestration — bridges AssistantSettings to price_monitor_bot.bot."""

from __future__ import annotations

import logging
import mimetypes
import os
import json
import shutil
import threading
import uuid
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from pathlib import Path
from typing import Callable

from assistant_runtime import AssistantSettings, build_ssl_context
from assistant_runtime.logging_utils import trim_for_log

from market_monitor.storage import MonitorDatabase
from price_monitor_bot.bot import (
    CatalogRenderer,
    LookupRenderer,
    PhotoLookupRenderer,
    PhotoLookupReply,
    ReputationRenderer,
    TelegramPhotoIntentAnalysis,
    TelegramPhotoIntentOption,
    TelegramPhotoQuery,
    TelegramReputationDelivery,
    TelegramReputationQuery,
    default_board_loader as _base_default_board_loader,
    default_lookup_renderer as _base_default_lookup_renderer,
    default_photo_renderer as _base_default_photo_renderer,
    TelegramCommandProcessor as _BaseTelegramCommandProcessor,
)
from telegram_core.contracts import RegisteredCommand, TelegramTextReplyPlan
from telegram_core.polling import run_telegram_polling as _core_run_telegram_polling
from telegram_core.transport import (
    TelegramBotClient,
    TelegramFileAttachment,
    send_telegram_test_message as _base_send_telegram_test_message,
)
from .telegram_compat import (  # noqa: F401 - legacy re-export surface (R2.1)
    BoardLoader,
    TelegramLookupQuery,
    TelegramResearchQuery,
    build_processing_ack,
    format_liquidity_board,
    format_photo_lookup_result,
    handle_telegram_message,
    parse_lookup_command,
    parse_reputation_snapshot_command,
)
from price_monitor_bot.watch_monitor import ensure_monitor as _ensure_watch_monitor
from tcg_tracker.image_lookup import TcgVisionSettings

from .backup_command import BackupScheduler, build_backup_handler, build_recover_handler
from .catalog_planner import CatalogPlanner
from .opportunity_scorecard import build_scorecard_handler
from .rag_daily_digest import RagDailyDigestScheduler, handle_ragdel_callback, handle_ragkeep_callback
from .dynamic_tools import (
    build_dynamic_tool_runner_from_settings,
)
from .image_translate import (
    build_image_ocr_translate_renderer_from_settings,
    build_image_translate_caption_recognizer,
)
from .knowledge_command import (
    build_knowledge_handler,
    build_knowledge_market_view_fn,
    build_knowledge_coding_view_fn,
    build_knowledge_item_deleters,
)
from .source_command import build_source_handler
from .music_command import (
    build_music_handler,
    build_musicnowbest_handler,
    build_musicqueue_handler,
)
from .music_browser import build_musiclistall_handler, build_music_callback_handler
from .bluetooth_command import (
    build_bluetooth_handler,
    build_bluetooth_callback_handler,
)
from .ir_command import build_ir_callback_handler, build_ir_handler
from .music_volume import mute_music, louder_music, lower_music
from .service_restart import build_restart_all_handler
from .home_schedule import (
    HomeScheduleScheduler,
    get_home_schedule_store,
    make_run_slash_command,
)
from .home_schedule_command import (
    build_schedulehome_callback_handler,
    build_schedulehome_handler,
    render_list as render_home_schedule_list,
)
from .workflow_command import build_workflow_handler, command_metadata, iter_command_metadata, _workflow_store
from .workflow_editor import WorkflowEditor
from .llm_pool_settings import (
    _LLM_NOT_CONFIGURED_MESSAGE,
    _TRANSLATE_NOT_CONFIGURED_MESSAGE,
    _select_text_generation_model,
    default_chat_backend,
)
from .research_telegram import (
    _ResearchReplyCache,
    _build_research_appreciation_enricher,
    _build_research_callback_handler,
    _build_research_ip_heat_lookup,
    _build_research_notifier_factory,
    _build_research_reply_formatter,
    _build_research_seller_snapshot_followup,
    _build_research_seller_snapshot_lookup,
    _build_yuyutei_code_resolver,
    _run_research_worker_call,
    default_web_research_renderer,
)
from .telegram_env import require_telegram_chat_id, require_telegram_token
from .local_stt import (
    LocalWhisperTranscriber,
    SttPayloadTooLarge,
    SttRequestError,
    SttRuntimeError,
    build_audio_request,
    validate_audio_mime_type,
)
from .command_bridge_models import STATUS_OK, WebCommandResponse, parse_request
from .music_favorites import (
    FavoritesStore,
    MUSIC_BEST_LIST_KIND,
    build_music_best_view_fn,
    build_music_best_item_deleter,
)
from telegram_core.list_view import LIST_VIEW_MODE_READ as _MB_READ
from .sns_commands import (
    PendingTelegramSnsBulkUpdate,
    build_sns_bulk_add_filter_plan,
    build_sns_bulk_remove_filter_plan,
    build_sns_bulk_update_schedule_plan,
    handle_sns_bulk_update_callback,
    build_sns_add_handler,
    build_snslist_handler,
    build_snslist_view_fn,
    build_sns_rule_deleter,
    build_sns_delete_handler,
    build_sns_buzz_handler,
    build_sns_clear_filter_handler,
    build_snsdel_callback_handler,
    build_snsaddok_callback_handler,
    build_snsfb_callback_handler,
)
from .quiz_command import (
    build_like_song_confirmation,
    build_quiz_callback_handler,
    build_quiz_handler,
    start_quiz_daily_scheduler,
)
from .voice_command import (
    build_generateaudio_handler,
    build_saynow_handler,
    build_voice_callback_handler,
    build_voice_handler,
)
from .fix_command import (
    FixPendingApplyCache,
    build_fix_callback_handler,
    build_fix_handler,
)
from .vpn_command import VpnConfigStore, VpnRotationScheduler, build_vpn_handler
from .research_command import (
    MercariItemAdapter,
    ResearchNotifier,
    build_ollama_entity_recognizer,
    build_ollama_sellable_unit_gate,
    build_research_handler,
    build_research_item_fetch_html,
)
from .item_condition import build_item_condition_assessor
from .natural_language import build_telegram_natural_language_router_from_settings
from .natural_language import fallback_route_openclaw_natural_language
from telegram_nl.natural_language import TelegramNaturalLanguageIntent
from .quiz_favorite_songs import extract_first_youtube_url
from .opportunity_command import (
    build_hunt_callback_handler,
    build_hunt_handler,
    build_huntlist_item_deleter,
    build_huntlist_view_fn,
)
from .reputation_agent import ensure_agent_thread
from .reputation_snapshot import (
    ReputationSnapshotResult,
    fetch_reputation_proof_document,
    request_reputation_snapshot,
)
from .web_search import (
    answer_page_with_ollama,
    build_web_fetch_answer,
    fetch_page_text,
    format_web_research_answer,
    web_search,
)

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
        return "已收到語音，正在本機轉成文字。"

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
        voice = message.get("voice")
        audio = message.get("audio")
        attachment = voice if isinstance(voice, dict) else audio
        if not isinstance(attachment, dict):
            return None
        if self._stt_transcriber is None:
            return None, "語音轉文字失敗：本機語音模型尚未設定。"

        file_id = attachment.get("file_id")
        if not isinstance(file_id, str) or not file_id.strip():
            return None, "語音轉文字失敗：Telegram 音訊缺少 file_id。"
        file_size = attachment.get("file_size")
        # Telegram marks file_size as optional. Use it as an early rejection
        # hint when present; download_file(max_bytes=...) remains the hard cap.
        if file_size is not None:
            if not isinstance(file_size, int) or isinstance(file_size, bool) or file_size <= 0:
                return None, "語音轉文字失敗：Telegram 音訊的 file_size 無效。"
            if file_size > self._stt_transcriber.max_audio_bytes:
                return None, (
                    "語音轉文字失敗："
                    f"音訊超過 {self._stt_transcriber.max_audio_bytes} bytes 上限。"
                )
        duration = attachment.get("duration")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < 0:
            return None, "語音轉文字失敗：Telegram 音訊缺少有效的 duration。"
        if duration > self._stt_transcriber.max_duration_seconds:
            return None, (
                "語音轉文字失敗："
                f"音訊長度超過 {self._stt_transcriber.max_duration_seconds} 秒上限。"
            )

        mime_type = attachment.get("mime_type")
        if not isinstance(mime_type, str) or not mime_type.strip():
            mime_type = "audio/ogg" if isinstance(voice, dict) else ""
        try:
            if mime_type:
                validate_audio_mime_type(mime_type)
            file_info = client.get_file(file_id=file_id)
            file_path = file_info.get("file_path") if isinstance(file_info, dict) else None
            if not isinstance(file_path, str) or not file_path:
                raise SttRequestError("Telegram 沒有回傳可下載的音訊路徑。")
            if not mime_type:
                file_name = attachment.get("file_name")
                guess_from = file_name if isinstance(file_name, str) and file_name else file_path
                mime_type = mimetypes.guess_type(guess_from)[0] or ""
                validate_audio_mime_type(mime_type)
            audio_bytes = client.download_file(
                file_path=file_path,
                max_bytes=self._stt_transcriber.max_audio_bytes,
            )
            request = build_audio_request(
                audio_bytes,
                mime_type=mime_type,
                max_audio_bytes=self._stt_transcriber.max_audio_bytes,
                language=getattr(self._settings, "openclaw_stt_language", None),
                trusted_duration_seconds=float(duration),
            )
            result = self._stt_transcriber.transcribe(request)
        except (SttPayloadTooLarge, SttRequestError, SttRuntimeError) as exc:
            return None, f"語音轉文字失敗：{exc}"
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("Telegram audio transcription failed: %s", exc)
            return None, "語音轉文字失敗：無法下載或處理 Telegram 音訊。"
        return result.transcript, None

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
        if self._goal_bridge is None and self._settings is not None:
            from .command_bridge import CommandBridge

            self._goal_bridge = CommandBridge(settings=self._settings)
        return self._goal_bridge

    def _goal_chat_backend(self) -> str:
        if self._settings is None:
            return "local"
        return default_chat_backend(self._settings)

    @staticmethod
    def _goal_conversation_id(chat_id: str | int) -> str:
        return f"telegram:{chat_id}"

    def _build_goal_bridge_request(self, text: str, chat_id: str | int):
        return parse_request(
            {
                "mode": "chat",
                "input": text,
                "conversation_id": self._goal_conversation_id(chat_id),
                "chat_backend": self._goal_chat_backend(),
                "source": "telegram",
            }
        )

    def _route_goal_loop_intent(self, text: str) -> TelegramNaturalLanguageIntent | None:
        bridge = self._get_goal_bridge()
        if bridge is None:
            return None
        try:
            plan, _metadata = bridge._select_chat_tool_plan(self._build_goal_bridge_request(text, "router"))
        except Exception:
            logger.exception("Telegram goal-loop router failed text=%s", trim_for_log(text, limit=240))
            return None
        if plan is None or plan.tool != "__goal__":
            return None
        return TelegramNaturalLanguageIntent(
            intent="execute_goal",
            workflow_description=plan.query,
            confidence=0.8,
        )

    def _execute_goal_bridge(self, text: str, chat_id: str | int) -> WebCommandResponse:
        bridge = self._get_goal_bridge()
        if bridge is None:
            return WebCommandResponse(status="error", message="goal bridge 尚未啟用。")
        return bridge.handle(self._build_goal_bridge_request(text, chat_id))

    def _run_goal_bridge(self, goal: str, chat_id: str | int) -> WebCommandResponse:
        bridge = self._get_goal_bridge()
        if bridge is None:
            return WebCommandResponse(status="error", message="goal bridge 尚未啟用。")
        req = self._build_goal_bridge_request(goal, chat_id)
        return bridge._run_goal_loop_blocking(req, goal, planner_metadata=None)

    def _execute_goal_bridge_reply(
        self,
        text: str,
        chat_id: str | int,
    ) -> tuple[str, dict[str, object] | None]:
        response = self._run_goal_bridge(text, chat_id)
        return response.message, self._goal_reply_markup(response)

    def _handle_goal_callback(
        self,
        payload: str,
        original_text: str,
        chat_id: str,
    ) -> tuple[object, str | None, object]:
        response = self._execute_goal_bridge(payload, chat_id)
        if response.status != STATUS_OK:
            return response.message, None, None
        return None, response.message, self._goal_reply_markup(response)

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
        try:
            client.answer_callback_query(
                callback_query_id=callback_id,
                text="收到，正在處理…",
            )
        except Exception:
            logger.exception("answer_callback_query failed for async goal callback chat_id=%s", chat_id)

        def _worker() -> None:
            try:
                response = self._execute_goal_bridge(payload, chat_id)
                reply_markup = self._goal_reply_markup(response) if response.status == STATUS_OK else None
                client.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=response.message,
                    reply_markup=reply_markup,
                )
            except Exception:
                logger.exception("async goal callback worker failed chat_id=%s payload=%s", chat_id, payload)
                try:
                    client.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=f"{original_text}\n\n⚠️ 目標執行失敗，請稍後再試。",
                        reply_markup=None,
                    )
                except Exception:
                    logger.exception("async goal callback fallback edit failed chat_id=%s", chat_id)

        threading.Thread(target=_worker, daemon=True).start()
        return True

    @staticmethod
    def _goal_reply_markup(response: WebCommandResponse) -> dict[str, object] | None:
        if not response.actions:
            return None
        rows = []
        for action in response.actions:
            if action.command != "chat" or not action.input:
                continue
            rows.append([{"text": action.label, "callback_data": f"goal:{action.input}"}])
        if not rows:
            return None
        return {"inline_keyboard": rows}


def default_lookup_renderer(settings: AssistantSettings) -> LookupRenderer:
    return _base_default_lookup_renderer(db_path=settings.monitor_db_path)


def default_photo_renderer(
    settings: AssistantSettings,
    *,
    research_renderer=None,
) -> PhotoLookupRenderer:
    return _base_default_photo_renderer(
        db_path=settings.monitor_db_path,
        tesseract_path=settings.openclaw_tesseract_path,
        tessdata_dir=settings.openclaw_tessdata_dir,
        vision_settings=TcgVisionSettings(
            backend=settings.openclaw_local_vision_backend or "",
            endpoint=settings.openclaw_local_vision_endpoint,
            model=settings.openclaw_local_vision_model,
            timeout_seconds=settings.openclaw_local_vision_timeout_seconds,
            ssl_context=build_ssl_context(settings),
        ),
        research_renderer=research_renderer,
    )


_IMAGE_TRANSLATE_CAPTION_TOKENS = ("翻譯", "翻訳", "translate", "ocr")


def _caption_requests_image_translation(caption: "str | None") -> bool:
    """Closed-token routing check used by the renderer. The user-facing,
    open-world recognition lives in the embedding recognizer
    (build_image_translate_caption_recognizer); by the time a caption reaches the
    renderer it is either the canonical「翻譯」token (menu / dispatch-canonicalized)
    or a literal keyword, so a small fixed token set is enough here."""
    if not caption:
        return False
    lowered = caption.strip().lower()
    return any(token in lowered for token in _IMAGE_TRANSLATE_CAPTION_TOKENS)


class _ImageTranslateOriginalCache:
    """Server-side store for the OCR原文 revealed by the 顯示原文 button.

    Telegram callback_data is capped at 64 bytes — far too small for full OCR
    text — so the原文 is stashed under a short token and only the token rides in
    the button. Mirrors _ResearchReplyCache: TTL + max-entries prune, chat_id
    verified on read."""

    def __init__(self, *, max_entries: int = 128, ttl_seconds: int = 3600) -> None:
        self._max_entries = max(8, max_entries)
        self._ttl_seconds = max(60, ttl_seconds)
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[str, float, str]] = {}

    def put(self, *, chat_id: str, ocr_text: str) -> str:
        token = uuid.uuid4().hex[:8]
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            self._entries[token] = (chat_id, now, ocr_text)
            while len(self._entries) > self._max_entries:
                self._entries.pop(next(iter(self._entries)), None)
        return token

    def get(self, *, token: str, chat_id: str) -> "str | None":
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            entry = self._entries.get(token)
            if entry is None:
                return None
            stored_chat_id, _created_at, ocr_text = entry
            if stored_chat_id != chat_id:
                return None
            return ocr_text

    def _prune_locked(self, now: float) -> None:
        expired = [
            token
            for token, (_chat_id, created_at, _ocr) in self._entries.items()
            if now - created_at > self._ttl_seconds
        ]
        for token in expired:
            self._entries.pop(token, None)


_IMAGE_TRANSLATE_ORIGINAL_CACHE = _ImageTranslateOriginalCache()


def _build_image_translate_reply_markup(token: str) -> dict[str, object]:
    return {"inline_keyboard": [[{"text": "顯示原文", "callback_data": f"imgtr:{token}"}]]}


def _build_image_translate_callback_handler(
    cache: _ImageTranslateOriginalCache,
) -> "Callable[[str, str, str], tuple[object, str | None, object]]":
    def handler(payload: str, original_text: str, chat_id: str) -> tuple[object, str | None, object]:
        token = (payload or "").partition(":")[0]
        ocr_text = cache.get(token=token, chat_id=str(chat_id))
        if ocr_text is None:
            return "原文已過期，請重新傳圖片翻譯。", None, None
        return "已顯示原文", f"{original_text}\n\n【原文】\n{ocr_text}", None

    return handler


def build_photo_renderer(
    settings: AssistantSettings,
    *,
    research_renderer=None,
) -> PhotoLookupRenderer:
    """Compose the existing TCG card-price renderer with the image OCR+translate
    renderer, dispatching by caption: a 翻譯/translate caption routes to OCR +
    Traditional-Chinese translation, everything else keeps card-price behavior.

    Translation is shown by default; the OCR原文 is cached and surfaced behind a
    顯示原文 button so the message stays short."""
    base_renderer = default_photo_renderer(settings, research_renderer=research_renderer)
    translate_renderer = build_image_ocr_translate_renderer_from_settings(settings)

    def render(query: TelegramPhotoQuery):
        if translate_renderer is not None and _caption_requests_image_translation(query.caption):
            result = translate_renderer(query.image_path, query.caption)
            if not result.ok:
                return result.message
            token = _IMAGE_TRANSLATE_ORIGINAL_CACHE.put(
                chat_id=str(query.chat_id), ocr_text=result.ocr_text
            )
            text = (
                f"🌐→🇹🇼 圖片文字翻譯（偵測語言：{result.source_language}）\n\n"
                f"{result.translation}"
            )
            return PhotoLookupReply(
                text=text,
                reply_markup=_build_image_translate_reply_markup(token),
            )
        return base_renderer(query)

    return render


# (action_key, button prompt, synthetic_caption). Order is the menu order the
# user sees; translation first, then per-game card/box price lookups. The
# synthetic_caption is what _execute_pending_photo_lookup feeds back into the
# photo renderer once a button is tapped — "翻譯" routes to OCR+translate, the
# "/scan <game>" captions route to the card-price pipeline. Box vs single-card
# is keyed off action_key (=="pokemon_box_price") downstream, not the caption.
_PHOTO_MENU_OPTIONS: tuple[tuple[str, str, str], ...] = (
    ("ocr_translate", "翻譯繁體中文", "翻譯"),
    ("pokemon_card_price", "查市價 — 寶可夢單卡", "/scan pokemon"),
    ("pokemon_box_price", "查市價 — 寶可夢卡盒", "/scan pokemon"),
    ("yugioh_card_price", "查市價 — 遊戲王單卡", "/scan yugioh"),
    ("ws_card_price", "查市價 — Weiss Schwarz 單卡", "/scan ws"),
    ("union_arena_card_price", "查市價 — Union Arena 單卡", "/scan union_arena"),
)


def default_photo_intent_analyzer(settings: AssistantSettings):
    """Return a fixed full action menu for every photo WITHOUT reading the image.

    The user wants every option listed up-front rather than the bot guessing
    intent from a vision/OCR parse, so this skips image analysis entirely and is
    effectively instant. The actual image is only read later, after the user taps
    a button (the chosen option's synthetic_caption drives the real lookup via the
    existing popt-callback + _execute_pending_photo_lookup path)."""
    options = tuple(
        TelegramPhotoIntentOption(
            option_number=index + 1,
            action_key=action_key,
            prompt=prompt,
            synthetic_caption=caption,
        )
        for index, (action_key, prompt, caption) in enumerate(_PHOTO_MENU_OPTIONS)
    )

    def analyze(query: TelegramPhotoQuery) -> TelegramPhotoIntentAnalysis:
        return TelegramPhotoIntentAnalysis(options=options)

    return analyze


def default_board_loader(settings: AssistantSettings | None = None) -> tuple:
    return _base_default_board_loader(ssl_context=build_ssl_context(settings) if settings else None)


def default_reputation_renderer(settings: AssistantSettings) -> ReputationRenderer:
    def render(query: TelegramReputationQuery) -> TelegramReputationDelivery:
        logger.info("Telegram reputation snapshot requested query_url=%s", trim_for_log(query.query_url, limit=240))
        thread, started_now = ensure_agent_thread(
            server_url=settings.reputation_agent_server_url,
            api_key=settings.reputation_agent_admin_token or "",
            poll_secs=settings.reputation_agent_poll_secs,
        )
        logger.info(
            "Telegram reputation agent ready started_now=%s thread_name=%s alive=%s",
            started_now,
            thread.name,
            thread.is_alive(),
        )
        result = request_reputation_snapshot(settings=settings, query_url=query.query_url)
        logger.info(
            "Telegram reputation snapshot completed query_url=%s proof_id=%s reused=%s",
            trim_for_log(query.query_url, limit=240),
            result.proof_id,
            result.reused,
        )
        proof_document = None
        if result.proof_id is not None:
            try:
                proof_document = fetch_reputation_proof_document(settings=settings, proof_id=result.proof_id)
            except Exception:
                logger.exception("Telegram reputation proof fetch failed proof_id=%s", result.proof_id)
        pdf_path, preview_path = render_reputation_snapshot_artifacts(settings=settings, result=result)
        return TelegramReputationDelivery(
            summary_text=format_reputation_snapshot_delivery_text(result, proof_document),
            attachments=(
                TelegramFileAttachment(kind="document", path=pdf_path, caption="Reputation snapshot PDF"),
                TelegramFileAttachment(kind="photo", path=preview_path, caption="Reputation snapshot preview"),
            ),
            cleanup_paths=(pdf_path, preview_path),
        )

    return render


def default_web_fetch_renderer(settings: AssistantSettings) -> "Callable[[str, str], str]":
    """Item 3: WebFetch equivalent — read one URL and answer a focused prompt."""
    backend = (settings.openclaw_local_text_backend or "").strip().lower()
    endpoint = settings.openclaw_local_text_endpoint
    model = _select_text_generation_model(settings)
    timeout = max(1, settings.openclaw_local_text_timeout_seconds)
    ssl_ctx = build_ssl_context(settings) if endpoint.startswith("https://") else None

    def render(url: str, prompt: str) -> str:
        if backend != "ollama" or not endpoint or not model:
            return _LLM_NOT_CONFIGURED_MESSAGE
        answer = build_web_fetch_answer(
            url,
            prompt,
            fetch_page_fn=lambda u: fetch_page_text(
                u,
                ssl_context=ssl_ctx,
                enable_browser_fallback=True,
            ),
            answer_fn=lambda u, p, content: answer_page_with_ollama(
                u,
                p,
                content,
                endpoint=endpoint,
                model=model,
                timeout_seconds=timeout,
                ssl_context=ssl_ctx,
            ),
        )
        return format_web_research_answer(answer)

    return render


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


def _call_local_text_model(
    *,
    endpoint: str,
    model: str,
    prompt: str,
    timeout_seconds: int,
    ssl_context,
) -> str:
    request_payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {"temperature": 0.2},
    }
    request = Request(
        f"{endpoint.rstrip('/')}/api/generate",
        data=json.dumps(request_payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds, context=ssl_context) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"翻譯 LLM HTTP {exc.code}.") from exc
    except URLError as exc:
        raise RuntimeError(f"翻譯 LLM request failed: {exc.reason}") from exc
    payload = json.loads(raw)
    result = payload.get("response", "")
    if not isinstance(result, str):
        raise RuntimeError(f"翻譯 LLM response type was {type(result).__name__}.")
    return result.strip()


def build_translate_handler(settings: AssistantSettings, *, target: str) -> Callable[[str, str], str]:
    backend = (settings.openclaw_local_text_backend or "").strip().lower()
    endpoint = settings.openclaw_local_text_endpoint
    model = _select_text_generation_model(settings)
    timeout = max(1, settings.openclaw_local_text_timeout_seconds)
    ssl_ctx = build_ssl_context(settings) if endpoint.startswith("https://") else None
    target = target.strip().lower()
    if target == "ja":
        usage = "用法：/translateja <要翻成日文的文字>"
        instruction = (
            "將下列文字翻譯成自然、通順的日文。"
            "只輸出譯文，不要解說，不要加引號，不要加前綴。"
            "保留 URL、專有名詞、產品名；必要時只做最自然的日文化。"
        )
    else:
        usage = "用法：/translatezh <要翻成繁體中文的文字>"
        instruction = (
            "將下列文字翻譯成自然、通順的繁體中文（台灣用語）。"
            "只輸出譯文，不要解說，不要加引號，不要加前綴。"
            "保留 URL、專有名詞、產品名。"
        )

    def handler(remainder: str, chat_id: str) -> str:
        text = (remainder or "").strip()
        if not text:
            return usage
        if backend != "ollama" or not endpoint or not model:
            return _TRANSLATE_NOT_CONFIGURED_MESSAGE
        prompt = f"{instruction}\n\n原文：\n{text}\n\n譯文："
        translated = _call_local_text_model(
            endpoint=endpoint,
            model=model,
            prompt=prompt,
            timeout_seconds=timeout,
            ssl_context=ssl_ctx,
        ).strip()
        return translated or "本地模型沒有回傳可用譯文。"

    return handler


def render_reputation_snapshot_artifacts(
    *,
    settings: AssistantSettings,
    result: ReputationSnapshotResult,
) -> tuple[Path, Path]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment-dependent.
        raise RuntimeError("playwright is not installed — run: pip install playwright && playwright install chromium") from exc

    proof_id = result.proof_id or f"proof_{uuid.uuid4().hex[:12]}"
    temp_root = Path.cwd() / ".openclaw_tmp" / "reputation_snapshot"
    temp_root.mkdir(parents=True, exist_ok=True)
    pdf_path = temp_root / f"{proof_id}.pdf"
    preview_path = temp_root / f"{proof_id}.png"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**_chromium_launch_options())
        context = browser.new_context(
            locale="ja-JP",
            viewport={"width": 1400, "height": 1800},
            ignore_https_errors=settings.openclaw_tls_insecure_skip_verify,
        )
        page = context.new_page()
        page.goto(result.proof_url, wait_until="networkidle", timeout=60000)
        page.emulate_media(media="screen")
        page.pdf(
            path=str(pdf_path),
            format="A4",
            print_background=True,
            margin={"top": "12mm", "right": "10mm", "bottom": "12mm", "left": "10mm"},
        )
        page.screenshot(path=str(preview_path), full_page=False)
        context.close()
        browser.close()

    return pdf_path, preview_path


def _chromium_launch_options() -> dict[str, object]:
    options: dict[str, object] = {"headless": True}
    executable_path = _resolve_chromium_executable()
    if executable_path:
        options["executable_path"] = executable_path
    return options


def _resolve_chromium_executable() -> str | None:
    configured = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    if configured:
        return configured
    for candidate in ("chromium", "chromium-browser", "google-chrome-stable"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def format_reputation_snapshot_result(result: ReputationSnapshotResult) -> str:
    action_text = "沿用既有快照" if result.reused else "已建立新快照"
    lines = [
        "信譽快照已就緒",
        action_text,
        result.proof_url,
    ]
    if result.proof_id:
        lines.insert(2, f"proof_id: {result.proof_id}")
    return "\n".join(lines)


def format_reputation_snapshot_delivery_text(
    result: ReputationSnapshotResult,
    proof_document: dict[str, object] | None,
) -> str:
    action_text = "沿用既有快照" if result.reused else "已建立新快照"
    subject = proof_document.get("subject", {}) if isinstance(proof_document, dict) else {}
    metrics = proof_document.get("metrics", {}) if isinstance(proof_document, dict) else {}
    quality = proof_document.get("quality", {}) if isinstance(proof_document, dict) else {}

    display_name = subject.get("display_name") if isinstance(subject, dict) else None
    captured_at = proof_document.get("captured_at") if isinstance(proof_document, dict) else None
    total_reviews = metrics.get("total_reviews") if isinstance(metrics, dict) else None
    listing_count = metrics.get("listing_count") if isinstance(metrics, dict) else None
    followers_count = metrics.get("followers_count") if isinstance(metrics, dict) else None
    following_count = metrics.get("following_count") if isinstance(metrics, dict) else None
    as_seller = quality.get("as_seller") if isinstance(quality, dict) else None
    as_buyer = quality.get("as_buyer") if isinstance(quality, dict) else None
    overall = quality.get("overall") if isinstance(quality, dict) else None

    lines = ["信譽快照已就緒", action_text]
    if display_name:
        lines.append(f"賣家：{display_name}")

    # Overall review count (from metrics, covers full history)
    meta_bits = []
    if total_reviews is not None:
        meta_bits.append(f"評價 {total_reviews}")
    if listing_count is not None:
        meta_bits.append(f"刊登 {listing_count}")
    if followers_count is not None:
        meta_bits.append(f"追蹤者 {followers_count}")
    if following_count is not None:
        meta_bits.append(f"追蹤中 {following_count}")
    if meta_bits:
        lines.append(" / ".join(meta_bits))

    # Buyer / seller breakdown (from quality, based on captured review entries)
    if isinstance(as_seller, dict) and (as_seller.get("positive") or as_seller.get("negative")):
        pos = as_seller.get("positive") or 0
        neg = as_seller.get("negative") or 0
        rate = as_seller.get("rate")
        rate_str = f"，好評率 {rate}%" if rate is not None else ""
        lines.append(f"身為賣家：好評 {pos} / 差評 {neg}{rate_str}")
    if isinstance(as_buyer, dict) and (as_buyer.get("positive") or as_buyer.get("negative")):
        pos = as_buyer.get("positive") or 0
        neg = as_buyer.get("negative") or 0
        rate = as_buyer.get("rate")
        rate_str = f"，好評率 {rate}%" if rate is not None else ""
        lines.append(f"身為買家：好評 {pos} / 差評 {neg}{rate_str}")
    elif isinstance(overall, dict) and as_seller is None and as_buyer is None:
        # Fallback: only overall quality, no role breakdown available
        rate = overall.get("rate")
        if rate is not None:
            lines.append(f"整體好評率：{rate}%")

    if captured_at:
        lines.append(f"快照時間：{captured_at}")
    if result.proof_id:
        lines.append(f"proof_id: {result.proof_id}")
    lines.append("已附上 PDF 與預覽圖，可直接在手機查看。")
    lines.append(result.proof_url)
    return "\n".join(lines)


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


def _start_rag_daily_digest(settings) -> RagDailyDigestScheduler | None:
    """Start the daily RAG digest daemon (fires at 22:00 local time)."""
    from telegram_core.transport import TelegramBotClient
    chat_ids = tuple(cid for cid in settings.openclaw_telegram_chat_ids if cid)
    if not chat_ids:
        logger.warning("_start_rag_daily_digest: no chat_ids configured — skipping")
        return None
    try:
        token = require_telegram_token(settings)
        ssl_ctx = build_ssl_context(settings)
        client = TelegramBotClient(token, ssl_context=ssl_ctx)

        def _send(chat_id: str, text: str, reply_markup: dict | None) -> None:
            client.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)

        scheduler = RagDailyDigestScheduler(
            db_path=settings.knowledge_db_path,
            chat_ids=chat_ids,
            send_fn=_send,
            signal_db_path=(
                settings.collectible_signal_db_path
                if settings.collectible_signal_store_enabled
                else None
            ),
        )
        scheduler.start()
        return scheduler
    except Exception:
        logger.exception("_start_rag_daily_digest: failed to start")
        return None


def _start_home_schedule_scheduler(settings, command_handlers) -> HomeScheduleScheduler | None:
    """Start the /schedulehome daemon (issue #39): fires due home schedules at
    minute resolution, re-dispatching their stored slash commands through the
    same command registry the bot uses. Results are reported back to Telegram."""
    from telegram_core.transport import TelegramBotClient

    chat_ids = tuple(cid for cid in settings.openclaw_telegram_chat_ids if cid)
    try:
        store = get_home_schedule_store(settings.openclaw_home_schedules_path)
        run_command = make_run_slash_command(command_handlers)
        notify = None
        if chat_ids:
            token = require_telegram_token(settings)
            client = TelegramBotClient(token, ssl_context=build_ssl_context(settings))

            def notify(text: str) -> None:  # noqa: F811 - intentional conditional def
                for cid in chat_ids:
                    client.send_message(chat_id=cid, text=text, reply_markup=None)

        # Single-user/local: scheduled commands deliver to the first configured
        # chat (e.g. /generateaudio sends its generated audio file there).
        scheduler_chat_id = chat_ids[0] if chat_ids else ""
        scheduler = HomeScheduleScheduler(
            store=store,
            run_command=run_command,
            chat_id=scheduler_chat_id,
            notify=notify,
        )
        scheduler.start()
        return scheduler
    except Exception:
        logger.exception("_start_home_schedule_scheduler: failed to start")
        return None


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


def _bootstrap_inboxes(settings):
    """Create and bootstrap the sns_inbox and knowledge_inbox for the telegram process.

    Telegram is the *producer*; sns_monitor service is the consumer.
    Returns (SnsInbox, KnowledgeInbox).
    """
    from sns_monitor.inbox import SnsInbox
    from .knowledge_inbox import KnowledgeInbox
    sns_inbox = SnsInbox(settings.sns_inbox_db_path)
    sns_inbox.bootstrap()
    knowledge_inbox = KnowledgeInbox(settings.knowledge_inbox_db_path)
    knowledge_inbox.bootstrap()
    logger.info(
        "telegram: inboxes bootstrapped sns=%s knowledge=%s",
        settings.sns_inbox_db_path, settings.knowledge_inbox_db_path,
    )
    return sns_inbox, knowledge_inbox


def _bootstrap_watch_inbox(settings):
    """Create and bootstrap the watch_inbox for the telegram process.

    Telegram is the *producer*; price_monitor service is the consumer.
    Returns WatchInbox.
    """
    from .watch_inbox import WatchInbox
    inbox = WatchInbox(settings.watch_inbox_db_path)
    inbox.bootstrap()
    logger.info("telegram: watch inbox bootstrapped path=%s", settings.watch_inbox_db_path)
    return inbox


def _bootstrap_opportunity_inbox(settings):
    """Create and bootstrap the opportunity_inbox for the telegram process.

    Telegram is the *producer*; opportunity_agent service is the consumer.
    Returns OpportunityInbox.
    """
    from .opportunity_inbox import OpportunityInbox
    inbox = OpportunityInbox(settings.opportunity_inbox_db_path)
    inbox.bootstrap()
    logger.info("telegram: opportunity inbox bootstrapped path=%s", settings.opportunity_inbox_db_path)
    return inbox


def _start_backup_scheduler(settings) -> None:
    """Start the daily auto-backup daemon (fires at 23:00 local time)."""
    data_dir = Path(settings.monitor_db_path).resolve().parent
    project_root = data_dir.parent
    generated_tools_dir = project_root / "generated_tools"
    dest = Path(settings.openclaw_backup_dir)
    hour = getattr(settings, "openclaw_backup_hour", 23)
    scheduler = BackupScheduler(
        data_dir=data_dir,
        generated_tools_dir=generated_tools_dir if generated_tools_dir.is_dir() else None,
        dest=dest,
        hour=hour,
        notify=_build_backup_notify(settings),
    )
    scheduler.start()


def _build_backup_notify(settings):
    """Telegram send callback for scheduled-backup reports; None → log-only."""
    chat_ids = tuple(cid for cid in settings.openclaw_telegram_chat_ids if cid)
    if not chat_ids:
        logger.warning("_build_backup_notify: no chat_ids configured — backup runs silent")
        return None
    try:
        from telegram_core.transport import TelegramBotClient
        token = require_telegram_token(settings)
        client = TelegramBotClient(token, ssl_context=build_ssl_context(settings))
    except Exception:
        logger.exception("_build_backup_notify: notify client unavailable — backup runs silent")
        return None

    def _notify(text: str) -> None:
        for chat_id in chat_ids:
            client.send_message(chat_id=chat_id, text=text)

    return _notify


def _start_title_corpus_rebuilder(settings) -> None:
    """Weekly: rebuild the comp-filter IDF table from the passive title corpus
    without noisy Telegram notices from the always-on bot runtime. Reads only
    locally cached titles — zero new external queries (Rule C7)."""
    try:
        from .title_corpus_rebuilder import TitleCorpusRebuilder
    except Exception:
        logger.exception("_start_title_corpus_rebuilder: import failed — skipping")
        return
    TitleCorpusRebuilder(notify_fn=lambda _text: None, notify_enabled=False).start()


def _start_card_image_crawler(watch_db: MonitorDatabase):
    """Kick off the trend-driven perceptual-hash crawler in the background.
    Pulls Snkrdunk's hot products every 6 hours and pre-populates
    `card_image_fingerprints` so user photo uploads of popular boxes/cards
    can short-circuit the slow OCR + vision LLM pipeline.

    Best-effort: if the price_monitor_bot package isn't importable or any
    other init issue arises, the bot keeps running without proactive
    fingerprinting (the lookup-time persist path still learns over time)."""
    try:
        from tcg_tracker.image_crawler import CardImageCrawler, CardImageCrawlMonitor
    except Exception as exc:
        logger.warning("card image crawler unavailable: %s", exc)
        return None
    try:
        crawler = CardImageCrawler(
            database=watch_db,
            games=("pokemon", "ws", "union_arena"),
            per_game_limit=30,
        )
        monitor = CardImageCrawlMonitor(
            crawler=crawler,
            interval_seconds=6 * 3600,   # every 6 hours
            initial_delay_seconds=120,    # let the rest of the bot finish booting
        )
        monitor.start()
        return monitor
    except Exception as exc:
        logger.warning("card image crawler failed to start: %s", exc)
        return None


def _bootstrap_watch_db(settings: AssistantSettings) -> MonitorDatabase:
    db = MonitorDatabase(settings.monitor_db_path)
    db.bootstrap()
    return db


def _start_watch_monitor(
    *,
    settings: AssistantSettings,
    watch_db: MonitorDatabase,
    token: str,
) -> None:
    chat_id = settings.openclaw_telegram_chat_id
    if not chat_id:
        logger.warning("Mercari watch monitor: no OPENCLAW_TELEGRAM_CHAT_ID set, notifications will be skipped")

    ssl_ctx = build_ssl_context(settings)

    def notify(notification_chat_id: str, text: str) -> None:
        resolved_chat = notification_chat_id if notification_chat_id and notification_chat_id != "dashboard" else chat_id
        if not resolved_chat:
            logger.warning("Mercari watch notify: no chat_id, dropping message")
            return
        if not text or not text.strip():
            return
        from .outbound_guards import guard_outbound
        reason = guard_outbound(text, proactive=True)
        if reason:
            logger.warning("outbound guard blocked push: %s", reason)
            return
        client = TelegramBotClient(token, ssl_context=ssl_ctx)
        client.send_message(chat_id=resolved_chat, text=text)

    def do_snapshot(notification_chat_id: str, urls: list[str]) -> None:
        resolved_chat = notification_chat_id if notification_chat_id and notification_chat_id != "dashboard" else chat_id
        if not resolved_chat:
            logger.warning("Auto-snapshot: no chat_id, skipping")
            return
        bot_client = TelegramBotClient(token, ssl_context=ssl_ctx)
        try:
            bot_client.send_message(
                chat_id=resolved_chat,
                text=f"正在為 {len(urls)} 筆新商品建立賣家信譽快照，請稍候…",
            )
        except Exception:
            logger.warning("Auto-snapshot: failed to send ack message")

        def _run() -> None:
            for url in urls:
                try:
                    result = request_reputation_snapshot(settings=settings, query_url=url)
                    proof_document = None
                    if result.proof_id:
                        try:
                            proof_document = fetch_reputation_proof_document(
                                settings=settings, proof_id=result.proof_id
                            )
                        except Exception:
                            logger.exception("Auto-snapshot: proof fetch failed proof_id=%s", result.proof_id)
                    pdf_path, preview_path = render_reputation_snapshot_artifacts(
                        settings=settings, result=result
                    )
                    summary = format_reputation_snapshot_delivery_text(result, proof_document)
                    from .outbound_guards import guard_outbound
                    _snap_reason = guard_outbound(summary, proactive=True)
                    if _snap_reason:
                        logger.warning("outbound guard blocked push: %s", _snap_reason)
                    else:
                        c = TelegramBotClient(token, ssl_context=ssl_ctx)
                        c.send_message(chat_id=resolved_chat, text=summary)
                        c.send_document(
                            chat_id=resolved_chat,
                            document_path=pdf_path,
                            caption="信譽快照 PDF",
                        )
                        c.send_photo(
                            chat_id=resolved_chat,
                            photo_path=preview_path,
                            caption="信譽快照預覽",
                        )
                    for p in (pdf_path, preview_path):
                        try:
                            p.unlink()
                        except Exception:
                            pass
                    logger.info("Auto-snapshot: completed url=%s proof_id=%s", url, result.proof_id)
                except Exception:
                    logger.exception("Auto-snapshot: failed url=%s", url)

        threading.Thread(target=_run, name="auto-snapshot", daemon=True).start()

    monitor, started = _ensure_watch_monitor(
        db_path=watch_db.path,
        notify_fn=notify,
        snapshot_fn=do_snapshot,
        interval_seconds=60,
    )
    logger.info("Mercari watch monitor started=%s running=%s", started, monitor.is_running())
    if started:
        print("[watch-monitor] Mercari watch monitor started (interval=60s)")


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
