# Opportunity Agent Handoff

Last updated: 2026-05-13

## Phase Log

### Phase 1 - Architecture

Decision: keep `opportunity_agent` inside `aka_no_claw` instead of creating a new repo.

Reason:

- `aka_no_claw` already owns Telegram runtime, `.env`, launchctl startup, SNS integration, price integration, and reputation integration.
- A new repo would add dependency and startup complexity before the opportunity loop is proven.
- Files are modular enough to split later.

### Phase 2 - Core MVP

Implemented modules:

- `opportunity_models.py`: dataclasses and deterministic IDs.
- `opportunity_store.py`: SQLite schema for candidates, price checks, and recommendations.
- `opportunity_scoring.py`: threshold and score rules.
- `opportunity_pipeline.py`: dependency-injected pipeline.
- `opportunity_agent.py`: live adapters for SNS LLM extraction, price lookup, Mercari search, reputation snapshot, and Telegram notification.

### Phase 3 - Runtime Wiring

Implemented:

- CLI tool: `python -m openclaw_adapter opportunity-agent`
- One-shot mode: `python -m openclaw_adapter opportunity-agent --once`
- CLI status: `python -m openclaw_adapter opportunity-status`
- Telegram status: `/hunt status`
- Mac launchctl job: `local.openclaw.opportunity`
- Stop script support for the new job.
- `.env.example` opportunity settings.

### Phase 4 - Verification

Completed on 2026-05-13:

- Focused opportunity tests: `7 passed`
- Expanded OpenClaw tests: `45 passed`
- Full `aka_no_claw` tests: `191 passed, 7 skipped`
- Price monitor focused tests after natural-language fallback fix: `28 passed`
- CLI smoke: `opportunity-agent --once` exits cleanly with an empty SNS database.
- Shell syntax: `start-mac-mini-stack.command` and `stop-mac-mini-stack.command` pass `bash -n`.
- Live launchctl smoke: `local.openclaw.opportunity` starts and remains running.
- First live tick: read SNS data, extracted 4 candidates, rejected all before notification because no reliable fair value was found.
- Visibility check: `opportunity-status --limit 4` shows the current 4 monitored candidates.

One adjacent fix was made in `price_monitor_bot/src/price_monitor_bot/natural_language.py`:

- Generic "最近什麼熱門排行" now stays in the TCG trend path and returns `None` without a game instead of being misrouted to SNS buzz.

### Phase 5 - Candidate Name Cleanup

Completed on 2026-05-13:

- The SNS extraction prompt now explicitly tells the local text model to store only the tradable product name.
- Candidate parsing now strips non-product terms such as `抽選情報`, `予約情報`, `発売情報`, `Mercari`, and `メルカリ`.
- `セット名収録 カード名` patterns are normalized to the individual card name, for example `アビスアイ収録 ホエルオーex` becomes `ホエルオーex`.
- Obvious unsupported franchises such as `遊☆戯☆王`, `デュエルマスターズ`, and `ONE PIECE CARD GAME` are rejected even if the LLM mislabels them as pokemon/ws.
- Existing local candidate rows were normalized so `/hunt status` no longer shows `アビスアイ 抽選情報` as a product.
- Focused opportunity tests after this fix: `7 passed`.

## Important Runtime Notes

- The agent is recommendation-only. It does not buy anything.
- It needs the local text model through Ollama for SNS product extraction.
- It needs `reputation_snapshot` running for seller checks.
- It reads SNS data from `SNS_DB_PATH`, currently defaulting to `data/sns.sqlite3`.
- It sends Telegram messages through the same OpenClaw bot token and allowed chat IDs.

## Useful Commands

Run one tick:

```bash
cd /path/to/aka_no_claw
PYTHONPATH=src ./.venv/bin/python -m openclaw_adapter opportunity-agent --once
```

Run focused tests:

```bash
cd /path/to/aka_no_claw
PYTHONPATH=src ./.venv/bin/pytest tests/test_opportunity_agent.py -q
```

Start the full Mac stack:

```bash
cd /path/to/aka_no_claw
./launchers/start-mac-mini-stack.command
```

## Next Handoff Target

Add remaining Telegram control commands for the opportunity loop:

- `/hunt pause`
- `/hunt resume`
- `/hunt summary`

Best place to wire this:

- `price_monitor_bot/src/price_monitor_bot/bot.py` for Telegram command parsing.
- `aka_no_claw/src/openclaw_adapter/telegram_bot.py` for OpenClaw runtime wiring.
