# Docs Index

Last reviewed: 2026-06-20

## Start Here

| Document | Status | Owner area | Purpose |
|---|---|---|---|
| [AGENT_ONBOARDING.md](AGENT_ONBOARDING.md) | Current | agent-maintenance | Fast first-read guide for future agents. |
| [SYSTEM_MAP.md](SYSTEM_MAP.md) | Current | architecture | Repo map, runtime flows, data ownership, and high-risk boundaries. |
| [CURRENT_STATE.md](CURRENT_STATE.md) | Current | agent-maintenance | Truth table for shipped/beta/partial/planned subsystem status. |
| [TASK_ROUTING.md](TASK_ROUTING.md) | Current | agent-maintenance | Where to edit for common tasks. |
| [VERIFICATION_MATRIX.md](VERIFICATION_MATRIX.md) | Current | verification | What to run for each type of change. |

## Design / Methodology

| Document | Status | Owner area | Notes |
|---|---|---|---|
| [LIQUIDITY_METHODOLOGY.md](LIQUIDITY_METHODOLOGY.md) | Current | price/liquidity | Scoring approach for liquidity and market support. |
| [RESEARCH_COMMAND_PLAN.md](RESEARCH_COMMAND_PLAN.md) | Current | research | `/research` implementation status, offload, and stage parallelization. |
| [NEW_DYNAMIC_TOOLS_PROGRESS.md](NEW_DYNAMIC_TOOLS_PROGRESS.md) | Current | dynamic-tools | `/new` implementation notes and benchmark history. |
| [NEW_E2E_DISCRIMINATING_TESTS.md](NEW_E2E_DISCRIMINATING_TESTS.md) | Current | dynamic-tools | Discriminating tests for generated tools. |
| [KB_EMBEDDING_PLAN.md](KB_EMBEDDING_PLAN.md) | Needs review | knowledge | Embedding/RAG plan and status may need fresh verification. |
| [OPENCLAW_TCG_MONITOR_PLAN.md](OPENCLAW_TCG_MONITOR_PLAN.md) | Historical / needs review | price/tcg | Original monitor plan; verify against current code before relying on it. |
| [COMMISSION_SYSTEM_PLAN.md](COMMISSION_SYSTEM_PLAN.md) | Planned / needs review | commissions | Plan document, not shipped truth. |
| [OPPORTUNITY_AGENT_SPEC.md](OPPORTUNITY_AGENT_SPEC.md) | Current / needs review | opportunity | Spec for recommendation pipeline; verify against code for exact thresholds. |
| [OPPORTUNITY_AGENT_HANDOFF.md](OPPORTUNITY_AGENT_HANDOFF.md) | Current / needs review | opportunity | Operational handoff notes. |

## SNS / Reputation / Operations

| Document | Status | Owner area | Notes |
|---|---|---|---|
| [SNS_MONITOR_USAGE.md](SNS_MONITOR_USAGE.md) | Current | sns | User-facing SNS monitor commands and behavior. |
| [SNS_MONITOR_TROUBLESHOOTING.md](SNS_MONITOR_TROUBLESHOOTING.md) | Current | sns | Service troubleshooting and logs. |
| [SNS_INTEGRATION_TEST_REPORT.md](SNS_INTEGRATION_TEST_REPORT.md) | Historical | sns | Test report snapshot. |
| [TELEGRAM_TOOL_SPEC.md](TELEGRAM_TOOL_SPEC.md) | Current / needs review | telegram | Telegram command/tool spec. |
| [LOGGING.md](LOGGING.md) | Current | operations | Logging conventions. |
| [AUTO_LOGIN_SETUP.md](AUTO_LOGIN_SETUP.md) | Needs review | operations | Local login/setup notes. |
| [MAC_MINI_M4.md](MAC_MINI_M4.md) | Current / local ops | operations | Machine-specific operational notes; avoid copying paths into code. |
| [RASPBERRY_PI_5.md](RASPBERRY_PI_5.md) | Needs review | operations | Pi deployment notes. |

## Quiz / Learning

| Document | Status | Owner area | Notes |
|---|---|---|---|
| [QUIZ_AUTHORING_PROGRESS.md](QUIZ_AUTHORING_PROGRESS.md) | Current / partial | quiz | Quiz authoring state. |
| [QUIZ_VOCAB_AUTHORING_PROGRESS.md](QUIZ_VOCAB_AUTHORING_PROGRESS.md) | Current / partial | quiz | Vocab authoring state. |
| [QUIZ_TEACHING_LOOP.md](QUIZ_TEACHING_LOOP.md) | Current / partial | quiz | Teaching loop notes. |
| [QUIZ_REVIEWS.md](QUIZ_REVIEWS.md) | Current / partial | quiz | Review notes. |
| [QUIZ_FAVORITE_SONGS.md](QUIZ_FAVORITE_SONGS.md) | Current / partial | quiz | Favorite song source notes. |

## Investigation / Historical Notes

| Document | Status | Owner area | Notes |
|---|---|---|---|
| [TEST_RECORD_2026-04-16.md](TEST_RECORD_2026-04-16.md) | Historical | testing | Test record snapshot. |
| [TEST_RECORD_2026-04-17.md](TEST_RECORD_2026-04-17.md) | Historical | testing | Test record snapshot. |
| [comp_filter_bm25_discussion.md](comp_filter_bm25_discussion.md) | Historical / needs review | price/research | Discussion note. |
| [search_ddg_block_investigation.md](search_ddg_block_investigation.md) | Historical / needs review | search | Investigation note. |

## Documentation Convention

Stateful docs should include:

```text
Last reviewed: YYYY-MM-DD
Status: Current / Needs review / Historical / Planned
Owner area: price / sns / reputation / telegram / dashboard / opportunity / agent-maintenance
```

## Maintenance Rule

When adding a new doc under `docs/`, add it here with status, owner area, and purpose. Mark old docs as `Historical` instead of deleting useful context.
