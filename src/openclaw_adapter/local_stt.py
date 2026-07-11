"""Local speech-to-text support for the Web command bridge and Telegram.

The implementation uses faster-whisper (CTranslate2) entirely in-process.  It
loads and caches the selected model on first use, writes each request to a
short-lived local file for PyAV, and never uploads audio to a remote service.
"""

from __future__ import annotations

import logging
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from assistant_runtime import AssistantSettings

logger = logging.getLogger(__name__)

_MIME_SUFFIXES = {
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/m4a": ".m4a",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/wave": ".wav",
    "audio/webm": ".webm",
    "audio/x-m4a": ".m4a",
    "audio/x-wav": ".wav",
}


class SttRequestError(ValueError):
    """The caller supplied an invalid audio request."""


class SttPayloadTooLarge(SttRequestError):
    """The encoded request or decoded audio exceeds its configured limit."""


class SttRuntimeError(RuntimeError):
    """The local transcription runtime could not process the audio."""


@dataclass(frozen=True, slots=True)
class AudioRequest:
    data: bytes
    mime_type: str
    suffix: str
    language: str | None = None
    trusted_duration_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    transcript: str
    language: str | None
    language_probability: float | None
    duration_seconds: float | None


def build_audio_request(
    audio: bytes,
    *,
    mime_type: str,
    max_audio_bytes: int,
    language: object = None,
    trusted_duration_seconds: float | None = None,
) -> AudioRequest:
    """Validate already-downloaded audio (used by Telegram voice/audio)."""
    normalized_mime, suffix = validate_audio_mime_type(mime_type)
    if not audio:
        raise SttRequestError("音訊內容為空。")
    if len(audio) > max_audio_bytes:
        raise SttPayloadTooLarge(f"音訊超過 {max_audio_bytes} bytes 上限。")
    normalized_language = str(language).strip() if language is not None else None
    if trusted_duration_seconds is not None:
        if (
            not isinstance(trusted_duration_seconds, (int, float))
            or isinstance(trusted_duration_seconds, bool)
            or trusted_duration_seconds <= 0
        ):
            raise SttRequestError("信任的音訊時長必須是正數。")
    return AudioRequest(
        data=audio,
        mime_type=normalized_mime,
        suffix=suffix,
        language=normalized_language or None,
        trusted_duration_seconds=trusted_duration_seconds,
    )


def validate_audio_mime_type(mime_type: str) -> tuple[str, str]:
    """Return normalized MIME/suffix or fail before any remote file download."""
    normalized_mime = mime_type.split(";", 1)[0].strip().lower()
    suffix = _MIME_SUFFIXES.get(normalized_mime)
    if suffix is None:
        raise SttRequestError(f"不支援的音訊格式：{normalized_mime or '未指定'}。")
    return normalized_mime, suffix


