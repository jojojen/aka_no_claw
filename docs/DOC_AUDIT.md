# Documentation Audit

Last reviewed: 2026-06-20
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
| `fix_benchmarks/README.md` | Current | agent-maintenance | Keep; `/fix` benchmark index and synthetic-fixture safety rules. |
| `fix_benchmarks/price_reference_sources/README.md` | Current | agent-maintenance | Keep; benchmark spec for synthetic multi-source price parsers. |
| `fix_benchmarks/price_reference_sources/FAILURE_TRACE.md` | Current | agent-maintenance | Keep; reproducible parser failure history for `/fix` evaluation. |
| `fix_benchmarks/image_translation_policy/README.md` | Current | agent-maintenance | Keep; public-media benchmark for adaptive OCR/image translation policies. |
| `fix_benchmarks/seller_snapshot_sources/README.md` | Current | agent-maintenance | Keep; benchmark spec for synthetic seller snapshot parser and lifecycle repair. |
| `fix_benchmarks/seller_snapshot_sources/FAILURE_TRACE.md` | Current | agent-maintenance | Keep; reproducible seller snapshot parser and cooldown failure history. |
| `fix_benchmarks/seller_snapshot_sources/lifecycle/README.md` | Current | agent-maintenance | Keep; rate-limit and bot-interstitial classifier benchmark. |
| `local_tool_calling_benchmark/README.md` | Current | agent-maintenance | Keep; local Ollama tool-calling benchmark harness and acceptance bar. |
| `local_tool_calling_benchmark/EXPERIMENT_LOG.md` | Current | agent-maintenance | Keep; feasibility results for local model tool calling before filing architecture issue. |
| `NEW_DYNAMIC_TOOLS_PROGRESS.md` | Current | dynamic-tools | Keep; canonical `/new` doc. |
| `NEW_E2E_DISCRIMINATING_TESTS.md` | Current | dynamic-tools | Keep; cross-link from dynamic-tools canonical. |
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
