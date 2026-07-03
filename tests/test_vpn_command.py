from __future__ import annotations

import pytest

from openclaw_adapter.vpn_command import (
    VpnConfigStore,
    VpnRotationScheduler,
    build_vpn_handler,
    mask_ip,
    open_connect_deeplink,
    switch_vpn,
)


def test_open_connect_deeplink_builds_encoded_url():
    seen: list[list[str]] = []
    open_connect_deeplink("Japan", runner=lambda cmd, **kw: seen.append(cmd))
    open_connect_deeplink(None, runner=lambda cmd, **kw: seen.append(cmd))
    open_connect_deeplink("United States", runner=lambda cmd, **kw: seen.append(cmd))
    assert seen[0] == ["open", "nordvpn://connect?country=Japan"]
    assert seen[1] == ["open", "nordvpn://connect"]
    assert seen[2] == ["open", "nordvpn://connect?country=United+States"]


def test_mask_ip_keeps_only_prefix():
    assert mask_ip("203.0.113.7") == "203.0.x.x"
    assert mask_ip("weird") == "weird"


def test_switch_vpn_detects_ip_change_and_builds_deeplink():
    opened: list[str] = []
    ips = iter(["1.1.1.1", "2.2.2.2"])
    result = switch_vpn(
        "Japan",
        opener=lambda country: opened.append(country),
        ip_probe=lambda: next(ips),
        sleep=lambda s: None,
    )
    assert opened == ["Japan"]
    assert result.changed
    assert result.old_ip == "1.1.1.1" and result.new_ip == "2.2.2.2"


def test_switch_vpn_reports_unchanged_when_app_ignores_deeplink():
    opened: list[str] = []
    result = switch_vpn(
        "Japan",
        opener=lambda country: opened.append(country),
        ip_probe=lambda: "1.1.1.1",
        sleep=lambda s: None,
        wait_seconds=8,
    )
    assert not result.changed
    # 沒反應時 deeplink 要整包重發一次（app 偶爾無視單發 deeplink）
    assert opened == ["Japan", "Japan"]


def test_switch_vpn_retry_deeplink_succeeds_on_second_attempt():
    opened: list[str] = []

    def probe():
        # 第一輪（2 次輪詢）IP 不變，第二次 deeplink 之後才換
        return "2.2.2.2" if len(opened) >= 2 else "1.1.1.1"

    result = switch_vpn(
        "Japan",
        opener=lambda country: opened.append(country),
        ip_probe=probe,
        sleep=lambda s: None,
        wait_seconds=8,
    )
    assert opened == ["Japan", "Japan"]
    assert result.changed
    assert result.new_ip == "2.2.2.2"


def test_switch_vpn_rejects_injection_country():
    with pytest.raises(ValueError):
        switch_vpn("Japan&exit=1", opener=lambda c: None, ip_probe=lambda: "1.1.1.1")


def _store(tmp_path) -> VpnConfigStore:
    return VpnConfigStore(tmp_path / "vpn_rotation.json")


def test_config_store_roundtrip_and_defaults(tmp_path):
    store = _store(tmp_path)
    config = store.load()
    assert config.countries == ["Japan"]
    assert not config.auto_enabled
    config.auto_enabled = True
    config.interval_minutes = 120
    config.notify_chat_id = "123"
    store.save(config)
    reloaded = store.load()
    assert reloaded.auto_enabled
    assert reloaded.interval_minutes == 120
    assert reloaded.notify_chat_id == "123"


def _handler(tmp_path, store=None, **overrides):
    store = store or _store(tmp_path)
    defaults = dict(
        switch_fn=lambda country: (_ for _ in ()).throw(AssertionError("no switch")),
        ip_probe=lambda: "203.0.113.7",
        country_probe=lambda: "JP",
        meshnet_probe=lambda: True,
    )
    defaults.update(overrides)
    return build_vpn_handler(object(), store, **defaults), store


def test_handler_status_masks_ip_and_reports_meshnet(tmp_path):
    handler, _ = _handler(tmp_path)
    reply = handler("", "123")
    assert "203.0.x.x" in reply
    assert "203.0.113.7" not in reply
    assert "JP" in reply
    assert "Meshnet：存活" in reply
    assert "自動輪替：關閉" in reply


def test_handler_switch_uses_pool_default_and_reports(tmp_path):
    calls: list[str] = []

    def fake_switch(country):
        calls.append(country)
        from openclaw_adapter.vpn_command import VpnSwitchResult

        return VpnSwitchResult(country=country, old_ip="1.1.1.1", new_ip="2.2.2.2")

    handler, _ = _handler(tmp_path, switch_fn=fake_switch)
    reply = handler("switch", "123")
    assert calls == ["Japan"]
    assert "1.1.x.x → 2.2.x.x" in reply

    handler("switch Singapore", "123")
    assert calls[-1] == "Singapore"


def test_handler_auto_on_off_persists_and_binds_chat(tmp_path):
    handler, store = _handler(tmp_path)
    reply = handler("auto on 6", "123")
    assert "已開啟" in reply
    config = store.load()
    assert config.auto_enabled
    assert config.interval_minutes == 360
    assert config.notify_chat_id == "123"

    reply = handler("auto off", "123")
    assert "已關閉" in reply
    assert not store.load().auto_enabled


def test_handler_pool_update_rejects_bad_names(tmp_path):
    handler, store = _handler(tmp_path)
    assert "已更新" in handler("pool Japan, Singapore", "123")
    assert store.load().countries == ["Japan", "Singapore"]
    assert "不合法" in handler("pool Jap;an", "123")
    assert store.load().countries == ["Japan", "Singapore"]


def test_scheduler_rotates_round_robin_only_when_due(tmp_path):
    store = _store(tmp_path)
    config = store.load()
    config.auto_enabled = True
    config.interval_minutes = 60
    config.countries = ["Japan", "Singapore"]
    config.notify_chat_id = "123"
    store.save(config)

    clock = {"now": 0.0}
    switched: list[str] = []
    sent: list[str] = []

    class Notifier:
        def send(self, text: str) -> None:
            sent.append(text)

    def fake_switch(country):
        from openclaw_adapter.vpn_command import VpnSwitchResult

        switched.append(country)
        return VpnSwitchResult(country=country, old_ip="1.1.1.1", new_ip="2.2.2.2")

    scheduler = VpnRotationScheduler(
        store,
        switch_fn=fake_switch,
        notifier_factory=lambda chat_id: Notifier(),
        monotonic=lambda: clock["now"],
    )
    assert scheduler.tick() is None  # not due yet
    clock["now"] = 3600
    assert scheduler.tick() is not None
    clock["now"] = 7200
    scheduler.tick()
    assert switched == ["Japan", "Singapore"]
    assert len(sent) == 2
    assert "自動輪替" in sent[0]


def test_scheduler_disabled_resets_countdown(tmp_path):
    store = _store(tmp_path)
    clock = {"now": 0.0}
    scheduler = VpnRotationScheduler(
        store,
        switch_fn=lambda c: (_ for _ in ()).throw(AssertionError("no switch")),
        monotonic=lambda: clock["now"],
    )
    clock["now"] = 999999
    assert scheduler.tick() is None
