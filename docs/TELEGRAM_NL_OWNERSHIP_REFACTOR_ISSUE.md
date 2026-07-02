# Telegram NL Ownership Refactor Issue Draft

Last reviewed: 2026-07-01
Status: Planned
Owner area: telegram

Draft GitHub issue / planning note for cleaning up the cross-repo ownership
problem exposed by issue #53.

Companion / cross-links: [TELEGRAM_CORE_EXTRACTION_PLAN.md](TELEGRAM_CORE_EXTRACTION_PLAN.md)
is the sibling infra split (transport/poll-loop/dispatch → `telegram_core`,
now shipped); this doc's NL-routing ownership move is a separate concern —
`CoreCommandProcessor._route_natural_language` is a hook precisely so this
refactor can relocate the router without touching `telegram_core`.

## Task 1 Work Log

2026-07-01 characterization pass for the model-first refactor:

- Goal of task 1: lock the current `telegram_nl` routing boundary before any
  fallback shrink or prompt trimming work.
- Decision: keep the execution log in this existing planning doc rather than
  create a parallel worklog, so docs governance stays simple and the refactor
  state is in one place.
- Current downstream dependency to protect:
  `price_monitor_bot/src/price_monitor_bot/bot.py` still runs the LLM router
  and then `fallback_route_telegram_natural_language(text)`, with explicit
  preference rules for deterministic fallback hits such as
  `sns_bulk_add_filter` and `sns_clear_filter`.
- Characterization focus added to `telegram_nl/tests/test_natural_language.py`:
  `sns_clear_filter` vs `sns_delete`, single-handle schedule updates,
  bulk schedule updates, explicit non-matches for bare marketplace URLs, and
  unrelated messages that should keep falling through to the model/app layer.
- Success condition for task 1: the pure `telegram_nl` test suite becomes the
  authoritative guardrail for fallback routing boundaries, so task 2 can
  safely reduce fallback scope without depending on app-layer tests to spot
  regressions.

## Task 2 Work Log

2026-07-01 routing split for model-first execution:

- Added three layers in `telegram_nl`:
  `fast_route_telegram_natural_language()`,
  `slow_fallback_route_telegram_natural_language()`, and the existing
  `fallback_route_telegram_natural_language()` kept as a compatibility wrapper
  (`fast or slow`).
- `price_monitor_bot.TelegramCommandProcessor` now runs routing in this order:
  app fast path, generic `telegram_nl` fast path, LLM router, then slow
  fallback.
- This removes the old need for post-LLM "rescue" branches for
  `sns_bulk_add_filter` and `sns_clear_filter`; those deterministic structured
  cases are now decided before the model path.
- Kept compatibility for downstream direct callers that still import
  `fallback_route_telegram_natural_language()` so repo-wide churn stays small
  during the refactor.
- Added processor-level coverage proving a generic fast-path hit short-circuits
  before the LLM router is even called.

## Task 3 Work Log

2026-07-01 app-intent ownership moved back to `aka_no_claw`:

- Removed `create_workflow`, `play_music`, and `home_action` from
  `telegram_nl`'s generic deterministic fallback and generic LLM prompt.
- Removed those intents from the base `telegram_nl` allowed-intent set; they
  are now accepted only when a caller explicitly opts in via
  `extra_allowed_intents`.
- `aka_no_claw` now injects its app-intent prompt suffix and allowed-intent
  list when building the Telegram NL router, including the cloud-first wrapper.
- Added `fallback_route_openclaw_natural_language()` in `aka_no_claw` for the
  residual app-specific fallback path, and wired both
  `openclaw_adapter.telegram_bot.TelegramCommandProcessor` and
  `openclaw_adapter.command_bridge` to use it instead of the generic package.
- Result: `telegram_nl` owns generic Telegram NL mechanics; `aka_no_claw`
  owns workflow/music/home semantics and examples.

## Verification Log

2026-07-01 post-refactor verification:

- `telegram_nl`: `.venv/bin/python -m pytest -q` -> 61 passed.
- `price_monitor_bot`: `.venv/bin/python -m pytest tests/test_natural_language.py tests/test_telegram_bot.py -q` -> 178 passed.
- `aka_no_claw`: `.venv/bin/python -m pytest tests/test_natural_language.py tests/test_telegram_bot.py tests/test_command_bridge.py tests/test_intent_fast_path.py tests/test_workflow_command.py tests/test_music_command.py tests/test_ir_command.py -q` -> 484 passed.
- Docs health: `.venv/bin/python scripts/check_docs_health.py` -> passed.
- Local command bridge e2e smoke:
  started `openclaw_adapter command-bridge --host 127.0.0.1 --port 8791`,
  then `POST /api/command/stream` for workflow creation returned
  `redirect:create_workflow` for both explicit `workflow` wording and
  `自動化流程` wording.
