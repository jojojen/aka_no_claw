# System Map

Status: Current
Owner area: architecture

Last reviewed: 2026-07-03

## Repo Map

| Repo | Role | Current relationship |
|---|---|---|
| `aka_no_claw` | Primary OpenClaw orchestrator. Owns assistant runtime, CLI, Telegram adapter, dashboard, opportunity pipeline, service wiring, dynamic tools, and docs. | Primary repo. |
| `telegram_core` | Zero-dependency shared package: Telegram transport (`TelegramBotClient`), generic dispatch contracts, list-view rendering, and the domain-free poll loop (`CoreCommandProcessor`, `polling.py`). | Depended on by BOTH `aka_no_claw` and `price_monitor_bot` — the correct dependency direction (see [TELEGRAM_CORE_EXTRACTION_PLAN.md](TELEGRAM_CORE_EXTRACTION_PLAN.md), now Current/complete). Neither consumer repo is imported by it. |
| `price_monitor_bot` | Historical TCG price monitor and reusable Telegram-domain package. | Still provides price-monitoring behavior (lookup/watch/photo/reputation-snapshot commands) used by OpenClaw flows; its `TelegramCommandProcessor` subclasses `telegram_core.processor.CoreCommandProcessor`. |
| `sns_monitor_bot` | SNS/X watch storage, polling, signal extraction, feedback-aware monitoring, reminders, and 4chan buzz digest logic. | Integrated through `openclaw_adapter.sns_tools` and `sns_monitor_service`. |
| `reputation_snapshot` | Mercari profile/item reputation capture, signed proof generation, and proof verification UI/API. | Integrated through `openclaw_adapter.reputation_agent` and `/snapshot`. |

## Local Package Layers

| Layer | Path | Responsibility |
|---|---|---|
| Runtime | `src/assistant_runtime` | Settings, logging, TLS, registry, and generic assistant runtime foundations. |
| Telegram core | `telegram_core` (sibling package, zero deps) | Transport, list-view rendering, dispatch contracts, and the generic poll loop / `CoreCommandProcessor`. Both `aka_no_claw` and `price_monitor_bot` depend on it; it depends on neither. Telegram-domain command/callback vocabulary (`/price`, `cond:`, `wprc:`, SNS bulk, photo pipeline, ...) does NOT live here — only in the two consumer repos' hook overrides and registries. |
| Generic market core | `price_monitor_bot/src/market_monitor` | Generic monitoring and source-management logic imported through the sibling package. Keep card-specific heuristics out. |
| TCG domain | `price_monitor_bot/src/tcg_tracker` | Card aliases, matching, TCG source adapters, and TCG-specific lookup behavior imported through the sibling package. |
| OpenClaw adapter | `src/openclaw_adapter` | CLI, Telegram, dashboard, dynamic tools, `/research`, SNS integration, opportunity agent, and sibling repo adapters. |

## Main Runtime Flow

```text
SNS / market / user command
  -> openclaw_adapter entrypoint
  -> source-specific adapter or sibling repo
  -> price / liquidity / reputation / research / opportunity logic
  -> Telegram, dashboard, CLI, or local database output
```

## Command And Service Flows

### `/price`

```text
Telegram command
  -> price_monitor_bot command dispatcher
  -> aka_no_claw openclaw_adapter injection
  -> tcg_tracker / market_monitor / price_monitor_bot helpers
  -> formatted Telegram reply
```

Primary paths:

- `src/openclaw_adapter/telegram_bot.py`
- `src/openclaw_adapter/toolset.py`
- sibling repo `price_monitor_bot/src/tcg_tracker`
- sibling repo `price_monitor_bot/src/market_monitor`

### `/snapshot`

```text
Telegram /snapshot
  -> openclaw_adapter.telegram_bot
  -> openclaw_adapter.reputation_snapshot client
  -> reputation_snapshot API
  -> reputation agent capture job if needed
  -> proof document, PDF, PNG preview, Telegram delivery
```

Primary paths:

- `src/openclaw_adapter/telegram_bot.py`
- `src/openclaw_adapter/reputation_snapshot.py`
- `src/openclaw_adapter/reputation_agent.py`
- sibling repo `reputation_snapshot`

