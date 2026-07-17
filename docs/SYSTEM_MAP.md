# System Map

Status: Current
Owner area: architecture

Last reviewed: 2026-07-17

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

The OpenClaw adapter also owns the Web command bridge's transport-compatible
event runtime. `session_event_journal.py` is the append-only authority,
`session_projection.py` rebuilds the Web view, and `command_bridge.py` records
blocking, streaming, and background-job lifecycles through `RunRecorder`.

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

### Web Command Bridge

```text
Web blocking / NDJSON / async request
  -> openclaw_adapter.command_bridge_server compatibility endpoint
  -> openclaw_adapter.command_bridge + RunRecorder
  -> versioned events appended to the per-session JSONL journal
  -> deterministic session projection and legacy response/poll/session views
  -> GET /api/command/events exact cursor replay after reconnect
```

When the opt-in prompt queue is enabled, busy Web input follows a separate,
single-session durable path: `POST /api/command/queue` records an atomic JSON
snapshot and appends `queue.changed`; one drain owner claims FIFO position only
after a terminal run. A claim carries its prompt ID into `run.accepted`, and
goal-loop interjections are consumed only at declared safe boundaries.

The journal is authoritative; mutable session and job snapshots are compatibility
views. Negotiated NDJSON live delivery starts at the atomic `latest_cursor`,
while historical recovery advances page-by-page with `server_cursor`. Background
completion and cancellation each use a single terminal compare-and-set so a run
cannot resurrect or emit duplicate final messages.

Generated-tool steps launched by Web workflows pass through a second boundary:

```text
resolved generated tool + arguments
  -> deterministic safety validation
  -> frozen action manifest + effect classification
  -> auto-allow harmless/read-only work, or persist approval.requested and pause
  -> POST /api/command/approval with a one-shot decision token
  -> reload and re-hash the artifact/dependencies/arguments
  -> execute once, or fail closed on reject/expiry/restart/hash mismatch
```

Approval does not override the existing generated-tool validators. The Web card
shows bounded effects and scopes, destructive actions require a second click,
and the event journal carries durable request/resolution state for reconnect.

Primary paths:

- `src/openclaw_adapter/session_events.py`
- `src/openclaw_adapter/session_event_journal.py`
- `src/openclaw_adapter/session_event_service.py`
- `src/openclaw_adapter/session_projection.py`
- `src/openclaw_adapter/run_recorder.py`
- `src/openclaw_adapter/approval_models.py`
- `src/openclaw_adapter/approval_store.py`
- `src/openclaw_adapter/approval_service.py`
- `src/openclaw_adapter/command_bridge.py`
- `src/openclaw_adapter/command_bridge_server.py`

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
| Web session event journals | `.openclaw_tmp/web_sessions/<session_id>/` | Command bridge authority: bounded JSONL events plus sequence metadata; rebuildable projections and compatibility snapshots must not overwrite it. |
| Web prompt queues | `.openclaw_tmp/web_prompt_queue/<session_id>.json` | Opt-in #86 bounded per-session request snapshots. Atomic claim/version mutations; event journal receives public `queue.changed` snapshots. |

## High-Risk Boundaries

- Telegram should not write directly to `sns.sqlite3`; use the inbox queue.
- `opportunity_agent` should own writes to `opportunities.sqlite3`; Telegram writes requests through `opportunity_inbox`.
- Runtime DB and log paths must be repo-root resolved, not current-working-directory guesses.
- TCG-specific matching belongs in sibling package `tcg_tracker`, not `market_monitor`.
- Generic source and price aggregation behavior belongs in sibling package `market_monitor`, not Telegram handlers.
- Web clients must recover durable history with `/api/command/events`; they must not treat mutable poll/session snapshots or an NDJSON page cursor as the authoritative journal head.
