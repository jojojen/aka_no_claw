# Documentation Audit

Last reviewed: 2026-07-17
Owner area: agent-maintenance

Full inventory of repository documentation with lifecycle status, owner area, and
recommended action. Status vocabulary matches [DOCUMENTATION_GOVERNANCE.md](DOCUMENTATION_GOVERNANCE.md):
`Current`, `Needs review`, `Historical`, `Planned`, `Duplicate`, `Archive candidate`.

## Authoritative truth sources

These define system truth. See [DOCUMENTATION_GOVERNANCE.md](DOCUMENTATION_GOVERNANCE.md)
for which question each one answers.

| Document | Status | Owner area | Recommended action |
|---|---|---|---|
| `../Constitution.md` | Current | agent-maintenance | Keep; first required read. |
| `../README.md` | Current | agent-maintenance | Keep; high-level entry, defer detail to docs. |
| `../SYSTEM_MANIFEST.yaml` | Current | agent-maintenance | Keep; machine-readable truth index. |
| `AGENT_ONBOARDING.md` | Current | agent-maintenance | Keep; fast first-read guide. |
| `SYSTEM_MAP.md` | Current | architecture | Keep; architecture truth. |
| `CURRENT_STATE.md` | Current | agent-maintenance | Keep; runtime/status truth. |
| `TASK_ROUTING.md` | Current | agent-maintenance | Keep; task-ownership truth. |
| `VERIFICATION_MATRIX.md` | Current | verification | Keep; verification truth. |
| `DOCS_INDEX.md` | Current | agent-maintenance | Keep; documentation map. |

## Governance docs (this issue)

| Document | Status | Owner area | Recommended action |
|---|---|---|---|
| `DOC_AUDIT.md` | Current | agent-maintenance | This file; refresh during each governance pass. |
| `DOCUMENTATION_GOVERNANCE.md` | Current | agent-maintenance | Keep; lifecycle + update rules. |
| `DOC_DRIFT_CHECKLIST.md` | Current | agent-maintenance | Keep; run before doc-touching pushes. |
| `DOCS_CHANGE_TEMPLATE.md` | Current | agent-maintenance | Keep; PR checklist mirroring the automated checks (`scripts/check_docs_*`). |

## Design / methodology (active)

