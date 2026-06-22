"""Tests for /bluetooth scan + connect (aka_no_claw #38).

Covers: system_profiler JSON parsing (connected / not-connected / XGIMI Z8X),
Bluetooth-off and tool-missing error messages, MAC validation, the opaque
address-token cache, missing-blueutil install help, connect success/failure, and
callback routing (scan / connect / already-connected / unknown) through the
CommandBridge web surface.
"""
from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest

from assistant_runtime.settings import get_settings
from openclaw_adapter import bluetooth_command as bt
from openclaw_adapter.command_bridge import CommandBridge


# A trimmed but structurally-faithful sample of `system_profiler
# SPBluetoothDataType -json` on this Mac (XGIMI Z8X connected, others not).
_SAMPLE = {
    "SPBluetoothDataType": [
        {
            "controller_properties": {"controller_state": "attrib_on"},
            "device_connected": [
                {"XGIMI Z8X": {"device_address": "80:9F:9B:46:9C:21"}},
            ],
            "device_not_connected": [
                {"AirPodsJ": {"device_address": "2C:76:00:6C:EE:A7"}},
                {"Bluetooth Keyboard": {"device_address": "54:46:6E:ED:00:0D"}},
            ],
        }
    ]
}

_XGIMI_ADDR = "80:9F:9B:46:9C:21"


@pytest.fixture
def settings(tmp_path):
    return SimpleNamespace(
        openclaw_bluetooth_token_cache_path=str(tmp_path / "bt_tokens.json"),
    )


# --- parsing ---------------------------------------------------------------

def test_parse_scan_lists_connected_and_not_connected():
    scan = bt._parse_scan(_SAMPLE)
    assert scan.ok
    names = {d.name: d.connected for d in scan.devices}
    assert names["XGIMI Z8X"] is True
    assert names["AirPodsJ"] is False
    assert names["Bluetooth Keyboard"] is False


def test_parse_scan_includes_xgimi_with_address():
    scan = bt._parse_scan(_SAMPLE)
    xgimi = next(d for d in scan.devices if d.name == "XGIMI Z8X")
    assert xgimi.address == _XGIMI_ADDR
    assert xgimi.connected is True


def test_parse_scan_bluetooth_off_reports_clearly():
    data = {"SPBluetoothDataType": [{"controller_properties": {"controller_state": "attrib_off"}}]}
    scan = bt._parse_scan(data)
    assert not scan.ok
    assert "藍牙" in scan.error and "關閉" in scan.error


def test_parse_scan_malformed_reports_clearly():
    scan = bt._parse_scan({"SPBluetoothDataType": []})
    assert not scan.ok
    assert scan.error


def test_parse_scan_skips_devices_without_valid_mac():
    data = {
        "SPBluetoothDataType": [
            {
                "controller_properties": {"controller_state": "attrib_on"},
                "device_not_connected": [
                    {"Ghost": {"device_address": "not-a-mac"}},
                    {"NoAddr": {}},
                    {"Real": {"device_address": "AA:BB:CC:DD:EE:FF"}},
                ],
            }
        ]
    }
    scan = bt._parse_scan(data)
    assert [d.name for d in scan.devices] == ["Real"]


# --- MAC validation --------------------------------------------------------

@pytest.mark.parametrize("addr", ["80:9F:9B:46:9C:21", "aa:bb:cc:dd:ee:ff"])
def test_is_mac_accepts_real_addresses(addr):
    assert bt._is_mac(addr)


@pytest.mark.parametrize("addr", ["", "--connect", "80:9F:9B", "zz:zz:zz:zz:zz:zz", "80-9F-9B-46-9C-21"])
def test_is_mac_rejects_bad_addresses(addr):
    assert not bt._is_mac(addr)


# --- address token cache ---------------------------------------------------

def test_token_cache_roundtrip(settings):
    cache = bt.AddressTokenCache(settings.openclaw_bluetooth_token_cache_path)
    token = cache.put(_XGIMI_ADDR)
    assert cache.resolve(token) == _XGIMI_ADDR


def test_token_cache_is_deterministic(settings):
    cache = bt.AddressTokenCache(settings.openclaw_bluetooth_token_cache_path)
    assert cache.put(_XGIMI_ADDR) == cache.put(_XGIMI_ADDR)


def test_token_cache_missing_token_returns_none(settings):
    cache = bt.AddressTokenCache(settings.openclaw_bluetooth_token_cache_path)
    assert cache.resolve("deadbeef") is None


def test_callback_data_never_exposes_mac(settings, monkeypatch):
    monkeypatch.setattr(bt, "scan_devices", lambda s: bt._parse_scan(_SAMPLE))
    cache = bt.AddressTokenCache(settings.openclaw_bluetooth_token_cache_path)
    _text, markup = bt.render_scan(settings, cache)
    blob = json.dumps(markup, ensure_ascii=False)
    assert _XGIMI_ADDR not in blob  # only opaque tokens ride in callback_data