- Live configured router smoke:
  routed `create_workflow`, `play_music`, `home_action`, and generic
  `sns_clear_filter` representative phrases through the configured router.
- Telegram connectivity smoke:
  `telegram-send-test` sent a non-command connectivity message to the configured
  chat successfully.
- Local incoming Telegram pipeline smoke:
  `handle_telegram_message` with stubbed `/music`, `/ir`, and `/workflow`
  handlers routed `放我最愛的音樂`, `關掉臥室燈`, and workflow creation to the
  expected command handlers without touching real devices.
- Not run automatically:
  real incoming Telegram messages that execute music/IR/workflow on live
  hardware. A production `telegram-poll` process was already running, so a
  second poller was not started.

## Current Discussion State

Local code has already moved the aka-specific natural-language behavior into
`aka_no_claw`:

- `src/openclaw_adapter/natural_language.py` now injects aka-only router schema,
  prompt examples, and deterministic fallback for `create_workflow` and
  `play_music`.
- `src/openclaw_adapter/telegram_bot.py` now handles aka-only app intents through
  the `TelegramCommandProcessor` subclass.
- The base `price_monitor_bot.TelegramCommandProcessor` has an app intent hook,
  so it no longer needs to hardcode `/workflow` dispatch behavior.

Local verification on 2026-06-30:

```text
.venv/bin/python -m pytest tests/test_natural_language.py -q
84 passed

.venv/bin/python -m pytest tests/test_telegram_bot.py -q
59 passed

.venv/bin/python -m pytest tests/test_task_workspace.py tests/test_workflow_command.py tests/test_command_bridge.py tests/test_workflow_editor.py tests/test_home_schedule.py tests/test_ir_command.py -q
348 passed
```

Remaining ownership problem:

`price_monitor_bot` still owns the full `natural_language.py` implementation.
If the goal is to remove that responsibility entirely, do not move it into
`aka_no_claw`. That would make `price_monitor_bot` depend on the app repo and
invert the current dependency direction.

Preferred next step:

Create a neutral sibling package/repo, tentatively:

```text
related_to_claw/telegram_nl
```

Dependency direction should become:

```text
telegram_nl
  ↑
price_monitor_bot
  ↑
aka_no_claw
```

`aka_no_claw` may also import `telegram_nl` directly, but
`price_monitor_bot` must not import `aka_no_claw`.

## What Should Move To `telegram_nl`

- `TelegramNaturalLanguageRouter`
- `TelegramNaturalLanguageIntent` or a neutral replacement carrier
- router JSON parsing / normalization helpers
- router schema extension mechanism
- prompt construction and `build_telegram_natural_language_router`
- pure NL tests for normalization, schema extension, and base routing mechanics

## What Should Stay Out Of `telegram_nl`

- `TelegramCommandProcessor`: it mixes command registry dispatch, photo
  clarification, callbacks, SNS, lookup, reputation, and app hooks. If this is
  extracted later, it belongs in a broader `telegram_bot_core`, not `telegram_nl`.
- `TelegramBotClient`, `RegisteredCommand`, `TelegramTextReplyPlan`, and
  `list_view.py`: reusable Telegram runtime/UI primitives, but not natural
  language. Treat as a possible later `telegram_bot_core` extraction.
- `price_monitor_bot.commands`, `formatters`, and `watch_monitor`: price/TCG
  domain code.
- aka-only profiles and examples: `create_workflow`, `play_music`,
  `workflow_description`, `music_query`, `/workflow`, `/music`, `/schedulehome`.

## Suggested Title

## Suggested Title

```text
Refactor Telegram natural-language routing ownership out of price_monitor_bot
```

## Issue Body

