"""Telegram bot orchestration — bridges AssistantSettings to price_monitor_bot.bot."""

from __future__ import annotations

import logging
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

import price_monitor_bot.bot as _price_bot_module
from assistant_runtime import AssistantSettings, build_ssl_context
from assistant_runtime.logging_utils import trim_for_log

from market_monitor.storage import MonitorDatabase
from price_monitor_bot.bot import (  # noqa: F401
    BoardLoader,
    CatalogRenderer,
    LookupRenderer,
    PhotoLookupRenderer,
    PhotoLookupReply,
    ResearchRenderer,
    ReputationRenderer,
    TelegramBotClient,
    TelegramFileAttachment,
    TelegramLookupQuery,
    TelegramPhotoIntentAnalysis,
    TelegramPhotoIntentOption,
    TelegramPhotoQuery,
    TelegramResearchQuery,
    TelegramReputationDelivery,
    TelegramReputationQuery,
    TelegramTextReplyPlan,
    RegisteredCommand,
    build_processing_ack,
    default_board_loader as _base_default_board_loader,
    default_lookup_renderer as _base_default_lookup_renderer,
    default_photo_renderer as _base_default_photo_renderer,
    format_liquidity_board,
    format_photo_lookup_result,
    handle_telegram_message,
    parse_lookup_command,
    parse_reputation_snapshot_command,
    run_telegram_polling as _base_run_telegram_polling,
    send_telegram_test_message as _base_send_telegram_test_message,
    TelegramCommandProcessor as _BaseTelegramCommandProcessor,
)
from price_monitor_bot.watch_monitor import ensure_monitor as _ensure_watch_monitor
from tcg_tracker.image_lookup import TcgVisionSettings

from .backup_command import BackupScheduler, build_backup_handler, build_recover_handler
from .opportunity_scorecard import build_scorecard_handler
from .rag_daily_digest import RagDailyDigestScheduler, handle_ragdel_callback, handle_ragkeep_callback
from .dynamic_tools import (
    CloudBackendUnavailable,
    build_dynamic_tool_runner_from_settings,
    build_research_cloud_text_client,
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
from .music_command import build_music_handler
from .sns_commands import (
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
    build_say_handler,
    build_voice_callback_handler,
    build_voice_handler,
)
from .research_command import (
    MercariItemAdapter,
    ResearchNotifier,
    ResearchReport,
    SellerReputationSnapshot,
    build_appreciation_enricher,
    build_ollama_entity_recognizer,
    build_ollama_sellable_unit_gate,
    build_research_handler,
    build_research_item_fetch_html,
    format_research_compact_report,
    format_research_detail_report,
    _build_seller_snapshot_section_result,
)
from .natural_language import build_telegram_natural_language_router_from_settings
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
    SnapshotStillPending,
    fetch_reputation_proof_document,
    request_reputation_snapshot,
)
from .web_search import (
    DEFAULT_WEB_SEARCH_LIMIT,
    _build_summary_prompt,
    answer_page_with_ollama,
    build_web_fetch_answer,
    build_web_research_answer,
    fetch_page_text,
    filter_relevant_sources_with_ollama,
    format_web_research_answer,
    reformulate_queries_with_ollama,
    summarize_web_sources_with_ollama,
    web_search,
)
from price_monitor_bot.bot import TelegramTextReplyPlan

logger = logging.getLogger(__name__)

PRICE_LOOKUP_COMMANDS = {"/lookup", "/price"}
TREND_BOARD_COMMANDS = {"/trend", "/trending", "/hot", "/heat", "/liquidity"}
PHOTO_SCAN_COMMANDS = {"/scan", "/image", "/photo"}
REPUTATION_SNAPSHOT_COMMANDS = {"/snapshot", "/proof", "/repcheck", "/reputation"}
HEAVY_COMMANDS = PRICE_LOOKUP_COMMANDS | TREND_BOARD_COMMANDS | REPUTATION_SNAPSHOT_COMMANDS


def _run_research_worker_call(func: Callable[[], object]) -> object:
    result_box: dict[str, object] = {}
    error_box: dict[str, BaseException] = {}
    done = threading.Event()

    def runner() -> None:
        try:
            result_box["value"] = func()
        except BaseException as exc:  # pragma: no cover - re-raised to caller
            error_box["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    done.wait()
    if "error" in error_box:
        raise error_box["error"]
    return result_box.get("value")


class _ResearchReplyCache:
    def __init__(self, *, max_entries: int = 128, ttl_seconds: int = 3600) -> None:
        self._max_entries = max(8, max_entries)
        self._ttl_seconds = max(60, ttl_seconds)
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[str, float, ResearchReport]] = {}

    def put(self, report: ResearchReport) -> str:
        token = uuid.uuid4().hex[:8]
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            self._entries[token] = (report.chat_id, now, report)
            while len(self._entries) > self._max_entries:
                oldest_token = next(iter(self._entries))
                self._entries.pop(oldest_token, None)
        return token

    def get(self, *, token: str, chat_id: str) -> ResearchReport | None:
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            entry = self._entries.get(token)
            if entry is None:
                return None
            stored_chat_id, _created_at, report = entry
            if stored_chat_id != chat_id:
                return None
            return report

    def _prune_locked(self, now: float) -> None:
        expired = [
            token
            for token, (_chat_id, created_at, _report) in self._entries.items()
            if now - created_at > self._ttl_seconds
        ]
        for token in expired:
            self._entries.pop(token, None)


