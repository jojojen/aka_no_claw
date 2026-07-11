from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from assistant_runtime import AssistantSettings
from openclaw_adapter.service_restart import RESTART_MESSAGE, _build_restart_script, build_restart_all_handler


def _extract_func(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    rest = script[start:]
    end = rest.index("\n}\n") + len("\n}\n")
    return rest[:end]


def test_restart_script_covers_core_services() -> None:
    script = _build_restart_script(
        workspace_dir=Path("/tmp/workspace"),
        claw_dir=Path("/tmp/workspace/aka_no_claw"),
        source="test",
    )

    # Non-launchd services: restarted via kill+nohup.
    # The bridge launches via its respawn wrapper (it is the rescue path when
    # the poller dies, so a crash must not close the pane permanently).
    assert "exec /bin/sh '$CLAW/scripts/run_command_bridge.sh'" in script
    # …but the inline python start must be gone (stop_pattern lines still
    # mention the command string — that's the kill pattern, not a start).
    assert ".venv/bin/python' -m openclaw_adapter command-bridge" not in script
    bridge_wrapper = (
        Path(__file__).resolve().parents[1] / "scripts" / "run_command_bridge.sh"
    ).read_text(encoding="utf-8")
    assert "command-bridge --lan --port 8781" in bridge_wrapper
    assert "reputation_snapshot/.venv/bin/python" in script
    assert "npm run dev -- --host 0.0.0.0" in script
    assert "openclaw_adapter.scrape_worker" in script


def test_launchd_services_use_kickstart_not_nohup_except_telegram() -> None:
    # The duplicate-poller bug: kill+nohup of a launchd KeepAlive service runs a
    # copy alongside the one launchd respawns. launchd-managed services must be
    # restarted via `kickstart -k` (single instance), never nohup-started.
    script = _build_restart_script(
        workspace_dir=Path("/tmp/workspace"),
        claw_dir=Path("/tmp/workspace/aka_no_claw"),
        source="test",
    )

    for label in ("price_monitor", "sns_monitor", "opportunity", "chat_web"):
        assert f'kickstart_service "{label}"' in script

    # Telegram needs local-network access for BroadLink RM4 Mini. On macOS,
    # launchctl-submitted daemon jobs fail ARP/route warm-up even when the same
    # command works from a user shell, so Telegram is intentionally not launchd.
    assert 'kickstart_service "telegram"' not in script
    assert 'launchctl remove "local.openclaw.telegram"' in script
    assert 'stop_pattern "telegram" "openclaw_adapter telegram-poll"' in script
    assert 'start_tmux_service "telegram"' in script
    # The poller launches via the respawn wrapper, NOT inline: telegram left
    # launchd, so a crash (or the poll-watchdog's os._exit) must be respawned
    # by the wrapper loop instead of silently closing the tmux pane
    # (2026-07-03: /vpn switch killed the poller with no respawner).
    assert "run_telegram_poll.sh" in script
    assert "-m openclaw_adapter telegram-poll" not in script
    wrapper = (
        Path(__file__).resolve().parents[1] / "scripts" / "run_telegram_poll.sh"
    ).read_text(encoding="utf-8")
    assert "-m openclaw_adapter telegram-poll" in wrapper
    assert "sleep 30" in wrapper  # > Telegram's ~20s getUpdates slot hold (409 guard)

    # These must NOT be nohup-started (that was the duplicate source).
    assert "-m openclaw_adapter price-monitor-service" not in script
    assert "-m openclaw_adapter sns-monitor-service" not in script
    assert "-m openclaw_adapter opportunity-agent" not in script
    assert "-m openclaw_adapter chat-web" not in script

    # The nohup chat-web "squatter" on :8780 is still stopped so launchd can bind.
    assert 'stop_pattern "chat web (nohup squatter)"' in script
    # kickstart uses launchctl in the user gui domain.
    assert "launchctl kickstart -k" in script


def test_broadlink_sensitive_services_start_in_dedicated_tmux_socket() -> None:
    # BroadLink RM4 Mini UDP auth failed from the old Terminal/default-tmux
    # identity but worked from a fresh Codex-launched tmux server. /restartall
    # must recreate bridge + telegram on that dedicated socket every time.
    script = _build_restart_script(
        workspace_dir=Path("/tmp/workspace"),
        claw_dir=Path("/tmp/workspace/aka_no_claw"),
        source="test",
    )

    assert 'TMUX_SOCKET="openclaw_stack"' in script
    assert "TMUX_BIN=\"$(command -v tmux || true)\"" in script
    assert "/opt/homebrew/bin/brew shellenv" in script
    assert "/usr/local/bin/brew shellenv" in script
    assert 'tmux start $label socket=$TMUX_SOCKET' in script
    assert '"$TMUX_BIN" -L "$TMUX_SOCKET" kill-server' in script
    assert 'start_tmux_service "telegram" "telegram"' in script
    assert 'start_tmux_service "bridge" "command bridge"' in script
    assert "source '$CLAW/run/mac-mini-stack.env' 2>/dev/null || true" in script
    assert 'start_service "telegram"' not in script
    assert 'start_service "command bridge"' not in script
    assert script.index('free_port "command bridge" 8781') < script.index(
        'start_tmux_service "bridge"'
    )


def test_restart_script_runs_broadlink_preflight_before_tmux_services() -> None:
    # BroadLink sometimes wedges across restarts while a fresh short-lived IR
    # worker can still auth successfully. /restartall should run that fresh
    # discover/auth probe before Telegram and bridge come back.
    script = _build_restart_script(
        workspace_dir=Path("/tmp/workspace"),
        claw_dir=Path("/tmp/workspace/aka_no_claw"),
        source="test",
    )
    preflight_call = script.rindex("\nbroadlink_preflight\n")

    assert 'BROADLINK_PREFLIGHT_ATTEMPTS="${BROADLINK_PREFLIGHT_ATTEMPTS:-3}"' in script
    assert 'broadlink_preflight() {' in script
    assert '"$CLAW/.venv/bin/python" -m openclaw_adapter.ir_worker discover' in script
    assert preflight_call > script.index('kill-server 2>/dev/null || true')
    assert preflight_call < script.index('start_tmux_service "telegram"')
    assert preflight_call < script.index('start_tmux_service "bridge"')


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
        'start_tmux_service "bridge"'
    )


