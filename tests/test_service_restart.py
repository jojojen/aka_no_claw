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

    assert "telegram-poll --with-reputation-agent --no-dashboard" in script
    assert "command-bridge --lan --port 8781" in script
    assert "chat-web --host 0.0.0.0 --port 8780" in script
    assert "reputation_snapshot/.venv/bin/python" in script
    assert "npm run dev -- --host 0.0.0.0" in script
    assert "openclaw_adapter.scrape_worker" in script


def test_restart_all_handler_schedules_detached_restart(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        "openclaw_adapter.service_restart.trigger_restart_all",
        lambda *, settings, source: calls.append(source),
    )

    handler = build_restart_all_handler(AssistantSettings())
    assert handler("", "chat-1") == RESTART_MESSAGE
    assert calls == ["telegram"]