def _build_research_reply_markup(token: str) -> dict[str, object]:
    return {
        "inline_keyboard": [
            [
                {"text": "摘要", "callback_data": f"rs:{token}:summary"},
                {"text": "看市價", "callback_data": f"rs:{token}:price"},
            ],
            [
                {"text": "看賣家", "callback_data": f"rs:{token}:seller"},
                {"text": "看來源", "callback_data": f"rs:{token}:sources"},
            ],
            [
                {"text": "看警告", "callback_data": f"rs:{token}:warnings"},
            ],
        ]
    }


def _build_research_reply_formatter(
    cache: _ResearchReplyCache,
) -> "Callable[[ResearchReport], tuple[str, dict[str, object]]]":
    def render(report: ResearchReport) -> tuple[str, dict[str, object]]:
        token = cache.put(report)
        return format_research_compact_report(report), _build_research_reply_markup(token)

    return render


def _build_research_callback_handler(
    cache: _ResearchReplyCache,
) -> "Callable[[str, str, str], tuple[object, str | None, object]]":
    def handler(payload: str, original_text: str, chat_id: str) -> tuple[object, str | None, object]:
        token, _, view = (payload or "").partition(":")
        report = cache.get(token=token, chat_id=str(chat_id))
        if report is None:
            return "研究結果已過期，請重新執行 /research。", None, None
        detail_text = format_research_detail_report(report, view=view or "summary")
        return "已切換研究視圖", detail_text, _build_research_reply_markup(token)

    return handler


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
        **kwargs,
    ) -> None:
        self._settings = settings
        if allowed_chat_ids is None and settings is not None and settings.openclaw_telegram_chat_id:
            allowed_chat_ids = frozenset({settings.openclaw_telegram_chat_id})
        super().__init__(allowed_chat_ids=allowed_chat_ids, **kwargs)

    def _help_text(self) -> str:
        return _build_openclaw_help_text()

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

    def build_reply_plan(self, *, chat_id: str | int, text: str | None) -> TelegramTextReplyPlan:
        youtube_plan = self._build_youtube_like_song_plan(chat_id=chat_id, text=text)
        if youtube_plan is not None:
            return youtube_plan
        translate_plan = self._build_auto_translate_plan(chat_id=chat_id, text=text)
        if translate_plan is not None:
            return translate_plan
        return super().build_reply_plan(chat_id=chat_id, text=text)


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


_LLM_NOT_CONFIGURED_MESSAGE = (
    "網路搜尋摘要功能已可使用，但本地文字 LLM 尚未設定。"
    "請設定 OPENCLAW_LOCAL_TEXT_BACKEND=ollama 與 OPENCLAW_LOCAL_TEXT_MODEL。"
)

_TRANSLATE_NOT_CONFIGURED_MESSAGE = (
    "翻譯功能尚未啟用。請設定 OPENCLAW_LOCAL_TEXT_BACKEND=ollama 與 "
    "OPENCLAW_LOCAL_TEXT_MODEL。"
)


def default_web_research_renderer(settings: AssistantSettings) -> ResearchRenderer:
    backend = (settings.openclaw_local_text_backend or "").strip().lower()
    endpoint = settings.openclaw_local_text_endpoint
    model = _select_text_generation_model(settings)
    timeout = max(1, settings.openclaw_local_text_timeout_seconds)
    # The grounded summarise step reads several fetched pages, so it is the
    # slowest LLM call in the pipeline. Give it extra headroom to survive
    # Ollama queue contention from background workers (entity researcher etc.).
    summarize_timeout = max(timeout, 120)
    ssl_ctx = build_ssl_context(settings) if endpoint.startswith("https://") else None

    def render(query: TelegramResearchQuery) -> str:
        if backend != "ollama" or not endpoint or not model:
            return _LLM_NOT_CONFIGURED_MESSAGE
        answer = build_web_research_answer(
            query.query,
            max_results=DEFAULT_WEB_SEARCH_LIMIT,
            search_fn=lambda q, limit: web_search(q, max_results=limit),
            # Item 4: turn the question into a few focused search queries first.
            reformulate_fn=lambda q: reformulate_queries_with_ollama(
                q,
                endpoint=endpoint,
                model=model,
                timeout_seconds=timeout,
                ssl_context=ssl_ctx,
            ),
            # Drop off-topic SEO hits before they reach the summary / source list.
            relevance_fn=lambda q, sources: filter_relevant_sources_with_ollama(
                q,
                sources,
                endpoint=endpoint,
                model=model,
                timeout_seconds=timeout,
                ssl_context=ssl_ctx,
            ),
            # Item 1: download the top results so the summary reads article text.
            fetch_page_fn=lambda url: fetch_page_text(url, ssl_context=ssl_ctx),
            summarize_fn=lambda q, sources: summarize_web_sources_with_ollama(
                q,
                sources,
                endpoint=endpoint,
                model=model,
                timeout_seconds=summarize_timeout,
                ssl_context=ssl_ctx,
            ),
        )
        return format_web_research_answer(answer)

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
            fetch_page_fn=lambda u: fetch_page_text(u, ssl_context=ssl_ctx),
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


