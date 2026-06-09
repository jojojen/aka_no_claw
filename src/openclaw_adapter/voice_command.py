"""Telegram-side handlers for ``/voice`` and ``/say`` (AivisSpeech tuning).

aka_no_claw owns the ``QuizDatabase`` (which now stores per-chat ``VoiceParams``)
and the synthesizers; price_monitor_bot's ``/voice`` / ``/say`` branches just call
the handlers built here. Contracts mirror the ``/quiz`` handlers:

  - command handler  ``handler(raw, chat_id) -> str | (text, reply_markup)``
  - callback handler ``handler(payload, original_text, chat_id) ->
                       (toast, new_text, reply_markup)``  (prefix ``voice`` already
                       stripped by bot.py).

``/voice`` shows the current five AivisSpeech scales with an inline ➖/➕ keyboard;
``/voice <param> <value>`` / ``/voice reset`` set them by text. ``/say <日文>``
synthesizes the text with the chat's stored params and sends back the WAV.

Callback payloads (prefix ``voice`` stripped):
  <param>:+ / <param>:-   — step one param up/down, persist, redraw
  reset                   — restore defaults, redraw
"""

from __future__ import annotations

import logging
from typing import Callable

from assistant_runtime import AssistantSettings, build_ssl_context
from price_monitor_bot.bot import TelegramBotClient

from .quiz_vocab_audio import (
    QuizVocabAudioError,
    VoiceParams,
    build_vocab_audio_cache_dir,
    build_vocab_synthesizer,
)

logger = logging.getLogger(__name__)

# Display order + Chinese labels for the five tunable scales.
_PARAM_LABELS: list[tuple[str, str]] = [
    ("speed", "語速"),
    ("pitch", "音高"),
    ("intonation", "抑揚"),
    ("tempo", "節奏"),
    ("volume", "音量"),
]

# Text-command aliases → canonical param name.
_ALIASES: dict[str, str] = {
    "speed": "speed",
    "rate": "speed",
    "語速": "speed",
    "pitch": "pitch",
    "音高": "pitch",
    "intonation": "intonation",
    "emotion": "intonation",
    "抑揚": "intonation",
    "tempo": "tempo",
    "節奏": "tempo",
    "volume": "volume",
    "vol": "volume",
    "音量": "volume",
}


def _open_db(settings: AssistantSettings):
    from .quiz_db import QuizDatabase

    return QuizDatabase(settings.quiz_db_path)


def _render(params: VoiceParams) -> "tuple[str, dict]":
    lines = ["🎚️ AivisSpeech 語音參數（本 chat）"]
    for name, label in _PARAM_LABELS:
        lo, hi = VoiceParams.RANGES[name]
        val = getattr(params, name)
        lines.append(f"・{label} {name}: {val}（{lo}~{hi}）")
    lines.append("")
    lines.append("用 ➖/➕ 微調，或 /voice <參數> <值>、/voice reset；/say <日文> 試聽。")
    rows: list[list[dict]] = []
    for name, label in _PARAM_LABELS:
        val = getattr(params, name)
        rows.append([
            {"text": "➖", "callback_data": f"voice:{name}:-"},
            {"text": f"{label} {val}", "callback_data": "noop"},
            {"text": "➕", "callback_data": f"voice:{name}:+"},
        ])
    rows.append([{"text": "↺ 還原預設", "callback_data": "voice:reset"}])
    return "\n".join(lines), {"inline_keyboard": rows}


def build_voice_handler(
    settings: AssistantSettings,
) -> Callable[[str, str], object]:
    db = _open_db(settings)

    def handler(raw: str, chat_id: str | None = None) -> object:
        cid = str(chat_id or "")
        if not cid:
            return "找不到 chat_id，無法調整語音參數。"
        body = (raw or "").strip()
        params = db.get_voice_params(cid)
        # bare /voice or /voice show → current values + keyboard
        if body == "" or body.lower() == "show":
            return _render(params)
        parts = body.split()
        head = parts[0].lower()
        if head == "reset":
            params = VoiceParams()
            db.set_voice_params(cid, params)
            return _render(params)
        canonical = _ALIASES.get(parts[0]) or _ALIASES.get(head)
        if canonical is None:
            known = "、".join(sorted({v for v in _ALIASES.values()}))
            return f"未知參數「{parts[0]}」。可調：{known}（或 reset / show）。"
        if len(parts) < 2:
            lo, hi = VoiceParams.RANGES[canonical]
            return f"用法：/voice {canonical} <值>（{lo}~{hi}）。"
        try:
            value = float(parts[1])
        except ValueError:
            return f"「{parts[1]}」不是數字。用法：/voice {canonical} <值>。"
        params = params.with_param(canonical, value)  # clamped on construction
        db.set_voice_params(cid, params)
        return _render(params)

    return handler


def build_voice_callback_handler(
    settings: AssistantSettings,
) -> Callable[[str, str], "tuple[object, str, object]"]:
    db = _open_db(settings)

    def handler(
        payload: str, original_text: str, chat_id: str | None = None
    ) -> "tuple[object, str, object]":
        cid = str(chat_id or "")
        if not cid:
            return "找不到 chat_id", None, None
        action, _, rest = (payload or "").partition(":")
        params = db.get_voice_params(cid)
        if action == "reset":
            params = VoiceParams()
            db.set_voice_params(cid, params)
            text, markup = _render(params)
            return "已還原預設", text, markup
        name = _ALIASES.get(action)
        if name is None or rest not in ("+", "-"):
            return "未知操作", None, None
        direction = 1 if rest == "+" else -1
        new_params = params.step(name, direction)
        db.set_voice_params(cid, new_params)
        label = dict(_PARAM_LABELS)[name]
        toast = f"{label} → {getattr(new_params, name)}"
        text, markup = _render(new_params)
        return toast, text, markup

    return handler


def build_say_handler(
    settings: AssistantSettings,
) -> Callable[[str, str], object]:
    db = _open_db(settings)

    def handler(raw: str, chat_id: str | None = None) -> object:
        cid = str(chat_id or "")
        text = (raw or "").strip()
        if not text:
            return "用法：/say <日文文字>（用目前語音參數朗讀）。"
        token = getattr(settings, "openclaw_telegram_bot_token", None)
        if not token:
            return "缺少 Telegram bot token，無法送出語音。"
        if not cid:
            return "找不到 chat_id，無法送出語音。"
        params = db.get_voice_params(cid)
        synth = build_vocab_synthesizer(settings, params)
        cache_dir = build_vocab_audio_cache_dir(settings=settings)
        try:
            audio = synth.synthesize_text(text=text, cache_dir=cache_dir)
        except QuizVocabAudioError as exc:
            return f"合成失敗：{exc}"
        except Exception as exc:
            logger.exception("/say synthesis failed text=%s", text[:40])
            return f"合成失敗：{exc}"
        client = TelegramBotClient(token, ssl_context=build_ssl_context(settings))
        caption = f"音源：{audio.engine_label}\n文字：{text}"
        client.send_document(
            chat_id=cid,
            document_path=audio.output_path,
            caption=caption[:1024],
        )
        return None

    return handler
