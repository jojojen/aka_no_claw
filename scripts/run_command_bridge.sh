#!/bin/sh
# Respawn wrapper for the tmux `bridge` pane (started by service_restart.py).
#
# Same rationale as run_telegram_poll.sh: the pane used to `exec python …`, so
# a bridge crash closed the pane permanently and the Web UI — the rescue path
# when the Telegram poller dies — went down with it. The wrapper relaunches
# after a short pause (long enough for :8781 to leave TIME_WAIT).
#
# Kept as a separate script so the pane shell's argv does NOT contain
# "python … command-bridge" — service_restart.py counts and kills workers by
# that pattern and must only ever see the real bridge process.
cd "$(dirname "$0")/.." || exit 1
while true; do
  .venv/bin/python -m openclaw_adapter command-bridge --lan --port 8781
  rc=$?
  echo "[run_command_bridge] bridge exited rc=$rc — relaunch in 10s"
  sleep 10
done
