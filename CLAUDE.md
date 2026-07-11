# aka_no_claw — Claude Instructions

## Session Start (mandatory)

Fetch and read SKILL.md before the first response:
https://raw.githubusercontent.com/jojojen/claude-collab-rules/main/SKILL.md

Rules that must never be forgotten:
- **(H) Vassal's Oath**: End EVERY response with a one-line oath quoting the most relevant rule in your own words. Address the user as **my lord**.
- **(A) Git push**: Summarize repos/files/subjects first, wait for "推" or "ok", then push.
- **(C) Quality gates**: Run pytest yourself; verify live; report failures plainly.
- **(E) Anti-laziness**: Probe for discoverable info (paths, ports, symbols) instead of asking.
- **(G) Open-world**: Use LLM+RAG for recognition; never maintain hardcoded keyword lists.

## Project

Telegram bot on Mac Mini. Local Ollama (qwen3:14b). `/new` command → DynamicToolRunner generates Python tools, executes them, returns answer.

Two repos: `aka_no_claw` (bot logic) + `price_monitor_bot` (Telegram dispatcher).

## Operations — restarting the bot (龍蝦)

After editing code you MUST restart for changes to take effect.

**Correct restart (the ONLY supported way): the `/restartall` command** — this is
exactly what the user's「重啟龍蝦」button triggers. It runs the orchestrator in
`src/openclaw_adapter/service_restart.py` (`trigger_restart_all`), which writes a
detached script and brings the WHOLE Mac-mini stack back on the new code in one
clean pass. Claude cannot send a Telegram command, so when a restart is needed:
**ask the user to press「重啟龍蝦」(`/restartall`)**, then verify (below). Do NOT
hand-restart the poller — see the 409 warning.

The telegram bot is **no longer launchd-managed**; the old
`launchctl kickstart -k gui/$(id -u)/local.openclaw.telegram` label does not
exist anymore and will fail. Current topology:

- **tmux socket `openclaw_stack`** holds the two manually-launched workers:
  session `telegram` → `openclaw_adapter telegram-poll …`, and session `bridge`
  → `openclaw_adapter command-bridge --lan --port 8781`. `/restartall` does
  `tmux -L openclaw_stack kill-server` then recreates BOTH sessions, so it (and
  only it) is the safe way to restart the bridge too.
- **launchd-managed siblings** (restarted via `kickstart -k` inside the script):
  `local.openclaw.{price_monitor,sns_monitor,opportunity}` (+ `aivis`, `ollama`).

**NEVER restart the poller with manual `kill` + `nohup` / tmux respawn.** Two
processes both calling Telegram `getUpdates` → **HTTP 409 (Conflict) storm**; and
a 409 also fires if you relaunch within ~20s of killing a poller (Telegram holds
the old long-poll slot). `/restartall` sequences stop → force-kill → wait →
relaunch to avoid this; ad-hoc restarts race it. The startup drain in
`price_monitor_bot/bot.py` `run_telegram_polling` (`get_updates(timeout=0)`) is
NOT try-wrapped: a 409 there kills the poll loop while non-daemon threads keep
the process *alive*, so it looks healthy while `/new` is dead.

**Verify polling is live (don't trust process-alive alone):**

```
tmux -L openclaw_stack list-panes -a -F "#{session_name} pid=#{pane_pid}"  # telegram + bridge sessions present
lsof -nP -p <telegram-pid> | grep ESTABLISHED   # expect a conn to a Telegram IP 149.154.x.x:443
```

The stdout marker `Telegram bot polling as @Aka_No_Claw_bot` (printed by the
generic `telegram_core.polling` loop, which aka now calls directly) is a
block-buffered `print`, so it may lag — its absence is NOT proof of failure.