def _select_text_generation_model(settings: AssistantSettings) -> str | None:
    if not settings.openclaw_local_text_model:
        return None
    return next((part.strip() for part in settings.openclaw_local_text_model.split(",") if part.strip()), None)


def _build_openclaw_help_text() -> str:
    return "\n".join(
        [
            "OpenClaw — 指令一覧",
            "",
            "--- 系統 ---",
            "/ping  /status  /tools",
            "",
            "--- 價格查詢 ---",
            "/price pokemon Pikachu ex",
            "/price pokemon | Pikachu ex | 132/106 | SAR | sv08",
            "/price ws | Hatsune Miku | PJS/S91-T51 | TD | pjs",
            "/price ygo | 青眼の白龍 | QCCP-JP001 | UR",
            "/price ua | 綾波レイ | UAPR/EVA-1-71",
            "",
            "--- 市場熱度 ---",
            "/trend pokemon          # 熱銷榜（預設 10）",
            "/trend ws 5             # 指定數量",
            "/hot pokemon            # 同 /trend",
            "/liquidity ws 5         # 流動性排名",
            "",
            "--- 商品快照／信譽查詢 ---",
            "/snapshot https://jp.mercari.com/item/m123456789",
            "",
            "--- 網路搜尋 / 深度研究 ---",
            "/search 初音未來哪年發明的？",
            "/research https://jp.mercari.com/item/m123456789",
            "/research 初音ミク 15th フィギュア",
            "/fetch https://example.com 這篇文章的重點是什麼",
            "",
            "--- 圖片辨識 ---",
            "傳圖片 + caption: /scan pokemon",
            "",
            "--- Mercari / Rakuma / 遊々亭 追蹤 ---",
            "/watch 想いが重なる場所で 初音ミク SSP on 300000",
            "/watch アビスアイ box on 8000 markets:rakuma",
            "/watchlist",
            "/unwatch <ID>",
            "/setprice <ID> <新價格>",
            "",
            "--- SNS (X/Twitter) 監控 ---",
            "/snsadd @username",
            '/snsadd @username ["buy", "sell"]   # 加關鍵字過濾',
            "/snsadd keyword:搜詞",
            "/snsadd trend:trending",
            "/snslist",
            "/snsdelete <rule_id>",
            "/snsbuzz amd            # 4chan 收藏品 IP 熱度",
            "",
            "--- JLPT 日文測驗 (Miku 歌詞) ---",
            "/quiz                   # 出題選單",
            "/quiz random            # 隨機出一題",
            "/quiz wrong             # 錯題本",
            "/quiz stats             # 各考點正確率分析",
            "/quiz vocab             # 單字卡",
            "/quiz grammar           # 文法卡",
            "/quiz review            # 查看近期作答",
            "/quizlikesong <youtube_url>   # 收藏新歌並建立題庫",
            "",
            "--- 翻譯 ---",
            "/translateja 你好，今天辛苦了",
            "/ja 你好，今天辛苦了      # /translateja 短別名",
            "/translatezh お疲れさま、今日は大変だったね",
            "/zh お疲れさま、今日は大変だったね  # /translatezh 短別名",
            "",
            "--- 語音 ---",
            "/voice <日文>            # 語音合成（AivisSpeech），預覽參數",
            "/say <日文>              # 直接合成並傳送 WAV",
            "",
            "--- 知識庫 ---",
            "/knowledge market       # 查詢市場知識（集換式卡牌）",
            "/knowledge coding       # 查詢程式技術知識",
            "",
            "--- Opportunity Agent ---",
            "/hunt                   # 目標清單",
            "/hunt status            # 狀態 + 推薦紀錄",
            "/hunt remove 2          # 移除目標 #2",
            "/stats                  # 作答統計",
            "",
            "--- 動態自寫工具 ---",
            "/new 幫我查0050今年以來到5月的年化報酬",
            "",
            "--- 資料備份／還原 ---",
            "/backupclaw             # 備份到預設外接碟",
            "/backupclaw /path/to/dir",
            "/clawrecover            # 從預設外接碟還原",
            "/clawrecover force      # 覆蓋現有資料庫",
            "",
            "--- 自然語言也可以 ---",
            "pokemon 熱門前 5",
            "幫我查 pokemon Pikachu ex 132/106",
            "why are Pikachu Pokemon cards so popular?",
        ]
    )


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
    research_notifier_factory: "Callable[[str], ResearchNotifier] | None" = None,
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
    research_search_fn = lambda q, limit: _run_research_worker_call(
        lambda: web_search(q, max_results=limit, reuse_browser=False)
    )
    _yuyutei_resolver = _build_yuyutei_code_resolver(settings, research_search_fn)
    research_handler = build_research_handler(
        notifier_factory=research_notifier_factory,
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
        final_formatter=_build_research_reply_formatter(research_cache),
    )

    def _quizlikesong_handler(remainder: str, chat_id: str):
        return quiz_handler("like song " + (remainder or "").strip(), chat_id)

    def _new_handler(remainder: str, chat_id: str) -> str:
        if dynamic_tool_runner is None:
            return "/new 尚未啟用（需有本地 text model）。"
        return dynamic_tool_runner.run(remainder)

    command_handlers: dict[str, RegisteredCommand] = {
        "/quiz": RegisteredCommand(
            quiz_handler,
            ack="收到，正在出題（地端模型，可能要一點時間）…",
            background=True,
        ),
        "/quizlikesong": RegisteredCommand(
            _quizlikesong_handler, ack="收到，正在收藏歌曲…", background=True
        ),
        "/voice": RegisteredCommand(build_voice_handler(settings)),
        "/say": RegisteredCommand(
            build_say_handler(settings), ack="收到，正在合成語音…", background=True
        ),
        "/translateja": RegisteredCommand(
            build_translate_handler(settings, target="ja"),
            ack="收到，正在翻譯成日文…",
            background=True,
        ),
        "/ja": RegisteredCommand(
            build_translate_handler(settings, target="ja"),
            ack="收到，正在翻譯成日文…",
            background=True,
        ),
        "/jp": RegisteredCommand(
            build_translate_handler(settings, target="ja"),
            ack="收到，正在翻譯成日文…",
            background=True,
        ),
        "/translatezh": RegisteredCommand(
            build_translate_handler(settings, target="zh"),
            ack="收到，正在翻譯成繁體中文…",
            background=True,
        ),
        "/zh": RegisteredCommand(
            build_translate_handler(settings, target="zh"),
            ack="收到，正在翻譯成繁體中文…",
            background=True,
        ),
        "/new": RegisteredCommand(
            _new_handler,
            ack="收到，正在找/生成工具並執行（地端模型，可能要 1-2 分鐘）…",
            background=True,
        ),
        "/backupclaw": RegisteredCommand(
            lambda r, c: backup_handler(r),
            ack="收到，正在備份龍蝦的資料庫與自學工具規格…",
            background=True,
        ),
        "/backup": RegisteredCommand(
            lambda r, c: backup_handler(r),
            ack="收到，正在備份龍蝦的資料庫與自學工具規格…",
            background=True,
        ),
        "/clawrecover": RegisteredCommand(
            lambda r, c: recover_handler(r),
            ack="收到，正在從備份還原龍蝦的資料庫…",
            background=True,
        ),
        "/recoverclaw": RegisteredCommand(
            lambda r, c: recover_handler(r),
            ack="收到，正在從備份還原龍蝦的資料庫…",
            background=True,
        ),
        "/stats": RegisteredCommand(lambda r, c: scorecard_handler(r)),
        "/scorecard": RegisteredCommand(lambda r, c: scorecard_handler(r)),
        "/knowledge": RegisteredCommand(
            build_knowledge_handler(settings, knowledge_inbox=knowledge_inbox)
        ),
        "/kb": RegisteredCommand(
            build_knowledge_handler(settings, knowledge_inbox=knowledge_inbox)
        ),
        "/source": RegisteredCommand(build_source_handler(settings)),
        "/music": RegisteredCommand(build_music_handler(settings)),
        "/research": RegisteredCommand(
            research_handler,
            ack="收到，正在進行深度商品研究（會分階段回報進度）…",
            background=True,
        ),
        "/resaerch": RegisteredCommand(
            research_handler,
            ack="收到，正在進行深度商品研究（會分階段回報進度）…",
            background=True,
        ),
        "/snsadd": RegisteredCommand(
            build_sns_add_handler(sns_db, sns_inbox=sns_inbox),
            ack="收到 X 追蹤指令，正在設定…", background=True,
        ),
        "/sns_add": RegisteredCommand(
            build_sns_add_handler(sns_db, sns_inbox=sns_inbox),
            ack="收到 X 追蹤指令，正在設定…", background=True,
        ),
        "/snslist": RegisteredCommand(build_snslist_handler(sns_db)),
        "/sns_list": RegisteredCommand(build_snslist_handler(sns_db)),
        "/snsdelete": RegisteredCommand(build_sns_delete_handler(sns_db, sns_inbox=sns_inbox)),
        "/sns_delete": RegisteredCommand(build_sns_delete_handler(sns_db, sns_inbox=sns_inbox)),
        "/snsbuzz": RegisteredCommand(
            build_sns_buzz_handler(buzz_fn),
            ack="收到，正在掃描 4chan 收藏/IP 討論並交給 LLM 整理…",
            background=True,
        ),
        "/sns_buzz": RegisteredCommand(
            build_sns_buzz_handler(buzz_fn),
            ack="收到，正在掃描 4chan 收藏/IP 討論並交給 LLM 整理…",
            background=True,
        ),
        "/snsclearfilter": RegisteredCommand(
            build_sns_clear_filter_handler(sns_db, sns_inbox=sns_inbox)
        ),
        "/hunt": RegisteredCommand(
            build_hunt_handler(settings, opportunity_inbox=opportunity_inbox)
        ),
        "/opportunity": RegisteredCommand(
            build_hunt_handler(settings, opportunity_inbox=opportunity_inbox)
        ),
    }

    _rag_cb = _build_rag_callback_handler(settings, knowledge_inbox=knowledge_inbox)

    def _rag_keep_adapter(payload: str, original_text: str, chat_id: str):
        new_text, markup = _rag_cb("ragkeep", payload, original_text)
        return "✅ 已保留", new_text, markup

    def _rag_del_adapter(payload: str, original_text: str, chat_id: str):
        new_text, markup = _rag_cb("ragdel", payload, original_text)
        return "🗑️ 已刪除", new_text, markup

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
        "imgtr": _build_image_translate_callback_handler(_IMAGE_TRANSLATE_ORIGINAL_CACHE),
    }

    view_handlers = {
        "km": build_knowledge_market_view_fn(settings),
        "kc": build_knowledge_coding_view_fn(settings),
        "sl": build_snslist_view_fn(sns_db),
        "hl": build_huntlist_view_fn(settings),
    }
    item_deleter_handlers = {
        **build_knowledge_item_deleters(settings),
        "sl": build_sns_rule_deleter(sns_db, sns_inbox=sns_inbox),
        "hl": build_huntlist_item_deleter(settings, opportunity_inbox=opportunity_inbox),
    }

    return command_handlers, callback_handlers, view_handlers, item_deleter_handlers


