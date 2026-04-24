---
name: aka-no-claw-workflow
description: Work on the aka_no_claw assistant workspace. Use when modifying OpenClaw Telegram or dashboard behavior, assistant_runtime wiring, reputation snapshot integration, local configuration flow, or repo-specific docs and tests. Read Constitution.md first, keep assistant entrypoints thin, and preserve the boundary between assistant-specific adapters and reusable monitoring logic that belongs in sibling projects.
---

# Aka No Claw Workflow

## Overview

Use this skill before changing OpenClaw assistant behavior in this repository.
Treat this repo as the assistant-facing integration layer, not the place to hide reusable price-monitor or capture logic.

## Required Reading

Read these files before substantial edits:

- `Constitution.md`
- `README.md`
- `OPENCLAW_TCG_MONITOR_PLAN.md` when the task touches monitoring, Telegram flows, or architecture decisions
- `LOGGING.md` when changing logging or observability behavior
- `LIQUIDITY_METHODOLOGY.md` when changing hot-card or liquidity-facing copy and explanations

## Layer Boundaries

Choose the target layer before editing:

- `src/assistant_runtime`
  - Generic assistant runtime concerns such as registry, settings, TLS, or logging helpers
- `src/openclaw_adapter`
  - OpenClaw-specific CLI, Telegram, dashboard, formatter, and natural-language wiring
- Sibling repo `price_monitor_bot`
  - Reusable pricing, source parsing, image lookup, and TCG domain logic that should not be reimplemented here
- Sibling repo `reputation_snapshot`
  - Mercari capture, proof generation, verification, and parser behavior that should remain in the dedicated service

If a change starts to create reusable monitoring logic inside this repo, stop and move that work to the correct sibling project.

## Common Task Map

Use these file groupings as the default starting point:

- Telegram command or response behavior
  - Read `src/openclaw_adapter/commands.py`, `telegram_bot.py`, `formatters.py`
  - Verify with `tests/test_commands.py` and `tests/test_telegram_bot.py`
- Dashboard behavior or UI wiring
  - Read `src/openclaw_adapter/dashboard.py` and `src/openclaw_adapter/dashboard_assets/`
  - Verify with `tests/test_dashboard.py`
- Reputation snapshot bridge or proof-check flow
  - Read `src/openclaw_adapter/reputation_snapshot.py` and `reputation_agent.py`
  - Verify with `tests/test_reputation_snapshot.py` and `tests/test_reputation_agent.py`
- Tool registration or runtime settings
  - Read `src/assistant_runtime/registry.py` and `settings.py`
  - Verify with `tests/test_tool_registry.py` and `tests/test_settings.py`

## Workflow

1. State assumptions, especially which layer owns the change.
2. Read `Constitution.md` and the relevant nearby module before editing.
3. Keep entrypoints thin and push reusable logic down only when it still belongs in this repo.
4. Add or update tests for behavior changes and bug fixes.
5. Run targeted verification before finishing.
6. Call out any cross-repo follow-up explicitly instead of quietly duplicating logic.

## Verification

Prefer targeted checks first:

- `.\.venv\Scripts\python -m pytest tests/test_commands.py`
- `.\.venv\Scripts\python -m pytest tests/test_telegram_bot.py`
- `.\.venv\Scripts\python -m pytest tests/test_dashboard.py`
- `.\.venv\Scripts\python -m pytest tests/test_reputation_snapshot.py tests/test_reputation_agent.py`
- `.\.venv\Scripts\python -m pytest tests/test_tool_registry.py tests/test_settings.py`

Use runtime smoke checks when they directly match the task:

- `.\start-dashboard.bat`
- `.\start-telegram-bot.bat --notify-startup`
- `.\.venv\Scripts\python -m openclaw_adapter list-tools`

## Guardrails

- Do not hardcode secrets, chat IDs, tokens, or machine-local paths.
- Do not move reusable monitoring logic into assistant-specific modules just because it is faster.
- Do not silently change source weighting, liquidity logic, or proof semantics without updating tests and docs.
- Prefer the smallest coherent change that keeps repo boundaries clear.