```markdown
## Problem

Issue #53 introduced aka_no_claw-specific workflow routing into the sibling
`price_monitor_bot` repo.

Observed sibling repo commit:

```text
price_monitor_bot 7b6328e feat(#53): create_workflow NL intent + workflow_description field
```

Touched files:

```text
src/price_monitor_bot/natural_language.py
src/price_monitor_bot/bot.py
```

The change added:

- `create_workflow` natural-language intent
- `workflow_description` field on `TelegramNaturalLanguageIntent`
- base `TelegramCommandProcessor` dispatch from `create_workflow` to registered
  `/workflow`

This works mechanically because `aka_no_claw` depends on `price_monitor_bot`
through:

```text
requirements.txt: -e ../price_monitor_bot
```

But the ownership boundary is wrong:

- Workflow authoring/execution is an `aka_no_claw` feature.
- `/workflow`, `/schedulehome`, `/music`, and generated-tool composition are
  `aka_no_claw` application commands.
- `price_monitor_bot` should not know about `create_workflow`, `/workflow`, or
  aka-specific workflow semantics.

This makes future Telegram routing harder to reason about because adding an
aka-only feature can require changing the generic/sibling bot package.

## Goal

Move natural-language routing implementation ownership out of `price_monitor_bot`
without making `price_monitor_bot` depend on `aka_no_claw`.

Short-term status:

- aka-only intent behavior belongs in `aka_no_claw`.
- base `price_monitor_bot` should only expose generic app-extension hooks.

Long-term target:

- generic natural-language router mechanics belong in a neutral `telegram_nl`
  package.
- price-specific intent profiles stay in `price_monitor_bot`.
- aka-specific intent profiles stay in `aka_no_claw`.

## Desired Ownership Boundary

`price_monitor_bot` should own:

- generic Telegram bot processing primitives
- generic command registry dispatch
- generic text clarification UX
- domain-neutral extension hooks
- price-monitor-specific intents only when they belong to price monitoring

`aka_no_claw` should own:

- `create_workflow` intent semantics
- `workflow_description` parsing/slot meaning
- `/workflow create ...` dispatch
- workflow/music/schedulehome natural-language examples
- #53 workflow-specific tests

## Proposed Refactor Shape

### Recommended shape: new neutral package `telegram_nl`

Create a sibling repo/package:

```text
related_to_claw/telegram_nl
  pyproject.toml
  src/telegram_nl/__init__.py
  src/telegram_nl/natural_language.py
  tests/test_natural_language.py
```

Move the generic router implementation out of `price_monitor_bot` into
`telegram_nl`. Then:

- `price_monitor_bot` imports router primitives from `telegram_nl`.
- `aka_no_claw` imports router primitives from `telegram_nl` through
  `openclaw_adapter.natural_language`.
- `price_monitor_bot.natural_language` may remain temporarily as a compatibility
  shim that re-exports `telegram_nl`.
- new code should not import `price_monitor_bot.natural_language`.

Do not move `natural_language.py` into `aka_no_claw`; that would reverse the
dependency direction and make the reusable price package depend on the app repo.

### Option A: downstream NL intent extension hook

Add a generic extension seam to `price_monitor_bot`:

```python
class TelegramCommandProcessor:
    def _build_app_natural_language_reply_plan(
        self,
        intent: TelegramNaturalLanguageIntent | object | None,
        *,
        chat_id: str | int = "",
    ) -> TelegramTextReplyPlan | None:
        return None
```

Then in `price_monitor_bot.TelegramCommandProcessor._build_natural_language_reply_plan()`:

```python
app_plan = self._build_app_natural_language_reply_plan(intent, chat_id=chat_id)
if app_plan is not None:
    return app_plan
```

`aka_no_claw.TelegramCommandProcessor` overrides that hook and handles:

```python
if intent.intent == "create_workflow":
    ...
```

This keeps generic command dispatch in the base package but moves aka-specific
intent behavior to `aka_no_claw`.

### Option B: app intent handler registry

Add a constructor argument in `price_monitor_bot`:

```python
app_intent_handlers: dict[str, Callable[[TelegramNaturalLanguageIntent, str], TelegramTextReplyPlan | None]]
```

The base processor checks this registry before returning `None` for unknown
domain-specific intents.

`aka_no_claw` registers a handler for:

```text
create_workflow
```

### Recommendation

Keep Option A as the short-term app hook, and use the neutral `telegram_nl`
package as the long-term owner for the generic router code.

## Implementation Tasks

### 1. In `price_monitor_bot`

Files likely involved:

- `src/price_monitor_bot/bot.py`
- `src/price_monitor_bot/natural_language.py`
- `tests/test_telegram_bot.py`
- any natural-language tests covering `create_workflow`

Tasks:

- Replace implementation imports with `telegram_nl`.
- Keep `price_monitor_bot.natural_language` only as a temporary re-export shim,
  or remove it after all imports are migrated.
- Remove aka-only names from base schema/tests if they still exist:
  `create_workflow`, `play_music`, `workflow_description`, `music_query`.
- Keep the downstream/app-specific natural-language hook or registry.
- Keep existing price-monitor natural-language behavior unchanged.
- Add/adjust tests proving unknown app-specific intents can be delegated through
  the extension hook without hardcoding aka-specific names.

Expected after cleanup:

```bash
cd ../price_monitor_bot
rg -n "create_workflow|workflow_description|/workflow" src tests
```

Should show either no matches, or only generic extension-hook tests/docs that do
not encode aka workflow behavior.

### 2. In `telegram_nl`

Files likely involved:

- `src/telegram_nl/natural_language.py`
- `tests/test_natural_language.py`
- `pyproject.toml`

Tasks:

- Move generic router code out of `price_monitor_bot`.
- Keep the extension mechanism generic.
- Do not encode aka-only or price-only intent names as universal behavior.
- Add package tests for schema extension and unknown intent normalization.

### 3. In `aka_no_claw`

Files likely involved:

- `src/openclaw_adapter/telegram_bot.py`
- `src/openclaw_adapter/natural_language.py`
- `docs/TELEGRAM_TOOL_SPEC.md`
- `tests/test_telegram_bot.py`
- `tests/test_natural_language.py`

Tasks:

- Keep aka-specific `create_workflow` and `play_music` behavior in
  `aka_no_claw`.
- Teach the aka natural-language router prompt/tool spec to emit workflow
  creation intent.
- Dispatch `create_workflow` to registered `/workflow` inside
  `aka_no_claw.TelegramCommandProcessor`, not in `price_monitor_bot`.
- Add tests for:
  - router-produced `create_workflow` dispatching to `/workflow create ...`
  - missing `/workflow` registry producing a clear error
  - missing workflow description asking for clarification
  - natural language examples:
    - `建立一個 workflow：每天早上問候我，然後放音樂`
    - `幫我建立自動化流程：先說早安問候，再播放最愛音樂`

## Acceptance Criteria

- `price_monitor_bot` no longer owns the generic natural-language router
  implementation.
- `price_monitor_bot` no longer contains aka-specific workflow/music intent
  names or `/workflow` dispatch logic.
- `price_monitor_bot` remains standalone and does not depend on `aka_no_claw`.
- `aka_no_claw` still routes workflow creation NL requests to `/workflow create`.
- `aka_no_claw` still routes direct music NL requests to `/music`.
- `telegram_nl` contains only generic router mechanics, not app-specific command
  behavior.
- #53 workflow UX remains functional:
  - Telegram can draft a workflow from natural language.
  - Web Chat Mode can draft/edit/save a workflow.
  - `/schedulehome` can schedule `/workflow run <id>`.
- Music remains in #53 scope:
  - workflow creation can describe actions involving `/music`
  - command sink policy can support schedulehome-safe commands such as `/music`
    without letting arbitrary slash command strings execute
- Both repos' tests pass.

## Verification

Run in `price_monitor_bot`:

```bash
cd ../price_monitor_bot
.venv/bin/python -m pytest tests/test_telegram_bot.py tests/test_natural_language.py -q
.venv/bin/python -m pytest -q
```

If this repo does not have `.venv`, use the environment currently used by
`aka_no_claw`, but report that explicitly.

Run in `aka_no_claw`:

```bash
cd ../aka_no_claw
.venv/bin/python -m pytest \
  tests/test_natural_language.py \
  tests/test_telegram_bot.py \
  tests/test_workflow_command.py \
  tests/test_workflow_editor.py \
  tests/test_home_schedule.py \
  tests/test_command_bridge.py -q

.venv/bin/python -m pytest -q
```

Also run documentation checks if docs are touched:

```bash
.venv/bin/python scripts/check_docs_health.py
.venv/bin/python scripts/check_manifest.py
.venv/bin/python scripts/check_doc_drift.py
```

## Manual Smoke Checks

Use deterministic/stubbed tests where possible. If doing a manual Telegram smoke,
verify:

```text
建立一個 workflow：每天早上問候我，然後放音樂
```

Expected:

```text
ack: 正在用 AI 起草工作流程草稿
result: editable workflow card
not: generic clarification menu
not: direct /music execution
```

Then verify:

```text
/workflow run <saved_workflow_id>
```

and, if scheduled:

```text
/schedulehome 07:00 "/workflow run <saved_workflow_id>"
```

Expected:

```text
scheduler -> /workflow run -> workflow runner -> allowed command sinks
```

## Non-goals

- Do not rewrite all Telegram routing.
- Do not move all of `price_monitor_bot` into `aka_no_claw`.
- Do not make workflow execution run arbitrary model-generated slash strings.
- Do not close #53 solely by doing this refactor; #53 still needs command sink
  breadth, Web capture, and workflow/music E2E coverage.

## 2026-07-01 Web / Telegram Alignment Note

- Web chat checks the OpenClaw app-level NL fallback before generic chat tools
  for app-owned music intents with an explicit `music_query`.
- This keeps phrases such as "播放我的最愛清單歌曲" aligned with the Telegram
  path: OpenClaw NL resolves `play_music` with `music_query="playbest"`, then Web
  dispatches through the existing `/music` handler instead of rendering the
  favorite-list view.
- Generic Web controls now fall through to the shared Web Chat `/music` tool
  route instead of a bridge-local music detector.

## Notes

This should be a separate refactor issue, not folded into #53. #53 should focus
on behavior. This issue should focus on repo ownership and maintainability.
```
