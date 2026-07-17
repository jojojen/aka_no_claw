# Task Routing

Status: Current
Owner area: agent-maintenance

Last reviewed: 2026-07-17

## Quick Routing Table

| User request / task | Primary repo | Primary layer/module | Notes |
|---|---|---|---|
| Add or change Telegram command | `aka_no_claw` | `src/openclaw_adapter/telegram_bot.py`, `toolset.py` | Keep command wiring thin. Move business logic to dedicated modules. |
| Change Telegram infra (transport, poll loop, list-view pagination, generic dispatch/registries) | `telegram_core` | `src/telegram_core/{transport,polling,processor,list_view,contracts}.py` | Zero-dependency shared package; both `aka_no_claw` and `price_monitor_bot` consume it. Never add domain vocabulary (command names, callback prefixes) here — that belongs in a consumer repo's hook overrides/registries. Run its own `pytest` plus both consumer suites after any change. |
| Change CLI command | `aka_no_claw` | `src/openclaw_adapter/toolset.py`, `__main__.py` | Update docs and verification matrix if entry points change. |
| Change Web command/session/run recovery | `aka_no_claw` | `command_bridge.py`, `command_bridge_server.py`, `session_event_*.py`, `session_projection.py`, `run_recorder.py` | Preserve blocking/NDJSON/async/poll/session compatibility. The append-only journal and exact cursor contract are authoritative; test with more than one retained page. |
| Change card matching or card aliases | `price_monitor_bot` | `src/tcg_tracker` | `aka_no_claw` imports this package through `requirements.txt`; do not put TCG rules in `market_monitor`. |
| Change generic price source logic | `price_monitor_bot` | `src/market_monitor` | `aka_no_claw` imports this package through `requirements.txt`; keep domain-specific rules out. |
| Change price command formatting | `aka_no_claw` / `price_monitor_bot` | `openclaw_adapter`, historical bot formatter paths | Inspect integration boundary first. |
| Change liquidity scoring | `aka_no_claw` | `cross_signal_aggregator.py`, `LIQUIDITY_METHODOLOGY.md` | Update methodology docs with behavior changes. |
| Change `/research` | `aka_no_claw` | `research_command.py`, `telegram_bot.py` | Respect Yahoo budget, progress notifications, and stage ordering. |
| Change `/new` dynamic tools | `aka_no_claw` | `dynamic_tools.py`, codegen knowledge | Run selftest or targeted dynamic tool tests. |
| Change X account/keyword watching | `sns_monitor_bot` + `aka_no_claw` | `sns_monitor`, `openclaw_adapter.sns_tools` | Check producer/consumer DB boundary. |
| Change `/snsbuzz` | `sns_monitor_bot` + `aka_no_claw` | `sns_monitor.fourchan_buzz`, `digest`, `sns_tools.py` | Current source is 4chan, not X trending. |
| Change Mercari proof capture | `reputation_snapshot` | `services/*`, parser/proof modules | Watch parser drift and proof schema compatibility. |
| Change `/snapshot` behavior | `aka_no_claw` + `reputation_snapshot` | Telegram/reputation adapter + API | Verify both request path and proof artifact delivery. |
| Change opportunity recommendations | `aka_no_claw` | `opportunity_agent.py`, `opportunity_pipeline.py`, `opportunity_scoring.py` | Preserve rejection reasons and feedback hooks. |
| Update dashboard display | `aka_no_claw` | `dashboard.py`, `src/openclaw_adapter/dashboard_assets/` | Avoid silently changing data semantics in UI work. |
| Update launchd/service behavior | `aka_no_claw` | settings, service modules, launchers | Verify cwd-independent paths and logs. |
| Update docs only | `aka_no_claw` | `docs/`, `README.md` | No runtime tests required unless docs describe changed behavior. |

## Decision Tree

1. Is it assistant runtime, CLI, Telegram, dashboard, or launchd wiring?
   Go to `aka_no_claw/src/openclaw_adapter` or `src/assistant_runtime`.
2. Is it generic market monitoring, source catalog, or price aggregation?
   Go to `price_monitor_bot/src/market_monitor`.
3. Is it card-specific parsing, aliases, matching, rarity, or TCG source behavior?
   Go to `price_monitor_bot/src/tcg_tracker`.
4. Is it SNS polling, rule storage, tweet dedupe, classifier, feedback, or reminders?
   Go to sibling repo `sns_monitor_bot`, then check `aka_no_claw` adapter wiring.
5. Is it Mercari proof capture, verification, signed payloads, or capture UI/API?
   Go to sibling repo `reputation_snapshot`, then check `aka_no_claw` `/snapshot` integration.
6. Is it old Telegram price-bot behavior still living in `price_monitor_bot`?
   Inspect `price_monitor_bot` first, then decide whether the fix belongs there or in `aka_no_claw` integration.
7. Is it Telegram transport, the poll loop, list-view pagination, or the
   generic command/callback dispatch mechanism itself (not a specific
   command)? Go to sibling repo `telegram_core` — both `aka_no_claw` and
   `price_monitor_bot` depend on it, so a fix there benefits both.
8. Is it only documentation or agent maintenance?
   Keep it in `aka_no_claw/docs` unless it documents a sibling repo behavior that must be edited at the source.

## Cross-Repo Rules

- Make cross-repo changes explicit in the final summary.
- Do not assume a sibling repo branch or default branch matches `aka_no_claw`.
- Run tests in every repo whose runtime behavior you change.
- Keep docs in `aka_no_claw` factual about sibling repos; if uncertain, mark the status as `unclear` or `needs review`.

## Avoid Dumping Logic Into Toolset

`toolset.py` should register commands and connect handlers. It should not become a home for parsing, scoring, scraping, or long-running workflow logic.
