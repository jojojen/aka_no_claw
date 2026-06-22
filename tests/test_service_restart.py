from __future__ import annotations

from pathlib import Path

from assistant_runtime import AssistantSettings
from openclaw_adapter.service_restart import RESTART_MESSAGE, _build_restart_script, build_restart_all_handler


def test_restart_script_covers_core_services() -> None:
    script = _build_restart_script(
        workspace_dir=Path("/tmp/workspace"),
        claw_dir=Path("/tmp/workspace/aka_no_claw"),
        source="test",
    )

    # Non-launchd services: restarted via kill+nohup.
    assert "command-bridge --lan --port 8781" in script
    assert "reputation_snapshot/.venv/bin/python" in script
    assert "npm run dev -- --host 0.0.0.0" in script
    assert "openclaw_adapter.scrape_worker" in script


def test_launchd_services_use_kickstart_not_nohup() -> None:
    # The duplicate-poller bug: kill+nohup of a launchd KeepAlive service runs a
    # copy alongside the one launchd respawns. launchd-managed services must be
    # restarted via `kickstart -k` (single instance), never nohup-started.
    script = _build_restart_script(
        workspace_dir=Path("/tmp/workspace"),
        claw_dir=Path("/tmp/workspace/aka_no_claw"),
        source="test",
    )

    for label in ("telegram", "price_monitor", "sns_monitor", "opportunity", "chat_web"):
        assert f'kickstart_service "{label}"' in script

    # These must NOT be nohup-started (that was the duplicate source).
    assert "start_service \"telegram poll\"" not in script
    assert "-m openclaw_adapter telegram-poll" not in script
    assert "-m openclaw_adapter price-monitor-service" not in script
    assert "-m openclaw_adapter sns-monitor-service" not in script
    assert "-m openclaw_adapter opportunity-agent" not in script
    assert "-m openclaw_adapter chat-web" not in script

    # The nohup chat-web "squatter" on :8780 is still stopped so launchd can bind.
    assert 'stop_pattern "chat web (nohup squatter)"' in script
    # kickstart uses launchctl in the user gui domain.
    assert "launchctl kickstart -k" in script


def test_bridge_port_reclaimed_before_relaunch() -> None:
    # A `pgrep -f` pattern stop can miss the running bridge; if :8781 stays bound
    # the fresh bridge dies on EADDRINUSE and the web path keeps serving old code.
    # The restart must reclaim the port by listener, independent of the cmdline.
    script = _build_restart_script(
        workspace_dir=Path("/tmp/workspace"),
        claw_dir=Path("/tmp/workspace/aka_no_claw"),
        source="test",
    )

    assert 'free_port "command bridge" 8781' in script
    assert 'free_port "chat web" 8780' in script
    assert "lsof -nP -iTCP:" in script
    # The bridge port must be reclaimed BEFORE the bridge is (re)started.
    assert script.index('free_port "command bridge" 8781') < script.index(
        'start_service "command bridge"'
    )


def test_restart_all_handler_schedules_detached_restart(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        "openclaw_adapter.service_restart.trigger_restart_all",
        lambda *, settings, source: calls.append(source),
    )

    handler = build_restart_all_handler(AssistantSettings())
    assert handler("", "chat-1") == RESTART_MESSAGE
    assert calls == ["telegram"]