def _build_research_notifier_factory(settings: AssistantSettings) -> "Callable[[str], ResearchNotifier]":
    token = require_telegram_token(settings)
    client = TelegramBotClient(token, ssl_context=build_ssl_context(settings))

    class _TelegramResearchNotifier:
        def __init__(self, chat_id: str) -> None:
            self._chat_id = chat_id

        def send(self, text: str) -> None:
            client.send_message(chat_id=self._chat_id, text=text)

    return lambda chat_id: _TelegramResearchNotifier(str(chat_id))


def _build_research_seller_snapshot_lookup(
    settings: AssistantSettings,
) -> "Callable[[str], SellerReputationSnapshot]":
    def lookup(seller_url: str) -> SellerReputationSnapshot:
        result = request_reputation_snapshot(settings=settings, query_url=seller_url)
        try:
            proof_document = (
                fetch_reputation_proof_document(settings=settings, proof_id=result.proof_id)
                if result.proof_id
                else {}
            )
        except Exception as exc:
            # Job completed but the proof document fetch failed transiently (e.g.
            # socket timeout). Do not report a permanent failure: convert to a
            # pending state so /research schedules the background follow-up, which
            # re-fetches the proof once it is reachable.
            if not result.proof_id:
                raise
            logger.warning(
                "seller snapshot proof fetch failed after job done proof_id=%s, "
                "scheduling follow-up: %s",
                result.proof_id,
                exc,
            )

            def _poll_proof() -> "ReputationSnapshotResult | None":
                deadline = time.monotonic() + 900.0
                while time.monotonic() < deadline:
                    try:
                        fetch_reputation_proof_document(
                            settings=settings, proof_id=result.proof_id
                        )
                        return result
                    except Exception:
                        time.sleep(2.0)
                return None

            raise SnapshotStillPending(result.job_id or result.proof_id, poll_fn=_poll_proof)
        subject = proof_document.get("subject", {}) if isinstance(proof_document, dict) else {}
        metrics = proof_document.get("metrics", {}) if isinstance(proof_document, dict) else {}
        quality = proof_document.get("quality", {}) if isinstance(proof_document, dict) else {}
        review_entries = proof_document.get("review_entries", ()) if isinstance(proof_document, dict) else ()
        as_seller = quality.get("as_seller") if isinstance(quality, dict) else None
        as_buyer = quality.get("as_buyer") if isinstance(quality, dict) else None
        overall = quality.get("overall") if isinstance(quality, dict) else None
        return SellerReputationSnapshot(
            seller_url=seller_url,
            proof_url=result.proof_url,
            proof_id=result.proof_id,
            reused=result.reused,
            display_name=subject.get("display_name") if isinstance(subject, dict) else None,
            captured_at=proof_document.get("captured_at") if isinstance(proof_document, dict) else None,
            total_reviews=metrics.get("total_reviews") if isinstance(metrics, dict) else None,
            listing_count=metrics.get("listing_count") if isinstance(metrics, dict) else None,
            followers_count=metrics.get("followers_count") if isinstance(metrics, dict) else None,
            following_count=metrics.get("following_count") if isinstance(metrics, dict) else None,
            seller_positive=as_seller.get("positive") if isinstance(as_seller, dict) else None,
            seller_negative=as_seller.get("negative") if isinstance(as_seller, dict) else None,
            seller_rate=as_seller.get("rate") if isinstance(as_seller, dict) else None,
            buyer_positive=as_buyer.get("positive") if isinstance(as_buyer, dict) else None,
            buyer_negative=as_buyer.get("negative") if isinstance(as_buyer, dict) else None,
            buyer_rate=as_buyer.get("rate") if isinstance(as_buyer, dict) else None,
            overall_rate=overall.get("rate") if isinstance(overall, dict) else None,
            seller_negative_excerpts=_extract_negative_seller_review_excerpts(review_entries),
        )

    return lookup


