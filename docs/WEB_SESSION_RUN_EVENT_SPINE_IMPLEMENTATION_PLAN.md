# Web Session/Run Event Spine Implementation Plan

Last reviewed: 2026-07-17
Status: Planned
Owner area: command-bridge / conversation-runtime
Tracking issue: [`aka_no_claw#84`](https://github.com/jojojen/aka_no_claw/issues/84)
Companion Web issue: [`aka_no_claw_web#12`](https://github.com/jojojen/aka_no_claw_web/issues/12)
Companion Web plan: `jojojen/aka_no_claw_web/docs/AGENT_CONTROL_PLANE_IMPLEMENTATION_PLAN.md`

## Read This First

This is the canonical implementation plan for turning the local Web command
bridge into a replayable agent runtime without breaking its existing clients.
It owns the server-side event schema, append-only journal, cursor API, runtime
instrumentation, compatibility adapters, recovery rules, migration, tests,
rollout, and rollback.

The GitHub issue corresponding to this document should remain short. The issue
defines the outcome and acceptance boundary; this file owns file-level work and
execution order. During implementation, keep the progress checklist in section
18 current so a new agent can resume without reconstructing the design.

Read these first:

- `Constitution.md`
- `docs/R1_COMMAND_BRIDGE_INVENTORY.md`
- `docs/P1_ENGINEERING_HARDENING_IMPLEMENTATION_PLAN.md`, especially R1
- `src/openclaw_adapter/command_bridge_models.py`
- `src/openclaw_adapter/command_bridge_conversation.py`
- `src/openclaw_adapter/session_memory.py`
- `src/openclaw_adapter/job_store.py`
- `src/openclaw_adapter/command_bridge.py`
- `src/openclaw_adapter/command_bridge_planner.py`
- `src/openclaw_adapter/command_bridge_executor.py`
- `src/openclaw_adapter/command_bridge_server.py`
- Web companion plan before changing the consumer contract

## 1. Current Reality

The bridge already has several strong pieces:

1. `SessionMemoryStore` atomically persists one bounded
   `default_session.json` snapshot.
2. `ConversationSession` serializes snapshot access and preserves an orphaned
   final answer when a streaming client disconnects.
3. long Web jobs have an in-memory registry plus persisted `JobStore` snapshots;
4. `/api/command/stream` emits versioned NDJSON;
5. `/api/command/async`, `/poll`, and `/cancel` make long research and goal work
   recoverable across mobile connection loss;
6. issue #74 separated planner, executor, satisfaction judge, conversation,
   capability, workflow, home, and music responsibilities behind the bridge
   facade.

The remaining structural gap is that the saved session is a replace-in-place
UI snapshot rather than a canonical history. The bridge cannot currently answer
"which events occurred after sequence N?", rebuild the UI from first principles,
or inject a background completion into a durable conversation timeline without
editing the entire snapshot.

The current transport shapes also describe the same run differently:

| Path | Current representation |
|---|---|
| `POST /api/command` | one terminal response object |
| `POST /api/command/stream` | NDJSON `start/delta/process/job/done/error` |
| `POST /api/command/async` | accepted + job id |
| `GET /api/command/poll` | mutable job snapshot |
| `GET/POST /api/command/session` | mutable UI snapshot |

These endpoints must not be removed in this program. They become compatibility
views over one internal lifecycle event model.

## 2. Program Outcome

After completion:

1. every Web-visible turn/run has a stable `session_id`, `run_id`, and ordered
   event sequence;
2. the bridge appends lifecycle events to a durable JSONL journal before they
   are considered observable;
3. a client can request `events after cursor` and receive an exact, duplicate-
   free continuation;
4. live NDJSON emits the same typed events that the journal stores;
5. current HTTP response shapes remain available during migration;
6. background jobs append their terminal result to the session even when no Web
   client is connected;
7. the existing session snapshot becomes a bounded projection/cache and can be
   rebuilt from the journal;
8. private model reasoning is neither stored nor emitted; only structured,
   user-facing progress is journaled;
9. bridge restart preserves completed history and classifies truly interrupted
   work explicitly;
10. the implementation is single-user/local-first without introducing a relay,
    cloud database, WebSocket requirement, or multi-tenant auth system.

## 3. Explicit Non-Goals

- No removal or incompatible rewrite of current command endpoints.
- No ACP implementation or dependency on Grok Build.
- No WebSocket relay; NDJSON plus bounded cursor reads is sufficient.
- No multi-user ordering, distributed consensus, or cross-device concurrent
  editing guarantee.
- No persistence of chain-of-thought, hidden prompts, raw credentials, generated
  secret material, or full tool stdout by default.
- No unbounded retention.
- No conversion of all Telegram/background-daemon events into Web session events.
- No multi-session dashboard in this slice.
- No context compaction in this slice; see
  `WEB_CONVERSATION_COMPACTION_IMPLEMENTATION_PLAN.md`.
- No permission pause/resume mechanism in this slice; reserve event variants and
  implement it under `WEB_DYNAMIC_TOOL_APPROVAL_IMPLEMENTATION_PLAN.md`.

## 4. Architectural Invariants

### 4.1 Journal before fan-out

For durable events, append and fsync/flush according to the durability policy
before sending the event to the live client or updating the snapshot projection.
A client must never observe a durable event that cannot be replayed after a
process restart.

High-frequency text deltas are the exception: they may be transport-only and
coalesced into bounded checkpoints. Section 7 defines this split.

### 4.2 One session writer

`SessionEventJournal` owns event sequence allocation and append locking. No
planner, executor, HTTP handler, or job worker writes JSONL directly.

### 4.3 Cursor is server-issued

Clients store and echo the latest server sequence. They do not derive ordering
from timestamps, array position, message count, or job progress length.

### 4.4 Terminal state is monotonic

Once a run is `completed`, `failed`, `cancelled`, or `interrupted`, late worker
output cannot return it to `running` or replace its terminal outcome.

### 4.5 Projection is disposable

`default_session.json` is a fast-load compatibility projection. The event
journal is authoritative. Projection corruption must not corrupt the journal.

### 4.6 User-visible progress, not private reasoning

Events may describe actions such as "searching three sources" or "validation
failed". They must not contain hidden reasoning traces or model chain-of-thought.

## 5. Event Envelope Contract

Add a versioned DTO in a focused module, tentatively
`src/openclaw_adapter/session_events.py`.

```python
@dataclass(frozen=True)
class SessionRunEvent:
    event_version: int
    event_id: str
    session_id: str
    run_id: str
    seq: int
    occurred_at: float
    type: str
    visibility: str
    payload: dict[str, object]
```

Wire shape:

```json
{
  "event_version": 1,
  "event_id": "01J...",
  "session_id": "web-default",
  "run_id": "01J...",
  "seq": 42,
  "occurred_at": 1784250000.125,
  "type": "tool.progress",
  "visibility": "user",
  "payload": {
    "stage": "seller_reputation",
    "label": "檢查賣家信譽",
    "completed": 3,
    "total": 5
  }
}
```

Required envelope rules:

- `event_version` is an integer and starts at `1`.
- `event_id` is globally unique enough for diagnostics and client dedup.
- `seq` is strictly increasing within one session.
- `run_id` is stable from acceptance through terminal outcome.
- `type` is a closed protocol value validated at the boundary.
- unknown future event types are retained/ignored safely by older projections.
- `payload` size is bounded before append.
- strings are normalized to UTF-8 JSON and capped by field policy.
- `occurred_at` is informational; it never determines ordering.

### 5.1 Initial event vocabulary

| Event | Durable | Purpose |
|---|---:|---|
| `session.created` | yes | establish session metadata |
| `user.message` | yes | accepted visible user input |
| `run.accepted` | yes | stable run id assigned |
| `run.started` | yes | worker entered execution |
| `planner.completed` | yes | bounded plan summary and selected route |
| `tool.started` | yes | named user-visible step began |
| `tool.progress` | yes/coalesced | bounded progress checkpoint |
| `tool.completed` | yes | step outcome and artifact references |
| `judge.completed` | yes | satisfied/unsatisfied outcome and reason code |
| `assistant.delta` | transport-only by default | live answer chunk |
| `assistant.message` | yes | final/partial visible assistant message |
| `run.completed` | yes | successful terminal state |
| `run.failed` | yes | failed terminal state with safe category |
| `run.cancel_requested` | yes | user requested cooperative cancellation |
| `run.cancelled` | yes | cancellation became terminal |
| `run.interrupted` | yes | process restart/lost worker |
| `approval.requested` | reserved | implemented by approval plan |
| `approval.resolved` | reserved | implemented by approval plan |
| `context.checkpoint` | reserved | implemented by compaction plan |
| `queue.changed` | reserved | implemented by prompt queue plan |

### 5.2 Payload policy

Payloads use semantic fields, not prose blobs, whenever the information is used
by UI logic. Human labels remain allowed for display.

Do store:

- safe stage identifiers;
- user-facing labels;
- bounded counts and percentages;
- route/tool identifiers already allowed in the UI;
- artifact references;
- safe error category and display message;
- model metadata already covered by the current response contract.

Do not store by default:

- API keys, cookies, authorization headers;
- complete shell/Python source from generated tools;
- raw model system prompts;
- raw private reasoning;
- unlimited scraper output or HTML;
- local absolute paths unless reduced to an approved display reference;
- voice/audio bytes or image base64 payloads.

## 6. Storage Layout And Retention

Default layout under a gitignored runtime path:

```text
.openclaw_tmp/web_sessions/
└── web-default/
    ├── metadata.json
    ├── updates-000001.jsonl
    ├── projection.json
    ├── cursor.json
    └── quarantine/
```

Start with one segment if simpler, but design the filename/version boundary so
rotation can be added without rewriting the wire contract.

Configuration in `AssistantSettings`:

```text
OPENCLAW_WEB_EVENT_DIR
OPENCLAW_WEB_EVENT_MAX_BYTES
OPENCLAW_WEB_EVENT_MAX_AGE_DAYS
OPENCLAW_WEB_EVENT_MAX_PAYLOAD_BYTES
```

Defaults should remain local, bounded, and safe for a single operator. Suggested
initial policy:

- 30-day event retention;
- 25 MiB maximum active session journal;
- 64 KiB maximum durable payload;
- coalesce text/progress before the cap rather than silently truncating JSON;
- retain the most recent complete run boundary when trimming;
- atomic metadata/projection replacement;
- append-only journal segments are never edited in place.

Retention must not split a run in a way that makes its terminal result
uninterpretable. Rotate/expire complete segments or rewrite into a new compacted
segment under an explicit maintenance operation; never mutate the active file.

## 7. Delta And Progress Durability

Persisting every token would create excessive writes and noisy replay. Use two
levels:

1. `assistant.delta`: live transport event, not individually journaled.
2. `assistant.message`: durable final/partial answer checkpoint.

For long-running progress:

- journal a stage transition immediately;
- journal count/percent changes at a bounded cadence, e.g. at most once per
  500 ms per run unless the stage changes;
- retain the latest durable checkpoint for each stage;
- do not derive progress from a mutable string list;
- when a stream disconnects, append a bounded partial `assistant.message` only
  if it is meaningful and not superseded by a job-backed continuation.

The Web may animate live deltas, but after reload it restores the last durable
message plus subsequent events.

## 8. Cursor API

Add a read-only endpoint without removing existing routes:

```text
GET /api/command/events?session_id=web-default&after=41&limit=500
```

Response:

```json
{
  "status": "ok",
  "event_version": 1,
  "session_id": "web-default",
  "events": [],
  "server_cursor": 57,
  "has_more": false
}
```

Rules:

- missing `after` means bootstrap from retained history;
- negative/non-integer cursors return 400;
- `limit` has a server cap;
- `server_cursor` is the greatest sequence included/known according to the
  chosen contract; define and test this exactly;
- pagination must never skip or duplicate events;
- a cursor older than retention returns a typed `cursor_expired` response with
  the current projection/bootstrap path, not an empty false-success;
- reads tolerate a partial final JSONL line caused by process death by
  quarantining/ignoring only the incomplete tail;
- malformed committed lines fail visibly and do not silently reorder later
  events.

Optional later optimization: ETag or long-poll. It is not required for v1.

## 9. Compatibility Adapters

### 9.1 Blocking response

`POST /api/command` executes through a `RunRecorder` and returns the same
`CommandResponse`. The event journal records the accepted user message, run
lifecycle, and final assistant message.

### 9.2 Streaming response

During migration the stream may include both:

- existing event names required by the current Web client;
- new lifecycle envelope under a distinct wrapper or negotiated contract.

Prefer explicit capability negotiation over guessing by User-Agent. Example:

```text
X-OpenClaw-Event-Version: 1
```

or a request field already governed by the versioned DTO. Do not change current
stream framing until characterization tests pin both paths.

### 9.3 Async/poll

`JobStore` remains the worker recovery snapshot. Each job also has a `run_id`.
Poll responses remain available, but their state is derived consistently from
the same terminal transition rules used by event projection.

### 9.4 Session snapshot

`GET/POST/DELETE /api/command/session` remain compatible for the current Web.
During migration:

- GET returns the current projection;
- POST may update display preferences and import legacy message state once;
- DELETE clears the selected session through a journal-aware operation;
- arbitrary client snapshots must not overwrite authoritative run history.

## 10. Runtime Instrumentation Boundaries

Do not teach every service about JSONL. Inject a narrow recorder/callback.

Suggested interfaces:

```python
class RunEventSink(Protocol):
    def emit(self, event_type: str, payload: Mapping[str, object], *,
             durable: bool = True, visibility: str = "user") -> None: ...

class RunRecorder:
    def accepted(...): ...
    def started(...): ...
    def tool_started(...): ...
    def progress(...): ...
    def assistant_message(...): ...
    def terminal(...): ...
```

Instrumentation ownership:

| Layer | Emits |
|---|---|
| `CommandBridge` facade | accepted user message, run allocation, final adapter result |
| `ChatToolPlanner` | `planner.completed` only after strict plan validation |
| `ChatToolExecutor` | tool started/completed, judge outcome |
| goal loop | bounded step lifecycle and terminal result |
| async research worker | run started/progress/terminal |
| HTTP server | transport diagnostics only, not domain lifecycle |

Avoid duplicate emissions from facade and collaborator. Assign one owner for
each transition and test the exact sequence.

## 11. Background Completion Injection

When an async run completes with no connected client:

1. persist the job terminal snapshot;
2. append `assistant.message` with the authoritative final answer;
3. append the terminal run event;
4. update the session projection;
5. a later Web cursor fetch receives both exactly once.

If a live stream is still connected, it receives the same event IDs. The Web
deduplicates by `event_id`/`seq`; the server must not create one event for the
stream and a second semantically duplicate event for the journal.

Cancellation wins over a late worker result. The terminal compare-and-set must
be centralized so both job snapshot and event journal agree.

Bridge restart handling:

- persisted `completed/failed/cancelled` remains terminal;
- persisted `running` with no recoverable worker becomes `interrupted` once;
- repeated startup does not append repeated interruption events;
- a recoverable future worker may advertise a resume token, but v1 need not
  resume Python threads across process death.

## 12. Projection Model

Add a deterministic pure projector:

```python
def project_session(events: Iterable[SessionRunEvent]) -> SessionProjection:
    ...
```

Projection includes:

- visible messages;
- run summaries and current statuses;
- latest durable progress per stage;
- display preferences imported from legacy snapshot;
- latest cursor;
- active recoverable run IDs.

Projection properties:

- same ordered events produce byte-equivalent normalized projection;
- applying event N twice is a no-op or rejected as duplicate;
- unknown future event types do not corrupt known state;
- timestamp changes do not alter order/winner;
- terminal transitions cannot regress;
- a user message and assistant message are distinct from process/progress
  events;
- legacy snapshots import through an explicit migration event or marker.

## 13. Migration And Versioning

### 13.1 First-start migration

If `default_session.json` exists and no event journal exists:

1. load and normalize it with existing `SessionMemoryStore` rules;
2. write a `session.created` event;
3. append one legacy-import event or reconstructed message events tagged
   `evidence: legacy_snapshot`;
4. build the first projection;
5. record migration metadata atomically;
6. leave the old file intact until verification succeeds.

Do not infer historical run/tool stages that the snapshot never recorded.

### 13.2 Schema upgrades

- readers support event version 1 and reject unsupported required versions
  visibly;
- additive payload fields are tolerated;
- type renames require a migration adapter, not silent reinterpretation;
- projection schema version is separate from event schema version;
- migration tests keep golden v1 fixtures.

### 13.3 Downgrade

The compatibility snapshot must remain readable by the current Web throughout
the rollout. If new code is rolled back, historical event files remain ignored
but intact; the old Web can still load `default_session.json`.

## 14. Concurrency And Failure Semantics

Required cases:

- two HTTP requests arrive close together for the same session;
- a live stream and async worker append concurrently;
- client disconnects between append and socket write;
- process dies during JSONL append;
- projection write fails after journal append;
- disk full or permission error;
- retention rotates while a read occurs;
- cancel races final completion;
- legacy session POST races background terminal injection;
- duplicate request retry uses the same request/run id where idempotency is
  supported.

Policy:

- journal append lock serializes sequence assignment;
- projection update can be retried/rebuilt after failure;
- journal write failure prevents claiming durable success;
- transport failure does not roll back committed events;
- all terminal races resolve through one compare-and-set helper;
- errors are structured and visible; no empty-success fallback.

## 15. File-Level Implementation Plan

Expected new files:

- `src/openclaw_adapter/session_events.py`
  - DTOs, validators, event vocabulary, safe serialization.
- `src/openclaw_adapter/session_event_journal.py`
  - append, cursor reads, retention/rotation, tail recovery.
- `src/openclaw_adapter/session_projection.py`
  - deterministic pure reducer and projection serialization.
- `src/openclaw_adapter/run_recorder.py`
  - lifecycle facade, terminal monotonicity, progress coalescing.
- `tests/test_session_events.py`
- `tests/test_session_event_journal.py`
- `tests/test_session_projection.py`
- `tests/test_command_bridge_event_contract.py`

Expected changed files:

- `src/assistant_runtime/settings.py`
- `.env.example`
- `src/openclaw_adapter/command_bridge_conversation.py`
- `src/openclaw_adapter/session_memory.py`
- `src/openclaw_adapter/command_bridge_models.py`
- `src/openclaw_adapter/command_bridge_planner.py`
- `src/openclaw_adapter/command_bridge_executor.py`
- `src/openclaw_adapter/command_bridge.py`
- `src/openclaw_adapter/command_bridge_server.py`
- `src/openclaw_adapter/job_store.py`
- relevant HTTP/SSE/session/job tests
- `docs/R1_COMMAND_BRIDGE_INVENTORY.md`
- system truth docs only when behavior actually ships

Do not grow `command_bridge.py` with storage details. It should compose the
recorder/journal collaborator and retain thin compatibility methods.

## 16. Delivery Slices

### PR E1 — DTO, journal, and projector in shadow mode

- add event DTO/validation;
- add append/cursor storage;
- add pure projector;
- import legacy fixture tests;
- no production endpoint behavior change;
- optional disabled-by-default shadow recorder.

Exit gate: deterministic tests cover corruption, truncation, duplicate, cursor,
retention, terminal monotonicity, and migration.

### PR E2 — run recorder and blocking path

- allocate stable runs;
- instrument blocking command path;
- write compatibility projection;
- compare old response/snapshot with projected result.

Exit gate: existing command/session contract tests unchanged and new replay
projection matches visible result.

### PR E3 — streaming path and cursor endpoint

- add event-version negotiation;
- expose `GET /events`;
- journal durable stream checkpoints;
- maintain current NDJSON contract for old client.

Exit gate: disconnect at every event boundary, reconnect by cursor, and compare
with uninterrupted event set.

### PR E4 — async jobs and background injection

- assign job/run relationship;
- centralize terminal compare-and-set;
- inject final message and terminal event;
- rebuild interrupted state exactly once after restart.

Exit gate: screen lock, stream loss, Web absence, cancellation race, and bridge
restart scenarios all converge.

### PR E5 — collaborator instrumentation

- planner/executor/judge typed events;
- goal/research structured stages;
- replace user-facing free-form process strings where possible;
- no private reasoning event.

Exit gate: sequence tests pin accepted → plan → tool → judge → message → terminal.

### PR E6 — enable journal as authority

- switch GET session projection to journal-derived source;
- retain old snapshot as compatibility cache;
- enable retention and migration;
- update docs and live verification playbook.

Exit gate: deleting only the projection and restarting rebuilds the same visible
session from journal.

## 17. Verification Matrix

### Unit

- envelope validation and payload caps;
- strict sequence allocation under threads;
- cursor pagination: empty, exact boundary, multiple pages, expired cursor;
- append tail truncation and corruption handling;
- projector determinism and idempotency;
- terminal state monotonicity;
- progress coalescing;
- legacy snapshot import;
- retention at run boundaries;
- redaction/private-field rejection.

### Contract

- existing blocking response contract;
- existing NDJSON framing and envelope version;
- existing async start/poll/cancel contract;
- existing GET/POST/DELETE session contract;
- new event cursor golden JSON;
- unknown additive fields;
- old Web fixture against new bridge.

### Integration

- uninterrupted streaming vs reconnect-after-every-event produces the same
  durable event set and final projection;
- async job completes while Web is closed and appears after reopen;
- cancel vs completion race produces one terminal result;
- bridge restarts during running job and appends one interruption;
- corrupt projection rebuilds from valid journal;
- corrupt journal line produces explicit degraded/error response;
- concurrent runs do not share run IDs or progress.

### Live

Documentation-only planning does not require restart. When implementation lands:

1. run full targeted and repository test gates;
2. ask the user to trigger the supported `/restartall` flow per `CLAUDE.md`;
3. verify the bridge listener and Telegram polling health;
4. send one ordinary chat turn, one tool-routed turn, and one long job;
5. lock/close the phone during the long job;
6. reopen and prove cursor replay returns the final result exactly once;
7. cancel a separate run and prove it cannot resurrect as done;
8. show the actual event/projection output with sensitive payloads redacted.

Suggested focused test command names should be finalized with implementation,
then recorded in `docs/VERIFICATION_MATRIX.md`.

## 18. Progress / Handoff Checklist

E1 foundation is complete; it is deliberately not wired into request paths yet.
The first unchecked item is E2.1 (`RunRecorder`). Verify E1 with:
`\.venv/bin/python -m pytest -q tests/test_session_events.py
tests/test_session_event_journal.py tests/test_session_projection.py
tests/test_settings.py tests/test_session_memory.py`.

### E1 — foundation

- [x] E1.1 define event vocabulary and golden wire fixtures.
- [x] E1.2 implement bounded serializer/validator.
- [x] E1.3 implement append-only journal and sequence lock.
- [x] E1.4 implement cursor reads and pagination.
- [x] E1.5 implement tail recovery and explicit corruption handling.
- [x] E1.6 implement deterministic projector.
- [x] E1.7 implement legacy snapshot migration fixtures.

### E2 — blocking compatibility

- [x] E2.1 implement `RunRecorder`.
- [x] E2.2 instrument blocking command path.
- [x] E2.3 compare journal projection with legacy snapshot behavior.
- [x] E2.4 add compatibility and concurrency tests.

### E3 — stream/cursor

- [x] E3.1 define event negotiation.
- [x] E3.2 add cursor endpoint and golden contract.
- [x] E3.3 instrument durable stream checkpoints.
- [x] E3.4 add disconnect-at-boundary convergence harness.

### E4 — jobs

- [x] E4.1 bind jobs to runs.
- [x] E4.2 centralize terminal compare-and-set.
- [x] E4.3 inject background completion into session events.
- [x] E4.4 add restart and cancellation race tests.

### E5 — lifecycle detail

- [x] E5.1 planner event.
- [x] E5.2 executor tool events.
- [x] E5.3 satisfaction judge event.
- [x] E5.4 goal/research progress stages.
- [x] E5.5 audit visibility and private reasoning exclusion.

### E6 — authority/rollout

- [x] E6.1 rebuild compatibility snapshot from journal.
- [x] E6.2 enable migration and bounded retention.
- [x] E6.3 update system truth and verification docs.
- [ ] E6.4 restart and complete live recovery proof.

## 19. Rollback Strategy

- Feature flag event recording and event-backed projection separately.
- Keep existing snapshot writes during shadow and compatibility phases.
- A rollback disables readers/adapters but never deletes journal data.
- Do not downgrade by rewriting versioned events.
- If event recording fails in shadow mode, log and expose diagnostics without
  changing user-visible success; once journal authority is enabled, durable
  write failure must fail visibly rather than claim recoverable success.
- Roll back one delivery slice at a time; do not revert unrelated #74
  decomposition.

## 20. Decisions And Ruled-Out Alternatives

### JSONL instead of SQLite as the initial authority

The workload is append/replay-first, single-writer, local, and debug-oriented.
JSONL is sufficient and mirrors the operational value observed in Grok Build.
If indexed multi-session search becomes necessary, add a rebuildable SQLite
index later; do not make it the first source of truth.

### NDJSON + cursor reads instead of WebSocket

Existing infrastructure already supports NDJSON and private LAN/Meshnet access.
Adding a relay or WebSocket stack would create a new reconnect/security surface
without solving a current requirement.

### Internal normalization instead of endpoint replacement

Issue #74 deliberately stabilized public contracts. Reusing one event model
internally provides the architectural benefit without a big-bang consumer
migration.

### Single active session now, session-aware schema always

The UI remains single-operator and single-active-session. Stable `session_id`
prevents a future migration trap and allows tests to prove isolation, but no
dashboard or account model is introduced.

## 21. Final Exit Gate

This program is complete only when all of the following are true:

- the authoritative event journal is enabled and bounded;
- the current Web can reconnect by cursor after arbitrary stream loss;
- background completion appears after reopen without duplicate messages;
- projection deletion/rebuild is proven;
- old HTTP/NDJSON/poll/session contracts remain compatible;
- terminal state races are deterministic;
- private reasoning and secrets are excluded by tests;
- required test suites pass;
- supported live restart/recovery validation passes;
- this checklist, GitHub issue, system map, current state, and verification
  matrix agree on what shipped and what remains.
