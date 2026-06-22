"""Local Bluetooth scan + connect for the ``/bluetooth`` command (issue #38).

Scanning reads ``system_profiler SPBluetoothDataType -json`` — no extra install
is needed and this Mac already reports ``XGIMI Z8X`` there — to list the
currently connected and known not-connected devices with their addresses.

Connecting uses ``blueutil --connect <address>``. ``blueutil`` is optional: if it
is not installed we return a clear ``brew install blueutil`` help message instead
of failing silently, so the scan/list surface keeps working without it.

MAC addresses are never shown in normal user-facing text. Each device button
carries an opaque token that maps to its address via a gitignored runtime cache
(mirroring the music browser's TokenCache), and the resolved address is
re-validated as a real MAC before it is ever handed to ``blueutil``.

Both ``system_profiler`` and ``blueutil`` run with a bounded timeout so a stuck
adapter can never hang the bot.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from assistant_runtime import AssistantSettings

logger = logging.getLogger(__name__)

_SCAN_TIMEOUT_SECONDS = 15
_CONNECT_TIMEOUT_SECONDS = 25
_PROFILER_BINARY = "system_profiler"
_PROFILER_DATATYPE = "SPBluetoothDataType"
_BLUEUTIL_BINARY = "blueutil"

# A real colon-separated MAC (e.g. 80:9F:9B:46:9C:21). Resolved tokens are
# re-checked against this before reaching blueutil, defence-in-depth against a
# tampered cache injecting an option-like string.
_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

_INSTALL_HELP = (
    "藍牙連線需要 blueutil，但目前尚未安裝。\n"
    "請在 Mac mini 執行：brew install blueutil"
)


def _is_mac(address: str) -> bool:
    return bool(_MAC_RE.match(address or ""))


@dataclass(frozen=True)
class BluetoothDevice:
    name: str
    address: str
    connected: bool


@dataclass(frozen=True)
class BluetoothScan:
    """Outcome of a scan. ``ok`` False carries a user-facing ``error`` describing
    exactly what is missing (Bluetooth off, tool absent, permission, parse)."""

    ok: bool
    devices: tuple[BluetoothDevice, ...] = ()
    error: str | None = None


class AddressTokenCache:
    """Persistent token→MAC-address map (gitignored runtime JSON).

    Tokens are a deterministic hash of the address so re-rendering a scan
    reproduces the same token, and the cache survives bot restarts so a button
    clicked long after the scan still resolves."""

    def __init__(self, path: str) -> None:
        self._path = path

    def _load(self) -> dict:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        p = Path(self._path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)

    def put(self, address: str) -> str:
        token = hashlib.sha1(address.encode("utf-8")).hexdigest()[:16]
        data = self._load()
        if data.get(token) != address:
            data[token] = address
            self._save(data)
        return token

    def resolve(self, token: str) -> str | None:
        return self._load().get(token)


# --- scan ------------------------------------------------------------------
def _devices_from_section(section: object, *, connected: bool) -> list[BluetoothDevice]:
    """Parse one ``device_connected`` / ``device_not_connected`` list, each item
    a single-key ``{name: {device_address: ...}}`` mapping."""
    out: list[BluetoothDevice] = []
    if not isinstance(section, list):
        return out
    for item in section:
        if not isinstance(item, dict):
            continue
        for name, props in item.items():
            address = ""
            if isinstance(props, dict):
                address = str(props.get("device_address") or "")
            if not _is_mac(address):
                continue
            out.append(BluetoothDevice(name=str(name), address=address, connected=connected))
    return out


def _parse_scan(data: object) -> BluetoothScan:
    items = data.get("SPBluetoothDataType") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return BluetoothScan(ok=False, error="無法解析藍牙資訊（system_profiler 回傳格式異常）。")
    root = items[0]
    controller = root.get("controller_properties")
    state = controller.get("controller_state") if isinstance(controller, dict) else None
    if state is not None and state != "attrib_on":
        return BluetoothScan(ok=False, error="藍牙目前是關閉的，請到系統設定開啟藍牙後再試。")
    devices = (
        _devices_from_section(root.get("device_connected"), connected=True)
        + _devices_from_section(root.get("device_not_connected"), connected=False)
    )
    return BluetoothScan(ok=True, devices=tuple(devices))


def scan_devices(settings: AssistantSettings) -> BluetoothScan:  # noqa: ARG001
    """Run ``system_profiler`` and parse the device list. Never raises — every
    failure becomes an ``ok=False`` scan with a user-facing ``error``."""
    if shutil.which(_PROFILER_BINARY) is None:
        return BluetoothScan(ok=False, error="找不到 system_profiler，無法掃描藍牙（此功能僅支援 macOS）。")
    try:
        proc = subprocess.run(
            [_PROFILER_BINARY, _PROFILER_DATATYPE, "-json"],
            capture_output=True,
            text=True,
            timeout=_SCAN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return BluetoothScan(ok=False, error="藍牙掃描逾時，請稍後再試。")
    except OSError as exc:
        return BluetoothScan(ok=False, error=f"藍牙掃描失敗：{exc}")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return BluetoothScan(ok=False, error=f"藍牙掃描失敗：{err or '未知錯誤'}")
    try:
        data = json.loads(proc.stdout or "")
    except ValueError:
        return BluetoothScan(ok=False, error="無法解析藍牙資訊（system_profiler 輸出非 JSON）。")
    return _parse_scan(data)


# --- connect ---------------------------------------------------------------
def connect_device(settings: AssistantSettings, address: str, name: str) -> str:  # noqa: ARG001
    """Connect a device by MAC via ``blueutil``. Returns a user-facing message."""
    if not _is_mac(address):
        return "藍牙位址格式無效，請重新掃描。"
    blueutil = shutil.which(_BLUEUTIL_BINARY)
    if blueutil is None:
        return _INSTALL_HELP
    try:
        proc = subprocess.run(
            [blueutil, "--connect", address],
            capture_output=True,
            text=True,
            timeout=_CONNECT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"連線「{name}」逾時，請確認裝置已開機且在範圍內。"
    except OSError as exc:
        return f"連線「{name}」失敗：{exc}"
    if proc.returncode == 0:
        return f"已連線：{name}"
    err = (proc.stderr or proc.stdout or "").strip()
    return f"連線「{name}」失敗：{err or '未知錯誤'}"


# --- rendering -------------------------------------------------------------
def _refresh_row() -> list[dict]:
    return [{"text": "🔄 重新掃描", "callback_data": "bt:scan"}]


def render_scan(settings: AssistantSettings, cache: AddressTokenCache) -> tuple[str, dict]:
    """Render the current scan as (text, reply_markup). Each device is a button
    keyed by an opaque token; 🟢 marks connected, ⚪ not connected."""
    scan = scan_devices(settings)
    if not scan.ok:
        return scan.error or "藍牙掃描失敗。", {"inline_keyboard": [_refresh_row()]}
    if not scan.devices:
        return "找不到任何藍牙裝置（沒有已配對或在範圍內的裝置）。", {"inline_keyboard": [_refresh_row()]}
    rows: list[list[dict]] = []
    for d in scan.devices:
        token = cache.put(d.address)
        mark = "🟢" if d.connected else "⚪"
        rows.append([{"text": f"{mark} {d.name}", "callback_data": f"bt:c:{token}"}])
    rows.append(_refresh_row())
    text = "🔵 藍牙裝置（🟢 已連線 / ⚪ 未連線）\n點裝置即可嘗試連線。"
    return text, {"inline_keyboard": rows}


# --- command + callback handlers -------------------------------------------
def build_bluetooth_handler(settings: AssistantSettings):
    cache = AddressTokenCache(settings.openclaw_bluetooth_token_cache_path)

    def handler(raw: str, chat_id: str):  # noqa: ARG001
        return render_scan(settings, cache)

    return handler


def build_bluetooth_callback_handler(settings: AssistantSettings):
    """Return the ``bt:`` prefix callback handler.

    Signature matches the dispatcher registry: ``(payload, original_text,
    chat_id) -> (toast, new_text, new_reply_markup)``. ``bt:scan`` re-renders the
    list (new_text set); ``bt:c:<token>`` connects and replies with a toast."""
    cache = AddressTokenCache(settings.openclaw_bluetooth_token_cache_path)

    def cb(payload: str, original_text: str, chat_id: str):  # noqa: ARG001
        action, _, rest = payload.partition(":")
        if action == "scan":
            text, markup = render_scan(settings, cache)
            return None, text, markup
        if action == "c":
            address = cache.resolve(rest)
            if not address:
                return "找不到這個藍牙裝置（請重新掃描）。", None, None
            scan = scan_devices(settings)
            device = next(
                (d for d in scan.devices if d.address == address), None
            ) if scan.ok else None
            name = device.name if device else "裝置"
            if device is not None and device.connected:
                return f"「{name}」已經連線。", None, None
            return connect_device(settings, address, name), None, None
        return "未知的藍牙動作。", None, None

    return cb
