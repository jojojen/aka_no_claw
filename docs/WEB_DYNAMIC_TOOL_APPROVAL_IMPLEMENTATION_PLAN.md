# Web Dynamic-Tool Approval Implementation Plan

Last reviewed: 2026-07-17
Status: Current — shipped and locally enabled with `OPENCLAW_WEB_APPROVALS_ENABLED=true`
Owner area: dynamic-tools / command-bridge safety
Tracking issue: [`aka_no_claw#85`](https://github.com/jojojen/aka_no_claw/issues/85)
Depends on: `WEB_SESSION_RUN_EVENT_SPINE_IMPLEMENTATION_PLAN.md` through E3
Web consumer: `jojojen/aka_no_claw_web/docs/AGENT_CONTROL_PLANE_IMPLEMENTATION_PLAN.md`

## Read This First

This is the canonical plan for pausing risky Web-originated actions before side
effects occur, presenting a concrete approval request to the operator, and
resuming or rejecting the exact frozen action. The first protected path is
generated `/new` execution, but the contract must support future workflow,
schedule, device, file, and network actions without hardcoded entity lists.

Read:

- `Constitution.md`
- `docs/R4_DYNAMIC_TOOLS_INVENTORY.md`
- `docs/NEW_DYNAMIC_TOOLS_PROGRESS.md`
- `src/openclaw_adapter/dynamic_tools/service.py`
- `src/openclaw_adapter/dynamic_tools/specification.py`
- `src/openclaw_adapter/dynamic_tools/safety.py`
- `src/openclaw_adapter/generated_tool_catalog.py`
- `src/openclaw_adapter/fix_command.py` for existing token/TTL patterns
- event spine plan and Web control-plane plan

## 1. Problem Statement

The generated-tool pipeline already performs deterministic safety validation,
dependency allowlisting, execution isolation, repair/evaluation, and catalog
bookkeeping. Those controls answer "is this artifact permitted by policy?" They
do not give the Web operator a protocol-level opportunity to answer "do I want
this exact action to run now?"

The distinction matters for `/new` because a model can generate code and the
runtime can execute it in the same request. A generic confirmation after
execution is not a safety boundary. A generic "AI wants permission" dialog is
also insufficient because the approved code, arguments, network scope, and
filesystem scope could drift before execution.

## 2. Outcome

After completion:

1. policy determines whether an action is auto-allowed, denied, or requires
   interactive approval;
2. required approval pauses before the first protected side effect;
3. the bridge emits durable `approval.requested` and `approval.resolved` events;
4. Web shows a bounded, human-readable impact summary and approve/reject controls;
5. approval is bound cryptographically to the exact frozen action manifest;
6. approve-once resumes only that action and cannot authorize modified code;
7. rejection, expiry, disconnect, bridge restart, or hash mismatch fail closed;
8. current deterministic safety validation remains mandatory and cannot be
   bypassed by user approval;
9. low-risk, allowlisted read-only actions retain a low-friction path;
10. every decision is auditable without storing secrets or raw private data.

## 3. Non-Goals

- No role-based access control or multi-user identity system.
- No blanket "always approve everything" mode.
- No replacement of existing static safety validators.
- No approval for harmless chat generation or local read-only model calls.
- No approval based only on free-form model claims.
- No display of full generated source on the default mobile card.
- No persistent approval for arbitrary future code hashes.
- No remote/public approval relay.
- No hardcoded open-world list of domains, products, devices, or intents.

## 4. Authorization Pipeline

The decision order is fixed:

```text
request accepted
  → planner selects candidate action
  → code/tool artifact generated or resolved
  → deterministic safety validation
      deny → terminal denied result
      pass → compute frozen action manifest
  → risk policy
      auto_allow → execute
      deny → terminal denied result
      ask → persist approval.requested and pause
  → operator decision
      reject/expire/mismatch → terminal rejection
      approve → revalidate manifest hash and execute once
```

User approval never overrides a validator denial. Hooks/policy may deny an
otherwise approved action. A validator or policy outage fails closed for
protected actions and reports the unavailable component explicitly.

## 5. Frozen Action Manifest

Introduce a deterministic manifest owned by dynamic-tool execution, not the UI:

```python
@dataclass(frozen=True)
class FrozenActionManifest:
    schema_version: int
    action_kind: str
    tool_slug: str | None
    artifact_sha256: str
    arguments_sha256: str
    dependency_lock_sha256: str | None
    requested_capabilities: tuple[str, ...]
    network_scopes: tuple[str, ...]
    filesystem_scopes: tuple[str, ...]
    device_scopes: tuple[str, ...]
    created_at: float
```

Hash the canonical JSON representation. The approval token binds:

```text
approval_id
session_id
run_id
manifest_hash
expires_at
nonce
```

The MAC/signature key is machine-local configuration and never sent to the Web.
If the current project has an established token signer, reuse it through a
narrow interface rather than inventing another crypto format.

## 6. Risk Vocabulary

Use a small closed protocol enum:

| Risk | Default behavior | Examples |
|---|---|---|
| `read_only` | auto-allow if policy permits | local query, bounded fetch through approved client |
| `reversible` | configurable; ask for unfamiliar generated action | play/pause, temporary device state |
| `persistent_write` | ask | create/update workflow, write approved local file |
| `scheduled` | ask with schedule preview | recurring home action |
| `destructive` | ask with stronger confirmation | delete workflow/memory/tool |
| `privileged` | deny unless explicitly supported | broad shell, unrestricted filesystem/network |

Risk is not inferred from user prose alone. It is computed from validated
capabilities, policy, tool metadata, and concrete scopes. The LLM may propose a
label, but deterministic policy owns the decision.

## 7. Approval Event Contract

`approval.requested` payload:

```json
{
  "approval_id": "01J...",
  "action_kind": "generated_tool.execute",
  "display_title": "執行新產生的工具",
  "display_summary": "查詢指定網站並整理價格",
  "risk": "persistent_write",
  "effects": {
    "network": ["example.com"],
    "filesystem": ["read: input artifact", "write: generated result"],
    "devices": []
  },
  "expires_at": 1784250300,
  "decision_options": ["approve_once", "reject"],
  "manifest_hash_prefix": "b2a1f9d6"
}
```

Do not send the secret token until needed; an opaque decision token may be sent
with the request if it is single-use and MAC-bound. Event payloads must not
include generated source, environment values, headers, cookies, full local
paths, or raw arguments containing secrets.

`approval.resolved` payload:

```json
{
  "approval_id": "01J...",
  "decision": "approved_once",
  "resolved_at": 1784250012,
  "reason_code": "operator_approved"
}
```

Resolution reason codes include:

- `operator_approved`
- `operator_rejected`
- `expired`
- `manifest_mismatch`
- `run_cancelled`
- `bridge_restarted`
- `policy_changed`
- `already_resolved`

## 8. Approval API

Add one narrow endpoint:

```text
POST /api/command/approval
```

Request:

```json
{
  "approval_id": "01J...",
  "decision": "approve_once",
  "decision_token": "opaque",
  "session_id": "web-default",
  "run_id": "01J..."
}
```

Response is an acknowledgement, not the execution result:

```json
{
  "status": "ok",
  "approval_status": "resolved",
  "run_id": "01J..."
}
```

The resumed run reports progress/result through the event stream. Duplicate
submissions are idempotent only when identical; contradictory second decisions
return a typed conflict and do not alter the first terminal decision.

## 9. Pause/Resume Ownership

Do not leave a Web HTTP request blocked waiting for approval. Persist a bounded
`PendingApproval` record and return/stream the request event.

```python
class PendingApprovalStore:
    create(record) -> None
    resolve_once(approval_id, decision, token) -> Resolution
    get(approval_id) -> PendingApproval | None
    expire(now) -> list[ExpiredApproval]
```

The stored record contains a frozen manifest reference and resumable execution
descriptor, not a live Python closure. The descriptor must be reconstructable
and revalidated. If the execution cannot be safely resumed after process death,
restart resolves it as `bridge_restarted` and asks the user to run it again.

## 10. Dynamic `/new` Integration

Initial protected point:

1. generation finishes;
2. deterministic code/dependency/sandbox validation passes;
3. execution plan and concrete scopes are frozen;
4. risk policy decides ask/allow/deny;
5. ask path stores approval and returns without execution;
6. approval handler reloads the exact artifact by content hash;
7. reruns safety validation and policy evaluation;
8. compares manifest hash and policy version;
9. executes once through the existing DynamicToolRunner execution service;
10. records success/failure and consumes the approval token.

Do not splice approval logic across the generation, repair, and evaluation
loops. Add a single execution gate at the narrow boundary immediately before
side effects.

Reuse-path policy:

- a previously validated catalog tool is not automatically equivalent to a
  previously approved invocation;
- approval depends on current arguments and effects;
- known read-only catalog tools may be auto-allowed by stable policy;
- demoted/repaired/regenerated artifacts receive a new hash and new decision.

## 11. Other Action Families

The protocol should later cover:

- workflow save/delete/run;
- schedule create/update/delete;
- filesystem writes from future tools;
- device state changes;
- network access beyond an approved read-only HTTP client;
- destructive memory/session operations.

Do not expand every family in the first PR. After `/new` proves the contract,
onboard one action family per PR with its own risk/effect characterization.

## 12. Web UX Contract

Mobile approval card must show:

- what will happen;
- which concrete resources are affected;
- risk label in plain language;
- expiry/countdown only if useful;
- `核准一次` and `拒絕` actions;
- pending/resolved state;
- clear failure if the action expired or changed.

For destructive operations, require a deliberate stronger gesture such as
press-and-hold or a second explicit confirmation. Do not use generic browser
`confirm()` dialogs. Do not provide a permanent global allow button.

The UI must disable both buttons immediately after one decision while waiting
for acknowledgement, then reconcile from `approval.resolved` events.

## 13. Security And Privacy Threat Model

Required threats and controls:

| Threat | Control |
|---|---|
| approve benign code, execute changed code | canonical manifest hash + revalidation |
| replay decision token | single-use persisted resolution |
| approve one run, apply to another | bind session/run/approval IDs |
| bridge restart loses pending state | persisted record or fail-closed interruption |
| Web forges risk label | server computes policy; UI label is display only |
| generated source contains secret | never include source in default event/card |
| approval expires during execution | expiry checked before start; execution has separate cancellation policy |
| policy changes after request | bind/evaluate policy version before execution |
| duplicate click | idempotent compare-and-set |
| stolen LAN request | retain existing private-network allowlist and opaque token |
| validator outage | deny/ask cannot become auto-allow |

## 14. Configuration

Suggested settings:

```text
OPENCLAW_WEB_APPROVALS_ENABLED=0       # staged rollout
OPENCLAW_WEB_APPROVAL_TTL_SECONDS=300
OPENCLAW_WEB_APPROVAL_STORE_DIR=.openclaw_tmp/web_approvals
OPENCLAW_DYNAMIC_TOOL_APPROVAL_POLICY=ask_generated_writes
```

Do not expose a config that silently disables deterministic validators. Update
`.env.example`, settings tests, and system truth docs when enabled.

## 15. File-Level Implementation

Expected new files:

- `src/openclaw_adapter/action_risk.py`
- `src/openclaw_adapter/approval_models.py`
- `src/openclaw_adapter/approval_store.py`
- `src/openclaw_adapter/approval_service.py`
- `tests/test_action_risk.py`
- `tests/test_approval_store.py`
- `tests/test_dynamic_tool_approval.py`
- `tests/test_command_bridge_approval_http.py`

Expected changed files:

- `src/assistant_runtime/settings.py`
- `.env.example`
- `src/openclaw_adapter/dynamic_tools/service.py`
- the narrow dynamic-tool execution service identified during implementation
- `src/openclaw_adapter/command_bridge.py`
- `src/openclaw_adapter/command_bridge_server.py`
- event DTO/vocabulary modules
- Web types/components/tests in companion repo
- security/system/verification docs when behavior ships

## 16. Delivery Slices

### PR A1 — policy and manifest only

- frozen manifest and canonical hash;
- deterministic risk classification;
- golden fixtures for known action shapes;
- no behavior change.

### PR A2 — store, event, and HTTP decision contract

- pending approval persistence;
- one-shot resolution;
- expiry and restart behavior;
- event/API characterization;
- feature remains disabled.

### PR A3 — `/new` execution gate

- insert gate immediately before side effects;
- pause and resume exact artifact;
- revalidate on approval;
- terminal state and cancellation integration.

### PR A4 — Web approval card

- render request from typed event;
- submit one decision;
- reconnect to pending/resolved state;
- mobile interaction and accessibility tests.

### PR A5 — enable and live verify

- staged config enablement;
- actual benign generated-tool approval;
- actual rejection;
- hash-mismatch adversarial proof;
- documentation and issue progress update.

## 17. Verification Matrix

### Deterministic

- canonical manifest stable across dict order/platform-neutral inputs;
- changed code/args/dependencies/scopes change hash;
- invalid/expired token rejected;
- duplicate identical decision idempotent;
- conflicting decision rejected;
- policy deny cannot be overridden;
- validator exception fails closed;
- restart classifies non-resumable approval;
- cancel resolves pending request;
- artifact missing or replaced fails closed;
- approved action executes exactly once;
- source/secrets absent from events/logs;
- current non-Web Telegram `/new` policy remains explicitly characterized.

### HTTP/event

- malformed approval request 400;
- wrong session/run/token 403 or typed conflict per contract;
- unknown approval 404 typed response;
- approval event survives reconnect;
- old Web ignores unknown approval event safely until consumer rollout;
- event terminal sequence is deterministic.

### Live

When code ships, after normal test gates and supported `/restartall`:

1. request a benign generated read-only tool and capture policy result;
2. request a generated action requiring approval and show the exact card;
3. approve once and show fresh actual output;
4. repeat and reject; prove no side effect;
5. mutate/delete the frozen artifact before approval in a controlled fixture;
6. prove mismatch blocks execution;
7. let one approval expire;
8. verify restart/disconnect never auto-approves.

## 18. Progress / Handoff Checklist

The first protected boundary is the Web-only workflow shim's generated-tool
`run_tool_step`, immediately before it writes parameters and calls the existing
runner. Telegram and the shared runner are unchanged.

- [x] A1.1 inventory exact dynamic-tool side-effect boundary.
- [x] A1.2 define risk/effect vocabulary.
- [x] A1.3 define canonical manifest and hash fixtures.
- [x] A1.4 define policy outcomes and validator ordering.
- [x] A2.1 implement approval models/store.
- [x] A2.2 implement expiry and one-shot resolution.
- [x] A2.3 add reserved event variants and HTTP contract.
- [x] A2.4 characterize restart-safe persisted pending records and cancellation/rejection.
- [x] A3.1 add disabled execution gate.
- [x] A3.2 implement exact artifact reload/revalidation.
- [x] A3.3 implement one-shot resume and durable resolution recording.
- [x] A3.4 run adversarial hash/replay tests.
- [x] A4.1 implement Web card and decision client.
- [x] A4.2 retain pending approval on its workflow card across response updates.
- [x] A4.3 disable decision controls immediately after submit.
- [x] A5.1 enable policy in staged config.
- [x] A5.2 restart and run approve/reject/mismatch/expiry/reconnect live proof.
- [x] A5.3 update system truth.
- [ ] A5.4 update issue trail after the implementation commits are pushed.

### Staged Live Proof — 2026-07-17

- `/restartall` recreated the bridge, Telegram bot, and Web listener with the
  approval feature enabled.
- A controlled generated tool paused before its first write; approve-once ran
  it exactly once, while token replay did not execute it again.
- Explicit reject, expiry, and artifact-hash mismatch all failed closed without
  changing the controlled output.
- A pending card survived a Web reload and remained actionable; resolved cards
  stayed disabled.
- A destructive fixture required a second deliberate click. The first click
  only changed the button to `再按一次確認` and produced no side effect.
- `approval.requested` exposed bounded manifest metadata without source code or
  raw arguments. Temporary fixtures and traces were removed; durable approval
  audit events were retained.

## 19. Rollback

- Keep the approval gate behind configuration until Web consumer ships.
- Disabling the feature returns to existing deterministic validation/execution;
  pending records resolve as disabled/interrupted, never auto-execute.
- Do not delete audit events during rollback.
- Do not relax validators to make rollout failures disappear.
- Rollback the action family integration independently of shared event support.

## 20. Exit Gate

Complete means `/new` Web execution requiring approval cannot perform a side
effect until an unexpired, single-use, manifest-bound operator decision is
accepted; approval cannot bypass safety policy; rejection/mismatch/restart fail
closed; reconnect renders consistent status; deterministic and live proofs pass;
and docs/issues accurately describe enabled action families and remaining work.