# --- connect ---------------------------------------------------------------

def test_connect_missing_blueutil_returns_install_help(settings, monkeypatch):
    monkeypatch.setattr(bt.shutil, "which", lambda name: None)
    msg = bt.connect_device(settings, _XGIMI_ADDR, "XGIMI Z8X")
    assert "blueutil" in msg
    assert "brew install blueutil" in msg


def test_connect_rejects_non_mac_before_running(settings, monkeypatch):
    called = {"ran": False}

    def _fail_which(name):  # pragma: no cover - must not be reached
        called["ran"] = True
        return "/usr/bin/blueutil"

    monkeypatch.setattr(bt.shutil, "which", _fail_which)
    msg = bt.connect_device(settings, "--connect", "evil")
    assert "格式無效" in msg
    assert called["ran"] is False


def test_connect_success(settings, monkeypatch):
    monkeypatch.setattr(bt.shutil, "which", lambda name: "/usr/bin/blueutil")
    monkeypatch.setattr(
        bt.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    msg = bt.connect_device(settings, _XGIMI_ADDR, "XGIMI Z8X")
    assert msg == "已連線：XGIMI Z8X"


def test_connect_failure_surfaces_error(settings, monkeypatch):
    monkeypatch.setattr(bt.shutil, "which", lambda name: "/usr/bin/blueutil")
    monkeypatch.setattr(
        bt.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="device not found"),
    )
    msg = bt.connect_device(settings, _XGIMI_ADDR, "XGIMI Z8X")
    assert "失敗" in msg and "device not found" in msg


def test_scan_missing_system_profiler(settings, monkeypatch):
    monkeypatch.setattr(bt.shutil, "which", lambda name: None)
    scan = bt.scan_devices(settings)
    assert not scan.ok
    assert "system_profiler" in scan.error


# --- bridge callback routing -----------------------------------------------

def _bridge(tmp_path, monkeypatch) -> CommandBridge:
    # scan_devices is the only thing we stub; the bridge builds the full handler
    # registry so it needs real settings (quiz_db_path etc.), just with the
    # Bluetooth token cache redirected into tmp.
    monkeypatch.setattr(bt, "scan_devices", lambda s: bt._parse_scan(_SAMPLE))
    settings = dataclasses.replace(
        get_settings(),
        openclaw_bluetooth_token_cache_path=str(tmp_path / "bt_tokens.json"),
    )
    return CommandBridge(settings=settings)


def test_bridge_scan_returns_device_buttons(tmp_path, monkeypatch):
    b = _bridge(tmp_path, monkeypatch)
    res = b.run_bluetooth_command()
    assert res["status"] == "ok"
    labels = [a["label"] for a in res["actions"]]
    assert any("XGIMI Z8X" in l for l in labels)
    assert any("重新掃描" in l for l in labels)


def test_bridge_connect_already_connected_says_so(tmp_path, monkeypatch):
    b = _bridge(tmp_path, monkeypatch)
    res = b.run_bluetooth_command()
    xgimi = next(a for a in res["actions"] if "XGIMI Z8X" in a["label"])
    out = b.run_bluetooth_action(xgimi["callback_data"])
    assert out["status"] == "ok"
    assert "已經連線" in out["message"]


def test_bridge_connect_not_connected_calls_blueutil(tmp_path, monkeypatch):
    b = _bridge(tmp_path, monkeypatch)
    monkeypatch.setattr(bt.shutil, "which", lambda name: "/usr/bin/blueutil")
    monkeypatch.setattr(
        bt.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    res = b.run_bluetooth_command()
    airpods = next(a for a in res["actions"] if "AirPodsJ" in a["label"])
    out = b.run_bluetooth_action(airpods["callback_data"])
    assert out["status"] == "ok"
    assert out["message"] == "已連線：AirPodsJ"


def test_bridge_unknown_token_asks_to_rescan(tmp_path, monkeypatch):
    b = _bridge(tmp_path, monkeypatch)
    out = b.run_bluetooth_action("bt:c:deadbeefdeadbeef")
    assert "重新掃描" in out["message"]


def test_bridge_rescan_callback_rerenders(tmp_path, monkeypatch):
    b = _bridge(tmp_path, monkeypatch)
    out = b.run_bluetooth_action("bt:scan")
    assert out["status"] == "ok"
    assert any("XGIMI Z8X" in a["label"] for a in out["actions"])


def test_bridge_rejects_non_bt_prefix(tmp_path, monkeypatch):
    b = _bridge(tmp_path, monkeypatch)
    out = b.run_bluetooth_action("music:rnd")
    assert out["status"] == "error"
