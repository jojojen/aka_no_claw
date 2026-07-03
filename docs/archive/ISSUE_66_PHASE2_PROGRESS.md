# Issue #66 Phase 2 Progress

Last reviewed: 2026-07-03
Status: Historical
Owner area: agent-maintenance

Updated: 2026-07-02

## Scope

- Shared chat LLM pool settings for Web Chat and Telegram natural-language routing.
- Backend read/write API for `config/llm_pool.json`.
- Web settings UI for provider order, enable flags, model selection, and local model reload.

## Implementation Notes

- `.env` / `AssistantSettings` remains the source of secrets and default model ids.
- `config/llm_pool.json` stores only user-editable pool order, enable flags, and selected model ids.
- The shared resolver must be used by both:
  - web `CommandBridge`
  - Telegram `natural_language` router
- Local model reload stays behind a backend helper so the UI never speaks Ollama directly.

## Progress

- [x] Phase 1 accepted and merged to backend `main` / frontend `master`
- [x] Phase 2 branches created: `issue-66-cloud-pool-settings-phase2`
- [x] Shared llm-pool config module scaffolded
- [x] Backend settings endpoints
- [x] Local model warmup / rollback flow
- [x] Telegram shared resolver wiring
- [x] Web settings modal
- [x] Tests and end-to-end verification

## 2026-07-02 Follow-up: Web Chat Tool Decision Unification

Goal:

- remove the old Web Chat "local router first" behavior
- let the currently selected chat backend decide both:
  - direct answer (hidden no-tool path)
  - explicit tool use (`/search`, `/music`, `/bluetooth`, `/ir`)
- keep the existing closed allowlist / executor guardrails

Implementation direction:

- replace the visible "direct vs tool router" concept with a single chat-tool plan
- hidden direct path is represented internally as a no-tool plan and must not show a tool banner in the UI
- selected backend owns the decision; no fallback back to local routing when a cloud backend is selected

Current status:

- [x] backend selection model for tool decision follows the selected chat backend
- [x] remove old router-style main-path decision flow in favor of unified hidden no-tool plan
- [x] update tests to cover hidden no-tool + explicit tool paths

Completed notes:

- Web Chat main path now asks the selected backend for one strict JSON chat-tool plan:
  - `{"tool":"__no_tool__","answer":"..."}`
  - or `{"tool":"/search|/music|/bluetooth|/ir","query":"..."}`
- Direct answers no longer pass through a separate local router stage.
- When planner output is malformed / unavailable, the bridge fails soft to a plain direct answer path instead of guessing a tool.
- Regression coverage now includes:
  - selected-backend planner ownership
  - explicit tool execution
  - hidden no-tool direct responses
  - no tool banner on hidden no-tool streaming replies

## Open Decisions Resolved

- `config/llm_pool.json` is treated as local runtime config and gitignored.
- Cloud preview / route display must not probe provider APIs just to render UI.

## Archive Note

Issue #66 phase 2 shipped and no longer needs a living progress file. Keep this
document only as implementation history.