class LocalWhisperTranscriber:
    """Lazy, cached faster-whisper wrapper safe for the threaded HTTP server."""

    def __init__(
        self,
        *,
        model_name: str = "base",
        device: str = "auto",
        compute_type: str = "default",
        download_root: str | None = None,
        max_audio_bytes: int = 15 * 1024 * 1024,
        max_duration_seconds: int = 120,
        beam_size: int = 5,
        model_factory: Callable[..., Any] | None = None,
        duration_probe: Callable[[str, int], float | None] | None = None,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.download_root = download_root
        self.max_audio_bytes = max(1, max_audio_bytes)
        self.max_duration_seconds = max(1, max_duration_seconds)
        self.beam_size = max(1, beam_size)
        # Multipart adds only headers and boundaries; reserve bounded overhead
        # without accepting the 33% base64 expansion of the old JSON contract.
        self.max_request_bytes = self.max_audio_bytes + 256 * 1024
        self._model_factory = model_factory
        self._duration_probe = duration_probe or _probe_audio_duration
        self._model: Any | None = None
        self._model_lock = threading.Lock()
        # Avoid concurrent CTranslate2 work multiplying memory use when several
        # ThreadingHTTPServer requests arrive together.
        self._transcribe_lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: AssistantSettings) -> "LocalWhisperTranscriber":
        return cls(
            model_name=settings.openclaw_stt_model,
            device=settings.openclaw_stt_device,
            compute_type=settings.openclaw_stt_compute_type,
            download_root=settings.openclaw_stt_download_root,
            max_audio_bytes=settings.openclaw_stt_max_audio_bytes,
            max_duration_seconds=settings.openclaw_stt_max_duration_seconds,
            beam_size=settings.openclaw_stt_beam_size,
        )

    def prewarm(self) -> None:
        """Pre-load model to avoid latency on first transcription."""
        try:
            self._get_model()
        except Exception as exc:
            logger.warning("Model prewarm failed: %s", exc)

    def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is not None:
                return self._model
            try:
                factory = self._model_factory
                if factory is None:
                    from faster_whisper import WhisperModel

                    factory = WhisperModel
                if self.download_root:
                    Path(self.download_root).mkdir(parents=True, exist_ok=True)
                self._model = factory(
                    self.model_name,
                    device=self.device,
                    compute_type=self.compute_type,
                    download_root=self.download_root,
                )
            except (ImportError, OSError, RuntimeError, ValueError) as exc:
                logger.exception("failed to load local faster-whisper model")
                raise SttRuntimeError(
                    "本機語音模型無法載入；"
                    "請確認 faster-whisper 已安裝且模型可用。"
                ) from exc
            return self._model

    def transcribe(self, request: AudioRequest) -> TranscriptionResult:
        temp_path: str | None = None
        model_load_ms = 0
        transcribe_ms = 0
        try:
            with tempfile.NamedTemporaryFile(suffix=request.suffix, delete=False) as temp_file:
                temp_file.write(request.data)
                temp_path = temp_file.name

            # Skip duration probe if caller provided trusted_duration_seconds
            if request.trusted_duration_seconds is not None:
                duration = request.trusted_duration_seconds
                if duration > self.max_duration_seconds:
                    raise SttPayloadTooLarge(
                        f"音訊長度超過 {self.max_duration_seconds} 秒上限。"
                    )
            else:
                duration = self._duration_probe(temp_path, self.max_duration_seconds)
                if duration is not None and duration > self.max_duration_seconds:
                    raise SttPayloadTooLarge(
                        f"音訊長度超過 {self.max_duration_seconds} 秒上限。"
                    )

            model_start_ms = time.perf_counter() * 1000
            model = self._get_model()
            model_load_ms = int(time.perf_counter() * 1000 - model_start_ms)

            transcribe_start_ms = time.perf_counter() * 1000
            with self._transcribe_lock:
                segments, info = model.transcribe(
                    temp_path,
                    language=request.language,
                    beam_size=self.beam_size,
                    vad_filter=True,
                )
                transcript = _join_segments(segments)
            transcribe_ms = int(time.perf_counter() * 1000 - transcribe_start_ms)

            detected_duration = _optional_float(getattr(info, "duration", None))
            if detected_duration is None:
                detected_duration = duration
            if detected_duration is not None and detected_duration > self.max_duration_seconds:
                raise SttPayloadTooLarge(
                    f"音訊長度超過 {self.max_duration_seconds} 秒上限。"
                )
            if not transcript:
                raise SttRequestError("未辨識到可轉換的語音。")

            audio_s = detected_duration or 0.0
            logger.info(
                "[voice-latency] stage=stt model_load_ms=%d transcribe_ms=%d audio_s=%.1f model=%s beam=%d",
                model_load_ms,
                transcribe_ms,
                audio_s,
                self.model_name,
                self.beam_size,
            )

            return TranscriptionResult(
                transcript=transcript,
                language=_optional_str(getattr(info, "language", request.language)),
                language_probability=_optional_float(
                    getattr(info, "language_probability", None)
                ),
                duration_seconds=detected_duration,
            )
        except (SttPayloadTooLarge, SttRequestError, SttRuntimeError):
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            logger.exception("local audio transcription failed")
            raise SttRuntimeError(
                "本機音訊轉文字失敗；請確認錄音格式與模型狀態。"
            ) from exc
        finally:
            if temp_path is not None:
                Path(temp_path).unlink(missing_ok=True)


def _probe_audio_duration(path: str, max_duration_seconds: int) -> float | None:
    """Decode frames up to the limit instead of trusting container metadata."""
    try:
        import av
    except ImportError as exc:
        raise SttRuntimeError(
            "本機語音 runtime 缺少 PyAV；請重新安裝 faster-whisper。"
        ) from exc

    try:
        with av.open(path) as container:
            audio_stream = next(
                (stream for stream in container.streams if stream.type == "audio"),
                None,
            )
            if audio_stream is None:
                raise SttRequestError("檔案中沒有音訊軌。")
            decoded_seconds = 0.0
            for frame in container.decode(audio_stream):
                sample_rate = frame.sample_rate or audio_stream.rate
                if not sample_rate:
                    raise SttRequestError("無法判斷音訊取樣率。")
                decoded_seconds += frame.samples / float(sample_rate)
                if decoded_seconds > max_duration_seconds:
                    return decoded_seconds
            return decoded_seconds
    except SttRequestError:
        raise
    except Exception as exc:
        raise SttRequestError("無法讀取音訊檔，請重新錄音。") from exc


def _join_segments(segments: Iterable[Any]) -> str:
    return "".join(str(getattr(segment, "text", "")) for segment in segments).strip()


def _optional_str(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
