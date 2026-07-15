from __future__ import annotations

from types import SimpleNamespace

from openclaw_adapter import ir_command as ir


class FakeRm:
    def __init__(self, payload: bytes = b"ir-payload") -> None:
        self.host = ("192.0.2.38", 80)
        self.devtype = 0x5216
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

    def xdiscover(**kwargs):
        seen.update(kwargs)
        yield fake

    monkeypatch.setattr(ir, "_local_ip", lambda: "192.168.11.34")
    monkeypatch.setattr(ir, "_RM_CLASS_NAMES", {"fakerm"})
    monkeypatch.setattr(ir.broadlink, "xdiscover", xdiscover)
    device, msg = ir.discover_rm(_settings(tmp_path))
    assert device is fake
    assert fake.authed is True
    assert seen["local_ip_address"] == "192.168.11.34"
    assert seen["discover_ip_address"] == "192.168.11.255"
    assert "192.0.2.38" in msg


def test_discover_respects_configured_broadcast(tmp_path, monkeypatch):
    seen = {}

    def xdiscover(**kwargs):
        seen.update(kwargs)
        yield FakeRm()

    settings = _settings(tmp_path)
    settings.openclaw_broadlink_discover_broadcast = "10.0.0.255"
    monkeypatch.setattr(ir, "_local_ip", lambda: "192.168.11.34")
    monkeypatch.setattr(ir, "_RM_CLASS_NAMES", {"fakerm"})
    monkeypatch.setattr(ir.broadlink, "xdiscover", xdiscover)
    ir.discover_rm(settings)
    assert seen["discover_ip_address"] == "10.0.0.255"


def test_discover_retries_transient_auth_no_route(tmp_path, monkeypatch):
    fake = FakeRm()
    fake.auth_errors.append(OSError(65, "No route to host"))
    warms = []

    monkeypatch.setattr(ir, "_local_ip", lambda: "192.168.11.34")
    monkeypatch.setattr(ir, "_RM_CLASS_NAMES", {"fakerm"})
    monkeypatch.setattr(ir.broadlink, "xdiscover", lambda **kwargs: iter([fake]))
    monkeypatch.setattr(ir, "_warm_host_route", lambda device: warms.append(device))
    monkeypatch.setattr(ir.time, "sleep", lambda seconds: None)
    device, msg = ir.discover_rm(_settings(tmp_path))
    assert device is fake
    assert fake.authed is True
    assert fake.auth_calls == 2
    assert warms == [fake, fake]
    assert "192.0.2.38" in msg


def test_discover_stops_after_first_matching_rm(tmp_path, monkeypatch):
    fake = FakeRm()

    def xdiscover(**kwargs):
        yield fake
        raise AssertionError("discovery should stop after the first matching RM")

    monkeypatch.setattr(ir, "_local_ip", lambda: "192.168.11.34")
    monkeypatch.setattr(ir, "_RM_CLASS_NAMES", {"fakerm"})
    monkeypatch.setattr(ir.broadlink, "xdiscover", xdiscover)
    monkeypatch.setattr(ir, "_warm_host_route", lambda device: None)

    device, _ = ir.discover_rm(_settings(tmp_path))

    assert device is fake
    assert fake.auth_calls == 1


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
    msg = ir.learn_code(_settings(tmp_path), "ceiling_light", "night")
    assert "已儲存" in msg
    assert fake.learning is True
    stored = ir.IrStore(str(tmp_path / "ir_devices.json")).get("ceiling_light", "night")
    assert stored == "bGVhcm5lZA=="


