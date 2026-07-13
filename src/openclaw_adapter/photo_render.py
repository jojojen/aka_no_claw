"""Photo pipeline glue: TCG card-price renderer composed with image OCR+translate.

Moved out of telegram_bot.py in R2.2 (#75). Owns the `imgtr` callback prefix
(顯示原文 reveal), the fixed photo intent menu, and the caption-routed composite
renderer. telegram_bot re-imports these names so legacy import paths and
`_build_registries` registration sites are unchanged.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Callable

from assistant_runtime import AssistantSettings, build_ssl_context
from price_monitor_bot.bot import (
    LookupRenderer,
    PhotoLookupRenderer,
    PhotoLookupReply,
    TelegramPhotoIntentAnalysis,
    TelegramPhotoIntentOption,
    TelegramPhotoQuery,
    default_board_loader as _base_default_board_loader,
    default_lookup_renderer as _base_default_lookup_renderer,
    default_photo_renderer as _base_default_photo_renderer,
)
from tcg_tracker.image_lookup import TcgVisionSettings

from .image_translate import build_image_ocr_translate_renderer_from_settings


def default_lookup_renderer(settings: AssistantSettings) -> LookupRenderer:
    return _base_default_lookup_renderer(db_path=settings.monitor_db_path)


def default_board_loader(settings: AssistantSettings | None = None) -> tuple:
    return _base_default_board_loader(ssl_context=build_ssl_context(settings) if settings else None)


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
