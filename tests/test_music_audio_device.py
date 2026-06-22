"""Issue #35 — switch the macOS audio OUTPUT device for music playback.

The SwitchAudioSource subprocess is stubbed so these assert the list/switch
*logic* — index→device mapping, ✅-on-current marking, redraw after switch, and
failure surfacing — without touching real audio hardware.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from openclaw_adapter import music_audio_device as mad
from openclaw_adapter import music_browser as mb


@pytest.fixture
def settings(tmp_path):
    return SimpleNamespace(
        openclaw_music_dir=str(tmp_path / "Music"),
        openclaw_music_best_path=str(tmp_path / ".t" / "best.json"),
        openclaw_music_token_cache_path=str(tmp_path / ".t" / "tokens.json"),
    )


@pytest.fixture
def fake_audio(monkeypatch):
    """Stub the device primitives with a mutable in-memory output device list."""
    state = {"devices": ["Mac mini的揚聲器", "XGIMI Z8X"], "current": "XGIMI Z8X"}
    monkeypatch.setattr(mad, "list_output_devices", lambda: list(state["devices"]))
    monkeypatch.setattr(mad, "current_output_device", lambda: state["current"])

    def _set(name):
        state["current"] = name

    monkeypatch.setattr(mad, "set_output_device", _set)
    return state


# --- view ------------------------------------------------------------------
def test_view_lists_devices_and_marks_current(settings, fake_audio):
    text, markup = mad.audio_devices_view(settings)
    assert "Mac mini的揚聲器" in text
    assert "✅ XGIMI Z8X" in text  # current marked
    rows = markup["inline_keyboard"]
    assert rows[0][0]["callback_data"] == "music:setdev:0"
    assert rows[1][0]["callback_data"] == "music:setdev:1"
    assert rows[-1][0]["callback_data"] == "music:menu"  # back button


def test_view_reports_error_without_crashing(settings, monkeypatch):
    def boom():
        raise mad.AudioDeviceError("找不到 SwitchAudioSource")

    monkeypatch.setattr(mad, "list_output_devices", boom)
    text, markup = mad.audio_devices_view(settings)
    assert "無法取得音源裝置清單" in text
    assert markup["inline_keyboard"] == []


# --- switch ----------------------------------------------------------------
def test_setdev_switches_and_redraws(settings, fake_audio):
    toast, text, markup = mad.set_output_device_by_index(settings, 0)
    assert "已切換播放音源：Mac mini的揚聲器" in toast
    assert fake_audio["current"] == "Mac mini的揚聲器"
    assert "✅ Mac mini的揚聲器" in text  # redraw moved the ✅
    assert markup["inline_keyboard"]


def test_setdev_out_of_range_is_reported(settings, fake_audio):
    toast, text, markup = mad.set_output_device_by_index(settings, 9)
    assert "找不到這個音源裝置" in toast
    assert text is None and markup is None
    assert fake_audio["current"] == "XGIMI Z8X"  # unchanged


def test_setdev_failure_is_reported(settings, fake_audio, monkeypatch):
    def boom(_name):
        raise mad.AudioDeviceError("SwitchAudioSource 失敗（return 1）")

    monkeypatch.setattr(mad, "set_output_device", boom)
    toast, text, markup = mad.set_output_device_by_index(settings, 0)
    assert "切換音源失敗" in toast
    assert text is None and markup is None


# --- binary resolution (launchd PATH safety) -------------------------------
def test_binary_prefers_which(monkeypatch):
    monkeypatch.setattr(mad.shutil, "which", lambda b: "/somewhere/SwitchAudioSource")
    assert mad._switchaudio_binary() == "/somewhere/SwitchAudioSource"


def test_binary_falls_back_to_brew_path(monkeypatch):
    monkeypatch.setattr(mad.shutil, "which", lambda b: None)
    monkeypatch.setattr(mad.Path, "is_file", lambda self: str(self) in mad._BREW_PATHS)
    assert mad._switchaudio_binary() in mad._BREW_PATHS


def test_binary_missing_raises(monkeypatch):
    monkeypatch.setattr(mad.shutil, "which", lambda b: None)
    monkeypatch.setattr(mad.Path, "is_file", lambda self: False)
    with pytest.raises(mad.AudioDeviceError, match="switchaudio-osx"):
        mad._switchaudio_binary()


# --- subprocess wrapper ----------------------------------------------------
def test_run_switchaudio_raises_on_nonzero(monkeypatch):
    monkeypatch.setattr(mad, "_switchaudio_binary", lambda: "/x/SwitchAudioSource")
    monkeypatch.setattr(
        mad.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout=b"", stderr=b"boom"),
    )
    with pytest.raises(mad.AudioDeviceError, match="boom"):
        mad._run_switchaudio(["-c", "-t", "output"])


def test_run_switchaudio_raises_on_timeout(monkeypatch):
    monkeypatch.setattr(mad, "_switchaudio_binary", lambda: "/x/SwitchAudioSource")

    def timeout(*a, **k):
        raise mad.subprocess.TimeoutExpired(cmd="SwitchAudioSource", timeout=5)

    monkeypatch.setattr(mad.subprocess, "run", timeout)
    with pytest.raises(mad.AudioDeviceError, match="逾時"):
        mad._run_switchaudio(["-a", "-t", "output"])


def test_list_output_devices_parses_lines(monkeypatch):
    monkeypatch.setattr(mad, "_switchaudio_binary", lambda: "/x/SwitchAudioSource")
    monkeypatch.setattr(
        mad.subprocess, "run",
        lambda *a, **k: SimpleNamespace(
            returncode=0, stdout="Mac mini的揚聲器\nXGIMI Z8X\n".encode(), stderr=b""
        ),
    )
    assert mad.list_output_devices() == ["Mac mini的揚聲器", "XGIMI Z8X"]


# --- callback routing ------------------------------------------------------
def test_music_callback_routes_dev_and_setdev(settings, fake_audio):
    cb = mb.build_music_callback_handler(settings)

    toast, text, markup = cb("dev", "", "c")
    assert toast is None
    assert "✅ XGIMI Z8X" in text

    toast, text, markup = cb("setdev:0", "", "c")
    assert "已切換播放音源" in toast
    assert fake_audio["current"] == "Mac mini的揚聲器"

    toast, text, markup = cb("setdev:bad", "", "c")
    assert "格式錯誤" in toast


def test_music_callback_menu_redraws_menu(settings):
    cb = mb.build_music_callback_handler(settings)
    toast, text, markup = cb("menu", "", "c")
    assert toast is None
    assert "音樂控制" in text
    assert any(
        btn["callback_data"] == "music:dev"
        for row in markup["inline_keyboard"] for btn in row
    )
