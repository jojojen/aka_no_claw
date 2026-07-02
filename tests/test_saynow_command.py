"""Tests for /saynow — speak via AivisSpeech out the Mac mini speakers (#39).

Unlike /generateaudio (which sends a generated audio file), /saynow plays the synthesized
WAV locally through afplay. These tests mock the synthesizer and the player so
no audio device or network is touched.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest

from assistant_runtime.settings import get_settings
from openclaw_adapter import voice_command as vc


@pytest.fixture
def settings(tmp_path):
    return dataclasses.replace(get_settings(), quiz_db_path=str(tmp_path / "quiz.sqlite3"))


def _stub_synth(monkeypatch, tmp_path):
    audio = SimpleNamespace(output_path=Path(tmp_path / "out.wav"), engine_label="AivisSpeech")
    monkeypatch.setattr(
        vc, "build_vocab_synthesizer",
        lambda settings, params: SimpleNamespace(
            synthesize_text=lambda *, text, cache_dir: audio
        ),
    )
    monkeypatch.setattr(vc, "build_vocab_audio_cache_dir", lambda *, settings: tmp_path)
    # Don't let tests touch the real music state file / kill live playback.
    # By default nothing is interrupted (no token) and nothing is resumed.
    monkeypatch.setattr(vc, "acquire_audio_session", lambda s, *, reason="": None)
    monkeypatch.setattr(vc, "resume_after_voice", lambda s, token: False)
    return audio


def test_saynow_empty_returns_usage(settings):
    handler = vc.build_saynow_handler(settings)
    assert "用法" in handler("", "chat1")


def test_saynow_plays_locally_and_reports(settings, monkeypatch, tmp_path):
    audio = _stub_synth(monkeypatch, tmp_path)
    played: list[str] = []
    monkeypatch.setattr(vc, "play_audio_file", lambda p: (played.append(p) or (True, "")))

    handler = vc.build_saynow_handler(settings)
    reply = handler("ご主人様、おはようございます", "chat1")

    assert played == [str(audio.output_path)]
    assert "Mac mini 播放" in reply
    assert "AivisSpeech" in reply


def test_saynow_acquires_session_then_plays_then_resumes(settings, monkeypatch, tmp_path):
    # The Bluetooth output can't mix two streams, so /saynow must free the device
    # (acquire) BEFORE afplay runs, then resume the interrupted music afterwards.
    _stub_synth(monkeypatch, tmp_path)
    events: list[str] = []
    sentinel = object()  # stand-in ResumeToken
    monkeypatch.setattr(
        vc, "acquire_audio_session",
        lambda s, *, reason="": events.append(("acquire", reason)) or sentinel,
    )
    monkeypatch.setattr(vc, "play_audio_file", lambda p: (events.append("play") or (True, "")))
    resumed_with: list[object] = []
    monkeypatch.setattr(
        vc, "resume_after_voice",
        lambda s, token: (resumed_with.append(token) or events.append("resume")) and True or True,
    )

    handler = vc.build_saynow_handler(settings)
    reply = handler("ただいま音楽を停止いたします", "chat1")

    assert events == [("acquire", "saynow"), "play", "resume"]
    assert resumed_with == [sentinel]  # the captured token is handed back to resume
    assert "已恢復先前中斷的音樂播放" in reply


def test_saynow_resumes_even_if_playback_fails(settings, monkeypatch, tmp_path):
    # Resume must run in a finally — a failed announcement still restores music.
    _stub_synth(monkeypatch, tmp_path)
    monkeypatch.setattr(vc, "acquire_audio_session", lambda s, *, reason="": object())
    monkeypatch.setattr(vc, "play_audio_file", lambda p: (False, "找不到 afplay"))
    resumed: list[bool] = []
    monkeypatch.setattr(vc, "resume_after_voice", lambda s, token: resumed.append(True) or False)

    handler = vc.build_saynow_handler(settings)
    reply = handler("テスト", "chat1")

    assert resumed == [True]  # resume attempted despite playback failure
    assert "播放失敗" in reply


def test_saynow_works_without_chat_id(settings, monkeypatch, tmp_path):
    # Scheduled runs may have no chat; defaults must be used, playback still works.
    _stub_synth(monkeypatch, tmp_path)
    monkeypatch.setattr(vc, "play_audio_file", lambda p: (True, ""))
    handler = vc.build_saynow_handler(settings)
    reply = handler("音楽を停止しました", "")
    assert "Mac mini 播放" in reply


def test_saynow_playback_failure_is_reported(settings, monkeypatch, tmp_path):
    _stub_synth(monkeypatch, tmp_path)
    monkeypatch.setattr(vc, "play_audio_file", lambda p: (False, "找不到 afplay"))
    handler = vc.build_saynow_handler(settings)
    reply = handler("テスト", "chat1")
    assert "播放失敗" in reply
    assert "afplay" in reply


def test_saynow_synthesis_failure_is_reported(settings, monkeypatch, tmp_path):
    def boom(*, text, cache_dir):
        raise vc.QuizVocabAudioError("engine down")

    monkeypatch.setattr(
        vc, "build_vocab_synthesizer",
        lambda settings, params: SimpleNamespace(synthesize_text=boom),
    )
    monkeypatch.setattr(vc, "build_vocab_audio_cache_dir", lambda *, settings: tmp_path)
    monkeypatch.setattr(vc, "play_audio_file", lambda p: (True, ""))
    handler = vc.build_saynow_handler(settings)
    reply = handler("テスト", "chat1")
    assert "合成失敗" in reply


def test_play_audio_file_missing_afplay(monkeypatch):
    monkeypatch.setattr(vc.shutil, "which", lambda b: None)
    ok, err = vc.play_audio_file("/tmp/x.wav")
    assert ok is False
    assert "afplay" in err


def test_play_audio_file_success(monkeypatch):
    monkeypatch.setattr(vc.shutil, "which", lambda b: "/usr/bin/afplay")
    monkeypatch.setattr(
        vc.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    # On success coreaudiod is never restarted.
    restarts: list[bool] = []
    monkeypatch.setattr(vc, "restart_coreaudiod", lambda: restarts.append(True) or True)
    ok, err = vc.play_audio_file("/tmp/x.wav")
    assert ok is True
    assert err == ""
    assert restarts == []


def test_play_audio_file_nonzero_returns_error(monkeypatch):
    monkeypatch.setattr(vc.shutil, "which", lambda b: "/usr/bin/afplay")
    monkeypatch.setattr(
        vc.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    # coreaudiod restart unavailable (no NOPASSWD) → fail with the real error.
    monkeypatch.setattr(vc, "restart_coreaudiod", lambda: False)
    ok, err = vc.play_audio_file("/tmp/x.wav")
    assert ok is False
    assert "boom" in err


def test_play_audio_file_restarts_coreaudiod_and_retries_on_wedge(monkeypatch):
    # afplay fails first (AudioQueueStart wedge); after a coreaudiod restart the
    # retry succeeds. Proves the self-healing path: restart once, replay, report.
    monkeypatch.setattr(vc.shutil, "which", lambda b: "/usr/bin/afplay")
    results = iter([
        SimpleNamespace(returncode=1, stdout="", stderr="AudioQueueStart failed ('what')"),
        SimpleNamespace(returncode=0, stdout="", stderr=""),
    ])
    monkeypatch.setattr(vc.subprocess, "run", lambda *a, **k: next(results))
    restarts: list[bool] = []
    monkeypatch.setattr(vc, "restart_coreaudiod", lambda: restarts.append(True) or True)
    ok, err = vc.play_audio_file("/tmp/x.wav")
    assert ok is True
    assert restarts == [True]  # restarted exactly once


def test_play_audio_file_no_retry_when_restart_unavailable(monkeypatch):
    # afplay fails and coreaudiod can't be restarted → single attempt, real error.
    monkeypatch.setattr(vc.shutil, "which", lambda b: "/usr/bin/afplay")
    calls: list[str] = []

    def fake_run(args, **k):
        calls.append(args[0])
        return SimpleNamespace(returncode=1, stdout="", stderr="AudioQueueStart failed ('what')")

    monkeypatch.setattr(vc.subprocess, "run", fake_run)
    monkeypatch.setattr(vc, "restart_coreaudiod", lambda: False)
    ok, err = vc.play_audio_file("/tmp/x.wav")
    assert ok is False
    assert calls == ["afplay"]  # no retry without a working coreaudiod restart
    assert "AudioQueueStart" in err
