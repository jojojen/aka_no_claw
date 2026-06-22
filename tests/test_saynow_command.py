"""Tests for /saynow — speak via AivisSpeech out the Mac mini speakers (#39).

Unlike /say (which sends a Telegram voice file), /saynow plays the synthesized
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
    monkeypatch.setattr(vc, "stop_playback", lambda s: "")
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


def test_saynow_stops_music_before_playing(settings, monkeypatch, tmp_path):
    # The Bluetooth output can't mix two streams, so /saynow must stop OpenClaw
    # music BEFORE afplay runs, otherwise afplay fails with AudioQueueStart.
    _stub_synth(monkeypatch, tmp_path)
    events: list[str] = []
    monkeypatch.setattr(vc, "stop_playback", lambda s: events.append("stop") or "已停止")
    monkeypatch.setattr(vc, "play_audio_file", lambda p: (events.append("play") or (True, "")))

    handler = vc.build_saynow_handler(settings)
    handler("ただいま音楽を停止いたします", "chat1")

    assert events == ["stop", "play"]


def test_saynow_plays_even_if_stop_music_raises(settings, monkeypatch, tmp_path):
    # A failure to stop music must not abort the announcement.
    _stub_synth(monkeypatch, tmp_path)
    def boom(s):
        raise RuntimeError("state file locked")
    monkeypatch.setattr(vc, "stop_playback", boom)
    played: list[str] = []
    monkeypatch.setattr(vc, "play_audio_file", lambda p: (played.append(p) or (True, "")))

    handler = vc.build_saynow_handler(settings)
    reply = handler("テスト", "chat1")

    assert len(played) == 1
    assert "Mac mini 播放" in reply


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
    ok, err = vc.play_audio_file("/tmp/x.wav")
    assert ok is True
    assert err == ""


def test_play_audio_file_nonzero_returns_error(monkeypatch):
    monkeypatch.setattr(vc.shutil, "which", lambda b: "/usr/bin/afplay")
    monkeypatch.setattr(
        vc.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    ok, err = vc.play_audio_file("/tmp/x.wav")
    assert ok is False
    assert "boom" in err
