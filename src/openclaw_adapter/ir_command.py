"""BroadLink RM4 Mini IR learning and playback for local home control.

The command surface is intentionally small:

    /ir discover
    /ir learn <device> <button>
    /ir send <device> <button>
    /ir devices

Learned payloads are stored as base64 in a gitignored runtime JSON file so they
survive restarts without leaking into the repo.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import broadlink
from assistant_runtime import AssistantSettings

logger = logging.getLogger(__name__)

_DISCOVER_TIMEOUT_SECONDS = 8
_LEARN_TIMEOUT_SECONDS = 25
_LEARN_POLL_SECONDS = 1.0
_RM_CLASS_NAMES = {"rm", "rm4", "rm4mini"}


@dataclass(frozen=True)
class IrButton:
    device: str
    button: str
    payload_b64: str


class IrStore:
    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, dict[str, str]] = {}
        for device, buttons in data.items():
            if not isinstance(device, str) or not isinstance(buttons, dict):
                continue
            clean_buttons = {
                str(name): str(payload)
                for name, payload in buttons.items()
                if isinstance(name, str) and isinstance(payload, str)
            }
            if clean_buttons:
                out[device] = clean_buttons
        return out

    def _save(self, data: dict[str, dict[str, str]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def put(self, device: str, button: str, payload_b64: str) -> None:
        data = self._load()
        data.setdefault(device, {})[button] = payload_b64
        self._save(data)

    def get(self, device: str, button: str) -> str | None:
        return self._load().get(device, {}).get(button)

    def list_buttons(self) -> tuple[IrButton, ...]:
        rows: list[IrButton] = []
        for device, buttons in sorted(self._load().items()):
            for button, payload in sorted(buttons.items()):
                rows.append(IrButton(device=device, button=button, payload_b64=payload))
        return tuple(rows)


class IrTokenCache:
    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def _load(self) -> dict[str, list[str]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, list[str]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path)

    def put(self, device: str, button: str) -> str:
        raw = f"{device}\0{button}"
        token = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        data = self._load()
        data[token] = [device, button]
        self._save(data)
        return token

    def resolve(self, token: str) -> tuple[str, str] | None:
        value = self._load().get(token)
        if (
            isinstance(value, list)
            and len(value) == 2
            and isinstance(value[0], str)
            and isinstance(value[1], str)
        ):
            return value[0], value[1]
        return None


def _local_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.168.11.1", 80))
            return str(sock.getsockname()[0])
    except OSError:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("8.8.8.8", 80))
                return str(sock.getsockname()[0])
        except OSError:
            return None


def _broadcast_for(local_ip: str | None, configured: str | None = None) -> str:
    if configured:
        return configured
    if not local_ip:
        return "255.255.255.255"
    try:
        network = ipaddress.ip_network(f"{local_ip}/24", strict=False)
    except ValueError:
        return "255.255.255.255"
    return str(network.broadcast_address)


def discover_rm(settings: AssistantSettings) -> tuple[Any | None, str]:
    local_ip = _local_ip()
    broadcast = _broadcast_for(local_ip, settings.openclaw_broadlink_discover_broadcast)
    try:
        devices = broadlink.discover(
            timeout=_DISCOVER_TIMEOUT_SECONDS,
            local_ip_address=local_ip,
            discover_ip_address=broadcast,
        )
    except Exception as exc:  # noqa: BLE001 - surface actionable hardware errors.
        logger.warning("ir: BroadLink discovery failed: %s", exc)
        return None, f"BroadLink 掃描失敗：{exc}"

    rm_devices = [d for d in devices if type(d).__name__.lower() in _RM_CLASS_NAMES]
    if not rm_devices:
        return None, f"找不到 RM4 Mini（local_ip={local_ip or 'unknown'}, broadcast={broadcast}）。"
    device = rm_devices[0]
    try:
        device.auth()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ir: BroadLink auth failed host=%s error=%s", getattr(device, "host", None), exc)
        return None, f"找到 RM4 Mini 但無法連線：{exc}\n{_connectivity_help(exc)}"
    return device, _format_rm_device(device)


def _format_rm_device(device: Any) -> str:
    host = getattr(device, "host", None)
    ip = host[0] if isinstance(host, tuple) and host else "unknown"
    devtype = getattr(device, "devtype", None)
    return f"{type(device).__name__} {ip} type={hex(devtype) if isinstance(devtype, int) else devtype}"


def _connectivity_help(exc: Exception) -> str:
    text = str(exc)
    if "No route to host" in text:
        return (
            "請先確認 Mac mini 可直連 RM4：關閉 NordVPN/防火牆的區網阻擋，"
            "並把 RM4 Mini 斷電重插。"
        )
    return "請確認 BroadLink App 內沒有啟用 Lock device / 本地控制鎖定。"


def discover_message(settings: AssistantSettings) -> str:
    device, info = discover_rm(settings)
    if device is None:
        return info
    return f"找到 BroadLink：{info}"


def _valid_name(value: str) -> bool:
    return bool(value) and "/" not in value and "\0" not in value and len(value) <= 80


def learn_code(settings: AssistantSettings, device_name: str, button_name: str) -> str:
    if not _valid_name(device_name) or not _valid_name(button_name):
        return "名稱格式無效。請用：/ir learn ceiling_light night"
    rm_device, info = discover_rm(settings)
    if rm_device is None:
        return info
    try:
        rm_device.enter_learning()
    except Exception as exc:  # noqa: BLE001
        logger.exception("ir: enter learning failed")
        return f"RM4 Mini 進入學習模式失敗：{exc}"

    deadline = time.monotonic() + _LEARN_TIMEOUT_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        time.sleep(_LEARN_POLL_SECONDS)
        try:
            payload = rm_device.check_data()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
        if payload:
            payload_b64 = base64.b64encode(payload).decode("ascii")
            IrStore(settings.openclaw_ir_devices_path).put(
                device_name, button_name, payload_b64
            )
            return f"已儲存 IR：{device_name} / {button_name}\n來源：{info}"
    if last_error is not None:
        logger.info("ir: learning timed out after check_data errors: %s", last_error)
    return "學習逾時。請確認遙控器對準 RM4 Mini，然後再試一次。"


def send_code(settings: AssistantSettings, device_name: str, button_name: str) -> str:
    payload_b64 = IrStore(settings.openclaw_ir_devices_path).get(device_name, button_name)
    if not payload_b64:
        return f"找不到 IR：{device_name} / {button_name}。先用 /ir learn 學習。"
    try:
        payload = base64.b64decode(payload_b64)
    except ValueError:
        return f"IR payload 已損壞：{device_name} / {button_name}。請重新學習。"
    rm_device, info = discover_rm(settings)
    if rm_device is None:
        return info
    try:
        rm_device.send_data(payload)
    except Exception as exc:  # noqa: BLE001
        logger.exception("ir: send failed device=%s button=%s", device_name, button_name)
        return f"送出 IR 失敗：{exc}"
    return f"已送出 IR：{device_name} / {button_name}\n透過：{info}"


def render_devices(settings: AssistantSettings) -> tuple[str, dict]:
    buttons = IrStore(settings.openclaw_ir_devices_path).list_buttons()
    if not buttons:
        return (
            "目前沒有已學習的 IR。\n請先使用：/ir learn ceiling_light night",
            {"inline_keyboard": [[{"text": "掃描 RM4", "callback_data": "ir:discover"}]]},
        )
    cache = IrTokenCache(settings.openclaw_ir_token_cache_path)
    rows: list[list[dict[str, str]]] = []
    lines = ["IR 裝置"]
    current = None
    for item in buttons:
        if item.device != current:
            current = item.device
            lines.append(f"\n{item.device}")
        lines.append(f"- {item.button}")
        token = cache.put(item.device, item.button)
        rows.append([{"text": f"{item.device} / {item.button}", "callback_data": f"ir:s:{token}"}])
    rows.append([{"text": "掃描 RM4", "callback_data": "ir:discover"}])
    return "\n".join(lines), {"inline_keyboard": rows}


def build_ir_handler(settings: AssistantSettings):
    def handler(raw: str, chat_id: str):  # noqa: ARG001
        parts = (raw or "").strip().split()
        if not parts:
            return _help_text()
        action = parts[0].lower()
        if action == "discover":
            return discover_message(settings)
        if action == "devices":
            return render_devices(settings)
        if action == "learn" and len(parts) >= 3:
            return learn_code(settings, parts[1], parts[2])
        if action == "send" and len(parts) >= 3:
            return send_code(settings, parts[1], parts[2])
        return _help_text()

    return handler


def build_ir_callback_handler(settings: AssistantSettings):
    cache = IrTokenCache(settings.openclaw_ir_token_cache_path)

    def cb(payload: str, original_text: str, chat_id: str):  # noqa: ARG001
        if payload == "discover":
            return discover_message(settings), None, None
        action, _, rest = payload.partition(":")
        if action == "s":
            pair = cache.resolve(rest)
            if pair is None:
                return "找不到這個 IR 按鈕，請重新開 /ir devices。", None, None
            return send_code(settings, pair[0], pair[1]), None, None
        return "未知的 IR 動作。", None, None

    return cb


def _help_text() -> str:
    return (
        "IR 指令：\n"
        "/ir discover\n"
        "/ir learn <裝置名> <按鍵名>\n"
        "/ir send <裝置名> <按鍵名>\n"
        "/ir devices\n\n"
        "例：/ir learn ceiling_light night"
    )