def _build_research_seller_snapshot_followup(
    settings: "AssistantSettings",
) -> "Callable[[str, Callable, ResearchNotifier], None]":
    """Return a followup fn that background-polls a pending snapshot and pushes the result."""
    import threading as _threading

    def _proof_doc_to_seller_snapshot(
        seller_url: str,
        result: ReputationSnapshotResult,
        proof_doc: dict,
    ) -> SellerReputationSnapshot:
        subject = proof_doc.get("subject", {}) if isinstance(proof_doc, dict) else {}
        metrics = proof_doc.get("metrics", {}) if isinstance(proof_doc, dict) else {}
        quality = proof_doc.get("quality", {}) if isinstance(proof_doc, dict) else {}
        review_entries = proof_doc.get("review_entries", ()) if isinstance(proof_doc, dict) else ()
        as_seller = quality.get("as_seller") if isinstance(quality, dict) else None
        as_buyer = quality.get("as_buyer") if isinstance(quality, dict) else None
        overall = quality.get("overall") if isinstance(quality, dict) else None
        return SellerReputationSnapshot(
            seller_url=seller_url,
            proof_url=result.proof_url,
            proof_id=result.proof_id,
            reused=result.reused,
            display_name=subject.get("display_name") if isinstance(subject, dict) else None,
            captured_at=proof_doc.get("captured_at") if isinstance(proof_doc, dict) else None,
            total_reviews=metrics.get("total_reviews") if isinstance(metrics, dict) else None,
            listing_count=metrics.get("listing_count") if isinstance(metrics, dict) else None,
            followers_count=metrics.get("followers_count") if isinstance(metrics, dict) else None,
            following_count=metrics.get("following_count") if isinstance(metrics, dict) else None,
            seller_positive=as_seller.get("positive") if isinstance(as_seller, dict) else None,
            seller_negative=as_seller.get("negative") if isinstance(as_seller, dict) else None,
            seller_rate=as_seller.get("rate") if isinstance(as_seller, dict) else None,
            buyer_positive=as_buyer.get("positive") if isinstance(as_buyer, dict) else None,
            buyer_negative=as_buyer.get("negative") if isinstance(as_buyer, dict) else None,
            buyer_rate=as_buyer.get("rate") if isinstance(as_buyer, dict) else None,
            overall_rate=overall.get("rate") if isinstance(overall, dict) else None,
            seller_negative_excerpts=_extract_negative_seller_review_excerpts(review_entries),
        )

    def followup(seller_url: str, poll_fn: "Callable", notifier: ResearchNotifier) -> None:
        def _bg() -> None:
            try:
                result = poll_fn()
                if result is None:
                    notifier.send(
                        f"⏰ 賣家快照逾時或失敗，請手動查詢：/snapshot {seller_url}"
                    )
                    return
                proof_doc = (
                    fetch_reputation_proof_document(settings=settings, proof_id=result.proof_id)
                    if result.proof_id
                    else {}
                )
                snapshot = _proof_doc_to_seller_snapshot(seller_url, result, proof_doc)
                section = _build_seller_snapshot_section_result(snapshot)
                notifier.send(f"📋 賣家風險分析（補送）\n{section.summary}")
            except Exception as exc:
                logger.error("seller snapshot followup failed seller_url=%s: %s", seller_url, exc)
                notifier.send(
                    f"⚠️ 賣家快照補送失敗：{exc}\n請手動查詢：/snapshot {seller_url}"
                )

        _threading.Thread(target=_bg, daemon=True, name="reputation-followup").start()

    return followup


