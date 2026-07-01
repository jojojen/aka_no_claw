# Docs Index

Last reviewed: 2026-06-30

Governance for this index lives in [DOCUMENTATION_GOVERNANCE.md](DOCUMENTATION_GOVERNANCE.md).
Full inventory with recommended actions is in [DOC_AUDIT.md](DOC_AUDIT.md).
"Canonical" marks the single owner doc for a domain; companions cross-link to it.

## Start Here (authoritative truth sources)

| Document | Status | Owner area | Purpose |
|---|---|---|---|
| [SYSTEM_MANIFEST.yaml](../SYSTEM_MANIFEST.yaml) | Current | agent-maintenance | Machine-readable compact system truth index. |
| [AGENT_ONBOARDING.md](AGENT_ONBOARDING.md) | Current | agent-maintenance | Fast first-read guide for future agents. |
| [SYSTEM_MAP.md](SYSTEM_MAP.md) | Current | architecture | Architecture truth: repo map, runtime flows, data ownership, boundaries. |
| [CURRENT_STATE.md](CURRENT_STATE.md) | Current | agent-maintenance | Runtime/status truth for each subsystem. |
| [TASK_ROUTING.md](TASK_ROUTING.md) | Current | agent-maintenance | Task-ownership truth: where to edit for common tasks. |
| [VERIFICATION_MATRIX.md](VERIFICATION_MATRIX.md) | Current | verification | Verification truth: what to run for each change. |

## Documentation Governance

| Document | Status | Owner area | Purpose |
|---|---|---|---|
| [DOCUMENTATION_GOVERNANCE.md](DOCUMENTATION_GOVERNANCE.md) | Current | agent-maintenance | Truth ownership, lifecycle stages, update rules, where new docs go. |
| [DOC_AUDIT.md](DOC_AUDIT.md) | Current | agent-maintenance | Full doc inventory with status, owner, recommended action. |
| [DOC_DRIFT_CHECKLIST.md](DOC_DRIFT_CHECKLIST.md) | Current | agent-maintenance | Pre-push consistency check across the five linked truth sources. |
| [DOCS_CHANGE_TEMPLATE.md](DOCS_CHANGE_TEMPLATE.md) | Current | agent-maintenance | PR checklist for doc/status/wiring changes; mirrors the automated checks. |

## Design / Methodology

