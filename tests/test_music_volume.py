"""Issue #35 — /musicmute, /musiclouder, /musiclower.

The macOS mixer primitives (``_set_system_volume`` / ``_set_system_muted``) are
stubbed so these assert the volume/mute *logic* — persistence, one-step clamped
adjustments, and auto-unmute on adjust — without touching the real system mixer.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from openclaw_adapter import music_browser as mb
from openclaw_adapter import music_volume as mv


@pytest.fixture
def settings(tmp_path):
    return SimpleNamespace(
        openclaw_music_dir=str(tmp_path / "Music"),
        openclaw_music_index_path=str(tmp_path / ".t" / "idx.json"),
        openclaw_music_player_state_path=str(tmp_path / ".t" / "state.json"),
        openclaw_music_best_path=str(tmp_path / ".t" / "best.json"),
        openclaw_music_token_cache_path=str(tmp_path / ".t" / "tokens.json"),
        openclaw_music_volume_state_path=str(tmp_path / ".t" / "vol.json"),
    )


@pytest.fixture
def mixer(monkeypatch):
    """Record every push to the (stubbed) system mixer."""
    calls = {"volume": [], "muted": []}
    monkeypatch.setattr(mv, "_set_system_volume", lambda v: calls["volume"].append(v))
    monkeypatch.setattr(mv, "_set_system_muted", lambda m: calls["muted"].append(m))
    return calls


def _state(settings):
    return mv.VolumeStore(settings.openclaw_music_volume_state_path).load()


# --- store defaults --------------------------------------------------------
def test_default_state_is_70_unmuted(settings):
    st = _state(settings)
    assert st == {"volume": 70, "muted": False}


# --- mute ------------------------------------------------------------------
def test_mute_persists_and_pushes_muted(settings, mixer):
    msg = mv.mute_music(settings)
    assert "靜音" in msg
    assert _state(settings) == {"volume": 70, "muted": True}
    assert mixer["muted"][-1] is True


# --- louder / lower one step ----------------------------------------------
def test_louder_raises_one_step(settings, mixer):
    mv.louder_music(settings)
    assert _state(settings)["volume"] == 80
    assert mixer["volume"][-1] == 80
    assert mixer["muted"][-1] is False


def test_lower_drops_one_step(settings, mixer):
    mv.lower_music(settings)
    assert _state(settings)["volume"] == 60


# --- clamping --------------------------------------------------------------
def test_louder_clamps_at_max(settings, mixer):
    mv.VolumeStore(settings.openclaw_music_volume_state_path).save(volume=95, muted=False)
    mv.louder_music(settings)
    assert _state(settings)["volume"] == 100
    mv.louder_music(settings)
    assert _state(settings)["volume"] == 100  # stays clamped


def test_lower_clamps_at_min(settings, mixer):
    mv.VolumeStore(settings.openclaw_music_volume_state_path).save(volume=5, muted=False)
    mv.lower_music(settings)
    assert _state(settings)["volume"] == 0
    mv.lower_music(settings)
    assert _state(settings)["volume"] == 0


# --- auto-unmute on adjust -------------------------------------------------
def test_louder_unmutes_first(settings, mixer):
    mv.mute_music(settings)
    assert _state(settings)["muted"] is True
    msg = mv.louder_music(settings)
    assert _state(settings) == {"volume": 80, "muted": False}
    assert mixer["muted"][-1] is False
    assert "靜音" not in msg


def test_lower_unmutes_first(settings, mixer):
    mv.mute_music(settings)
    mv.lower_music(settings)
    assert _state(settings) == {"volume": 60, "muted": False}


# --- no regression: volume ops never touch player state / playbest ---------
def test_volume_ops_do_not_touch_player_state(settings, mixer, tmp_path):
    from pathlib import Path

    sp = Path(settings.openclaw_music_player_state_path)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text('{"pid": 1234, "name": "x", "path": "/m/x.flac", "start": "s"}')
    before = sp.read_text()
    mv.mute_music(settings)
    mv.louder_music(settings)
    mv.lower_music(settings)
    assert sp.read_text() == before  # player state untouched by volume control


# --- callback routing ------------------------------------------------------
def test_music_callbacks_route_volume(settings, mixer):
    cb = mb.build_music_callback_handler(settings)
    toast, new_text, markup = cb("mute", "", "c")
    assert "靜音" in toast and new_text is None and markup is None
    assert _state(settings)["muted"] is True
    toast, _, _ = cb("louder", "", "c")
    assert _state(settings) == {"volume": 80, "muted": False}
    toast, _, _ = cb("lower", "", "c")
    assert _state(settings)["volume"] == 70
