"""Safe local restart orchestration for OpenClaw services.

The caller may be the Telegram bot or the web command bridge, so the real work
must happen in a detached process after the response is sent.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from assistant_runtime import AssistantSettings

RESTART_MESSAGE = "已排程重啟龍蝦所有本機服務，約 5-15 秒後恢復。"


def build_restart_all_handler(settings: AssistantSettings):
    def _handler(remainder: str, chat_id: str) -> str:
        trigger_restart_all(settings=settings, source="telegram")
        return RESTART_MESSAGE

    return _handler


def trigger_restart_all(*, settings: AssistantSettings, source: str = "unknown") -> Path:
    """Write and launch a detached restart script.

    Returns the script path for logging/tests. The script intentionally sleeps
    before stopping processes so the HTTP/Telegram response can leave first.
    """
    claw_dir = Path(__file__).resolve().parents[2]
    workspace_dir = claw_dir.parent
    restart_dir = claw_dir / ".openclaw_tmp" / "restart"
    restart_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    script_path = restart_dir / f"restart_all_{stamp}_{os.getpid()}.sh"
    log_path = claw_dir / "logs" / "restart_all.log"
    script_path.write_text(
        _build_restart_script(
            workspace_dir=workspace_dir,
            claw_dir=claw_dir,
            source=source,
        ),
        encoding="utf-8",
    )
    script_path.chmod(0o700)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("ab")
    subprocess.Popen(
        ["/bin/bash", str(script_path)],
        cwd=str(claw_dir),
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    return script_path


def _build_restart_script(*, workspace_dir: Path, claw_dir: Path, source: str) -> str:
    web_dir = workspace_dir / "aka_no_claw_web" / "frontend"
    reputation_dir = workspace_dir / "reputation_snapshot"
    return f"""#!/usr/bin/env bash
set -u

WORKSPACE={_sh(workspace_dir)}
CLAW={_sh(claw_dir)}
WEB={_sh(web_dir)}
REPUTATION={_sh(reputation_dir)}
SOURCE={_sh(source)}
LOG_DIR="$CLAW/logs"
UID_NUM="$(id -u)"
TMUX_SOCKET="openclaw_codex"
TMUX_BIN="$(command -v tmux || true)"

mkdir -p "$LOG_DIR"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] restartall requested source=$SOURCE pid=$$"

# Let the caller send its response before this script stops the caller process.
sleep 2

    # Most launchd KeepAlive services are restarted with `kickstart -k` (kill +
    # relaunch under supervision = exactly ONE instance). Telegram is intentionally
    # excluded: launchctl-submitted daemon jobs cannot reliably access macOS local
    # network devices such as BroadLink RM4 Mini, so Telegram is restarted with the
    # same shell/nohup path as the user-launched process.
kickstart_service() {{
  local label="$1"
  echo "[$(date '+%H:%M:%S')] kickstart $label"
  launchctl kickstart -k "gui/$UID_NUM/local.openclaw.$label" 2>/dev/null \\
    || echo "[$(date '+%H:%M:%S')] kickstart $label failed (not loaded?)"
}}

stop_pattern() {{
  local label="$1"
  local pattern="$2"
  local pids
  pids="$(pgrep -f "$pattern" 2>/dev/null | tr '\\n' ' ' || true)"
  if [ -z "$pids" ]; then
    echo "[$(date '+%H:%M:%S')] stop $label: none"
    return 0
  fi
  echo "[$(date '+%H:%M:%S')] stop $label: $pids"
  for pid in $pids; do
    [ "$pid" = "$$" ] && continue
    kill "$pid" 2>/dev/null || true
  done
}}

force_pattern() {{
  local label="$1"
  local pattern="$2"
  local pids
  pids="$(pgrep -f "$pattern" 2>/dev/null | tr '\\n' ' ' || true)"
  if [ -z "$pids" ]; then
    return 0
  fi
  echo "[$(date '+%H:%M:%S')] force $label: $pids"
  for pid in $pids; do
    [ "$pid" = "$$" ] && continue
    kill -9 "$pid" 2>/dev/null || true
  done
}}