### SNS Watch Rules

```text
Telegram /snsadd, /snsdelete, callbacks
  -> openclaw_adapter.telegram_bot
  -> openclaw_adapter.sns_tools
  -> sns_inbox.sqlite3 producer queue
  -> local.openclaw.sns_monitor / sns-monitor-service
  -> sns_monitor_bot storage and monitor
  -> sns.sqlite3
```

Primary paths:

- `src/openclaw_adapter/telegram_bot.py`
- `src/openclaw_adapter/sns_tools.py`
- `src/openclaw_adapter/sns_monitor_service.py`
- sibling repo `sns_monitor_bot/src/sns_monitor`

### `/snsbuzz`

```text
Telegram /snsbuzz <keyword>
  -> openclaw_adapter.telegram_bot
  -> openclaw_adapter.sns_tools
  -> sns_monitor_bot 4chan buzz client and digest
  -> optional local LLM summary
  -> Telegram reply
```

Current source note: `/snsbuzz` is 4chan-backed; it is not an X trending-topic scraper.

### Opportunity Agent

```text
SNS / watch / manual target inputs
  -> opportunity inbox and candidate providers
  -> price, liquidity, SNS heat, seller reputation checks
  -> opportunity scoring and rejection reasons
  -> Telegram notification and opportunity database
```

Primary paths:

- `src/openclaw_adapter/opportunity_agent.py`
- `src/openclaw_adapter/opportunity_pipeline.py`
- `src/openclaw_adapter/opportunity_store.py`
- `src/openclaw_adapter/opportunity_inbox.py`

### Dashboard

```text
python -m openclaw_adapter serve-dashboard
  -> openclaw_adapter.dashboard
  -> current DB-backed board loaders
  -> local browser UI
```

Primary paths:

- `src/openclaw_adapter/dashboard.py`
- `src/openclaw_adapter/dashboard_assets/`

### `/research`

```text
Telegram /research <Mercari URL or product name>
  -> openclaw_adapter.telegram_bot
  -> openclaw_adapter.research_command
  -> Mercari item fetch, entity recognition, appreciation, price, liquidity, seller risk
  -> Telegram progress updates and final report
```

Current status: implemented with OpenCode Big Pickle appreciation offload when enabled and parallel stage 3/4/6 execution.

Primary docs:

- [docs/RESEARCH_COMMAND_PLAN.md](RESEARCH_COMMAND_PLAN.md)
- [docs/LIQUIDITY_METHODOLOGY.md](LIQUIDITY_METHODOLOGY.md)

## Data Stores

Runtime paths are resolved through `assistant_runtime.settings`; keep docs generic and avoid committing machine-specific absolute paths.

| Store | Typical default | Owner / notes |
|---|---|---|
| Monitor DB | `data/monitor.sqlite3` | Generic assistant monitor state. |
| SNS DB | `data/sns.sqlite3` | Owned by SNS monitor service; Telegram opens read-only. |
| SNS inbox | `data/sns_inbox.sqlite3` | Telegram writes requests; SNS monitor service consumes. |
| Knowledge DB | `data/knowledge.sqlite3` | RAG facts, aliases, codegen knowledge, research facts. |
| Knowledge inbox | `data/knowledge_inbox.sqlite3` | Telegram/request producer queue for background knowledge writes. |
| Opportunity DB | `data/opportunities.sqlite3` | Opportunity agent store. |
| Opportunity inbox | `data/opportunity_inbox.sqlite3` | Telegram writes requests; opportunity agent consumes. |
| Quiz DB | `data/quiz.sqlite3` | Quiz state and review material. |

## High-Risk Boundaries

- Telegram should not write directly to `sns.sqlite3`; use the inbox queue.
- `opportunity_agent` should own writes to `opportunities.sqlite3`; Telegram writes requests through `opportunity_inbox`.
- Runtime DB and log paths must be repo-root resolved, not current-working-directory guesses.
- TCG-specific matching belongs in sibling package `tcg_tracker`, not `market_monitor`.
- Generic source and price aggregation behavior belongs in sibling package `market_monitor`, not Telegram handlers.