def _extract_negative_seller_review_excerpts(review_entries: object) -> tuple[str, ...]:
    if not isinstance(review_entries, list):
        return ()
    excerpts: list[str] = []
    seen: set[str] = set()
    for entry in review_entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("role") != "seller" or entry.get("rating") != "negative":
            continue
        text = " ".join(str(entry.get("body_excerpt") or "").split()).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        excerpts.append(text)
        if len(excerpts) >= 3:
            break
    return tuple(excerpts)


def _build_research_ip_heat_lookup(
    settings: AssistantSettings,
) -> "Callable[[tuple[str, ...]], dict[str, tuple[object, ...]]]":
    from pathlib import Path as _Path

    from .ip_heat_store import IpHeatStore

    store = IpHeatStore(_Path(settings.knowledge_db_path).with_name("ip_heat.sqlite3"))

    def lookup(canonicals: tuple[str, ...]) -> dict[str, tuple[object, ...]]:
        result: dict[str, tuple[object, ...]] = {}
        for canonical in canonicals:
            signals = tuple(store.latest_for_ip(canonical))
            if signals:
                result[canonical] = signals
        return result

    return lookup


def _build_research_appreciation_enricher(settings: AssistantSettings):
    """A4: build the appreciation web-enricher reusing the same page fetch + LLM
    summariser the /research web-research renderer uses. Returns None when neither
    the local text LLM nor the cloud enricher is configured (falls back to
    snippet-only evidence).

    Phase-1 cloud offload: when ``OPENCLAW_RESEARCH_CLOUD_ENRICHER=opencode`` and
    the OpenCode CLI probes ok, the summariser (the open-ended, abstract step)
    runs on cloud big-pickle while the price gate stays local. A cloud outage
    (``CloudBackendUnavailable``) or empty reply degrades to a single in-process
    local summarise — it must NOT trigger /new's bot-restart failover. The local
    relevance gate is skipped in cloud mode so stage 3 doesn't queue a second
    call on the same local Ollama as the price gate."""
    backend = (settings.openclaw_local_text_backend or "").strip().lower()
    endpoint = settings.openclaw_local_text_endpoint
    model = _select_text_generation_model(settings)
    local_ready = backend == "ollama" and bool(endpoint) and bool(model)
    timeout = max(1, settings.openclaw_local_text_timeout_seconds)
    summarize_timeout = max(timeout, 120)
    ssl_ctx = build_ssl_context(settings) if (endpoint or "").startswith("https://") else None

    def _local_summarize(q, sources):
        return summarize_web_sources_with_ollama(
            q,
            sources,
            endpoint=endpoint,
            model=model,
            timeout_seconds=summarize_timeout,
            ssl_context=ssl_ctx,
        )

    cloud_client = build_research_cloud_text_client(settings)
    if cloud_client is not None:
        def _cloud_summarize(q, sources):
            if not sources:
                return f"我找不到足夠有用的網路來源來回答：{q}"
            prompt = _build_summary_prompt(q, sources)
            try:
                text = (cloud_client.generate(prompt, temperature=0.2) or "").strip()
            except CloudBackendUnavailable:
                logger.warning(
                    "research appreciation cloud enricher unavailable; "
                    "falling back to local for this request",
                    exc_info=True,
                )
                return _local_summarize(q, sources) if local_ready else ""
            if text:
                return text
            return _local_summarize(q, sources) if local_ready else ""

        logger.info("research appreciation enricher: cloud big-pickle (local fallback)")
        return build_appreciation_enricher(
            fetch_page_fn=lambda url: fetch_page_text(url, ssl_context=ssl_ctx),
            summarize_fn=_cloud_summarize,
            relevance_fn=None,
        )

    if not local_ready:
        return None
    return build_appreciation_enricher(
        fetch_page_fn=lambda url: fetch_page_text(url, ssl_context=ssl_ctx),
        summarize_fn=_local_summarize,
        relevance_fn=lambda q, sources: filter_relevant_sources_with_ollama(
            q,
            sources,
            endpoint=endpoint,
            model=model,
            timeout_seconds=timeout,
            ssl_context=ssl_ctx,
        ),
    )


