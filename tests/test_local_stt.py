from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from openclaw_adapter.local_stt import (
    LocalWhisperTranscriber,
    SttPayloadTooLarge,
    SttRequestError,
    build_audio_request,
    _probe_audio_duration,
)


def test_build_audio_request_accepts_uploaded_audio() -> None:
    audio = b"fake webm bytes"

    request = build_audio_request(
        audio,
        mime_type="audio/webm;codecs=opus",
        max_audio_bytes=1024,
    )

    assert request.data == audio
    assert request.mime_type == "audio/webm"
    assert request.suffix == ".webm"
    assert request.language is None


@pytest.mark.parametrize(
    ("audio", "mime_type", "message"),
    [
        (b"", "audio/webm", "空"),
        (b"a", "text/plain", "不支援"),
    ],
)
def test_build_audio_request_rejects_invalid_input(audio, mime_type, message) -> None:
    with pytest.raises(SttRequestError, match=message):
        build_audio_request(
            audio,
            mime_type=mime_type,
            max_audio_bytes=1024,
        )


def test_build_audio_request_rejects_oversized_audio() -> None:
    with pytest.raises(SttPayloadTooLarge):
        build_audio_request(
            b"12345",
            mime_type="audio/webm",
            max_audio_bytes=4,
        )


def test_transcriber_lazy_loads_once_reuses_model_and_deletes_temp_file() -> None:
    created: list[dict[str, object]] = []
    seen_paths: list[str] = []

    class FakeModel:
        def transcribe(self, path, **kwargs):
            assert Path(path).exists()
            seen_paths.append(path)
            assert kwargs == {"language": None, "beam_size": 5, "vad_filter": True}
            return (
                [SimpleNamespace(text=" 你好"), SimpleNamespace(text="，世界。")],
                SimpleNamespace(language="zh", language_probability=0.98, duration=1.5),
            )

    def factory(model_name, **kwargs):
        created.append({"model_name": model_name, **kwargs})
        return FakeModel()

    transcriber = LocalWhisperTranscriber(
        model_name="base",
        device="cpu",
        compute_type="int8",
        download_root=None,
        model_factory=factory,
        duration_probe=lambda _path, _limit: 1.5,
    )
    request = build_audio_request(
        b"audio",
        mime_type="audio/webm",
        max_audio_bytes=1024,
    )

    first = transcriber.transcribe(request)
    second = transcriber.transcribe(request)

    assert first.transcript == "你好，世界。"
    assert first.language == "zh"
    assert second.transcript == first.transcript
    assert len(created) == 1
    assert created[0] == {
        "model_name": "base",
        "device": "cpu",
        "compute_type": "int8",
        "download_root": None,
    }
    assert len(seen_paths) == 2
    assert all(not Path(path).exists() for path in seen_paths)


def test_transcriber_rejects_long_audio_before_loading_model() -> None:
    model_loaded = False

    def factory(*args, **kwargs):
        nonlocal model_loaded
        model_loaded = True
        return object()

    transcriber = LocalWhisperTranscriber(
        max_duration_seconds=10,
        model_factory=factory,
        duration_probe=lambda _path, _limit: 10.1,
    )
    request = build_audio_request(
        b"audio",
        mime_type="audio/webm",
        max_audio_bytes=1024,
    )

    with pytest.raises(SttPayloadTooLarge, match="10 秒"):
        transcriber.transcribe(request)

    assert not model_loaded


def test_duration_probe_rejects_malformed_media(monkeypatch) -> None:
    fake_av = SimpleNamespace(open=lambda _path: (_ for _ in ()).throw(ValueError("bad")))
    monkeypatch.setitem(sys.modules, "av", fake_av)

    with pytest.raises(SttRequestError, match="無法讀取音訊"):
        _probe_audio_duration("broken.webm", 10)


