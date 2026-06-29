# Telegram NL Ownership Refactor Issue Draft

Last reviewed: 2026-06-30
Status: Planned
Owner area: telegram

Draft GitHub issue for cleaning up the cross-repo ownership problem exposed by
issue #53. This is a supporting handoff document; paste the issue body below
into GitHub when ready.

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

Move aka-specific natural-language workflow routing and dispatch ownership back
to `aka_no_claw`, while keeping `price_monitor_bot` as a reusable/generic
Telegram bot foundation.

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

Prefer Option A if inheritance is already the local pattern. It is the smallest
change and fits the existing `aka_no_claw` subclass.

## Implementation Tasks

### 1. In `price_monitor_bot`

Files likely involved:

- `src/price_monitor_bot/bot.py`
- `src/price_monitor_bot/natural_language.py`
- `tests/test_telegram_bot.py`
- any natural-language tests covering `create_workflow`

Tasks:

- Remove `create_workflow` handling from the base `TelegramCommandProcessor`.
- Remove `/workflow` dispatch from `price_monitor_bot`.
- Prefer removing `workflow_description` from the generic
  `TelegramNaturalLanguageIntent` if no generic extension mechanism requires it.
- Add a downstream/app-specific natural-language hook or registry.
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

### 2. In `aka_no_claw`

Files likely involved:

- `src/openclaw_adapter/telegram_bot.py`
- `src/openclaw_adapter/natural_language.py`
- `docs/TELEGRAM_TOOL_SPEC.md`
- `tests/test_telegram_bot.py`
- `tests/test_natural_language.py`

Tasks:

- Define the aka-specific `create_workflow` intent behavior in `aka_no_claw`.
- If needed, add an aka-specific intent dataclass or wrapper field for
  `workflow_description`.
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

- `price_monitor_bot` no longer contains aka-specific workflow intent names or
  `/workflow` dispatch logic.
- `aka_no_claw` still routes workflow creation NL requests to `/workflow create`.
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

## Notes

This should be a separate refactor issue, not folded into #53. #53 should focus
on behavior. This issue should focus on repo ownership and maintainability.
```
