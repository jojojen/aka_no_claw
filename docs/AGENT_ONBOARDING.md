# Agent Onboarding

Status: Current
Owner area: agent-maintenance

Last reviewed: 2026-06-20

## Read This First

`aka_no_claw` is the primary OpenClaw orchestrator. Do not treat it as only a card price monitor.

OpenClaw watches SNS and market signals, evaluates price and liquidity, checks Mercari seller reputation, and sends qualified results through Telegram and local services.

## Required Reading Order

1. [Constitution.md](../Constitution.md)
2. [README.md](../README.md)
3. [SYSTEM_MANIFEST.yaml](../SYSTEM_MANIFEST.yaml)
4. [docs/SYSTEM_MAP.md](SYSTEM_MAP.md)
5. [docs/CURRENT_STATE.md](CURRENT_STATE.md)
6. [docs/TASK_ROUTING.md](TASK_ROUTING.md)
7. [docs/VERIFICATION_MATRIX.md](VERIFICATION_MATRIX.md)

## Mental Model

This repo owns OpenClaw's runtime wiring: CLI commands, Telegram integration, dashboard, service orchestration, dynamic tools, research, opportunity flows, and adapters into sibling repos.

Sibling repos provide major reusable capabilities:

| Repo | Role |
|---|---|
| `price_monitor_bot` | Historical TCG price-monitoring and Telegram-domain package. |
| `sns_monitor_bot` | SNS/X watch rules, signal collection, classifier, feedback, reminders, and 4chan buzz analysis. |
| `reputation_snapshot` | Mercari seller reputation capture, signed proof generation, and verification UI/API. |

## Golden Rules

- Do not hardcode secrets, chat IDs, tokens, cookies, or local-only runtime paths.
- Do not collapse generic monitoring logic into `openclaw_adapter`.
- Do not put TCG-specific matching rules in generic `market_monitor` code.
- Do not change DB schemas without checking all producers and consumers.
- Do not document planned behavior as shipped behavior.
- Do not treat sibling repo boundaries as incidental; they are part of the architecture.
- Do not push runtime databases, generated tools, logs, caches, cookies, or `.env`.

## Before Editing

1. Identify the subsystem.
2. Identify the owning repo.
3. Identify the layer: runtime, adapter, generic core, domain module, or service.
4. Read the relevant docs from [docs/DOCS_INDEX.md](DOCS_INDEX.md).
5. Choose the smallest verification path from [docs/VERIFICATION_MATRIX.md](VERIFICATION_MATRIX.md).

## Common Entry Points

```bash
python -m openclaw_adapter list-tools
python -m openclaw_adapter telegram-poll --notify-startup
python -m openclaw_adapter serve-dashboard --open-browser
python -m openclaw_adapter sns-monitor-service
python -m openclaw_adapter opportunity-agent
python -m openclaw_adapter reputation-agent
```

## When Unsure

Prefer marking a status as `unclear` or `needs review` in docs instead of guessing. If behavior matters to a user-facing command, verify it through tests or a controlled smoke run.
