from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from assistant_runtime import AssistantSettings


class QuizVocabAudioError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SynthesizedVocabAudio:
    output_path: Path
    engine_tag: str
    engine_label: str


@dataclass(frozen=True, slots=True)
class AivisSpeechSynthesizer:
    endpoint: str
    timeout_seconds: int = 20
    speaker_id: int | None = None
    engine_tag: str = "aivis"
    engine_label: str = "AivisSpeech"

    def synthesize_to_path(self, *, text: str, output_path: Path) -> Path:
        cleaned = (text or "").strip()
        if not cleaned:
            raise QuizVocabAudioError("example sentence is empty")
        speaker_id = self.speaker_id if self.speaker_id is not None else self._resolve_speaker_id()
        query = self._post_json(
            "/audio_query",
            params={"text": cleaned, "speaker": str(speaker_id)},
            body=b"",
            content_type="application/json",
        )
        audio = self._post_bytes(
            "/synthesis",
            params={"speaker": str(speaker_id)},
            body=json.dumps(query, ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(audio)
        return output_path

    def _resolve_speaker_id(self) -> int:
        speakers = self._get_json("/speakers")
        if not isinstance(speakers, list) or not speakers:
            raise QuizVocabAudioError("AivisSpeech returned no speakers")
        first = speakers[0] if isinstance(speakers[0], dict) else None
        styles = (first or {}).get("styles") if isinstance(first, dict) else None
        if not isinstance(styles, list) or not styles or not isinstance(styles[0], dict):
            raise QuizVocabAudioError("AivisSpeech speaker list has no styles")
        style_id = styles[0].get("id")
        if not isinstance(style_id, int):
            raise QuizVocabAudioError("AivisSpeech style id missing")
        return style_id

    def _build_url(self, path: str, params: dict[str, str] | None = None) -> str:
        base = self.endpoint.rstrip("/")
        qs = f"?{urlencode(params)}" if params else ""
        return f"{base}{path}{qs}"

    def _get_json(self, path: str) -> object:
        req = Request(self._build_url(path), headers={"Accept": "application/json"}, method="GET")
        return self._read_json(req)

    def _post_json(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: bytes,
        content_type: str,
    ) -> object:
        req = Request(
            self._build_url(path, params),
            data=body,
            headers={"Accept": "application/json", "Content-Type": content_type},
            method="POST",
        )
        return self._read_json(req)

    def _post_bytes(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: bytes,
        content_type: str,
    ) -> bytes:
        req = Request(
            self._build_url(path, params),
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        try:
            with urlopen(req, timeout=self.timeout_seconds) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            raise QuizVocabAudioError(f"AivisSpeech synthesis failed: {exc}") from exc

    def _read_json(self, request: Request) -> object:
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as exc:
            raise QuizVocabAudioError(f"AivisSpeech request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise QuizVocabAudioError("AivisSpeech returned invalid JSON") from exc


@dataclass(frozen=True, slots=True)
class MacOSSaySynthesizer:
    voice_name: str = "Kyoko"
    engine_tag: str = "macos-kyoko"

    @property
    def engine_label(self) -> str:
        return f"macOS {self.voice_name}"

    def synthesize_to_path(self, *, text: str, output_path: Path) -> Path:
        cleaned = (text or "").strip()
        if not cleaned:
            raise QuizVocabAudioError("example sentence is empty")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        aiff_path = output_path.with_suffix(".aiff")
        try:
            subprocess.run(
                ["say", "-v", self.voice_name, "-o", str(aiff_path), cleaned],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["afconvert", "-f", "WAVE", "-d", "LEI16@22050", str(aiff_path), str(output_path)],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise QuizVocabAudioError(f"macOS say synthesis failed: {exc}") from exc
        finally:
            if aiff_path.exists():
                aiff_path.unlink()
        return output_path


@dataclass(frozen=True, slots=True)
class FallbackSynthesizer:
    synths: tuple[object, ...]

    def synthesize_to_cache(
        self,
        *,
        text: str,
        cache_dir: Path,
        vocab_id: str,
    ) -> SynthesizedVocabAudio:
        errors: list[str] = []
        for synth in self.synths:
            output_path = build_vocab_audio_cache_path(
                cache_dir=cache_dir,
                vocab_id=vocab_id,
                engine_tag=synth.engine_tag,
            )
            if output_path.exists():
                return SynthesizedVocabAudio(
                    output_path=output_path,
                    engine_tag=synth.engine_tag,
                    engine_label=synth.engine_label,
                )
            try:
                synth.synthesize_to_path(text=text, output_path=output_path)
                return SynthesizedVocabAudio(
                    output_path=output_path,
                    engine_tag=synth.engine_tag,
                    engine_label=synth.engine_label,
                )
            except QuizVocabAudioError as exc:
                errors.append(str(exc))
        raise QuizVocabAudioError(" ; ".join(errors) or "no synthesizer available")


def build_vocab_audio_cache_dir(*, settings: AssistantSettings) -> Path:
    return Path(settings.quiz_db_path).resolve().parent.parent / ".openclaw_tmp" / "quiz_vocab_audio"


def build_vocab_audio_cache_path(*, cache_dir: Path, vocab_id: str, engine_tag: str) -> Path:
    return cache_dir / f"{vocab_id}--{engine_tag}.wav"


def build_vocab_synthesizer(settings: AssistantSettings) -> FallbackSynthesizer:
    return FallbackSynthesizer(
        synths=(
            AivisSpeechSynthesizer(
                endpoint=settings.openclaw_local_tts_endpoint,
                timeout_seconds=max(1, settings.openclaw_local_tts_timeout_seconds),
                speaker_id=settings.openclaw_local_tts_speaker_id,
            ),
            MacOSSaySynthesizer(),
        )
    )