def _build_yuyutei_code_resolver(
    settings: AssistantSettings, search_fn: "Callable[[str, int], object]"
) -> "object | None":
    """Build the LLM/RAG resolver that maps a bare card name → yuyutei game code
    so the 遊々亭 買取/販売 band can appear for queries with no game keyword.
    Returns the resolver (exposing ``.resolve`` and ``.enrich_cache``) or ``None``
    (band falls back to keyword-only routing) when the local text LLM isn't
    configured."""
    backend = (settings.openclaw_local_text_backend or "").strip().lower()
    endpoint = settings.openclaw_local_text_endpoint
    model = _select_text_generation_model(settings)
    if backend != "ollama" or not endpoint or not model:
        return None

    from .opportunity_agent import _call_ollama_json
    from .yuyutei_code_resolver import YuyuteiGameCodeResolver

    return YuyuteiGameCodeResolver(
        knowledge_db_path=settings.knowledge_db_path,
        json_call_fn=_call_ollama_json,
        endpoint=endpoint,
        model=model,
        timeout_seconds=max(1, settings.openclaw_local_text_timeout_seconds),
        ssl_context=build_ssl_context(settings) if endpoint.startswith("https://") else None,
        search_fn=search_fn,
    )


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
    rag_digest_scheduler = _start_rag_daily_digest(settings)
    quiz_daily_scheduler = start_quiz_daily_scheduler(settings)
    dynamic_tool_runner = build_dynamic_tool_runner_from_settings(settings)
    command_handlers, callback_handlers, view_handlers, item_deleter_handlers = (
        _build_registries(settings, dynamic_tool_runner, sns_db=sns_db, buzz_fn=sns_buzz_fn,
                          sns_inbox=sns_inbox, knowledge_inbox=knowledge_inbox,
                          opportunity_inbox=opportunity_inbox,
                          research_notifier_factory=_build_research_notifier_factory(settings))
    )

    _price_bot_module.TelegramCommandProcessor = (
        lambda **kwargs: TelegramCommandProcessor(settings=settings, **kwargs)
    )
    return _base_run_telegram_polling(
        token=token,
        lookup_renderer=lookup_renderer,
        board_loader=board_loader,
        catalog_renderer=catalog_renderer,
        photo_renderer=photo_renderer or build_photo_renderer(settings, research_renderer=research_renderer),
        photo_intent_analyzer=default_photo_intent_analyzer(settings),
        reputation_renderer=default_reputation_renderer(settings),
        research_renderer=research_renderer,
        fetch_renderer=default_web_fetch_renderer(settings),
        natural_language_router=build_telegram_natural_language_router_from_settings(settings),
        intent_fast_path=_build_intent_fast_path(settings),
        image_translate_recognizer=build_image_translate_caption_recognizer(settings),
        ssl_context=build_ssl_context(settings),
        allowed_chat_ids=frozenset(settings.openclaw_telegram_chat_ids),
        status_renderer=lambda: _build_status_text(settings, dynamic_tool_runner),
        command_handlers=command_handlers,
        callback_handlers=callback_handlers,
        view_handlers=view_handlers,
        item_deleter_handlers=item_deleter_handlers,
        watch_db=watch_db,
        watch_inbox=watch_inbox,
        sns_db=sns_db,
        sns_buzz_fn=sns_buzz_fn,
        feedback_service=feedback_service,
        poll_timeout=poll_timeout,
        notify_startup=notify_startup,
        drop_pending_updates=drop_pending_updates,
    )


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
    from price_monitor_bot.bot import TelegramBotClient
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
        from price_monitor_bot.bot import TelegramBotClient
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


