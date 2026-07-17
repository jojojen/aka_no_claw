# Docs Index

Last reviewed: 2026-07-17

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
| [R3_RESEARCH_PIPELINE_INVENTORY.md](R3_RESEARCH_PIPELINE_INVENTORY.md) | Current | research | R3 responsibility and compatibility inventory for `/research` decomposition (issue #76). |
| [BROWSER_UI_VALIDATION_PLAYBOOK.md](BROWSER_UI_VALIDATION_PLAYBOOK.md) | Current | verification | Browser smoke-test playbook for local web console validation. |
| [NEW_DYNAMIC_TOOLS_PROGRESS.md](NEW_DYNAMIC_TOOLS_PROGRESS.md) | Current (canonical: dynamic-tools) | dynamic-tools | `/new` implementation notes and benchmark history. |
| [R4_DYNAMIC_TOOLS_INVENTORY.md](R4_DYNAMIC_TOOLS_INVENTORY.md) | Current | dynamic-tools | R4 responsibility, threat, and resource inventory for the `/new` pipeline (issue #76). |
| [NEW_E2E_DISCRIMINATING_TESTS.md](NEW_E2E_DISCRIMINATING_TESTS.md) | Current | dynamic-tools | Companion: discriminating tests for generated tools. |
| [NEW_OPENCODE_DECOUPLING_PLAN.md](NEW_OPENCODE_DECOUPLING_PLAN.md) | Planned | dynamic-tools | Companion: plan to move `/new` + Chat off OpenCode CLI to direct HTTP; add Mistral switch (issues #51/#59). |
| [CHAT_GOAL_LOOP_PLAN.md](CHAT_GOAL_LOOP_PLAN.md) | Planned | dynamic-tools | Companion: chat goal loop — plan-then-execute multi-tool runs with replan, budget-continue UX, narrated trace, eval harness (issues #50–#54). |
| [CHAT_REWORK_DEBUG_TRACE.md](CHAT_REWORK_DEBUG_TRACE.md) | Current | dynamic-tools | Debug record: why one chat turn ran /research ×3 (rework); root causes + seed-variables/tool-ledger fix rationale. |
| [WEB_CHAT_MULTIMODAL_PLAN.md](WEB_CHAT_MULTIMODAL_PLAN.md) | Planned | dynamic-tools | Web chat multimodal: image upload, text/vision cloud pool split with rotation, vision-as-a-tool orchestration, image-translation A/B replacement (issue #71). |
| [CHAT_CLOUD_POOL_STICKY_PROVIDER_PLAN.md](CHAT_CLOUD_POOL_STICKY_PROVIDER_PLAN.md) | Planned | dynamic-tools | Companion: pin the `cloud_pool` chat backend to whichever provider last answered successfully within a conversation, instead of re-walking the chain from the top every turn; ported from musubi issue #11 Case C's P1 sticky-provider fix. |
| [CHAT_TOOL_PLANNER_STABILITY_FIX.md](CHAT_TOOL_PLANNER_STABILITY_FIX.md) | Done | telegram | Debug record + fix: local chat-tool planner ran on the chat-pool model override (qwen2.5-coder:7b) and misjudged 開燈 as `__no_tool__`; re-homed hidden judgment calls to the dedicated local text model. |
| [P1_ENGINEERING_HARDENING_IMPLEMENTATION_PLAN.md](P1_ENGINEERING_HARDENING_IMPLEMENTATION_PLAN.md) | Planned | agent-maintenance | Canonical execution and handoff plan for P1 deterministic tests, multi-repo CI, reproducible sibling revisions, versioned contracts, and oversized-orchestrator decomposition (issue #80). |
| [WEB_SESSION_RUN_EVENT_SPINE_IMPLEMENTATION_PLAN.md](WEB_SESSION_RUN_EVENT_SPINE_IMPLEMENTATION_PLAN.md) | Current | command-bridge / conversation-runtime | Shipped backend contract and implementation record for append-only session/run events, exact cursor recovery, compatibility adapters, and background completion injection (issue #84). |
| [WEB_DYNAMIC_TOOL_APPROVAL_IMPLEMENTATION_PLAN.md](WEB_DYNAMIC_TOOL_APPROVAL_IMPLEMENTATION_PLAN.md) | Current | dynamic-tools / command-bridge safety | Shipped manifest-bound, fail-closed Web approval gate for generated workflow tools, with staged live proof (issue #85). |
| [WEB_PROMPT_QUEUE_IMPLEMENTATION_PLAN.md](WEB_PROMPT_QUEUE_IMPLEMENTATION_PLAN.md) | Current | command-bridge / conversation-runtime | Durable next-turn queue plus bounded safe-boundary interjections, capture isolation, and exact-once drain (issue #86). Depends on the event spine. |
| [WEB_CONVERSATION_COMPACTION_IMPLEMENTATION_PLAN.md](WEB_CONVERSATION_COMPACTION_IMPLEMENTATION_PLAN.md) | Planned | command-bridge / model-context | Grounded, versioned context checkpoints that preserve the authoritative event history (issue #87). Depends on the event spine. |
| [R1_COMMAND_BRIDGE_INVENTORY.md](R1_COMMAND_BRIDGE_INVENTORY.md) | Living | agent-maintenance | R1.0 characterization inventory of `command_bridge.py` before decomposition (issue #74): public surface × routes × consumers, state/locks, threads/cancellation, providers, stores, response contracts, coverage gaps, risk notes. |
| [R2_TELEGRAM_OWNERSHIP_INVENTORY.md](R2_TELEGRAM_OWNERSHIP_INVENTORY.md) | Current | telegram | R2.0 ownership inventory (issue #75): every command/callback prefix's owning layer, registration site, DB access, and the dispatch/merge precedence contract pinned by `tests/test_registry_precedence.py`. |
| [WORKSPACE_DEPENDENCIES.md](WORKSPACE_DEPENDENCIES.md) | Current | build | D1.1 inventory: direct dependencies, sibling distributions, import packages, and version matrix. |
| [CROSS_REPO_CONTRACTS.md](CROSS_REPO_CONTRACTS.md) | Planned | agent-maintenance | D2.1 inventory: cross-repository boundaries (DB, HTTP, SSE), owners, versions, failure semantics, and migration policy. |
| [RESEARCH_CHAT_SUMMARY_DATA_DROP_FIX_PLAN.md](RESEARCH_CHAT_SUMMARY_DATA_DROP_FIX_PLAN.md) | Planned | research | Lightweight fix: chat's compact `/research` reply drops seller reputation stats (`_compact_seller_summary` keeps only the risk verdict); before/after diff, tests, and explicit out-of-scope notes for the job/button and comp-recall issues found alongside it. |
| [TELEGRAM_NL_OWNERSHIP_REFACTOR_ISSUE.md](TELEGRAM_NL_OWNERSHIP_REFACTOR_ISSUE.md) | Planned | telegram | Draft issue for moving aka-specific Telegram NL workflow routing out of `price_monitor_bot`. |
| [TELEGRAM_CORE_EXTRACTION_PLAN.md](TELEGRAM_CORE_EXTRACTION_PLAN.md) | Current | telegram | Shared `telegram_core` package (transport, dispatcher contract, polling, list_view) extracted from `price_monitor_bot`, P0–P4 shipped; kept as the implementation record and hook/registry contract reference. |
| [KB_EMBEDDING_PLAN.md](KB_EMBEDDING_PLAN.md) | Needs review | knowledge | Embedding/RAG plan and status may need fresh verification. |
| [voice-latency-optimization-references.md](voice-latency-optimization-references.md) | Current | telegram | Voice-pipeline latency work: semantic intent cache (threshold + digit/containment guards, live-probed), keep_alive/KV reuse, audio-side STT fixes, deferred options; citation trail. |
| [VOICE_CONTROL_PERSONALIZATION_DESIGN.md](VOICE_CONTROL_PERSONALIZATION_DESIGN.md) | Planned | voice / command-bridge / web | Canonical #82 design for voice provenance, pre-tool intent gating, registry-derived clarification, local prototype learning, safety policy, API contracts, rollout, and evaluation. |
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
| [BROADLINK_RESTART_RECOVERY_PLAYBOOK.md](BROADLINK_RESTART_RECOVERY_PLAYBOOK.md) | Current | operations | BroadLink instability playbook: distinguish device/network failures from startup-context failures; documents the restart preflight approach. |
| [BROADLINK_LOCAL_NETWORK_TROUBLESHOOTING.html](BROADLINK_LOCAL_NETWORK_TROUBLESHOOTING.html) | Current | operations | Sanitized, public-shareable standalone HTML version of the BroadLink LAN troubleshooting guide (no internal paths/IPs/ports); linked from MUSIC_PLAYBACK_TROUBLESHOOTING.md. Exempt from internal metadata headers by design (see DOCUMENTATION_GOVERNANCE.md). |
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
| [archive/ISSUE_66_PHASE2_PROGRESS.md](archive/ISSUE_66_PHASE2_PROGRESS.md) | agent-maintenance | Shipped issue #66 phase-2 implementation/progress record, kept only as history. |
| [archive/BLUETOOTH_XGIMI_DEBUG_2026-07-01.md](archive/BLUETOOTH_XGIMI_DEBUG_2026-07-01.md) | operations | Resolved Bluetooth/XGIMI incident record kept as sanitized historical context. |
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