def test_transcriber_serializes_concurrent_inference() -> None:
    active = 0
    max_active = 0
    state_lock = threading.Lock()

    class FakeModel:
        def transcribe(self, path, **kwargs):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.02)
            with state_lock:
                active -= 1
            return [SimpleNamespace(text=" ok")], SimpleNamespace(duration=1.0)

    transcriber = LocalWhisperTranscriber(
        model_factory=lambda *args, **kwargs: FakeModel(),
        duration_probe=lambda _path, _limit: 1.0,
    )
    request = build_audio_request(
        b"audio",
        mime_type="audio/webm",
        max_audio_bytes=1024,
    )
    threads = [
        threading.Thread(target=transcriber.transcribe, args=(request,))
        for _ in range(2)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert max_active == 1


# ── trusted duration / beam_size / prewarm ───────────────────────────────────

def _fake_model_factory_ok(expected_beam):
    class FakeModel:
        def transcribe(self, path, **kwargs):
            assert kwargs["beam_size"] == expected_beam
            return (
                [SimpleNamespace(text="測試")],
                SimpleNamespace(language="zh", language_probability=0.9, duration=1.0),
            )

    return lambda *args, **kwargs: FakeModel()


def test_trusted_duration_skips_probe() -> None:
    def _probe_must_not_run(_path, _limit):
        raise AssertionError("duration probe must be skipped for trusted duration")

    transcriber = LocalWhisperTranscriber(
        model_factory=_fake_model_factory_ok(5),
        duration_probe=_probe_must_not_run,
    )
    request = build_audio_request(
        b"audio",
        mime_type="audio/webm",
        max_audio_bytes=1024,
        trusted_duration_seconds=2.0,
    )

    result = transcriber.transcribe(request)

    assert result.transcript == "測試"


def test_trusted_duration_over_limit_rejected_before_model_load() -> None:
    loaded = []

    def factory(*args, **kwargs):
        loaded.append(True)
        return object()

    transcriber = LocalWhisperTranscriber(
        max_duration_seconds=10,
        model_factory=factory,
        duration_probe=lambda _path, _limit: pytest.fail("probe must not run"),
    )
    request = build_audio_request(
        b"audio",
        mime_type="audio/webm",
        max_audio_bytes=1024,
        trusted_duration_seconds=10.5,
    )

    with pytest.raises(SttPayloadTooLarge, match="10 秒"):
        transcriber.transcribe(request)
    assert not loaded


@pytest.mark.parametrize("bad", [0, -1.5, True])
def test_build_audio_request_rejects_invalid_trusted_duration(bad) -> None:
    with pytest.raises(SttRequestError):
        build_audio_request(
            b"audio",
            mime_type="audio/webm",
            max_audio_bytes=1024,
            trusted_duration_seconds=bad,
        )


def test_beam_size_flows_from_constructor_into_transcribe() -> None:
    transcriber = LocalWhisperTranscriber(
        beam_size=1,
        model_factory=_fake_model_factory_ok(1),
        duration_probe=lambda _path, _limit: 1.0,
    )
    request = build_audio_request(
        b"audio",
        mime_type="audio/webm",
        max_audio_bytes=1024,
    )

    assert transcriber.transcribe(request).transcript == "測試"


def test_from_settings_passes_beam_size() -> None:
    from assistant_runtime.settings import AssistantSettings

    settings = AssistantSettings(openclaw_stt_beam_size=2)
    transcriber = LocalWhisperTranscriber.from_settings(settings)

    assert transcriber.beam_size == 2


def test_prewarm_loads_model_once_and_swallows_errors() -> None:
    loads = []

    def factory(*args, **kwargs):
        loads.append(True)
        return object()

    transcriber = LocalWhisperTranscriber(model_factory=factory)
    transcriber.prewarm()
    transcriber.prewarm()
    assert len(loads) == 1

    def broken_factory(*args, **kwargs):
        raise RuntimeError("no model")

    broken = LocalWhisperTranscriber(model_factory=broken_factory)
    broken.prewarm()  # must not raise
