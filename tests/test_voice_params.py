"""Unit tests for VoiceParams + per-chat persistence + /voice command parsing.

No network, no Ollama, no AivisSpeech engine — pure model/DB/handler logic."""
from __future__ import annotations

import pytest

from openclaw_adapter.quiz_db import QuizDatabase
from openclaw_adapter.quiz_vocab_audio import (
    VoiceParams,
    build_vocab_audio_cache_path,
)


def _db(tmp_path) -> QuizDatabase:
    return QuizDatabase(tmp_path / "quiz.sqlite3")


# ── VoiceParams model ─────────────────────────────────────────────────────────


def test_defaults_are_engine_neutral():
    p = VoiceParams()
    assert (p.speed, p.pitch, p.intonation, p.tempo, p.volume) == (1.0, 0.0, 1.0, 1.0, 1.0)
    assert p.is_default()


def test_clamp_below_and_above_range():
    # speed range 0.5–2.0, pitch -0.15–0.15
    low = VoiceParams(speed=0.1, pitch=-9.0, intonation=-1.0, tempo=-1.0, volume=-1.0)
    assert low.speed == 0.5
    assert low.pitch == -0.15
    assert low.intonation == 0.0
    assert low.tempo == 0.0
    assert low.volume == 0.0
    high = VoiceParams(speed=99.0, pitch=9.0, intonation=99.0, tempo=99.0, volume=99.0)
    assert high.speed == 2.0
    assert high.pitch == 0.15
    assert high.intonation == 2.0
    assert high.tempo == 2.0
    assert high.volume == 2.0


def test_with_param_clamps_and_rejects_unknown():
    p = VoiceParams().with_param("speed", 5.0)
    assert p.speed == 2.0
    with pytest.raises(KeyError):
        VoiceParams().with_param("nope", 1.0)


def test_step_uses_per_param_step_and_clamps():
    p = VoiceParams()
    assert p.step("speed", 1).speed == 1.1
    assert p.step("pitch", 1).pitch == 0.01
    # stepping down past the floor clamps, never goes below range
    assert VoiceParams(speed=0.5).step("speed", -1).speed == 0.5


def test_apply_to_query_overwrites_all_five_scale_keys():
    p = VoiceParams(speed=1.5, pitch=0.1, intonation=1.2, tempo=0.8, volume=1.3)
    query = {"speedScale": 1.0, "pitchScale": 0.0, "intonationScale": 1.0,
             "tempoDynamicsScale": 1.0, "volumeScale": 1.0, "kana": "テスト"}
    out = p.apply_to_query(query)
    assert out["speedScale"] == 1.5
    assert out["pitchScale"] == 0.1
    assert out["intonationScale"] == 1.2
    assert out["tempoDynamicsScale"] == 0.8
    assert out["volumeScale"] == 1.3
    assert out["kana"] == "テスト"  # unrelated keys untouched


def test_fingerprint_stable_and_distinct():
    a = VoiceParams(speed=1.5)
    b = VoiceParams(speed=1.5)
    c = VoiceParams(speed=1.6)
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != c.fingerprint()


def test_cache_path_default_vs_tuned_differs():
    default_path = build_vocab_audio_cache_path(
        cache_dir=__import__("pathlib").Path("/tmp/x"), vocab_id="v1", engine_tag="aivis",
        param_tag="",
    )
    tuned_path = build_vocab_audio_cache_path(
        cache_dir=__import__("pathlib").Path("/tmp/x"), vocab_id="v1", engine_tag="aivis",
        param_tag=VoiceParams(speed=1.5).fingerprint(),
    )
    assert default_path != tuned_path
    assert default_path.name == "v1--aivis.wav"  # backward-compatible default name


# ── per-chat persistence ──────────────────────────────────────────────────────


def test_get_returns_default_when_no_row(tmp_path):
    db = _db(tmp_path)
    assert db.get_voice_params("chat-1").is_default()


def test_set_then_get_roundtrip(tmp_path):
    db = _db(tmp_path)
    db.set_voice_params("chat-1", VoiceParams(speed=1.5, pitch=0.1))
    got = db.get_voice_params("chat-1")
    assert got.speed == 1.5
    assert got.pitch == 0.1


def test_persistence_survives_reopen(tmp_path):
    path = tmp_path / "quiz.sqlite3"
    QuizDatabase(path).set_voice_params("chat-1", VoiceParams(speed=1.7))
    # fresh handle simulates a bot restart
    assert QuizDatabase(path).get_voice_params("chat-1").speed == 1.7


def test_set_is_per_chat(tmp_path):
    db = _db(tmp_path)
    db.set_voice_params("chat-1", VoiceParams(speed=1.5))
    db.set_voice_params("chat-2", VoiceParams(speed=0.8))
    assert db.get_voice_params("chat-1").speed == 1.5
    assert db.get_voice_params("chat-2").speed == 0.8


def test_upsert_overwrites(tmp_path):
    db = _db(tmp_path)
    db.set_voice_params("chat-1", VoiceParams(speed=1.5))
    db.set_voice_params("chat-1", VoiceParams(speed=0.9))
    assert db.get_voice_params("chat-1").speed == 0.9


# ── /voice command parsing (handler-level, no Telegram) ───────────────────────


def _settings(tmp_path):
    from assistant_runtime import AssistantSettings

    return AssistantSettings(quiz_db_path=str(tmp_path / "quiz.sqlite3"))


def test_voice_handler_show_returns_text_and_keyboard(tmp_path):
    from openclaw_adapter.voice_command import build_voice_handler

    handler = build_voice_handler(_settings(tmp_path))
    result = handler("", "chat-1")
    assert isinstance(result, tuple)
    text, markup = result
    assert "語音參數" in text
    assert "inline_keyboard" in markup
    # one ➖/value/➕ row per param + reset row
    assert len(markup["inline_keyboard"]) == 6


def test_voice_handler_alias_set_persists(tmp_path):
    from openclaw_adapter.voice_command import build_voice_handler

    settings = _settings(tmp_path)
    handler = build_voice_handler(settings)
    handler("rate 1.5", "chat-1")  # alias rate → speed
    assert QuizDatabase(settings.quiz_db_path).get_voice_params("chat-1").speed == 1.5


def test_voice_handler_reset(tmp_path):
    from openclaw_adapter.voice_command import build_voice_handler

    settings = _settings(tmp_path)
    handler = build_voice_handler(settings)
    handler("speed 1.8", "chat-1")
    handler("reset", "chat-1")
    assert QuizDatabase(settings.quiz_db_path).get_voice_params("chat-1").is_default()


def test_voice_handler_rejects_unknown_param(tmp_path):
    from openclaw_adapter.voice_command import build_voice_handler

    handler = build_voice_handler(_settings(tmp_path))
    out = handler("wobble 1.0", "chat-1")
    assert isinstance(out, str)
    assert "未知參數" in out


def test_voice_callback_steps_and_persists(tmp_path):
    from openclaw_adapter.voice_command import build_voice_callback_handler

    settings = _settings(tmp_path)
    cb = build_voice_callback_handler(settings)
    toast, new_text, markup = cb("speed:+", "old", "chat-1")
    assert QuizDatabase(settings.quiz_db_path).get_voice_params("chat-1").speed == 1.1
    assert markup is not None
    toast, new_text, markup = cb("reset", "old", "chat-1")
    assert QuizDatabase(settings.quiz_db_path).get_voice_params("chat-1").is_default()