# Reclaim a TCP port by killing whatever LISTENs on it. This is the robust
# backstop for the command bridge: a `pgrep -f` pattern stop
# can miss (a process whose argv differs from the expected pattern), but a port
# that stays bound means the freshly-launched service can't bind and silently
# dies with EADDRINUSE — so we reclaim by port, independent of the cmdline.
free_port() {{
  local label="$1"
  local port="$2"
  local pids
  pids="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | tr '\\n' ' ' || true)"
  if [ -z "$pids" ]; then
    echo "[$(date '+%H:%M:%S')] free $label :$port: none"
    return 0
  fi
  echo "[$(date '+%H:%M:%S')] free $label :$port: $pids"
  for pid in $pids; do
    [ "$pid" = "$$" ] && continue
    kill "$pid" 2>/dev/null || true
  done
  sleep 1
  pids="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | tr '\\n' ' ' || true)"
  for pid in $pids; do
    [ "$pid" = "$$" ] && continue
    kill -9 "$pid" 2>/dev/null || true
  done
}}

start_service() {{
  local label="$1"
  local cwd="$2"
  local logfile="$3"
  shift 3
  if [ ! -d "$cwd" ]; then
    echo "[$(date '+%H:%M:%S')] start $label skipped: missing $cwd"
    return 0
  fi
  echo "[$(date '+%H:%M:%S')] start $label"
  (
    cd "$cwd" || exit 1
    nohup "$@" </dev/null >> "$logfile" 2>&1 &
  )
}}

start_tmux_service() {{
  local session="$1"
  local label="$2"
  local cwd="$3"
  shift 3
  if [ ! -d "$cwd" ]; then
    echo "[$(date '+%H:%M:%S')] tmux start $label skipped: missing $cwd"
    return 0
  fi
  if [ -z "$TMUX_BIN" ]; then
    echo "[$(date '+%H:%M:%S')] tmux start $label failed: tmux not found"
    return 1
  fi
  echo "[$(date '+%H:%M:%S')] tmux start $label socket=$TMUX_SOCKET session=$session"
  "$TMUX_BIN" -L "$TMUX_SOCKET" new-session -d -s "$session" \\
    "cd '$cwd' && $*"
}}

# Kill ORPHAN copies of a launchd-managed worker (aka_no_claw#40). `kickstart -k`
# only ever touches launchd's own instance; a duplicate started by hand / left
# over from an earlier kill+nohup keeps running the same command line and burns
# ~100% CPU alongside the supervised one. We find launchd's current PID for the
# label and kill every OTHER process matching the command pattern. If launchd's
# PID can't be read we skip (never risk killing the live supervised instance).
reap_orphans() {{
  local label="$1"
  local pattern="$2"
  local keep pids killed
  keep="$(launchctl list 2>/dev/null | awk -v l="local.openclaw.$label" '$3==l && $1 ~ /^[0-9]+$/ {{print $1}}')"
  pids="$(pgrep -f "$pattern" 2>/dev/null | tr '\\n' ' ' || true)"
  if [ -z "$pids" ]; then
    echo "[$(date '+%H:%M:%S')] reap $label: none"
    return 0
  fi
  if [ -z "$keep" ]; then
    echo "[$(date '+%H:%M:%S')] reap $label: launchd PID unknown — skipping (pids=$pids)"
    return 0
  fi
  killed=""
  for pid in $pids; do
    [ "$pid" = "$$" ] && continue
    [ "$pid" = "$keep" ] && continue
    killed="$killed $pid"
    kill "$pid" 2>/dev/null || true
  done
  if [ -z "$killed" ]; then
    echo "[$(date '+%H:%M:%S')] reap $label: only launchd PID $keep — clean"
    return 0
  fi
  echo "[$(date '+%H:%M:%S')] reap $label: launchd keeps $keep; killed orphans$killed"
  sleep 1
  for pid in $killed; do
    kill -9 "$pid" 2>/dev/null || true
  done
}}

# Count REAL worker processes for a service. The dedicated tmux launcher
# (telegram/bridge) keeps the worker's `python -m openclaw_adapter …` command in
# its OWN argv, so a bare `pgrep -f` would match the tmux server too and report 2
# for a single worker (aka_no_claw#40). Drop any PID whose command line is the
# tmux launcher so only the spawned Python worker is counted.
count_service() {{
  local label="$1"
  local pattern="$2"
  local n
  n="$(ps -Ao pid,command 2>/dev/null \\
    | grep -E "$pattern" \\
    | grep -v "tmux -L $TMUX_SOCKET" \\
    | grep -v grep \\
    | wc -l | tr -d ' ')"
  echo "[$(date '+%H:%M:%S')] final count $label: $n"
}}

snapshot() {{
  echo "[$(date '+%H:%M:%S')] $1 snapshot:"
  ps -Ao pid,%cpu,command 2>/dev/null \\
    | grep -E "openclaw_adapter (price-monitor-service|opportunity-agent|sns-monitor-service|telegram-poll|command-bridge)" \\
    | grep -v grep \\
    | grep -v "tmux -L $TMUX_SOCKET" \\
    | sed 's/^/    /' || true
}}

