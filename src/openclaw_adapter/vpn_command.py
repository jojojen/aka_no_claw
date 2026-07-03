"""/vpn — control the macOS NordVPN app through its ``nordvpn://`` deeplinks.

Empirically verified on 2026-07-03 (NordVPN.app, Meshnet active):

- ``nordvpn://connect``                → connect to the recommended server
- ``nordvpn://connect?country=Japan``  → connect to that country; repeating the
  same country picks a NEW server (= new exit IP) — this is the rotation
  primitive.
- ``disconnect`` / ``pause`` / ``reconnect`` deeplinks are silently ignored by
  the app, so "turn VPN off" is NOT scriptable; only switching is.
- Meshnet (100.64/10 utun) survives every switch — the operator's remote
  session rides on it, so status always reports it.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_IP_PROBE_URL = "https://api.ipify.org"
_COUNTRY_PROBE_URL = "https://ipinfo.io/country"
_PROBE_TIMEOUT_SECONDS = 10
_SWITCH_POLL_SECONDS = 4
_SWITCH_WAIT_SECONDS = 40
_MIN_INTERVAL_MINUTES = 30
_COUNTRY_RE = re.compile(r"^[A-Za-z][A-Za-z _-]{1,40}$")
_MESHNET_ADDR_RE = re.compile(r"inet 100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.")


def _fetch_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=_PROBE_TIMEOUT_SECONDS) as resp:
        return resp.read().decode("utf-8", errors="replace").strip()


def probe_public_ip(fetch=_fetch_text) -> str:
    return fetch(_IP_PROBE_URL)


def probe_country(fetch=_fetch_text) -> str:
    return fetch(_COUNTRY_PROBE_URL)


def mask_ip(ip: str) -> str:
    parts = ip.split(".")
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.x.x"
    return ip[:8] + "…" if len(ip) > 8 else ip


def open_connect_deeplink(country: str | None, runner=subprocess.run) -> None:
    url = "nordvpn://connect"
    if country:
        url += "?" + urllib.parse.urlencode({"country": country})
    runner(["open", url], check=True, capture_output=True, timeout=15)


def meshnet_alive(runner=subprocess.run) -> bool:
    try:
        out = runner(
            ["ifconfig"], check=True, capture_output=True, timeout=10
        ).stdout.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — status probe must not raise
        logger.warning("vpn_command: ifconfig probe failed", exc_info=True)
        return False
    return bool(_MESHNET_ADDR_RE.search(out))


@dataclass
class VpnSwitchResult:
    country: str
    old_ip: str
    new_ip: str

    @property
    def changed(self) -> bool:
        return bool(self.new_ip) and self.new_ip != self.old_ip


def switch_vpn(
    country: str,
    *,
    opener=open_connect_deeplink,
    ip_probe=probe_public_ip,
    sleep=time.sleep,
    wait_seconds: int = _SWITCH_WAIT_SECONDS,
    deeplink_attempts: int = 2,
) -> VpnSwitchResult:
    if not _COUNTRY_RE.match(country):
        raise ValueError(f"不合法的國家名稱：{country!r}")
    old_ip = ip_probe()
    new_ip = old_ip
    # NordVPN app 偶爾會無視單發 deeplink（2026-07-03 13:28 實測 40 秒沒反應，
    # 手動重打同一 deeplink 4 秒就換 IP）——沒反應時重發一次再等，別急著回報失敗。
    for _ in range(max(1, deeplink_attempts)):
        opener(country)
        waited = 0
        while waited < wait_seconds:
            sleep(_SWITCH_POLL_SECONDS)
            waited += _SWITCH_POLL_SECONDS
            try:
                new_ip = ip_probe()
            except Exception:  # noqa: BLE001 — mid-switch probes are expected to fail
                continue
            if new_ip != old_ip:
                return VpnSwitchResult(country=country, old_ip=old_ip, new_ip=new_ip)
    return VpnSwitchResult(country=country, old_ip=old_ip, new_ip=new_ip)


@dataclass
class VpnRotationConfig:
    countries: list[str] = field(default_factory=lambda: ["Japan"])
    auto_enabled: bool = False
    interval_minutes: int = 360
    notify_chat_id: str | None = None


class VpnConfigStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def load(self) -> VpnRotationConfig:
        with self._lock:
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return VpnRotationConfig()
            except Exception:  # noqa: BLE001 — corrupt config falls back to defaults
                logger.warning("vpn_command: config unreadable", exc_info=True)
                return VpnRotationConfig()
        countries = [
            c for c in raw.get("countries", []) if isinstance(c, str) and _COUNTRY_RE.match(c)
        ] or ["Japan"]
        return VpnRotationConfig(
            countries=countries,
            auto_enabled=bool(raw.get("auto_enabled", False)),
            interval_minutes=max(
                _MIN_INTERVAL_MINUTES, int(raw.get("interval_minutes", 360))
            ),
            notify_chat_id=raw.get("notify_chat_id") or None,
        )

    def save(self, config: VpnRotationConfig) -> None:
        payload = {
            "countries": config.countries,
            "auto_enabled": config.auto_enabled,
            "interval_minutes": config.interval_minutes,
            "notify_chat_id": config.notify_chat_id,
        }
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )


class VpnRotationScheduler:
    """Round-robin auto-switch. Enabled/interval live in the config store, so
    the state survives restarts; the enabling chat receives each report."""

    def __init__(
        self,
        store: VpnConfigStore,
        *,
        switch_fn=switch_vpn,
        notifier_factory=None,
        monotonic=time.monotonic,
    ) -> None:
        self._store = store
        self._switch_fn = switch_fn
        self._notifier_factory = notifier_factory
        self._monotonic = monotonic
        self._last_switch = monotonic()
        self._rotation_index = 0
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, name="vpn-rotation-scheduler", daemon=True
        )
        self._thread.start()
        logger.info("VpnRotationScheduler started (minute-resolution).")

    def _loop(self) -> None:
        while True:
            time.sleep(60)
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — scheduler must survive bad ticks
                logger.warning("vpn_command: scheduler tick failed", exc_info=True)

    def tick(self) -> str | None:
        config = self._store.load()
        if not config.auto_enabled:
            self._last_switch = self._monotonic()
            return None
        due = self._monotonic() - self._last_switch >= config.interval_minutes * 60
        if not due:
            return None
        country = config.countries[self._rotation_index % len(config.countries)]
        self._rotation_index += 1
        self._last_switch = self._monotonic()
        try:
            result = self._switch_fn(country)
            message = _format_switch_result(result, prefix="🔄 VPN 自動輪替：")
        except Exception as exc:  # noqa: BLE001 — report, keep rotating next tick
            logger.warning("vpn_command: auto switch failed", exc_info=True)
            message = f"⚠️ VPN 自動輪替失敗（{country}）：{exc}"
        if self._notifier_factory is not None and config.notify_chat_id:
            try:
                self._notifier_factory(config.notify_chat_id).send(message)
            except Exception:  # noqa: BLE001
                logger.warning("vpn_command: rotation notify failed", exc_info=True)
        return message


def _format_switch_result(result: VpnSwitchResult, *, prefix: str = "") -> str:
    if result.changed:
        return (
            f"{prefix}已切換到 {result.country} 的新伺服器\n"
            f"出口 IP：{mask_ip(result.old_ip)} → {mask_ip(result.new_ip)}"
        )
    return (
        f"{prefix}⚠️ 對 {result.country} 發出切換後出口 IP 未變"
        f"（{mask_ip(result.old_ip)}）— NordVPN app 可能未回應 deeplink。"
    )


def _parse_auto_args(rest: str) -> tuple[bool, int | None]:
    """``on [hours]`` / ``off`` → (enabled, interval_minutes|None)."""
    parts = rest.split()
    if not parts or parts[0] not in {"on", "off"}:
        raise ValueError("用法：/vpn auto on [小時數] 或 /vpn auto off")
    if parts[0] == "off":
        return False, None
    interval_minutes = None
    if len(parts) > 1:
        raw = parts[1].lower().rstrip("h")
        hours = float(raw)
        interval_minutes = max(_MIN_INTERVAL_MINUTES, int(hours * 60))
    return True, interval_minutes


def build_vpn_handler(
    settings,
    store: VpnConfigStore,
    *,
    switch_fn=switch_vpn,
    ip_probe=probe_public_ip,
    country_probe=probe_country,
    meshnet_probe=meshnet_alive,
):
    def _status() -> str:
        config = store.load()
        try:
            ip = mask_ip(ip_probe())
            country = country_probe()
        except Exception as exc:  # noqa: BLE001 — report probe failure plainly
            return f"⚠️ 無法探測出口 IP：{exc}"
        mesh = "存活" if meshnet_probe() else "❌ 不見了"
        auto = (
            f"每 {config.interval_minutes / 60:g} 小時輪替（池：{'、'.join(config.countries)}）"
            if config.auto_enabled
            else "關閉"
        )
        return (
            f"VPN 狀態\n出口：{country}（{ip}）\nMeshnet：{mesh}\n自動輪替：{auto}\n"
            "註：NordVPN deeplink 只能切換、不能斷線；要關 VPN 請用 app。"
        )

    def handler(remainder: str, chat_id: str):
        text = (remainder or "").strip()
        if not text or text == "status":
            return _status()

        verb, _, rest = text.partition(" ")
        rest = rest.strip()

        if verb == "switch":
            config = store.load()
            country = rest or config.countries[0]
            try:
                result = switch_fn(country)
            except ValueError as exc:
                return str(exc)
            except Exception as exc:  # noqa: BLE001 — report switch failure plainly
                logger.warning("vpn_command: manual switch failed", exc_info=True)
                return f"⚠️ VPN 切換失敗（{country}）：{exc}"
            return _format_switch_result(result)

        if verb == "auto":
            try:
                enabled, interval_minutes = _parse_auto_args(rest)
            except ValueError as exc:
                return str(exc)
            config = store.load()
            config.auto_enabled = enabled
            if interval_minutes is not None:
                config.interval_minutes = interval_minutes
            config.notify_chat_id = chat_id if enabled else config.notify_chat_id
            store.save(config)
            if enabled:
                return (
                    f"✅ 自動輪替已開啟：每 {config.interval_minutes / 60:g} 小時"
                    f"換一次（池：{'、'.join(config.countries)}）。"
                )
            return "✅ 自動輪替已關閉。"

        if verb == "pool":
            config = store.load()
            if not rest:
                return f"目前輪替池：{'、'.join(config.countries)}"
            countries = [c.strip() for c in rest.split(",") if c.strip()]
            bad = [c for c in countries if not _COUNTRY_RE.match(c)]
            if bad or not countries:
                return f"不合法的國家名稱：{'、'.join(bad) or '(空)'}"
            config.countries = countries
            store.save(config)
            return f"✅ 輪替池已更新：{'、'.join(countries)}"

        return (
            "用法：/vpn [status] | switch [國家] | auto on [小時] | auto off | "
            "pool [國家,國家,…]"
        )

    return handler
