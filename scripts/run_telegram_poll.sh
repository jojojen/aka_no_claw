#!/bin/sh
# Respawn wrapper for the tmux `telegram` pane (started by service_restart.py).
#
# The pane used to `exec python -m openclaw_adapter telegram-poll …`, so any
# process death closed the pane and NOTHING relaunched it: the bot left launchd
# management, which broke the poll-watchdog's "os._exit(1) and launchd KeepAlive
# respawns us" assumption (2026-07-03: a /vpn exit switch tore the getUpdates
# stream and killed Telegram this way while every other service stayed up).
#
# The 30s pause before relaunch matters: Telegram holds the previous getUpdates
# long-poll slot for ~20s, and reconnecting sooner triggers an HTTP 409 storm.
#
# Kept as a separate script (not an inline tmux loop) so the pane shell's argv
# does NOT contain "python … telegram-poll" — service_restart.py counts and
# kills workers by that pattern and must only ever see the real poller.
cd "$(dirname "$0")/.." || exit 1
while true; do
  .venv/bin/python -m openclaw_adapter telegram-poll --with-reputation-agent --no-dashboard
  rc=$?
  echo "[run_telegram_poll] poller exited rc=$rc — relaunch in 30s"
  sleep 30
done
