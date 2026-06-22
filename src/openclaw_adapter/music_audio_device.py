"""Switch the macOS audio **output device** for OpenClaw music (issue #35).

``/musicmute`` etc. (music_volume.py) move the system *volume*; this module moves
where that audio comes OUT — between the Mac mini's built-in speakers and any
connected Bluetooth/AirPlay sink (e.g. the "XGIMI Z8X" projector). That matters
because ``afplay`` plays to whatever the current default output device is; if the
default points at a powered-off Bluetooth sink, playback fails with
``AudioQueueStart failed`` even though the bot is healthy.

The lever is the ``SwitchAudioSource`` CLI (brew: ``switchaudio-osx``). The bot
runs under launchd whose ``PATH`` is just ``/usr/bin:/bin:/usr/sbin:/sbin`` — so
we resolve the binary via ``shutil.which`` AND fall back to the known Homebrew
location, otherwise the running service would never find it.

Primitives are module-level so tests can stub the subprocess and assert the
list/switch logic without touching the real audio hardware.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from assistant_runtime import AssistantSettings

logger = logging.getLogger(__name__)

# launchd's PATH omits /opt/homebrew/bin, so probe the known install location too.
_BINARY_NAME = "SwitchAudioSource"
_BREW_PATHS = ("/opt/homebrew/bin/SwitchAudioSource", "/usr/local/bin/SwitchAudioSource")
_RUN_TIMEOUT = 5


class AudioDeviceError(RuntimeError):
    """Raised when a SwitchAudioSource call actually fails — missing binary,
    non-zero exit, or timeout — so the command layer reports the real failure
    instead of a false success (no silent fallback)."""


def _switchaudio_binary() -> str:
    found = shutil.which(_BINARY_NAME)
    if found:
        return found
    for candidate in _BREW_PATHS:
        if Path(candidate).is_file():
            return candidate
    raise AudioDeviceError(
        "找不到 SwitchAudioSource（請先 brew install switchaudio-osx）。"
    )


def _run_switchaudio(args: list[str]) -> str:
    binary = _switchaudio_binary()
    try:
        proc = subprocess.run(
            [binary, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_RUN_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioDeviceError("SwitchAudioSource 逾時，未能切換音源") from exc
    except OSError as exc:
        raise AudioDeviceError(f"無法執行 SwitchAudioSource：{exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise AudioDeviceError(detail or f"SwitchAudioSource 失敗（return {proc.returncode}）")
    return (proc.stdout or b"").decode("utf-8", "replace")


# --- device primitives (module-level so tests can monkeypatch) -------------
def list_output_devices() -> list[str]:
    out = _run_switchaudio(["-a", "-t", "output"])
    return [line.strip() for line in out.splitlines() if line.strip()]


def current_output_device() -> str:
    return _run_switchaudio(["-c", "-t", "output"]).strip()


def set_output_device(name: str) -> None:
    _run_switchaudio(["-s", name, "-t", "output"])


# --- command / callback entry points ---------------------------------------
def _devices_view_payload() -> "tuple[str, dict]":
    """Build the device-picker (text, inline_keyboard). Devices are re-listed at
    render time so a button's index always maps to the current device list."""
    devices = list_output_devices()
    try:
        current = current_output_device()
    except AudioDeviceError:
        current = ""
    lines = ["🔈 播放音源（目前的輸出裝置以 ✅ 標記）"]
    rows: list[list[dict]] = []
    for idx, name in enumerate(devices):
        mark = "✅ " if name == current else ""
        lines.append(f"・{mark}{name}")
        rows.append([{"text": f"{mark}{name}", "callback_data": f"music:setdev:{idx}"}])
    rows.append([{"text": "↩︎ 返回音樂選單", "callback_data": "music:menu"}])
    return "\n".join(lines), {"inline_keyboard": rows}


def audio_devices_view(settings: AssistantSettings) -> "tuple[str, dict]":
    """``music:dev`` — show the output-device picker. On failure (no binary, etc.)
    return an error message and no keyboard rather than a misleading empty list."""
    try:
        return _devices_view_payload()
    except AudioDeviceError as exc:
        return f"無法取得音源裝置清單：{exc}", {"inline_keyboard": []}


def set_output_device_by_index(settings: AssistantSettings, index: int) -> "tuple[str, str, dict]":
    """``music:setdev:<i>`` — switch to the i-th output device, then redraw the
    picker so the ✅ moves. Returns ``(toast, text, markup)``."""
    try:
        devices = list_output_devices()
        if index < 0 or index >= len(devices):
            return "找不到這個音源裝置（清單可能已變動）。", None, None
        target = devices[index]
        set_output_device(target)
    except AudioDeviceError as exc:
        logger.warning("music audio device: switch failed: %s", exc)
        return f"切換音源失敗：{exc}", None, None
    text, markup = _devices_view_payload()
    return f"已切換播放音源：{target}", text, markup
