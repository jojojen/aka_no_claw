"""Volume / mute control for OpenClaw music playback (issue #35).

``afplay`` (used by #33/#34) sets its gain only at launch and cannot be adjusted
while a track is running, so the controllable lever here is the **macOS system
output volume** — set via ``osascript`` (``set volume output volume`` /
``set volume output muted``). This affects all system audio, not just music;
that trade-off is accepted by the issue as a valid Mac-compatible mechanism.

The intended ``{volume, muted}`` is also persisted to a gitignored runtime JSON
so later commands (and a bot restart) know the level to restore when unmuting.

Commands:

    /musicmute     mute output, remember muted state
    /musiclouder   unmute (if muted) then raise volume one step, clamped
    /musiclower    unmute (if muted) then lower volume one step, clamped

The ``osascript`` primitives are module-level so tests can stub them and assert
the volume/mute logic without touching the real system mixer.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from assistant_runtime import AssistantSettings

logger = logging.getLogger(__name__)

_VOLUME_MIN = 0
_VOLUME_MAX = 100
_VOLUME_STEP = 10
_VOLUME_DEFAULT = 70


# --- system mixer primitives (module-level so tests can monkeypatch) -------
def _set_system_volume(volume: int) -> None:
    subprocess.run(
        ["osascript", "-e", f"set volume output volume {int(volume)}"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )


def _set_system_muted(muted: bool) -> None:
    flag = "true" if muted else "false"
    subprocess.run(
        ["osascript", "-e", f"set volume output muted {flag}"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )


def _clamp(volume: int) -> int:
    return max(_VOLUME_MIN, min(_VOLUME_MAX, int(volume)))


class VolumeStore:
    """Gitignored JSON holding the intended ``{volume, muted}`` state."""

    def __init__(self, path: str) -> None:
        self._path = path

    def load(self) -> dict:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        volume = data.get("volume")
        volume = _clamp(volume) if isinstance(volume, (int, float)) else _VOLUME_DEFAULT
        return {"volume": volume, "muted": bool(data.get("muted", False))}

    def save(self, *, volume: int, muted: bool) -> dict:
        state = {"volume": _clamp(volume), "muted": bool(muted)}
        p = Path(self._path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
        return state


def _apply(volume: int, muted: bool) -> None:
    """Push the intended state to the system mixer. The volume level is always
    set so unmuting reveals the persisted level rather than a stale one."""
    _set_system_volume(volume)
    _set_system_muted(muted)


def _status_text(volume: int, muted: bool) -> str:
    if muted:
        return f"已將音樂靜音。（音量保留在 {volume}/100，提高/降低音量會自動取消靜音）"
    return f"目前音量：{volume}/100。"


# --- command entry points --------------------------------------------------
def mute_music(settings: AssistantSettings) -> str:
    store = VolumeStore(settings.openclaw_music_volume_state_path)
    state = store.load()
    saved = store.save(volume=state["volume"], muted=True)
    _apply(saved["volume"], True)
    return _status_text(saved["volume"], True)


def _adjust(settings: AssistantSettings, delta: int) -> str:
    # /musiclouder and /musiclower always unmute first, then apply a single
    # clamped step to the persisted level — so the user never jumps from muted
    # straight to a surprising loud volume.
    store = VolumeStore(settings.openclaw_music_volume_state_path)
    state = store.load()
    new_volume = _clamp(state["volume"] + delta)
    saved = store.save(volume=new_volume, muted=False)
    _apply(saved["volume"], False)
    return _status_text(saved["volume"], False)


def louder_music(settings: AssistantSettings) -> str:
    return _adjust(settings, _VOLUME_STEP)


def lower_music(settings: AssistantSettings) -> str:
    return _adjust(settings, -_VOLUME_STEP)