def test_send_code_replays_persisted_payload(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    fake = FakeRm()
    ir.IrStore(settings.openclaw_ir_devices_path).put("ceiling_light", "night", "cGxheQ==")
    monkeypatch.setattr(ir, "discover_rm", lambda settings: (fake, "fake rm"))
    msg = ir.send_code(settings, "ceiling_light", "night")
    assert "已送出" in msg
    assert fake.sent == [b"play"]


def test_send_code_replays_fan_payload_through_the_shared_rm_path(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    fake = FakeRm()
    ir.IrStore(settings.openclaw_ir_devices_path).put("fan", "power", "cGxheQ==")
    monkeypatch.setattr(ir, "discover_rm", lambda settings: (fake, "fake rm"))

    msg = ir.send_code(settings, "fan", "power")

    assert "已送出" in msg
    assert fake.sent == [b"play"]


def _patch_resolver_llm(monkeypatch, reply: str):
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def generate(self, prompt, *, temperature=0.0, think=False):
            calls.append(prompt)
            return reply

    from openclaw_adapter import dynamic_tools, llm_pool_settings

    monkeypatch.setattr(dynamic_tools, "OllamaTextClient", FakeClient)
    monkeypatch.setattr(llm_pool_settings, "resolve_provider_model", lambda s, p: "fake-model")
    return calls


def _resolver_settings(tmp_path):
    settings = _settings(tmp_path)
    settings.openclaw_local_text_endpoint = "http://127.0.0.1:11434"
    settings.openclaw_local_text_timeout_seconds = 5
    return settings


def test_send_code_resolves_spoken_name_via_grounded_llm(tmp_path, monkeypatch):
    settings = _resolver_settings(tmp_path)
    fake = FakeRm()
    ir.IrStore(settings.openclaw_ir_devices_path).put("fan", "power", "cGxheQ==")
    monkeypatch.setattr(ir, "discover_rm", lambda settings: (fake, "fake rm"))
    calls = _patch_resolver_llm(monkeypatch, "fan power")
    msg = ir.send_code(settings, "電風扇", "on")
    assert "已送出" in msg
    assert "fan / power" in msg
    assert fake.sent == [b"play"]
    assert len(calls) == 1
    assert "fan power" in calls[0] and "電風扇" in calls[0]


def test_send_code_exact_match_skips_resolver(tmp_path, monkeypatch):
    settings = _resolver_settings(tmp_path)
    fake = FakeRm()
    ir.IrStore(settings.openclaw_ir_devices_path).put("fan", "power", "cGxheQ==")
    monkeypatch.setattr(ir, "discover_rm", lambda settings: (fake, "fake rm"))
    calls = _patch_resolver_llm(monkeypatch, "fan power")
    msg = ir.send_code(settings, "fan", "power")
    assert "已送出" in msg
    assert calls == []


def test_send_code_keeps_error_when_resolver_declines(tmp_path, monkeypatch):
    settings = _resolver_settings(tmp_path)
    ir.IrStore(settings.openclaw_ir_devices_path).put("fan", "power", "cGxheQ==")
    _patch_resolver_llm(monkeypatch, "none")
    msg = ir.send_code(settings, "冷氣", "on")
    assert "找不到 IR：冷氣 / on" in msg


def test_send_code_rejects_llm_invented_pair(tmp_path, monkeypatch):
    settings = _resolver_settings(tmp_path)
    ir.IrStore(settings.openclaw_ir_devices_path).put("fan", "power", "cGxheQ==")
    _patch_resolver_llm(monkeypatch, "aircon power")
    msg = ir.send_code(settings, "冷氣", "on")
    assert "找不到 IR：冷氣 / on" in msg


def test_handler_send_joins_multiword_device(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    calls = []
    monkeypatch.setattr(ir, "send_code", lambda s, d, b: calls.append((d, b)) or "sent")
    handler = ir.build_ir_handler(settings)
    assert handler("send all lights on", "chat") == "sent"
    assert calls == [("all lights", "on")]


def test_render_devices_uses_opaque_tokens(tmp_path):
    settings = _settings(tmp_path)
    ir.IrStore(settings.openclaw_ir_devices_path).put("ceiling_light", "night", "abc")
    text, markup = ir.render_devices(settings)
    assert "ceiling_light" in text
    cb = markup["inline_keyboard"][0][0]["callback_data"]
    assert cb.startswith("ir:s:")
    assert "ceiling_light" not in cb
    assert "night" not in cb


def test_callback_sends_tokenized_button(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    token = ir.IrTokenCache(settings.openclaw_ir_token_cache_path).put("ceiling_light", "night")
    calls = []
    monkeypatch.setattr(ir, "send_code", lambda s, d, b: calls.append((d, b)) or "sent")
    cb = ir.build_ir_callback_handler(settings)
    toast, new_text, markup = cb(f"s:{token}", "", "chat")
    assert toast == "sent"
    assert new_text is None
    assert markup is None
    assert calls == [("ceiling_light", "night")]
