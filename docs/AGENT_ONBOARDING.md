# Agent Onboarding

## First Principle — General Correctness

1. **Correctness first means general correctness, not correctness for one case.**
   Never make the current example pass with hardcoded keywords, values, output
   text, or exception branches. Prefer a structural solution that removes
   special cases; a sound fix should normally reduce total code and branch
   count. If a proposed fix adds case-specific code, stop and redesign it.

2. **Research uncertainty before coding.** When the correct general solution
   is unclear, consult current primary sources and proven implementations before
   changing code. Use that evidence to define a general contract or design;
   never replace uncertainty with a case-specific hardcode.

Status: Current
Owner area: agent-maintenance

Last reviewed: 2026-06-25

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
- Runtime entry points are intentionally limited: use
  `launchers/start-mac-mini-stack.command` only for first setup / cold start,
  and use `/restartall` or `POST /api/command/restartall` for live restart.

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

## Restarting 龍蝦

Supported live restart paths:

```text
Telegram: /restartall
Web console: restart button
HTTP bridge: POST http://127.0.0.1:8781/api/command/restartall
```

Do not introduce another manual restart shell. If the running system needs to
reload code or configuration, go through `/restartall` so Telegram, command
bridge, web frontend, and background services stay in one runtime identity.

## When Unsure

Prefer marking a status as `unclear` or `needs review` in docs instead of guessing. If behavior matters to a user-facing command, verify it through tests or a controlled smoke run.