def test_orphan_launchd_workers_are_reaped() -> None:
    # aka_no_claw#40: kickstart -k only replaces launchd's OWN instance, so a
    # hand-started duplicate of a managed worker (e.g. price-monitor-service,
    # opportunity-agent) survives and pegs the CPU. The restart must reap orphans
    # — keeping only launchd's PID per service — for every managed worker.
    script = _build_restart_script(
        workspace_dir=Path("/tmp/workspace"),
        claw_dir=Path("/tmp/workspace/aka_no_claw"),
        source="test",
    )

    reaped = {
        "price_monitor": "openclaw_adapter price-monitor-service",
        "sns_monitor": "openclaw_adapter sns-monitor-service",
        "opportunity": "openclaw_adapter opportunity-agent",
        "chat_web": "openclaw_adapter chat-web",
    }
    for label, pattern in reaped.items():
        assert f'reap_orphans "{label}" "{pattern}"' in script

    # reap_orphans keeps launchd's PID and kills the rest; it must run AFTER the
    # kickstart that (re)establishes that PID.
    assert "launchctl list" in script
    assert script.index('kickstart_service "price_monitor"') < script.index(
        'reap_orphans "price_monitor"'
    )
    # Before/after snapshots + per-service final counts land in the restart log.
    assert 'snapshot "before"' in script
    assert 'snapshot "after"' in script
    assert 'count_service "opportunity"' in script

    # bash needs snapshot() defined BEFORE it is called, or the "before" snapshot
    # dies with "snapshot: command not found" and never reaches the log.
    assert script.index("snapshot() {") < script.index('snapshot "before"')
    assert script.index('snapshot "before"') < script.index('snapshot "after"')


def test_count_service_excludes_tmux_launcher(tmp_path) -> None:
    # aka_no_claw#40: telegram/bridge launch via `tmux -L openclaw_stack
    # new-session … python -m openclaw_adapter telegram-poll …`, so the tmux
    # server's OWN argv contains the worker pattern. A bare pgrep then matches
    # both the tmux launcher and the real worker → final count 2 for one worker.
    # count_service must drop the tmux launcher and report exactly 1.
    script = _build_restart_script(
        workspace_dir=Path("/tmp/workspace"),
        claw_dir=Path("/tmp/workspace/aka_no_claw"),
        source="test",
    )
    count_func = _extract_func(script, "count_service")

    # Fake `ps -Ao pid,command`: row 111 is the tmux launcher (its argv embeds
    # the worker command), row 222 is the real python worker. Only 222 counts.
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "ps").write_text(
        "#!/bin/bash\n"
        'echo "  111 tmux -L openclaw_stack new-session -d -s telegram '
        'cd /x && exec /x/.venv/bin/python -m openclaw_adapter telegram-poll"\n'
        'echo "  222 /x/.venv/bin/python -m openclaw_adapter telegram-poll '
        '--with-reputation-agent --no-dashboard"\n'
    )
    p = fakebin / "ps"
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    harness = (
        'TMUX_SOCKET="openclaw_stack"\n'
        + count_func
        + '\ncount_service "telegram" "python.*openclaw_adapter telegram-poll"\n'
    )
    env = dict(os.environ, PATH=f"{fakebin}:{os.environ['PATH']}")
    out = subprocess.run(
        ["/bin/bash", "-c", harness], capture_output=True, text=True, env=env
    ).stdout
    assert "final count telegram: 1" in out


def test_restart_all_handler_schedules_detached_restart(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        "openclaw_adapter.service_restart.trigger_restart_all",
        lambda *, settings, source: calls.append(source),
    )

    handler = build_restart_all_handler(AssistantSettings())
    assert handler("", "chat-1") == RESTART_MESSAGE
    assert calls == ["telegram"]
