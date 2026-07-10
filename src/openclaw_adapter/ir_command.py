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
import os
import socket
import subprocess
import sys
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
_AUTH_ATTEMPTS = 3
_PING_CANDIDATES = ("/sbin/ping", "/bin/ping", "ping")
_ROUTE_WARMUP_PING_ARGS = ("-o", "-c", "3", "-W", "3000")
_ROUTE_WARMUP_TIMEOUT_SECONDS = 10
_RM_CLASS_NAMES = {"rm", "rm4", "rm4mini"}
_WORKER_ENV = "OPENCLAW_IR_INLINE"
_WORKER_TIMEOUT_SECONDS = 45


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
    auth_error: Exception | None = None
    for attempt in range(1, _AUTH_ATTEMPTS + 1):
        _warm_host_route(device)
        try:
            device.auth()
            break
        except Exception as exc:  # noqa: BLE001
            auth_error = exc
            logger.warning(
                "ir: BroadLink auth failed host=%s attempt=%s/%s error=%s",
                getattr(device, "host", None),
                attempt,
                _AUTH_ATTEMPTS,
                exc,
            )
            time.sleep(1)
    else:
        assert auth_error is not None
        return None, f"找到 RM4 Mini 但無法連線：{auth_error}\n{_connectivity_help(auth_error)}"
    return device, _format_rm_device(device)


def _format_rm_device(device: Any) -> str:
    host = getattr(device, "host", None)
    ip = host[0] if isinstance(host, tuple) and host else "unknown"
    devtype = getattr(device, "devtype", None)
    return f"{type(device).__name__} {ip} type={hex(devtype) if isinstance(devtype, int) else devtype}"


def _host_ip(device: Any) -> str | None:
    host = getattr(device, "host", None)
    if isinstance(host, tuple) and host:
        return str(host[0])
    return None