| Document | Status | Owner area | Notes |
|---|---|---|---|
| [LIQUIDITY_METHODOLOGY.md](LIQUIDITY_METHODOLOGY.md) | Current | price/liquidity | Scoring approach for liquidity and market support. |
| [RESEARCH_COMMAND_PLAN.md](RESEARCH_COMMAND_PLAN.md) | Current | research | `/research` implementation status, offload, and stage parallelization. |
| [BROWSER_UI_VALIDATION_PLAYBOOK.md](BROWSER_UI_VALIDATION_PLAYBOOK.md) | Current | verification | Browser smoke-test playbook for local web console validation. |
| [NEW_DYNAMIC_TOOLS_PROGRESS.md](NEW_DYNAMIC_TOOLS_PROGRESS.md) | Current (canonical: dynamic-tools) | dynamic-tools | `/new` implementation notes and benchmark history. |
| [NEW_E2E_DISCRIMINATING_TESTS.md](NEW_E2E_DISCRIMINATING_TESTS.md) | Current | dynamic-tools | Companion: discriminating tests for generated tools. |
| [NEW_OPENCODE_DECOUPLING_PLAN.md](NEW_OPENCODE_DECOUPLING_PLAN.md) | Planned | dynamic-tools | Companion: plan to move `/new` + Chat off OpenCode CLI to direct HTTP; add Mistral switch (issues #51/#59). |
| [TELEGRAM_NL_OWNERSHIP_REFACTOR_ISSUE.md](TELEGRAM_NL_OWNERSHIP_REFACTOR_ISSUE.md) | Planned | telegram | Draft issue for moving aka-specific Telegram NL workflow routing out of `price_monitor_bot`. |
| [KB_EMBEDDING_PLAN.md](KB_EMBEDDING_PLAN.md) | Needs review | knowledge | Embedding/RAG plan and status may need fresh verification. |
| [OPENCLAW_TCG_MONITOR_PLAN.md](OPENCLAW_TCG_MONITOR_PLAN.md) | Needs review | price/tcg | Original monitor plan; verify against current code before relying on it. |
| [OPPORTUNITY_AGENT_SPEC.md](OPPORTUNITY_AGENT_SPEC.md) | Needs review (canonical: opportunity) | opportunity | Spec for recommendation pipeline; verify thresholds against code. |
| [OPPORTUNITY_AGENT_HANDOFF.md](OPPORTUNITY_AGENT_HANDOFF.md) | Needs review | opportunity | Companion: operational handoff notes. |
| [fix_benchmarks/README.md](fix_benchmarks/README.md) | Current | agent-maintenance | Deterministic `/fix` benchmark directory and safety rules for synthetic fixtures. |
| [fix_benchmarks/price_reference_sources/README.md](fix_benchmarks/price_reference_sources/README.md) | Current | agent-maintenance | Multi-source synthetic price reference parser repair benchmark. |
| [fix_benchmarks/price_reference_sources/FAILURE_TRACE.md](fix_benchmarks/price_reference_sources/FAILURE_TRACE.md) | Current | agent-maintenance | Reproducible broken-parser attempts and verifier outcomes for the benchmark. |
| [fix_benchmarks/image_translation_policy/README.md](fix_benchmarks/image_translation_policy/README.md) | Current | agent-maintenance | Public-media benchmark for adaptive OCR/image translation policies. |
| [fix_benchmarks/seller_snapshot_sources/README.md](fix_benchmarks/seller_snapshot_sources/README.md) | Current | agent-maintenance | Synthetic seller snapshot parser and cooldown lifecycle repair benchmark. |
| [fix_benchmarks/seller_snapshot_sources/FAILURE_TRACE.md](fix_benchmarks/seller_snapshot_sources/FAILURE_TRACE.md) | Current | agent-maintenance | Reproducible seller snapshot parser and lifecycle failure history. |
| [fix_benchmarks/seller_snapshot_sources/lifecycle/README.md](fix_benchmarks/seller_snapshot_sources/lifecycle/README.md) | Current | agent-maintenance | Rate-limit and bot-interstitial lifecycle classifier benchmark. |
| [local_tool_calling_benchmark/README.md](local_tool_calling_benchmark/README.md) | Current | agent-maintenance | Reproducible local Ollama tool-calling benchmark harness. |
| [local_tool_calling_benchmark/EXPERIMENT_LOG.md](local_tool_calling_benchmark/EXPERIMENT_LOG.md) | Current | agent-maintenance | Local model tool-calling feasibility results and next experiment criteria. |

## SNS / Reputation / Operations

| Document | Status | Owner area | Notes |
|---|---|---|---|
| [SNS_MONITOR_USAGE.md](SNS_MONITOR_USAGE.md) | Current (canonical: sns) | sns | User-facing SNS monitor commands and behavior. |
| [SNS_MONITOR_TROUBLESHOOTING.md](SNS_MONITOR_TROUBLESHOOTING.md) | Current | sns | Companion: service troubleshooting and logs. |
| [TELEGRAM_TOOL_SPEC.md](TELEGRAM_TOOL_SPEC.md) | Needs review | telegram | Telegram command/tool spec. |
| [LOGGING.md](LOGGING.md) | Current | operations | Logging conventions. |
| [AUTO_LOGIN_SETUP.md](AUTO_LOGIN_SETUP.md) | Needs review | operations | Local login/setup notes. |
| [MAC_MINI_M4.md](MAC_MINI_M4.md) | Current | operations | Machine-specific operational notes; avoid copying paths into code. |
| [BLUETOOTH_XGIMI_DEBUG_2026-07-01.md](BLUETOOTH_XGIMI_DEBUG_2026-07-01.md) | Current | operations | Sanitized incident/debug record for XGIMI Bluetooth reconnect and macOS Bluetooth permission behavior. |
| [BROADLINK_RESTART_RECOVERY_PLAYBOOK.md](BROADLINK_RESTART_RECOVERY_PLAYBOOK.md) | Current | operations | BroadLink instability playbook: distinguish device/network failures from startup-context failures; documents the restart preflight approach. |
| [MUSIC_PLAYBACK_TROUBLESHOOTING.md](MUSIC_PLAYBACK_TROUBLESHOOTING.md) | Current | music | afplay/CoreAudio -66681 wedge: coreaudiod-restart self-heal + one-time NOPASSWD sudo setup. |
| [RASPBERRY_PI_5.md](RASPBERRY_PI_5.md) | Needs review | operations | Pi deployment notes. |

## Quiz / Learning

| Document | Status | Owner area | Notes |
|---|---|---|---|
| [QUIZ_AUTHORING_PROGRESS.md](QUIZ_AUTHORING_PROGRESS.md) | Current / partial | quiz | Quiz authoring state. |
| [QUIZ_VOCAB_AUTHORING_PROGRESS.md](QUIZ_VOCAB_AUTHORING_PROGRESS.md) | Current / partial | quiz | Vocab authoring state. |
| [QUIZ_TEACHING_LOOP.md](QUIZ_TEACHING_LOOP.md) | Current / partial | quiz | Teaching loop notes. |
| [QUIZ_REVIEWS.md](QUIZ_REVIEWS.md) | Current / partial | quiz | Review notes. |
| [QUIZ_FAVORITE_SONGS.md](QUIZ_FAVORITE_SONGS.md) | Current / partial | quiz | Favorite song source notes. |

## Archived

Frozen, superseded context only — do not rely on for current truth. See
[archive/README.md](archive/README.md).

| Document | Owner area | Reason |
|---|---|---|
| [archive/TEST_RECORD_2026-04-16.md](archive/TEST_RECORD_2026-04-16.md) | testing | Dated test snapshot. |
| [archive/TEST_RECORD_2026-04-17.md](archive/TEST_RECORD_2026-04-17.md) | testing | Dated test snapshot. |
| [archive/SNS_INTEGRATION_TEST_REPORT.md](archive/SNS_INTEGRATION_TEST_REPORT.md) | sns | One-time integration report. |
| [archive/ISSUE_53_WORKFLOW_REVIEW_HANDOFF.md](archive/ISSUE_53_WORKFLOW_REVIEW_HANDOFF.md) | telegram | Archived pre-resolution review handoff after issue #53 shipped. |
| [archive/comp_filter_bm25_discussion.md](archive/comp_filter_bm25_discussion.md) | price/research | Superseded design discussion. |
| [archive/search_ddg_block_investigation.md](archive/search_ddg_block_investigation.md) | search | One-time investigation. |
| [archive/COMMISSION_SYSTEM_PLAN.md](archive/COMMISSION_SYSTEM_PLAN.md) | commissions | Business shelved. |

## Documentation Convention

Stateful docs should include:

```text
Last reviewed: YYYY-MM-DD
Status: Current / Needs review / Historical / Planned
Owner area: price / sns / reputation / telegram / dashboard / opportunity / agent-maintenance
```

## Maintenance Rule

When adding a doc under `docs/`, add it here with status, owner area, and purpose,
and update [DOC_AUDIT.md](DOC_AUDIT.md). Mark superseded docs `Historical` and move
them to `docs/archive/` instead of deleting useful context. Before pushing doc or
status changes, run [DOC_DRIFT_CHECKLIST.md](DOC_DRIFT_CHECKLIST.md).
