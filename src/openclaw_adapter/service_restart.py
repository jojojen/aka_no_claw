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

mkdir -p "$LOG_DIR"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] restartall requested source=$SOURCE pid=$$"

# Let the caller send its response before this script stops the caller process.
sleep 2

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
    nohup "$@" >> "$logfile" 2>&1 &
  )
}}

stop_pattern "vite web" "aka_no_claw_web/frontend/node_modules/.bin/vite --host 0.0.0.0"
stop_pattern "command bridge" "openclaw_adapter command-bridge --lan --port 8781"
stop_pattern "chat web" "openclaw_adapter chat-web --host 0.0.0.0 --port 8780"
stop_pattern "telegram poll" "openclaw_adapter telegram-poll --with-reputation-agent --no-dashboard"
stop_pattern "opportunity agent" "openclaw_adapter opportunity-agent"
stop_pattern "sns monitor" "openclaw_adapter sns-monitor-service"
stop_pattern "price monitor" "openclaw_adapter price-monitor-service"
stop_pattern "reputation snapshot" "reputation_snapshot/.venv/bin/python app.py"
stop_pattern "scrape workers" "openclaw_adapter.scrape_worker"
stop_pattern "playwright drivers" "related_to_claw/.*/playwright/driver/package/cli.js run-driver"

sleep 3

force_pattern "vite web" "aka_no_claw_web/frontend/node_modules/.bin/vite --host 0.0.0.0"
force_pattern "command bridge" "openclaw_adapter command-bridge --lan --port 8781"
force_pattern "chat web" "openclaw_adapter chat-web --host 0.0.0.0 --port 8780"
force_pattern "telegram poll" "openclaw_adapter telegram-poll --with-reputation-agent --no-dashboard"
force_pattern "opportunity agent" "openclaw_adapter opportunity-agent"
force_pattern "sns monitor" "openclaw_adapter sns-monitor-service"
force_pattern "price monitor" "openclaw_adapter price-monitor-service"
force_pattern "reputation snapshot" "reputation_snapshot/.venv/bin/python app.py"
force_pattern "scrape workers" "openclaw_adapter.scrape_worker"
force_pattern "playwright drivers" "related_to_claw/.*/playwright/driver/package/cli.js run-driver"

start_service "reputation_snapshot" "$REPUTATION" "$LOG_DIR/reputation_snapshot.log" "$REPUTATION/.venv/bin/python" app.py
start_service "price monitor" "$CLAW" "$LOG_DIR/openclaw_price_monitor.log" "$CLAW/.venv/bin/python" -m openclaw_adapter price-monitor-service
start_service "sns monitor" "$CLAW" "$LOG_DIR/openclaw_sns_monitor.log" "$CLAW/.venv/bin/python" -m openclaw_adapter sns-monitor-service
start_service "opportunity agent" "$CLAW" "$LOG_DIR/opportunity_agent.log" "$CLAW/.venv/bin/python" -m openclaw_adapter opportunity-agent
start_service "telegram poll" "$CLAW" "$LOG_DIR/telegram-poll.log" "$CLAW/.venv/bin/python" -m openclaw_adapter telegram-poll --with-reputation-agent --no-dashboard
start_service "chat web" "$CLAW" "$LOG_DIR/openclaw_chat_web.log" "$CLAW/.venv/bin/python" -m openclaw_adapter chat-web --host 0.0.0.0 --port 8780
start_service "command bridge" "$CLAW" "$LOG_DIR/command_bridge.log" "$CLAW/.venv/bin/python" -m openclaw_adapter command-bridge --lan --port 8781
start_service "web frontend" "$WEB" "$LOG_DIR/openclaw_web_vite.log" npm run dev -- --host 0.0.0.0

sleep 2
echo "[$(date '+%Y-%m-%d %H:%M:%S')] restartall finished"
"""


def _sh(path_or_text: object) -> str:
    text = str(path_or_text)
    return "'" + text.replace("'", "'\"'\"'") + "'"
