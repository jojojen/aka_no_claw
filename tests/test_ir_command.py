from __future__ import annotations

from types import SimpleNamespace

from openclaw_adapter import ir_command as ir


class FakeRm:
    def __init__(self, payload: bytes = b"ir-payload") -> None:
        self.host = ("192.0.2.38", 80)
        self.devtype = 0x0000
        self.authed = False
        self.learning = False
        self.sent: list[bytes] = []
        self._payload = payload
        self.auth_errors: list[Exception] = []
        self.auth_calls = 0

    def auth(self) -> None:
        self.auth_calls += 1
        if self.auth_errors:
            raise self.auth_errors.pop(0)
        self.authed = True

    def enter_learning(self) -> None:
        self.learning = True

    def check_data(self) -> bytes:
        return self._payload

    def send_data(self, data: bytes) -> None:
        self.sent.append(data)


def _settings(tmp_path):
    return SimpleNamespace(
        openclaw_ir_devices_path=str(tmp_path / "ir_devices.json"),
        openclaw_ir_token_cache_path=str(tmp_path / "ir_tokens.json"),
        openclaw_broadlink_discover_broadcast=None,
    )


def test_discover_uses_lan_broadcast_from_local_ip(tmp_path, monkeypatch):
    seen = {}
    fake = FakeRm()

    def discover(**kwargs):
        seen.update(kwargs)
        return [fake]

    monkeypatch.setattr(ir, "_local_ip", lambda: "192.0.2.34")
    monkeypatch.setattr(ir, "_RM_CLASS_NAMES", {"fakerm"})
    monkeypatch.setattr(ir.broadlink, "discover", discover)
    device, msg = ir.discover_rm(_settings(tmp_path))
    assert device is fake
    assert fake.authed is True
    assert seen["local_ip_address"] == "192.0.2.34"
    assert seen["discover_ip_address"] == "192.0.2.255"
    assert "192.0.2.38" in msg


def test_discover_respects_configured_broadcast(tmp_path, monkeypatch):
    seen = {}

    def discover(**kwargs):
        seen.update(kwargs)
        return [FakeRm()]

    settings = _settings(tmp_path)
    settings.openclaw_broadlink_discover_broadcast = "10.0.0.255"
    monkeypatch.setattr(ir, "_local_ip", lambda: "192.0.2.34")
    monkeypatch.setattr(ir, "_RM_CLASS_NAMES", {"fakerm"})
    monkeypatch.setattr(ir.broadlink, "discover", discover)
    ir.discover_rm(settings)
    assert seen["discover_ip_address"] == "10.0.0.255"


def test_discover_retries_transient_auth_no_route(tmp_path, monkeypatch):
    fake = FakeRm()
    fake.auth_errors.append(OSError(65, "No route to host"))
    warms = []

    monkeypatch.setattr(ir, "_local_ip", lambda: "192.0.2.34")
    monkeypatch.setattr(ir, "_RM_CLASS_NAMES", {"fakerm"})
    monkeypatch.setattr(ir.broadlink, "discover", lambda **kwargs: [fake])
    monkeypatch.setattr(ir, "_warm_host_route", lambda device: warms.append(device))
    monkeypatch.setattr(ir.time, "sleep", lambda seconds: None)
    device, msg = ir.discover_rm(_settings(tmp_path))
    assert device is fake
    assert fake.authed is True
    assert fake.auth_calls == 2
    assert warms == [fake, fake]
    assert "192.0.2.38" in msg


def test_route_warmup_uses_available_ping_candidate(tmp_path, monkeypatch):
    fake_ping = tmp_path / "ping"
    fake_ping.write_text("", encoding="utf-8")
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(ir, "_PING_CANDIDATES", (str(fake_ping), "ping"))
    monkeypatch.setattr(ir.subprocess, "run", run)
    ir._warm_host_route(FakeRm())
    assert calls == [[str(fake_ping), "-o", "-c", "3", "-W", "3000", "192.0.2.38"]]


def test_learn_code_persists_base64_payload(tmp_path, monkeypatch):
    fake = FakeRm(payload=b"learned")
    monkeypatch.setattr(ir, "discover_rm", lambda settings: (fake, "fake rm"))
    monkeypatch.setattr(ir.time, "sleep", lambda seconds: None)
    msg = ir.learn_code(_settings(tmp_path), "demo_light", "night")
    assert "已儲存" in msg
    assert fake.learning is True
    stored = ir.IrStore(str(tmp_path / "ir_devices.json")).get("demo_light", "night")
    assert stored == "bGVhcm5lZA=="


def test_send_code_replays_persisted_payload(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    fake = FakeRm()
    ir.IrStore(settings.openclaw_ir_devices_path).put("demo_light", "night", "cGxheQ==")
    monkeypatch.setattr(ir, "discover_rm", lambda settings: (fake, "fake rm"))
    msg = ir.send_code(settings, "demo_light", "night")
    assert "已送出" in msg
    assert fake.sent == [b"play"]


def test_render_devices_uses_opaque_tokens(tmp_path):
    settings = _settings(tmp_path)
    ir.IrStore(settings.openclaw_ir_devices_path).put("demo_light", "night", "abc")
    text, markup = ir.render_devices(settings)
    assert "demo_light" in text
    cb = markup["inline_keyboard"][0][0]["callback_data"]
    assert cb.startswith("ir:s:")
    assert "demo_light" not in cb
    assert "night" not in cb


def test_callback_sends_tokenized_button(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    token = ir.IrTokenCache(settings.openclaw_ir_token_cache_path).put("demo_light", "night")
    calls = []
    monkeypatch.setattr(ir, "send_code", lambda s, d, b: calls.append((d, b)) or "sent")
    cb = ir.build_ir_callback_handler(settings)
    toast, new_text, markup = cb(f"s:{token}", "", "chat")
    assert toast == "sent"
    assert new_text is None
    assert markup is None
    assert calls == [("demo_light", "night")]
