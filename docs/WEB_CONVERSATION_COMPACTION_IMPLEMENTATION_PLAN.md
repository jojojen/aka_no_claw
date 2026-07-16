# Web Conversation Compaction Implementation Plan

Last reviewed: 2026-07-17
Status: Planned
Owner area: command-bridge / model-context / memory
Tracking issue: [`aka_no_claw#87`](https://github.com/jojojen/aka_no_claw/issues/87)
Depends on: `WEB_SESSION_RUN_EVENT_SPINE_IMPLEMENTATION_PLAN.md`
Web consumer: `jojojen/aka_no_claw_web/docs/AGENT_CONTROL_PLANE_IMPLEMENTATION_PLAN.md`

## Read This First

This plan defines how long Web conversations remain useful when model context is
bounded. Compaction is a model-input optimization, not history deletion. The
append-only session journal remains authoritative; compaction creates a
versioned checkpoint describing older context that may replace raw turns only
when assembling a model request.

Read:

- event spine plan;
- `src/openclaw_adapter/command_bridge_conversation.py`;
- `src/openclaw_adapter/command_bridge_planner.py`;
- current conversation-context builders in `command_bridge.py`;
- provider/model route settings;
- knowledge/memory controls and Web clear-memory behavior;
- Grok-inspired distinction between session history and compacted model context.

## 1. Problem

The Web sends recent visible history and the bridge adds bounded conversation
context/tool ledger. This is adequate for short sessions but eventually forces
one of three bad outcomes:

- silently trim important earlier constraints;
- exceed a local/cloud model context budget;
- mix user-visible history, tool evidence, and long progress narration without
  a documented retention policy.

The local qwen model has a smaller practical context budget than some cloud
providers. A provider-independent compaction checkpoint prevents long sessions
from degrading unpredictably while keeping the full audit/replay history.

## 2. Outcome

1. context assembly estimates the budget of each component;
2. compaction triggers before a configurable threshold, not after provider
   failure;
3. a checkpoint summarizes only a specific closed event range;
4. raw journal events remain untouched until normal retention expiry;
5. checkpoint provenance records source cursor range, model, prompt version,
   and validation result;
6. recent turns and unresolved/pinned facts remain verbatim;
7. tool outputs are reduced to grounded facts and artifact references, not
   invented narrative;
8. failure to compact is explicit and falls back to a safe bounded context;
9. users can inspect and clear compacted memory separately from conversation
   history;
10. provider switching does not reinterpret an incompatible checkpoint silently.

## 3. Non-Goals

- No deletion/rewrite of the authoritative event journal.
- No permanent knowledge-base promotion from a session summary.
- No vector database requirement.
- No autonomous extraction of sensitive secrets into memory.
- No chain-of-thought summary.
- No guarantee that every old conversational nuance survives compaction.
- No compaction during an unresolved approval, capture flow, or active mutable
  plan boundary unless those states are preserved explicitly.
- No one-size-fits-all token count claimed to be exact across providers.

## 4. Context Layers

Assemble model input from explicit layers:

```text
system/runtime policy                    never compacted here
active mode/tool contract                current, bounded
pinned user facts/preferences            explicit and inspectable
latest valid compaction checkpoint       older session range
recent verbatim user/assistant turns     sliding tail
active run/plan/tool state               always current
bounded tool ledger/evidence refs        structured
current user input                       never omitted
```

Progress events and UI-only process narration are excluded from model history
unless a structured outcome is needed. Model metadata and transport diagnostics
do not enter user context.

## 5. Checkpoint Contract

```python
@dataclass(frozen=True)
class ContextCheckpoint:
    checkpoint_version: int
    checkpoint_id: str
    session_id: str
    source_seq_start: int
    source_seq_end: int
    created_at: float
    model_provider: str
    model_id: str
    prompt_version: str
    summary: str
    pinned_facts: tuple[GroundedFact, ...]
    unresolved_items: tuple[UnresolvedItem, ...]
    artifact_refs: tuple[str, ...]
    validation_status: str
    previous_checkpoint_id: str | None
```

Grounded facts include source event IDs or artifact references. Do not store a
fact solely because the summarizer asserted it.

`context.checkpoint` event payload contains checkpoint metadata and a bounded
summary/reference; large internal validation details stay in the checkpoint
store.

## 6. Trigger Policy

Use a configurable budget estimator with model-route metadata.

Suggested trigger inputs:

- estimated input tokens by layer;
- model context-window setting;
- reserved output/tool-call budget;
- percentage threshold, initially 75-85% after reserve;
- minimum number of eligible old turns;
- cooldown since last checkpoint;
- no currently unresolved safety/capture boundary.

Trigger options:

- automatic before a request that would cross threshold;
- explicit Web action `壓縮對話內容`;
- optional maintenance after a run completes.

Never repeatedly compact an unchanged range. `source_seq_end` and prompt version
form part of idempotency.

## 7. Eligible Range And Preservation Rules

Only compact a closed prefix ending before the recent verbatim tail.

Always preserve outside the summary:

- current user input;
- latest N user/assistant turns or token-equivalent tail;
- active run and queued prompt state;
- unresolved approvals;
- active workflow/schedule capture context;
- user-pinned facts/preferences;
- explicit corrections and negative feedback relevant to current context;
- references required to interpret current artifacts/actions.

Exclude from summary input:

- heartbeat/delta transport noise;
- hidden/private reasoning;
- secrets and raw authentication material;
- duplicate progress checkpoints superseded by a later stage event;
- raw base64/audio/image bytes;
- full generated source unless the user explicitly asks about it and policy
  permits a bounded excerpt/reference.

## 8. Summarization Output Schema

Require strict JSON from the compactor:

```json
{
  "summary": "...",
  "facts": [
    {
      "text": "使用者偏好日本市場資料",
      "source_event_ids": ["01J..."]
    }
  ],
  "decisions": [],
  "unresolved_items": [],
  "artifact_refs": [],
  "discarded_categories": ["transport_progress"]
}
```

Validation:

- schema and size limits;
- every fact/decision cites retained source events;
- source IDs must fall inside the compacted range;
- reject unsupported references;
- reject secret-like fields based on existing policy;
- preserve explicit negation/corrections;
- cap facts/items;
- if validation fails, do not install checkpoint.

The model is a summarizer, not the authority. Deterministic extraction supplies
event metadata and validates references.

## 9. Provider Strategy

- Default to the configured local model when it can meet the budget/JSON
  contract.
- Allow configured cloud pool only under existing privacy/provider settings.
- Record which provider/model generated the checkpoint.
- Do not silently send local-only content to cloud because local compaction
  failed.
- Provider failure must return an explicit degraded result and use a bounded
  deterministic fallback context.

Deterministic fallback:

- keep recent verbatim turns;
- keep explicit pinned facts and current run state;
- omit the oldest eligible uncheckpointed content with a visible diagnostic;
- never fabricate a summary.

## 10. Checkpoint Chaining

Later compaction may summarize:

- the previous checkpoint summary;
- events after the previous `source_seq_end`;
- while retaining source range/provenance links.

Do not recursively summarize summaries indefinitely without validation. Keep a
bounded chain and periodically regenerate from retained source events while they
remain available. After source retention expires, mark checkpoint evidence as
`summary_only` rather than pretending raw evidence remains.

## 11. API And Web UX

Suggested endpoints:

```text
GET    /api/command/context
POST   /api/command/context/compact
DELETE /api/command/context/checkpoint
```

Status response:

```json
{
  "status": "ok",
  "estimated_tokens": 42000,
  "context_window": 65536,
  "usage_percent": 64,
  "checkpoint": {
    "checkpoint_id": "01J...",
    "source_seq_end": 120,
    "created_at": 1784250000,
    "summary_preview": "..."
  }
}
```

Web behavior:

- settings/session info shows context usage category, not false precision;
- automatic compaction appears as a quiet system notice;
- user can inspect the summary and pinned facts;
- `清除摘要記憶` deletes checkpoint/pins according to explicit scope;
- clearing checkpoint does not delete visible chat history;
- clearing the whole session remains a separate destructive action;
- no raw chain-of-thought view.

## 12. Storage And Retention

Store checkpoints under the session runtime directory with atomic write and
versioned metadata. The journal event references the checkpoint ID.

Bounds:

- maximum checkpoint summary bytes;
- maximum fact/unresolved/artifact entries;
- bounded number of checkpoints;
- retention aligned with session policy;
- projection rebuild tolerates missing expired checkpoint by falling back to
  later valid checkpoint or verbatim tail.

## 13. File-Level Plan

Expected new files:

- `src/openclaw_adapter/context_budget.py`
- `src/openclaw_adapter/context_checkpoint.py`
- `src/openclaw_adapter/context_compactor.py`
- `src/openclaw_adapter/context_projection.py`
- tests for budgets, validation, chaining, provider failure, API

Expected changed files:

- settings and `.env.example`;
- conversation/session runtime composition;
- planner/request context assembly;
- provider route metadata contract if context windows are absent;
- command bridge/server endpoints;
- event vocabulary/projector;
- Web settings/session UI and tests;
- system truth/verification docs when enabled.

## 14. Delivery Slices

### PR C1 — budget accounting and fixtures

- classify existing context layers;
- estimator interface and model metadata;
- expose diagnostics without behavior change.

### PR C2 — checkpoint schema/store/validation

- strict output schema;
- grounded source references;
- atomic store and event metadata;
- fake summarizer tests.

### PR C3 — manual compaction

- explicit endpoint;
- build checkpoint from closed event range;
- install only after validation;
- inspect/delete API.

### PR C4 — context assembly integration

- checkpoint + recent tail + current state;
- deterministic fallback;
- provider/privacy policy;
- regression tests for follow-up quality and corrections.

### PR C5 — automatic trigger and Web UX

- threshold/cooldown;
- notice/status/inspect controls;
- live local/cloud-policy proof;
- documentation.

## 15. Verification Matrix

- exact eligible range and source cursor boundaries;
- no active/unresolved state compacted away;
- strict JSON and citation validation;
- hallucinated source ID rejected;
- explicit user correction wins over older fact;
- negation retained;
- checkpoint idempotency;
- chained checkpoint provenance;
- missing/corrupt checkpoint fallback;
- provider timeout/unavailable and privacy rule;
- local vs cloud context-window settings;
- bounded output and retention;
- delete checkpoint vs delete session semantics;
- old Web compatibility;
- same current query with/without compaction retains required facts in
  discriminating fixtures;
- hidden reasoning/secrets absent from input/output stores.

Live proof when implemented:

1. create a long conversation containing a preference, correction, unresolved
   item, tool artifact, and irrelevant progress;
2. trigger compaction;
3. show the actual checkpoint summary/facts to the user;
4. ask discriminating follow-ups proving preference/correction survived;
5. prove irrelevant progress and secrets did not enter summary;
6. clear checkpoint and show visible history remains;
7. restart through the supported flow and repeat a follow-up.

## 16. Progress / Handoff Checklist

Implementation has not started. First unchecked item: C1.1.

- [ ] C1.1 inventory every current model-context layer and owner.
- [ ] C1.2 define estimator interface and reserve policy.
- [ ] C1.3 add diagnostics/golden fixture without behavior change.
- [ ] C2.1 define checkpoint/output schema.
- [ ] C2.2 implement source-reference validation.
- [ ] C2.3 implement bounded atomic checkpoint store.
- [ ] C2.4 add fake compactor/provider policy tests.
- [ ] C3.1 implement manual compact/status/delete APIs.
- [ ] C3.2 append/project `context.checkpoint`.
- [ ] C3.3 add range/idempotency/failure tests.
- [ ] C4.1 integrate checkpoint into request assembly.
- [ ] C4.2 implement deterministic bounded fallback.
- [ ] C4.3 add discriminating memory/correction tests.
- [ ] C5.1 add auto-trigger threshold and cooldown.
- [ ] C5.2 implement Web usage/inspect/clear UX.
- [ ] C5.3 run live fresh-summary proof and update docs.

## 17. Rollback

- Manual and automatic compaction have separate flags.
- Disable automatic trigger first while retaining checkpoints for inspection.
- Context assembly can ignore checkpoints and revert to current bounded history.
- Never delete journal history during rollback.
- If a checkpoint is suspected corrupt, quarantine it and surface the degraded
  state; do not silently use it.

## 18. Exit Gate

Complete means long Web conversations compact before exceeding configured model
budgets; checkpoints are grounded, versioned, bounded, inspectable, and separate
from authoritative history; recent/active/corrected context survives; provider
or validation failure is explicit and safe; no private reasoning/secrets are
stored; and live discriminating follow-ups prove useful continuity.