# Capture the pre-restart process table now that snapshot() is defined (it must
# be called AFTER its definition, or bash errors "snapshot: command not found").
snapshot "before"

# Non-launchd processes — stop these by command pattern.
# The launchd-managed services are intentionally absent here: `kickstart -k`
# below does their kill+relaunch, so pattern-killing them would only race
# launchd's KeepAlive respawn.
launchctl remove "local.openclaw.telegram" 2>/dev/null || true
stop_pattern "telegram" "openclaw_adapter telegram-poll"
stop_pattern "vite web" "aka_no_claw_web/frontend/node_modules/.bin/vite --host 0.0.0.0"
stop_pattern "command bridge" "openclaw_adapter command-bridge --lan --port 8781"
stop_pattern "reputation snapshot" "reputation_snapshot/.venv/bin/python app.py"
stop_pattern "scrape workers" "openclaw_adapter.scrape_worker"
stop_pattern "playwright drivers" "related_to_claw/.*/playwright/driver/package/cli.js run-driver"

sleep 3

force_pattern "telegram" "openclaw_adapter telegram-poll"
force_pattern "vite web" "aka_no_claw_web/frontend/node_modules/.bin/vite --host 0.0.0.0"
force_pattern "command bridge" "openclaw_adapter command-bridge --lan --port 8781"
force_pattern "reputation snapshot" "reputation_snapshot/.venv/bin/python app.py"
force_pattern "scrape workers" "openclaw_adapter.scrape_worker"
force_pattern "playwright drivers" "related_to_claw/.*/playwright/driver/package/cli.js run-driver"

# launchd-managed services: one clean instance each via kickstart (NOT nohup).
kickstart_service "price_monitor"
kickstart_service "sns_monitor"
kickstart_service "opportunity"

# Give launchd a moment to relaunch + register the new PIDs, then kill any
# ORPHAN duplicates of the managed workers (aka_no_claw#40): kickstart only
# replaces launchd's own instance, so a hand-started copy keeps running and
# pegs the CPU. reap_orphans keeps just the launchd PID per service.
sleep 2
reap_orphans "price_monitor" "openclaw_adapter price-monitor-service"
reap_orphans "sns_monitor" "openclaw_adapter sns-monitor-service"
reap_orphans "opportunity" "openclaw_adapter opportunity-agent"

# Reclaim the bridge port before relaunch: the pattern stop above can miss the
# running bridge, and a still-bound :8781 makes the fresh bridge die on
# EADDRINUSE (so the web 生活 mode keeps serving the OLD code).
free_port "command bridge" 8781

# BroadLink RM4 Mini UDP auth is sensitive to the macOS app/launch context.
# The verified-good path is a fresh, Codex-launched tmux server on a dedicated
# socket. Kill any stale dedicated server before recreating bridge/telegram so
# /restartall never falls back to the old Terminal/default-tmux identity.
if [ -n "$TMUX_BIN" ]; then
  echo "[$(date '+%H:%M:%S')] reset dedicated tmux socket=$TMUX_SOCKET"
  "$TMUX_BIN" -L "$TMUX_SOCKET" kill-server 2>/dev/null || true
fi

# Genuinely non-launchd services: (re)start detached with nohup.
start_service "reputation_snapshot" "$REPUTATION" "$LOG_DIR/reputation_snapshot.log" "$REPUTATION/.venv/bin/python" app.py
start_tmux_service "telegram" "telegram" "$CLAW" "source '$CLAW/run/mac-mini-stack.env' 2>/dev/null || true; export PYTHONPATH='.:src'; exec '$CLAW/.venv/bin/python' -m openclaw_adapter telegram-poll --with-reputation-agent --no-dashboard"
start_tmux_service "bridge" "command bridge" "$CLAW" "exec '$CLAW/.venv/bin/python' -m openclaw_adapter command-bridge --lan --port 8781"
start_service "web frontend" "$WEB" "$LOG_DIR/openclaw_web_vite.log" npm run dev -- --host 0.0.0.0

sleep 2
snapshot "after"
count_service "price_monitor" "openclaw_adapter price-monitor-service"
count_service "sns_monitor" "openclaw_adapter sns-monitor-service"
count_service "opportunity" "openclaw_adapter opportunity-agent"
count_service "telegram" "python.*openclaw_adapter telegram-poll"
count_service "command bridge" "python.*openclaw_adapter command-bridge --lan --port 8781"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] restartall finished"
"""


def _sh(path_or_text: object) -> str:
    text = str(path_or_text)
    return "'" + text.replace("'", "'\"'\"'") + "'"