def require_telegram_token(settings: AssistantSettings) -> str:
    token = settings.openclaw_telegram_bot_token
    if token is None:
        raise RuntimeError("Telegram bot token is missing. Put it in .env as OPENCLAW_TELEGRAM_BOT_TOKEN.")
    return token


def require_telegram_chat_id(settings: AssistantSettings) -> str:
    chat_id = settings.openclaw_telegram_chat_id
    if chat_id is None:
        raise RuntimeError("Telegram chat id is missing. Put it in .env as OPENCLAW_TELEGRAM_CHAT_ID.")
    return chat_id


def _build_status_text(settings: AssistantSettings, dynamic_tool_runner=None) -> str:
    allowed_chats = ", ".join(settings.openclaw_telegram_chat_ids) if settings.openclaw_telegram_chat_ids else "not restricted"
    configured = _load_status_configuration_snapshot()
    tesseract = settings.openclaw_tesseract_path or configured.get("OPENCLAW_TESSERACT_PATH") or "PATH lookup"
    tessdata = settings.openclaw_tessdata_dir or configured.get("OPENCLAW_TESSDATA_DIR") or "auto"
    text_backend = (settings.openclaw_local_text_backend or "").strip().lower() or "none"
    text_model = _select_router_model_for_status(settings)
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
