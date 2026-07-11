Last reviewed: 2026-07-11
Status: Done
Owner area: telegram

# Chat-tool planner stability fix (開燈 → __no_tool__ 誤判)

## Symptom (2026-07-11)

Web chat「開燈」three consecutive live probes against the bridge gave three
different outcomes, two of them wrong in a user-misleading way:

1. `tool=__no_tool__`, answer「好的，已開燈。」— claims the action was done,
   no IR was sent.
2. Same again for「幫我開燈」.
3. Streaming path: asked back「請問您要開哪一盞燈？」— no tool either.

The user's real session the day before DID plan `/ir` for the same utterance,
so this is planner instability, not a hard regression.

## Root cause chain

- `_select_chat_tool_plan` → `_generate_local_chat_tool_plan` → `self._local_model()`
  → `resolve_provider_model(settings, LLM_PROVIDER_LOCAL)` → **`config/llm_pool.json`
  override**: local provider model = `qwen2.5-coder:7b` (web-UI chat setting saved
  2026-07-07; gitignored, so invisible in git history).
- So the tool PLANNER runs on a 7B code-completion model, judging Traditional-
  Chinese home-automation intent. `.env` `OPENCLAW_LOCAL_TEXT_MODEL` is
  `qwen3:14b`, but the chat pool override wins for anything routed through the
  chat backend — including the hidden planner call.
- The satisfaction judge and NL router already use the dedicated text model
  (qwen3:14b); the planner is the odd one out.

## Constraints

- Rule G: no keyword/qualifier lists anywhere in the fix — model/prompt/structural
  levers only.
- The chat ANSWER model stays user-selectable (web UI choice is respected);
  only the hidden judgment step may be re-homed.
- Voice-latency budget: planner runs on every chat turn; measure before adopting
  a bigger model.

## Blast radius of the same bug

`_generate_chat_tool_plan_with_chat_backend`'s local path is shared by THREE
hidden judgment consumers, so all of them currently run on the chat-pool
override (qwen2.5-coder:7b), none on the intended judgment model:

1. `_select_chat_tool_plan` — the chat tool planner (this incident).
2. `_generate_chat_tool_satisfaction_text` — the satisfaction /
   environment_blocked judge (meaning the 2026-07-10 live verification of the
   judge on qwen3:14b did NOT match production, which judges on coder:7b).
3. `_goal_planner_client` — goal-loop workflow drafting.

None of these is the user-visible chat answer, so re-homing them does not
override the user's web-UI model choice.

## Fix design

Single-point change in `_generate_local_chat_tool_plan`: use the dedicated
local judgment model (first entry of `settings.openclaw_local_text_model`,
same pattern as the NL router / opportunity agent / dynamic tools) instead of
`self._local_model()` (chat-pool override). Falls back to the pool model when
the env var is blank. No keyword lists anywhere (Rule G).

## Progress log

- 2026-07-11 11:35 — Root cause located (`config/llm_pool.json` local model
  override reaches the planner via `_local_model()`). Started live A/B probe of
  the exact planner prompt on qwen2.5-coder:7b vs qwen3:14b, N=3 per case
  (開燈 / 關掉電風扇 / multi-step / chitchat control), measuring correctness
  and latency.
- 2026-07-11 11:45 — Found the satisfaction judge and goal planner share the
  same local-model path; scoped the fix to `_generate_local_chat_tool_plan`.
- 2026-07-11 12:05 — **Correction on severity**: the user's web UI sends
  `chat_backend=cloud_pool` (planning happens on cloud models there); my curl
  probes omitted `chat_backend`, and `parse_request` defaults to `local` —
  so the flakiness I reported was the LOCAL FALLBACK path, not the user's
  primary path. The fix still matters: local is the quality floor (all-clouds-
  down fallback, the satisfaction judge's explicit fallback, and a selectable
  backend).
- 2026-07-11 12:10 — A/B probe (N=3/case, exact planner prompt, live Ollama):
  qwen2.5-coder:7b 6/12 — 開燈 0/3 (claims done, no tool), multi-step 0/3
  (truncated to /ir, the forbidden failure mode); qwen3:14b 12/12. Latency
  same ballpark (1.3–8.1s vs 2.9–9.2s).
- 2026-07-11 12:15 — Implemented `_local_judgment_model()` (first entry of
  `OPENCLAW_LOCAL_TEXT_MODEL`, falls back to pool model when blank) and
  switched `_generate_local_chat_tool_plan` to it. Regression test
  `test_local_chat_tool_plan_uses_judgment_model_not_pool_override` (pool
  pinned to a fake tiny model → judgment calls still use qwen3:14b).
  Known trade-off: for local-backend chats a `__no_tool__` plan's inline
  answer IS the final reply, so those answers now also come from the judgment
  model (slightly slower, better quality) — cloud-backend answers unchanged.
- 2026-07-11 14:30 — Full suite 2500 passed / 7 skipped (only failure was the
  missing DOCS_INDEX entry for this doc; added). Restarted stack via
  `trigger_restart_all`; live E2E on the restarted bridge: 「開燈」(local
  default backend) planned `/ir send 燈 開` and actually sent IR (light
  responded). Fixed-path probe against live Ollama: 開燈然後放一首歌 →
  `__goal__`, 米津玄師是誰 → `__no_tool__`, both on qwen3:14b while the pool
  override remains qwen2.5-coder:7b. Done.
