# Web Prompt Queue Implementation Plan

Last reviewed: 2026-07-17
Status: Current — implementation and supported-restart live proof complete
Owner area: command-bridge / conversation-runtime
Tracking issue: [`aka_no_claw#86`](https://github.com/jojojen/aka_no_claw/issues/86)
Depends on: `WEB_SESSION_RUN_EVENT_SPINE_IMPLEMENTATION_PLAN.md`
Web consumer: `jojojen/aka_no_claw_web/docs/AGENT_CONTROL_PLANE_IMPLEMENTATION_PLAN.md`

## Read This First

This is the canonical plan for accepting user input while a Web agent run is
busy, representing queued follow-ups durably, and draining them in deterministic
order. It intentionally implements a small single-operator queue rather than a
multi-agent collaboration system.

Read the event spine plan, current conversation/session modules, command request
models, cancellation contract, and Web `InputBar`/`App.tsx` before editing.

## 1. Problem

The Web currently has one global `generating` state. While a long research or
goal run is active, new input is effectively blocked or risks being routed into
the wrong capture mode. A user who wants to say "只看日本市場" must stop the
run, wait, or lose the intended relationship between the new text and the
running task.

The solution is not to allow arbitrary concurrent turns against the same model
context. The solution is a durable per-session queue with explicit intent:

- `next_turn`: send after the current run reaches terminal state;
- `interjection`: deliver at the next safe orchestration boundary when the
  active run explicitly supports it.

## 2. Outcome

1. Web input remains available while a run is busy.
2. Every queued prompt has stable ID, version, position, owner/session, intent,
   and creation time.
3. Queue mutations are server-authoritative and emitted as `queue.changed`.
4. Refresh/reconnect restores the same queue.
5. A prompt drains at most once and starts a run with a traceable source ID.
6. Users can edit, cancel, and reorder prompts that have not started.
7. Stale edits cannot overwrite a newer queue version.
8. Capture-mode input (workflow/schedule editor) remains isolated from ordinary
   queued chat prompts.
9. Cancellation of the current run does not silently delete queued work.
10. The implementation stays bounded and single-operator.

## 3. Non-Goals

- No concurrent model turns sharing mutable context.
- No multi-user ownership/conflict UI.
- No priority scheduler for arbitrary background daemons.
- No guarantee that every tool can consume mid-run interjections.
- No automatic interpretation of whether text should interrupt; the user or
  explicit UI action selects intent.
- No queue of raw files/audio larger than existing attachment limits.
- No recurring scheduling; home schedules remain a separate subsystem.

## 4. Queue Data Contract

```python
@dataclass(frozen=True)
class QueuedPrompt:
    prompt_id: str
    session_id: str
    version: int
    position: int
    intent: Literal["next_turn", "interjection"]
    mode: str
    capture_context: str | None
    text: str
    attachment_refs: tuple[str, ...]
    input_source: str
    created_at: float
    updated_at: float
```

Do not persist base64 attachment bodies inside queue events. Persist approved,
bounded local attachment references with expiry and ownership checks.

Queue snapshot event:

```json
{
  "type": "queue.changed",
  "payload": {
    "running_prompt_id": "p0",
    "entries": [
      {
        "prompt_id": "p1",
        "version": 2,
        "position": 0,
        "intent": "next_turn",
        "mode": "chat",
        "text": "只看日本市場"
      }
    ]
  }
}
```

For a single operator, queue events may publish the full bounded queue snapshot.
The journal still records mutations or snapshots with monotonic session seq so
replay is deterministic.

## 5. State Machine

Prompt states:

```text
queued → draining → started → completed
   │         │
   ├→ cancelled
   └→ expired
```

Only `queued` entries can be edited/reordered. `draining` is an internal
compare-and-set state that prevents two drain loops from starting the same
prompt. Once `started`, the run/event history is authoritative.

Queue/session state:

```text
idle + queue nonempty → atomically claim first eligible prompt → start run
busy + next_turn      → hold
busy + interjection   → deliver only at declared safe boundary
terminal run          → drain next prompt
bridge restart        → reset orphan draining entries to queued or interrupted
```

## 6. API

Suggested endpoints:

```text
GET    /api/command/queue?session_id=web-default
POST   /api/command/queue
PATCH  /api/command/queue/<prompt_id>
DELETE /api/command/queue/<prompt_id>
POST   /api/command/queue/reorder
```

Create request includes the same validated command payload fields required to
reconstruct a later turn, plus `intent`. The server assigns ID/version/position.

Edit request must include `expected_version`. A stale version returns conflict
with the current queue snapshot. Reorder carries all queued IDs and expected
versions, rejecting missing/duplicate/running IDs.

An optional future endpoint may promote one prompt to "send now" by cancelling
the active run first, but v1 should not combine two state changes behind one
ambiguous button.

## 7. Ordering And Drain Rules

- Default order is server-assigned FIFO position.
- Reorder is explicit and atomic.
- Timestamps never determine the winner.
- At most one foreground run drains per session.
- A drain transaction records `running_prompt_id` before execution begins.
- A prompt-start event references `source_prompt_id`.
- Terminal run handling triggers drain through one owner, not every observer.
- Repeated terminal callbacks are idempotent.
- If start fails before a run is accepted, return prompt to queued with bounded
  retry metadata or mark failed visibly; do not silently drop it.
- If a goal ends before consuming its queued interjection, safely demote that
  text to `next_turn`; never inject it into a later run or strand it on the
  terminated run ID.
- Apply a maximum queue length and text/attachment budget.

Suggested initial limits:

- 20 queued prompts;
- existing command text limit;
- existing attachment count/byte caps;
- 24-hour expiry for stale queued prompts;
- no automatic retry loop beyond one transient start retry.

## 8. Interjection Contract

`interjection` is not a second concurrent chat turn. It is a typed input to the
currently running orchestration.

Each run advertises whether it supports:

```json
{
  "interjection": {
    "supported": true,
    "safe_boundaries": ["between_goal_steps"],
    "accepted_kinds": ["constraint", "clarification"]
  }
}
```

V1 may support interjection only for the goal loop. Research and simple model
streaming can queue as `next_turn` until safe semantics exist.

At a safe boundary:

1. atomically claim the earliest interjection;
2. append a durable event linking it to the active run;
3. update the bounded goal context/constraints;
4. mark the queue item started/consumed;
5. continue with a visible acknowledgement.

Never inject text in the middle of generated Python execution or an external
side effect.

## 9. Capture Mode Isolation

Current workflow and schedule editors route the next plain text to specialized
endpoints. Queueing must preserve that routing context explicitly.

Rules:

- text entered while a capture card is active receives a `capture_context` ID;
- it can only drain back into the same live editor context;
- if the editor closes/expires before drain, mark the item expired and ask the
  user to resubmit; do not reinterpret it as ordinary chat;
- ordinary `next_turn` messages cannot be stolen by workflow/schedule capture;
- sentinel IDs are not used; the Web control-plane plan replaces them with
  typed run/card context.

## 10. Persistence

Implement a small `PromptQueueStore` under the session runtime directory. It may
use an atomic JSON snapshot initially because the authoritative mutation trail
is already present in the session event journal.

The queue store owns:

- current entries and versions;
- running prompt claim;
- atomic create/edit/delete/reorder/claim/complete;
- restart reconciliation;
- bounds/expiry.

Do not place the queue in `SessionMemoryStore`'s client-replaceable snapshot.

## 11. Web UX

Composer behavior:

- remains enabled while running;
- send button opens a lightweight choice only when ambiguity exists:
  `排到下一則` / `補充目前任務`;
- if active run does not support interjection, send defaults to next turn and
  explains this once;
- queued items appear as compact chips/rows above the composer;
- each row supports edit and cancel; reorder may be drag or simple up/down;
- display position and running association;
- preserve draft text when switching intent;
- no dashboard-heavy sidebar.

Accessibility/mobile:

- 44px tap targets;
- no hover-only affordance;
- queue labels do not wrap into ambiguous two-line controls;
- status announced via an accessible live region without reading every token.

## 12. File-Level Plan

Expected backend files:

- `src/openclaw_adapter/prompt_queue.py`
- `src/openclaw_adapter/prompt_queue_store.py`
- queue request/response DTO additions
- `command_bridge_server.py` routes
- `command_bridge.py` drain integration
- goal-loop safe-boundary integration
- event vocabulary/projector additions
- focused tests for store, API, drain, capture isolation, restart

Expected Web work is detailed in the companion plan:

- queue DTO/client methods;
- `usePromptQueue`;
- composer behavior;
- queue strip/editor;
- reconnect and stale-version tests.

## 13. Delivery Slices

### PR Q1 — DTO/store/mutations

- queue model and bounds;
- atomic store;
- create/edit/delete/reorder contracts;
- no automatic drain.

### PR Q2 — event/API and restart reconciliation

- queue endpoints;
- `queue.changed` events;
- stale-version conflict;
- recovery of orphan `draining` entries.

### PR Q3 — next-turn drain

- bind prompt to run;
- drain after terminal state;
- retry/failure semantics;
- exact-once and concurrent terminal tests.

### PR Q4 — Web queue UX

- keep composer enabled;
- queue strip and mutation controls;
- reconnect and mobile tests.

### PR Q5 — bounded goal-loop interjection

- advertise capability;
- consume only at safe boundary;
- reject unsupported/expired capture context;
- live proof.

## 14. Verification Matrix

- FIFO exact ordering independent of timestamps;
- create at max queue length;
- duplicate prompt ID/request retry;
- stale edit and stale reorder;
- edit/cancel races drain claim;
- two terminal callbacks drain once;
- start failure does not lose prompt;
- bridge restart with queued/draining/running prompt;
- current-run cancellation retains queue;
- session clear handles queue explicitly;
- unsupported interjection becomes next-turn or typed rejection per request;
- capture context cannot leak into ordinary chat;
- attachment expiry and ownership checks;
- old Web ignores queue events safely;
- queue restoration after reload;
- accessibility and mobile tap behavior.

Live proof after implementation:

1. start a long research/goal run;
2. queue two follow-ups and reorder them;
3. reload the Web and show the same queue;
4. cancel one queued item;
5. let current run finish and prove the expected next prompt starts once;
6. send a goal-loop interjection at a safe boundary;
7. show unsupported interjection does not corrupt a running tool.

## 15. Progress / Handoff Checklist

Implementation and supported-restart live proof are complete.

- [x] Q1.1 characterize current `generating` and capture routing behavior.
- [x] Q1.2 define DTO, bounds, state machine, and golden wire fixtures.
- [x] Q1.3 implement atomic queue store and version conflicts.
- [x] Q2.1 implement queue HTTP contract.
- [x] Q2.2 emit/project `queue.changed`.
- [x] Q2.3 implement restart/expiry reconciliation.
- [x] Q3.1 bind drain claim to run acceptance.
- [x] Q3.2 trigger one drain after terminal transition.
- [x] Q3.3 cover failure/cancel/concurrency exact-once cases.
- [x] Q4.1 implement Web queue client/hook.
- [x] Q4.2 implement composer choice and queue strip.
- [x] Q4.3 add reconnect/mobile/a11y tests.
- [x] Q5.1 define goal-loop safe-boundary contract.
- [x] Q5.2 implement bounded interjection.
- [x] Q5.3 run live queue/interjection proof.

## 16. Rollback

- `OPENCLAW_WEB_PROMPT_QUEUE_ENABLED=0` disables the queue contract without
  executing or deleting stored items.
- The durable store remains inspectable on disk while the feature is disabled.
- Re-enabling recovers orphaned drain claims once at first session access.
- Event history is retained through rollback.

## 17. Exit Gate

Complete means a busy Web session can durably accept, edit, cancel, reorder, and
exactly-once drain bounded follow-ups; supported interjections occur only at
declared safe boundaries; capture modes remain isolated; reconnect/restoration
is deterministic; current command contracts remain compatible; and live mobile
behavior is proven after the supported restart flow.
