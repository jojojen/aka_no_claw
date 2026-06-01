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

The bot is **launchd-managed with KeepAlive**, NOT a plain background process.
After editing code you MUST restart it for changes to take effect.

**Correct restart (the ONLY supported way):**

```
launchctl kickstart -k gui/$(id -u)/local.openclaw.telegram
```

`-k` = kill the running instance and relaunch it under supervision → exactly one
instance. Sibling services: `local.openclaw.{reputation,opportunity,ollama}`.
Logs (stdout+stderr): `logs/openclaw_telegram.log`.

**NEVER restart with manual `kill` + `nohup`.** KeepAlive instantly respawns the
killed instance, so your manual launch and the respawn both call Telegram
`getUpdates` → **HTTP 409 (Conflict) storm**. The startup drain in
`price_monitor_bot/bot.py` `run_telegram_polling` (~line 3370,
`get_updates(timeout=0)`) is NOT try-wrapped: a 409 there propagates to
`SystemExit(main())` and the poll loop never starts — yet non-daemon monitor
threads keep the process *alive*, so it looks healthy while `/new` is dead.
Also: a 409 fires if you restart within ~20s of killing a poller (Telegram holds
the old long-poll slot for the poll_timeout) — `kickstart -k` handles this, but
don't race it with manual launches.

**Verify polling is live (don't trust process-alive alone):**

```
lsof -nP -p <pid> | grep ESTABLISHED   # expect a conn to a Telegram IP 149.154.x.x:443
```

The stdout marker `OpenClaw Telegram bot polling as @Aka_No_Claw_bot` is a
block-buffered `print`, so it may lag — its absence is NOT proof of failure.