def _warm_host_route(device: Any) -> None:
    """Prime ARP/route state for RM4 before UDP auth.

    On this Mac, discovery broadcast can succeed while the first unicast UDP send
    to the just-discovered RM4 returns ``Errno 65 No route to host``. A short ping
    often refreshes the host route; failures are harmless because many devices
    drop ICMP.
    """
    ip = _host_ip(device)
    if not ip:
        return
    ping_cmd = next((cmd for cmd in _PING_CANDIDATES if Path(cmd).exists()), _PING_CANDIDATES[-1])
    try:
        result = subprocess.run(
            [ping_cmd, *_ROUTE_WARMUP_PING_ARGS, ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_ROUTE_WARMUP_TIMEOUT_SECONDS,
            check=False,
        )
        logger.debug(
            "ir: BroadLink route warmup host=%s cmd=%s args=%s exit=%s",
            ip,
            ping_cmd,
            _ROUTE_WARMUP_PING_ARGS,
            result.returncode,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("ir: BroadLink route warmup failed host=%s error=%s", ip, exc)
        return


def _connectivity_help(exc: Exception) -> str:
    text = str(exc)
    if "No route to host" in text:
        return (
            "請先確認 Mac mini 可直連 RM4：關閉 NordVPN/防火牆的區網阻擋，"
            "並把 RM4 Mini 斷電重插。"
        )
    return "請確認 BroadLink App 內沒有啟用 Lock device / 本地控制鎖定。"


def _use_worker(settings: AssistantSettings) -> bool:
    # Unit tests pass SimpleNamespace settings and should keep exercising the
    # inline logic. Real app settings use a short-lived worker so BroadLink UDP
    # sockets do not get stuck inside long-running Telegram/HTTP processes.
    return (
        os.getenv(_WORKER_ENV) != "1"
        and settings.__class__.__name__ == "AssistantSettings"
    )


def _run_worker(action: str, *args: str) -> str:
    env = dict(os.environ)
    env[_WORKER_ENV] = "1"
    cmd = [sys.executable, "-m", "openclaw_adapter.ir_worker", action, *args]
    started = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_WORKER_TIMEOUT_SECONDS,
            check=False,
            env=env,
            cwd=str(Path(__file__).resolve().parents[2]),
            close_fds=True,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        return "IR worker 逾時，請稍後重試。"
    except OSError as exc:
        logger.exception("ir: worker launch failed")
        return f"IR worker 啟動失敗：{exc}"
    output = (result.stdout or "").strip()
    logger.debug(
        "ir: worker finished action=%s rc=%s elapsed=%.3fs stdout=%r stderr=%r",
        action,
        result.returncode,
        time.monotonic() - started,
        output[-500:],
        (result.stderr or "").strip()[-500:],
    )
    if result.returncode == 0:
        return output
    detail = output or (result.stderr or "").strip()
    return detail or f"IR worker 失敗：exit={result.returncode}"


def discover_message(settings: AssistantSettings) -> str:
    if _use_worker(settings):
        return _run_worker("discover")
    return _discover_message_inline(settings)


def _discover_message_inline(settings: AssistantSettings) -> str:
    device, info = discover_rm(settings)
    if device is None:
        return info
    return f"找到 BroadLink：{info}"


def _valid_name(value: str) -> bool:
    return bool(value) and "/" not in value and "\0" not in value and len(value) <= 80


def learn_code(settings: AssistantSettings, device_name: str, button_name: str) -> str:
    if _use_worker(settings):
        return _run_worker("learn", device_name, button_name)
    return _learn_code_inline(settings, device_name, button_name)


def _learn_code_inline(settings: AssistantSettings, device_name: str, button_name: str) -> str:
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
    resolved = _resolve_send_target(settings, device_name, button_name)
    if resolved is not None:
        device_name, button_name = resolved
    if _use_worker(settings):
        return _run_worker("send", device_name, button_name)
    return _send_code_inline(settings, device_name, button_name)


def _resolve_send_target(
    settings: AssistantSettings, device_name: str, button_name: str
) -> tuple[str, str] | None:
    """Ground a natural-language device/button request in the learned IR store.

    Voice/NL routes hand over spoken names (e.g. 電風扇 / on) that rarely match
    the learned store keys (fan / power). Exact keys pass through untouched;
    otherwise the local LLM picks the matching learned pair — or nothing, in
    which case the caller keeps the original names and the exact-miss error.
    """
    store = IrStore(settings.openclaw_ir_devices_path)
    if store.get(device_name, button_name):
        return device_name, button_name
    buttons = store.list_buttons()
    if not buttons:
        return None
    pairs = sorted({f"{item.device} {item.button}" for item in buttons})
    prompt = (
        "你是紅外線遙控解析器。以下是已學習的按鍵清單，每行格式為「裝置 按鍵」：\n"
        + "\n".join(pairs)
        + f"\n\n使用者的要求：裝置「{device_name}」、動作「{button_name}」。\n"
        "從清單中選出語意最符合的一行，原樣輸出（裝置 按鍵）。"
        "選擇時考慮按鍵語意，例如切換電源的按鍵可同時對應開或關的要求。"
        "若清單中沒有合理的對應，輸出 none。只輸出一行，不要解釋。"
    )
    try:
        from .dynamic_tools import OllamaTextClient
        from .llm_pool_settings import resolve_provider_model

        model = (resolve_provider_model(settings, "local") or "").strip()
        if not model:
            return None
        client = OllamaTextClient(
            endpoint=settings.openclaw_local_text_endpoint,
            model=model,
            timeout_seconds=max(1, settings.openclaw_local_text_timeout_seconds),
        )
        raw = client.generate(prompt, temperature=0.0)
    except Exception:  # noqa: BLE001
        logger.warning("ir: local resolver unavailable", exc_info=True)
        return None
    choice = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    if not choice or choice.lower() in {"none", "null"}:
        return None
    parts = choice.split()
    if len(parts) < 2:
        return None
    candidate = (" ".join(parts[:-1]), parts[-1])
    # Only send pairs that exist in the store — never an LLM-invented one.
    if store.get(*candidate):
        logger.info(
            "ir: resolved %s/%s -> %s/%s",
            device_name, button_name, candidate[0], candidate[1],
        )
        return candidate
    return None


def _send_code_inline(settings: AssistantSettings, device_name: str, button_name: str) -> str:
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
            # NL routes may hand over multi-word device names; the last token
            # is always the button.
            return send_code(settings, " ".join(parts[1:-1]), parts[-1])
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
