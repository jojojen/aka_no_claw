# Current State

Status: Current
Owner area: agent-maintenance

Last reviewed: 2026-07-03

## Status Legend

| Status | Meaning |
|---|---|
| `shipped` | Implemented and expected to work in normal local operation. |
| `beta` | Implemented but still being hardened or actively tuned. |
| `partial` | Exists but has meaningful gaps, limits, or manual steps. |
| `planned` | Intended but not implemented. |
| `deprecated` | Should not be extended. |
| `unclear` | Needs fresh verification before relying on it. |

## Subsystems

| Subsystem | Status | Entry points | Data stores | Notes |
|---|---|---|---|---|
| Telegram bot | `beta` | `python -m openclaw_adapter telegram-poll --notify-startup`, `launchers/start-telegram-bot.*` | Telegram-facing runtime settings, inbox DBs | Main user interface. Requires token/chat ID in `.env`. Three-package split: `telegram_core` (zero-dep transport/poll-loop/dispatch, shared) → `price_monitor_bot.bot.TelegramCommandProcessor` (price-domain hooks/registries) → `openclaw_adapter.telegram_bot.TelegramCommandProcessor` (aka-domain hooks/registries, the class actually run in production). See [TELEGRAM_CORE_EXTRACTION_PLAN.md](TELEGRAM_CORE_EXTRACTION_PLAN.md). |
| CLI tool registry | `shipped` | `python -m openclaw_adapter list-tools` | n/a | Central registry for assistant tools. |
| Price lookup | `beta` | `/price`, `python -m openclaw_adapter lookup-card` | Source caches / runtime DBs | Uses TCG and market layers plus historical price monitor behavior. |
| Liquidity board | `beta` | `/liquidity`, dashboard | Market/source data | Methodology is documented in `LIQUIDITY_METHODOLOGY.md`. |
| `/research` | `beta` | Telegram `/research`, `/resaerch` alias | `knowledge.sqlite3`, Mercari and market evidence | Implemented with Mercari item research, seller risk, price, liquidity, and optional Big Pickle appreciation offload. |
| Dynamic tools `/new` | `beta` | Telegram `/new`, `openclaw_adapter.dynamic_tools` selftest | `generated_tools/`, `knowledge.sqlite3` | Local Ollama by default; OpenCode Big Pickle codegen can be enabled. |
| SNS monitor | `beta` | `/snsadd`, `/snslist`, `python -m openclaw_adapter sns-monitor-service` | `sns.sqlite3`, `sns_inbox.sqlite3` | Background service owns SNS DB writes; Telegram writes via inbox. |
| `/snsbuzz` | `beta` | Telegram `/snsbuzz <keyword>` | IP heat and local catalog data | Current source is 4chan plus LLM/IP catalog context, not X trending. |
| Reputation snapshot | `beta` | `/snapshot`, `/proof`, `/repcheck`, `python -m openclaw_adapter reputation-agent` | reputation_snapshot server DB/proofs | Requires sibling `reputation_snapshot` server and admin token. |
| Opportunity agent | `partial` | `/hunt`, `/opportunity`, `python -m openclaw_adapter opportunity-agent` | `opportunities.sqlite3`, `opportunity_inbox.sqlite3` | Combines SNS, price, liquidity, and reputation; keep rejection reasons visible. |
| Dashboard | `beta` | `python -m openclaw_adapter serve-dashboard --open-browser` | Read-only current state from local stores | Local UI for boards and status. |
| Knowledge / RAG | `beta` | `/know`, daily digest, research writes | `knowledge.sqlite3`, `knowledge_inbox.sqlite3` | Must store grounded facts, not formulas or transient product pages as “knowledge” without meaningful normalization. |
| Quiz / teaching loop | `partial` | `/quiz`, docs under quiz files | `quiz.sqlite3` | Active but not the core OpenClaw market pipeline. |
| Backup / launchd services | `beta` | `/restartall`, `POST /api/command/restartall`, launchd labels, backup commands | runtime DBs and configured backup target | Running stack restarts go through `/restartall`. `launchers/start-mac-mini-stack.command` is only first setup / cold start. |

## Known Drift / Mismatch

- Some README sections are operational and long; use [DOCS_INDEX.md](DOCS_INDEX.md) for the current documentation map.
- `price_monitor_bot` still carries historical Telegram price-bot behavior (lookup/watch/photo/reputation-snapshot commands); the shared transport/poll-loop/dispatch machinery has been extracted to `telegram_core` (see [TELEGRAM_CORE_EXTRACTION_PLAN.md](TELEGRAM_CORE_EXTRACTION_PLAN.md)) — the old `aka_no_claw` → `price_monitor_bot` monkey-patch is gone.
- `sns_monitor_bot` owns SNS monitor internals, while `aka_no_claw` owns Telegram and service wiring.
- `reputation_snapshot` remains independently runnable; OpenClaw integration should not assume it is only an embedded component.
- Some older plan docs are historical. Prefer files marked `Current` in [DOCS_INDEX.md](DOCS_INDEX.md).

## Sibling Repo README Alignment Checklist

These are tracked here for visibility; actual sibling repo edits should be separate changes unless the task explicitly spans repos.

| Repo | Needed follow-up |
|---|---|
| `price_monitor_bot` | README should describe its current role, install/test commands, and which behavior is integrated by OpenClaw. |
| `sns_monitor_bot` | README should state Python version expectations, DB/watch-rule boundaries, and how `aka_no_claw` integrates through adapters and launchd services. |
| `reputation_snapshot` | README should document `/snapshot`, reputation-agent usage, job claim/result flow, Playwright, SQLite, and proof verification. |

## Update Rule

Update this file after any change that affects shipped status, entry points, data ownership, or cross-repo responsibilities.