| Document | Status | Owner area | Recommended action |
|---|---|---|---|
| `LIQUIDITY_METHODOLOGY.md` | Current | price/liquidity | Keep; update with scoring changes. |
| `RESEARCH_COMMAND_PLAN.md` | Current | research | Keep; living `/research` status. |
| `BROWSER_UI_VALIDATION_PLAYBOOK.md` | Current | verification | Keep; browser smoke-test playbook for local web console validation. |
| `NEW_DYNAMIC_TOOLS_PROGRESS.md` | Current | dynamic-tools | Keep; canonical `/new` doc. |
| `NEW_E2E_DISCRIMINATING_TESTS.md` | Current | dynamic-tools | Keep; cross-link from dynamic-tools canonical. |
| `NEW_OPENCODE_DECOUPLING_PLAN.md` | Planned | dynamic-tools | Keep until shipped; then fold into canonical and archive. Cross-linked from canonical. |
| `CHAT_GOAL_LOOP_PLAN.md` | Planned | dynamic-tools | Keep until shipped (chat goal loop + eval harness, issues #50–#54); then mark Current or fold into canonical and archive. |
| `CHAT_CLOUD_POOL_STICKY_PROVIDER_PLAN.md` | Planned | dynamic-tools | Keep until shipped (sticky-provider pin for chat `cloud_pool`, ported from musubi issue #11 Case C P1); then mark Current or fold into canonical and archive. |
| `P1_ENGINEERING_HARDENING_IMPLEMENTATION_PLAN.md` | Planned | agent-maintenance | Canonical P1 execution/handoff plan for issue #80; keep while active, then fold stable rules into truth docs and archive. |
| `WEB_SESSION_RUN_EVENT_SPINE_IMPLEMENTATION_PLAN.md` | Current | command-bridge / conversation-runtime | Shipped #84 event-spine contract and implementation record; stable ownership and verification are folded into the authoritative truth docs. |
| `WEB_DYNAMIC_TOOL_APPROVAL_IMPLEMENTATION_PLAN.md` | Current | dynamic-tools / command-bridge safety | Shipped #85 manifest-bound approval contract and staged live-proof record; depends on the #84 event spine. |
| `WEB_PROMPT_QUEUE_IMPLEMENTATION_PLAN.md` | Current | command-bridge / conversation-runtime | Canonical #86 prompt queue and safe interjection implementation record; supported-restart proof complete. Depends on #84 and the Web consumer plan. |
| `WEB_CONVERSATION_COMPACTION_IMPLEMENTATION_PLAN.md` | Current | command-bridge / model-context | Canonical #87 grounded deterministic compaction/checkpoint implementation record; depends on #84 and the Web consumer plan. |
| `TELEGRAM_NL_OWNERSHIP_REFACTOR_ISSUE.md` | Planned | telegram | Draft issue; keep until the cross-repo NL ownership refactor is filed/resolved. |
| `TELEGRAM_CORE_EXTRACTION_PLAN.md` | Current | telegram | Extraction shipped (phases P0–P4, all 3 repos' suites green); kept in place (not archived) as the `telegram_core` hook/registry contract reference. |
| `VOICE_CONTROL_PERSONALIZATION_DESIGN.md` | Planned | voice / command-bridge / web | Canonical #82 design for local-only voice-control personalization, pre-tool safety gate, and registry-derived action clarification; keep as the implementation source until shipped. |
| `OPPORTUNITY_AGENT_SPEC.md` | Needs review | opportunity | Verify thresholds vs code; canonical Opportunity doc. |
| `OPPORTUNITY_AGENT_HANDOFF.md` | Needs review | opportunity | Keep as operational companion; cross-link to spec. |
| `KB_EMBEDDING_PLAN.md` | Needs review | knowledge | Re-verify embedding/RAG status. |
| `OPENCLAW_TCG_MONITOR_PLAN.md` | Needs review | price/tcg | Verify against code; archive candidate if superseded. |
| `TELEGRAM_TOOL_SPEC.md` | Needs review | telegram | Verify command list vs code. |

## SNS / operations (active)

| Document | Status | Owner area | Recommended action |
|---|---|---|---|
| `SNS_MONITOR_USAGE.md` | Current | sns | Canonical SNS user/usage doc. |
| `SNS_MONITOR_TROUBLESHOOTING.md` | Current | sns | Keep as ops companion; cross-link to usage. |
| `LOGGING.md` | Current | operations | Keep. |
| `MAC_MINI_M4.md` | Current | operations | Keep; machine-specific, never copy paths into code. |
| `BROADLINK_RESTART_RECOVERY_PLAYBOOK.md` | Current | operations | Keep; incident-derived BroadLink recovery/runbook for restart-context failures. |
| `BROADLINK_LOCAL_NETWORK_TROUBLESHOOTING.html` | Current | operations | Keep; sanitized public-shareable HTML companion to the recovery playbook, linked from MUSIC_PLAYBACK_TROUBLESHOOTING.md. Explicitly named (not glob-matched) in check_docs_health.py so fix_benchmarks/ HTML test fixtures aren't swept into doc governance. |
| `AUTO_LOGIN_SETUP.md` | Needs review | operations | Re-verify setup steps. |
| `RASPBERRY_PI_5.md` | Needs review | operations | Re-verify Pi deployment relevance. |

## Quiz / learning (active, partial)

| Document | Status | Owner area | Recommended action |
|---|---|---|---|
| `QUIZ_AUTHORING_PROGRESS.md` | Current | quiz | Keep; living authoring state. |
| `QUIZ_VOCAB_AUTHORING_PROGRESS.md` | Current | quiz | Keep; living vocab state. |
| `QUIZ_TEACHING_LOOP.md` | Current | quiz | Keep. |
| `QUIZ_REVIEWS.md` | Current | quiz | Keep. |
| `QUIZ_FAVORITE_SONGS.md` | Current | quiz | Keep; source notes. |

## Archived (segregated 2026-06-20)

Moved under `docs/archive/`; see [archive/README.md](archive/README.md).

| Document | Status | Owner area | Reason |
|---|---|---|---|
| `archive/TEST_RECORD_2026-04-16.md` | Historical | testing | Dated test snapshot. |
| `archive/TEST_RECORD_2026-04-17.md` | Historical | testing | Dated test snapshot. |
| `archive/SNS_INTEGRATION_TEST_REPORT.md` | Historical | sns | One-time integration report. |
| `archive/ISSUE_53_WORKFLOW_REVIEW_HANDOFF.md` | Historical | telegram | Pre-resolution workflow review handoff; archived after issue #53 shipped. |
| `archive/ISSUE_66_PHASE2_PROGRESS.md` | Historical | agent-maintenance | Shipped phase-2 implementation record for issue #66; retained for implementation history only. |
| `archive/BLUETOOTH_XGIMI_DEBUG_2026-07-01.md` | Historical | operations | Resolved Bluetooth/XGIMI incident debug record kept as sanitized historical context. |
| `archive/comp_filter_bm25_discussion.md` | Historical | price/research | Superseded design discussion. |
| `archive/search_ddg_block_investigation.md` | Historical | search | One-time investigation. |
| `archive/COMMISSION_SYSTEM_PLAN.md` | Historical | commissions | Business shelved. |

## Duplicate / canonical-location decisions

Per [DOCUMENTATION_GOVERNANCE.md](DOCUMENTATION_GOVERNANCE.md), overlapping domains
keep one canonical doc with companions cross-linked rather than physically merged
(preserves nuance):

| Domain | Canonical | Companion(s) |
|---|---|---|
| SNS monitor | `SNS_MONITOR_USAGE.md` | `SNS_MONITOR_TROUBLESHOOTING.md`; archived test report |
| Opportunity agent | `OPPORTUNITY_AGENT_SPEC.md` | `OPPORTUNITY_AGENT_HANDOFF.md` |
| Dynamic tools (`/new`) | `NEW_DYNAMIC_TOOLS_PROGRESS.md` | `NEW_E2E_DISCRIMINATING_TESTS.md` |
