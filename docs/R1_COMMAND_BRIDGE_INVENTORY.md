# R1.0 — Command Bridge Characterization Inventory

Last reviewed: 2026-07-12
Status: Living
Owner area: agent-maintenance

Workstream R1, issue #74. Snapshot taken 2026-07-12 at
`command_bridge.py` = 5,322 lines (25 public / 133 private methods).
Purpose: lock down the observable surface BEFORE code motion, per
[P1 plan §9](P1_ENGINEERING_HARDENING_IMPLEMENTATION_PLAN.md#9-workstream-r1--command-bridge-decomposition).
Line numbers drift; symbol names are the stable reference.

## 1. Already-extracted collaborators (do not re-invent)

| Module | Owns |
| --- | --- |
| `command_bridge_models.py` (~740 ln) | modes/submodes/backends/status enums, NDJSON event vocabulary + `stream_*` builders, `WebCommandRequest/Response`, `parse_request`, history sanitization, chat-tool constants, `ChatToolPlan` parsing, pure helpers (`build_chat_prompt`, `_clip`, `_tool_calling_notice`, `_extract_gemini_text`, `_seed_variable_name_for_tool`, `markup_to_actions`, image-attachment helpers) |
| `command_bridge_providers.py` | provider routing (R1.2): `_MODEL_STATUS_*` vocabulary, `_GeminiTextClient` + `_GeminiRequestError` + HTTP-status classification, `_walk_cloud_pool_chain` shared failover walk, `_pin_provider_chain` sticky reorder, and `ProviderRouter` (per-provider model resolution, cloud/vision pool chains, sticky per-conversation pins, model metadata, blocking cloud-pool / Gemini-fallback generation) behind the `ChatClientDeps` protocol — client builders stay on the bridge so instance monkeypatching keeps working |
| `command_bridge_server.py` (610 ln) | HTTP routing only (stdlib server), envelope versioning |
| `job_store.py` | persisted job payloads (see §6) |
| `session_memory.py` | session snapshot persistence |
| `goal_loop.py` / `task_workspace.py` / `task_loop.py` | goal orchestration, workflow runner, bounded loop |
| `continuation_policy.py` | tool outcome classification, `operation_key` dedup identity |
| `goal_planner.py` | trusted plan generation/validation |

R1.1 is DONE: the remaining pure helpers (`build_chat_prompt`, `_clip`,
`_tool_calling_notice`, `_extract_gemini_text`, `_seed_variable_name_for_tool`,
`markup_to_actions`, `_is_supported_image` / `_encode_image_attachment` /
`_image_temp_suffix`, plus `_CHAT_SYSTEM_PROMPT` / `_CHAT_ROLE_LABELS` /
image-format constants) now live in `command_bridge_models.py`;
`command_bridge.py` re-imports them so existing consumers/tests keep importing
from `openclaw_adapter.command_bridge` unchanged (`CommandBridge._markup_to_actions`
stays as a `staticmethod` alias).

## 2. Public `CommandBridge` surface × HTTP route × frontend consumer

Frontend = `aka_no_claw_web/frontend/src/api/commandClient.ts` (sole web consumer).

| Bridge method | Route | Web uses? | Contract |
| --- | --- | --- | --- |
| `handle(req)` | `POST /api/command` | yes | blocking `WebCommandResponse` JSON |
| `stream(req, request_id)` | `POST /api/command/stream` | yes | NDJSON events (§7) |
| `start_async(req)` | `POST /api/command/async` | yes | `{status, job_id}`; research/investment only |
| `poll_job(job_id)` | `GET /api/command/poll` | yes | job snapshot; `not_found` for unknown |
| `cancel_job(job_id)` | `POST /api/command/cancel` | **no (pending stop button)** | cooperative cancel → `interrupted` |
| `run_action(job_id, cb)` | `POST /api/command/action` | yes | research follow-up buttons |
| `run_music_command` / `run_musicqueue_command` / `run_music_action` / `now_playing` | `POST /api/command/music`, `GET /api/command/music/now` | yes | music surface |
| `run_workflow_command` / `run_workflow_action` | `POST /api/command/workflow` | yes | workflow draft/edit/run cards |
| `run_schedulehome_command` / `run_schedulehome_action` | `POST /api/command/schedulehome` | yes | schedule surface |
| `run_bluetooth_command` / `run_bluetooth_action` | `POST /api/command/bluetooth` | yes | BT surface |
| `run_ir_command` / `run_ir_action` | `POST /api/command/ir` | yes | IR surface |
| `load_session` / `save_session` / `clear_session` | `GET/POST/DELETE /api/command/session` | yes | session snapshot |
| `load_chat_settings` / `save_chat_settings` | `GET/POST /api/command/chat-settings` | yes | backend/model prefs |
| `model_routes()` | `GET /api/command/model-routes` | yes | route→model listing |
| `restart_all()` | `POST /api/command/restartall` | yes | `service_restart.trigger_restart_all` |
| (server-only) | `POST /api/command/transcribe` | yes | `local_stt`, multipart audio; returns opaque `utterance_id` (#82 PR1) |
| `confirm_voice_action(action_id)` | `POST /api/command/voice/confirm` | pending (#82 PR1 frontend) | executes a voice clarification candidate; backend re-resolves the `voice/` action registry, client submits only `action_id` |

Telegram is NOT a consumer of `CommandBridge`; it shares the underlying
handler registries via `telegram_bot._build_registries` (§4).

## 3. State fields and synchronization

All mutable state is on the bridge instance; every dict below has a dedicated
`threading.Lock` (16 locks total, none reentrant — see DEADLOCK GUARD comment
in `_ensure_registries` for why `_workflow_lock` must never nest inside
`_registry_lock`).

| State | Lock | Lifetime / notes |
| --- | --- | --- |
| `_command/_callback/_view/_item_deleter_handlers` | `_registry_lock` | lazy, shared with Telegram registries; double-checked locking |
| `_jobs` (`_JobManager`) + `_job_store_inst` | own `_lock` / `_job_store_lock` | in-memory jobs + persisted `JobStore` |
| `_live_notifiers` | `_live_notifier_lock` | chat_id → stream callback while NDJSON stream open |
| `_session_store` | `_session_lock` | lazy `SessionMemoryStore` |
| `_image_renderer` | `_image_renderer_lock` | lazy, heavy build |
| `_music_continuations` | `_music_cont_lock` | per-conversation paused music plan (in-process only) |
| `_goal_continuations` | `_goal_cont_lock` | per-conversation paused goal loop |
| `_chat_pool_pins` | `_chat_pool_pins_lock` | sticky cloud-pool provider per conversation |
| `_goal_pending_confirms` | `_goal_pending_lock` | goal runs awaiting user confirm |
| `_goal_completed_workflows` | `_goal_completed_lock` | "存為工作流" button payloads |
| `_chat_tool_ledgers` | `_chat_tool_ledger_lock` | bounded per-conversation tool-run ledger fed to the router |
| `_workflow_handler/_editor` | `_workflow_lock` | lazy; editor keeps draft sessions across HTTP requests |
| `_sh_handler/_sh_cb_handler/_sh_store` | `_sh_lock` | schedule surface, store shared with Telegram scheduler |

## 4. Threads, processes, cancellation

- Pattern: every long operation spawns ONE daemon `threading.Thread` worker,
  coordinated by `done = threading.Event()` (14 sites), with the generator
  thread emitting heartbeats while waiting. Streaming goal-loop adds
  `abandoned` (client disconnect) and `job.cancel_event` (explicit cancel).
- Cancellation semantics (issue #81, shipped `7045c76`):
  - stream drop → worker keeps running, result recoverable via job poll;
  - explicit cancel (`cancel_job` / `POST /api/command/cancel`) → per-job
    `threading.Event` observed at goal-loop stage boundaries and before each
    workflow step → terminal `JOB_INTERRUPTED`, no synthesis, no continuation.
- Subprocesses: scraping runs via `scrape_subprocess.py` (owned outside the
  bridge); the bridge itself spawns no processes except `restart_all()`'s
  detached restart script.

## 5. Providers and fallback semantics

- Backends: `local` (Ollama), `cloud_pickle` (big-pickle), `cloud_mistral`,
  `cloud_nvidia`, `gemini`, `cloud_pool` (rotating chain).
- `_walk_cloud_pool_chain` iterates providers; `_pin_provider_chain` +
  `_chat_pool_pins` keep a conversation sticky to the provider that answered.
- Gemini: `_GeminiTextClient` with `_GeminiRequestError`;
  `_is_gemini_fallback_status` gates primary→flash fallback
  (`_generate_gemini_with_fallback`).
- Failure reporting is explicit per §G/C4: unavailable backends return typed
  error messages (`_chat_backend_disabled_message`), never silent source swaps.
  `ModelMetadata`/`ModelAttempt` (models module) record which model actually
  answered — the frontend renders this.

## 6. Stores and persisted payloads

| Store | Payload |
| --- | --- |
| `JobStore` | `{job_id, status: running/done/error/interrupted, progress[], message, actions[], error, created_at, updated_at}` |
| `SessionMemoryStore` | web session snapshot (history, view state) |
| `WorkflowStore` | workflows + run traces (`task_workspace.py` schema) |
| `HomeScheduleStore` | scheduled home-control commands |

## 7. Response contracts

1. **Blocking JSON** — `WebCommandResponse` (status/message/mode/actions/
   sources/model_metadata). Envelope version stamped by the server
   (`test_command_bridge_server.py`).
2. **Async job** — `start_async` → `poll_job` (→ `run_action` | `cancel_job`).
   Long research MUST use this path (mobile screen-lock drops held streams).
3. **NDJSON stream** — event `type` vocabulary (models module):
   `start`, `delta`, `heartbeat`, `done`, `error`, `redirect`, `process`,
   `job`. Ordering contract: `start` first; `job` precedes goal-loop progress;
   terminal event is exactly one of `done`/`error`/`redirect`.

## 8. Test coverage and known gaps

Existing: `tests/test_command_bridge.py` (251 tests — routing, chat tools,
goal loop escalation/resume/confirm, jobs/poll/cancel, music/workflow/schedule
surfaces, streaming incl. disconnect + recovery); `tests/test_command_bridge_server.py`
(4 tests — envelope version only).

Characterization gaps to fill before/during code motion (R1.0 second half):

- [ ] HTTP-layer route characterization: real `command_bridge_server` request
      → JSON/NDJSON framing, error statuses, malformed-body handling (only the
      envelope stamper is covered today).
- [ ] Orphaned result handling (`_push_orphaned_result`): zero direct tests.
- [ ] Concurrent conversations: parallel sessions hitting pins/ledgers/
      continuations (locks exist; behavior untested).
- [ ] NDJSON event-ordering assertion as an explicit contract test (today
      asserted implicitly inside feature tests).

## 9. Decomposition risk notes (read before moving code)

- `_ensure_registries` lazy-imports `telegram_bot._build_registries`: the
  bridge and the Telegram bot share ONE registry construction path — R1 and
  R2 (issue #75) touch the same seam; land registry extraction once, not twice.
- The deadlock guard in `_ensure_registries` is load-bearing; any extraction
  of the workflow surface must preserve lazy resolution at dispatch time.
- `_live_notifiers` bridges two worlds (job-backed progress vs open stream);
  moving job management without it silently drops /research milestones.
- `restart_all()` must keep using `service_restart.trigger_restart_all`
  (the only supported restart path — see CLAUDE.md 409 warning).
