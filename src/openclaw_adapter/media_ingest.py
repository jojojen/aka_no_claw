"""Telegram media ingestion: download + validate + transcribe voice/audio.

Moved out of telegram_bot.py in R2.3 (#75). The processor's
``handle_audio_message`` / ``build_audio_intake_ack_text`` hooks now delegate
here, keeping the aka wrapper thin. Behavior is unchanged: validation order,
error strings, and the (transcript, error) tuple contract are identical to the
old in-processor implementation.
"""

from __future__ import annotations

import logging
import mimetypes

from .local_stt import (
    LocalWhisperTranscriber,
    SttPayloadTooLarge,
    SttRequestError,
    SttRuntimeError,
    build_audio_request,
    validate_audio_mime_type,
)

logger = logging.getLogger(__name__)

AUDIO_INTAKE_ACK_TEXT = "已收到語音，正在本機轉成文字。"


def transcribe_telegram_audio(
    *,
    client,
    message: dict[str, object],
    stt_transcriber: LocalWhisperTranscriber | None,
    stt_language: str | None,
) -> tuple[str | None, str | None] | None:
    """Transcribe a Telegram voice/audio message to text.

    Returns None when the message carries no voice/audio attachment (so the
    caller falls through to text handling), ``(None, error)`` when the audio
    can't be transcribed, and ``(transcript, None)`` on success. The transcript
    is handed back for telegram_core to redispatch, preserving the exact
    pending-reply / pre-dispatch / natural-language path used by text messages.
    """
    voice = message.get("voice")
    audio = message.get("audio")
    attachment = voice if isinstance(voice, dict) else audio
    if not isinstance(attachment, dict):
        return None
    if stt_transcriber is None:
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
        if file_size > stt_transcriber.max_audio_bytes:
            return None, (
                "語音轉文字失敗："
                f"音訊超過 {stt_transcriber.max_audio_bytes} bytes 上限。"
            )
    duration = attachment.get("duration")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < 0:
        return None, "語音轉文字失敗：Telegram 音訊缺少有效的 duration。"
    if duration > stt_transcriber.max_duration_seconds:
        return None, (
            "語音轉文字失敗："
            f"音訊長度超過 {stt_transcriber.max_duration_seconds} 秒上限。"
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
            max_bytes=stt_transcriber.max_audio_bytes,
        )
        request = build_audio_request(
            audio_bytes,
            mime_type=mime_type,
            max_audio_bytes=stt_transcriber.max_audio_bytes,
            language=stt_language,
            trusted_duration_seconds=float(duration),
        )
        result = stt_transcriber.transcribe(request)
    except (SttPayloadTooLarge, SttRequestError, SttRuntimeError) as exc:
        return None, f"語音轉文字失敗：{exc}"
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning("Telegram audio transcription failed: %s", exc)
        return None, "語音轉文字失敗：無法下載或處理 Telegram 音訊。"
    return result.transcript, None
