# Issue 53 Workflow Review Handoff

Last reviewed: 2026-06-30
Status: Historical
Owner area: telegram

Historical review handoff for GitHub issue #53.

The issue is now resolved and this file is archived as a pre-resolution review
snapshot. The shipped fix landed in commit `bd80e59`
(`fix(#53): denylist-based command sink policy + web registry wiring`).

Resolution summary:

- workflow command sinks now use a denylist-based policy
- Telegram and Web workflow surfaces both receive the full command registry
- `/schedulehome -> /workflow run -> command_sink` was re-verified locally for
  `/saynow`, `/music`, `/ir`, and `/musiclistall`
- Web workflow picker and execution were re-verified locally for registry-backed
  safe commands
- full local suite passed: `2095 passed, 7 skipped`

The remaining cross-repo ownership cleanup about Telegram natural-language
workflow routing is tracked separately in
`docs/TELEGRAM_NL_OWNERSHIP_REFACTOR_ISSUE.md`.

## Scope

Issue #53 aims to make workflows composable:

- `/workflow` authors, lists, shows, runs, deletes, and inspects workflow traces.
- `/schedulehome` schedules `/workflow run <workflow_id>`.
- Workflow runtime passes named variables between `tool_call`, `llm_transform`,
  and allowlisted `command_sink` steps.
- Telegram and Web Chat Mode should support card-based workflow authoring without
  requiring raw JSON editing on a phone.

## Archived Verdict

This section and the detailed findings below are preserved as the review context
from before the final fix landed. They are no longer the current project state.

## Latest Local Acceptance Pass

Date: 2026-06-30

Commands run:

```bash
.venv/bin/python -m pytest \
  tests/test_task_workspace.py \
  tests/test_workflow_command.py \
  tests/test_command_bridge.py \
  tests/test_home_schedule.py \
  tests/test_ir_command.py -q
```

Result:

```text
303 passed in 12.70s
```

Full suite:

```bash
.venv/bin/python -m pytest -q
```

Result:

```text
2088 passed, 7 skipped, 215 warnings in 141.25s
```

Manual E2E acceptance script passed for the happy path:

- direct `/workflow run` dispatches workflow command sinks to `/saynow`,
  `/music playbest`, and `/ir send ceiling_light power`.
- `/schedulehome`-style replay of `/workflow run <id>` reaches the same
  workflow sinks.
- `chat_id` is preserved through `/schedulehome -> /workflow run -> command_sink`.

Manual E2E acceptance script also proved the remaining blocker:

- a command present in the Telegram command registry and therefore schedulable
  through `/schedulehome`, such as `/musiclistall`, is rejected by workflow
  validation because it is not in `COMMAND_SINK_ALLOWLIST`.

Conclusion:

Tests are green, but the #53 "all `/schedulehome` safe actions" requirement is
not satisfied. The implementation must switch from a workflow-specific positive
allowlist to a shared schedule-safe policy with an explicit denylist.

## Required Fixes

### 0. Cross-repo boundary: why #53 touched `price_monitor_bot`

Severity: Architectural note

Observed sibling repo commit:

```text
price_monitor_bot 7b6328e feat(#53): create_workflow NL intent + workflow_description field
```

Touched files:

```text
src/price_monitor_bot/bot.py
src/price_monitor_bot/natural_language.py
```

Reason:

`aka_no_claw` imports `price_monitor_bot` as an editable sibling dependency
(`requirements.txt` has `-e ../price_monitor_bot`). The generic Telegram
natural-language intent schema and base `TelegramCommandProcessor` live in
`price_monitor_bot`, while `aka_no_claw` extends that processor with local command
registries.

So #53 added:

- `create_workflow` intent
- `workflow_description` field
- base processor dispatch from `create_workflow` to registered `/workflow`

This makes the current implementation work, but the ownership boundary is
awkward: workflow authoring/execution is an `aka_no_claw` feature, not a
price-monitoring feature.

Preferred cleanup direction:

- Keep generic extension seams in `price_monitor_bot`.
- Keep `create_workflow`-specific routing, tests, and command dispatch in
  `aka_no_claw` where possible.
- If `price_monitor_bot` must keep generic support, document it as an extension
  point, not as price-monitor behavior.
- Any fix touching this area should run tests in both repos or explicitly state
  why the sibling repo was not changed.

### 1. Web Chat Mode editor text capture

Severity: Resolved in latest unpushed changes; keep regression coverage

Files:

- `src/openclaw_adapter/command_bridge.py`
- `src/openclaw_adapter/command_bridge_server.py`
- `src/openclaw_adapter/workflow_editor.py`
- `tests/test_command_bridge.py`

Original problem:

`CommandBridge.run_workflow_command()` always sends the `input` text to the
`/workflow` command handler as a new subcommand. It does not first check whether
the shared `WorkflowEditor` is waiting for a captured text field.

Telegram already has the correct pattern in
`TelegramCommandProcessor._build_workflow_capture_plan()`:

```text
if workflow_editor.is_capturing(chat_id):
    workflow_editor.handle_text_capture(text, chat_id)
```

Web needs equivalent behavior for the fixed Web workflow chat id
`web-workflow`.

Observed local E2E failure:

```text
POST /api/command/workflow input="new"
-> "請輸入 ID 和目標..."
editor.is_capturing("web-workflow") == True

POST /api/command/workflow input="wf-web / Web goal"
-> returns /workflow help text
workflow id remains ""
```

The same failure happens after:

```text
wfe:add
wfe:kind:tool_call
input="city_weather"
```

Latest local result:

The unpushed working tree now handles this capture path correctly in a manual
script:

```text
new_status ok capturing True
id_goal 'wf-web' 'Web goal'
actions_after_goal ['wfe:add', 'wfe:save', 'wfe:cancel']
capturing_tool True
adding_fields {'tool': 'city_weather'}
```

Expected behavior:

```text
input="wf-web / Web goal"
-> renders editor card for wf-web

input="city_weather"
-> stores adding.fields["tool"] == "city_weather"
-> prompts for args
```

Suggested implementation shape:

```python
def run_workflow_command(self, text: str) -> dict:
    handler, editor = self._workflow_surface()
    raw = (text or "").strip()

    if editor.is_capturing(_WF_WEB_CHAT_ID):
        captured = editor.handle_text_capture(raw, _WF_WEB_CHAT_ID)
        if captured is not None:
            message, markup = captured
            return {
                "status": STATUS_OK,
                "message": str(message),
                "actions": self._markup_to_actions(markup),
            }

    remainder = raw
    if remainder.startswith("/workflow"):
        remainder = remainder[len("/workflow"):].strip()
    ...
```

Add tests that use the real `WorkflowEditor`, not only fake handlers:

- `run_workflow_command("new")`
- `run_workflow_command("wf-web / Web goal")`
- assert the returned actions include `wfe:add` and `wfe:save`
- `run_workflow_action("wfe:add")`
- `run_workflow_action("wfe:kind:tool_call")`
- `run_workflow_command("city_weather")`
- assert the editor advanced to the args prompt

Also add an HTTP-level route test for `/api/command/workflow` with `input` after
capture, so the browser API surface keeps this fixed.

### 2. Workflow command sinks are too narrow for the #53 scope

Severity: High

Files:

- `src/openclaw_adapter/task_workspace.py`
- `src/openclaw_adapter/workflow_command.py`
- `src/openclaw_adapter/home_schedule.py`
- `src/openclaw_adapter/home_schedule_command.py`
- `tests/test_task_workspace.py`
- `tests/test_workflow_command.py`
- `tests/test_home_schedule.py`

Problem:

The original workflow runtime allowlisted only `/saynow` as a command sink. The
latest unpushed changes expanded this to a hardcoded `COMMAND_SINK_ALLOWLIST`,
but that still does not match the requested policy.

Correct scope:

```text
Any safe slash command that /schedulehome can schedule should be representable
as a workflow execution action.
```

In practical terms, `/workflow` is the authoring layer and `/schedulehome` is the
execution layer. If `/schedulehome` can run an action by replaying a slash
command, a workflow step should be able to call that action through a safe,
structured command sink instead of being limited to speech.

This is not a one-command patch. Adding `/music`, `/ir`, or several more command
names to another static list is still incomplete.

At minimum this issue must include:

```text
/saynow
/music
/ir
```

`/ir` covers home-appliance actions such as light/power controls:

```text
/ir send ceiling_light power
```

Examples that should be supported by #53:

```text
1. greeting = llm_transform(...)
2. /saynow(greeting)
3. /music playbest
```

```text
1. light_action = "send ceiling_light power"
2. /ir(light_action)
```

```text
1. mood = llm_transform(...)
2. /music(mood)
```

Safety requirement:

Do not execute arbitrary model-generated slash command strings. The workflow
definition should store command identity and input separately, validate command
allowability, then dispatch through the same command registry semantics that
`/schedulehome` uses.

Required policy shape:

- Source of truth: the same command registry that `/schedulehome` dispatches.
- Default for registered commands: schedulable as workflow sinks unless denied
  by an explicit unsafe/meta denylist.
- Denylist examples:
  - `/restartall`
  - `/new`
  - `/backupclaw`
  - `/backup`
  - `/clawrecover`
  - `/recoverclaw`
  - `/schedulehome`
  - `/workflow`
  - destructive monitoring/config commands such as `/snsadd`, `/snsdelete`,
    `/sns_delete`, `/snsclearfilter`
  - shell/exec/rm/bash-like commands if they are ever registered
- The workflow editor command picker should render from the same effective
  policy, not from a separate workflow-only list.

Suggested implementation direction:

- Replace the hardcoded `COMMAND_SINK_ALLOWLIST` with a reusable schedule-safe
  command policy.
- Do not keep a second hand-maintained positive allowlist that immediately drifts
  from `/schedulehome`.
- Derive workflow command sink dispatch from the same command registry /
  `make_run_slash_command` semantics that `/schedulehome` uses, then apply an
  explicit denylist for unsafe/meta commands.
- Safe scheduled action commands should be allowed as workflow sinks. Examples
  currently in the command registry include:
  - `/saynow`
  - `/music`
  - `/ir`
  - `/bluetooth` where safe and non-destructive
  - other home/utility commands that are already acceptable for `/schedulehome`
- Unsafe/meta commands must remain blocked, even if they are registered:
  - `/restartall`
  - `/new`
  - `/backupclaw`
  - `/clawrecover`
  - shell/exec/bash/rm-like commands if ever present
- For command sinks with no variable input, support a validated literal argument
  or a `literal`/`args` field, so workflows can express `/music playbest` without
  needing a fake variable.
- Keep dangerous/meta commands out of workflow sinks, even if they exist in the
  command registry.

Add tests:

- workflow `command_sink` can call `/music` with a prior variable value.
- workflow can express a literal `/music playbest`-style action safely.
- workflow can express `/ir send ceiling_light power` safely.
- workflow command sink dispatch uses the same handler behavior as
  `/schedulehome` for `/music` and `/ir`.
- scheduled `/workflow run <id>` can execute a workflow that reaches `/music`.
- scheduled `/workflow run <id>` can execute a workflow that reaches `/ir`.
- adding a new schedule-safe command to the command registry should not require
  editing a workflow-specific allowlist.
- a registered command not in the denylist, for example `/musiclistall`, is
  accepted by workflow validation and dispatched through the registry.
- arbitrary/meta commands such as `/restartall`, `/new`, `/backupclaw`,
  `/clawrecover`, `/bash`, `/exec`, `/rm` remain rejected.

### 3. Type mismatch rejection is still missing

Severity: Medium

Files:

- `src/openclaw_adapter/task_workspace.py`
- `tests/test_task_workspace.py`

Problem:

Issue #53 acceptance criterion C says command sinks must reject incompatible
variable types. Example: `/saynow` should accept `speech_text` or `plain_text`,
but not raw `search_results`. This applies to all allowed command sinks, not only
`/saynow`.

Current behavior:

- `Workflow.validate_references()` validates only reference order and allowlisted
  command names.
- `VariableStore.bind()` defaults every variable to `"text"`.
- `_run_command_sink()` resolves the variable value and sends it to the command
  handler without checking variable type.

Expected:

```text
Given weather/search result/object-like output is tagged search_results
When /saynow consumes it directly
Then runtime rejects before calling /saynow
And trace.ok == False
And trace records the type mismatch
```

Minimal acceptable implementation:

- Introduce coarse type tags used by workflow steps:
  - `plain_text`
  - `weather_summary`
  - `speech_text`
  - `search_results`
  - `command_result`
  - `error`
- Give `WorkflowStep` either an optional output type or deterministic type
  inference per step kind.
- Define command sink accepted input types, starting with:

```python
COMMAND_SINK_INPUT_TYPES = {
    "/saynow": {"plain_text", "speech_text"},
}
```

- In `_run_command_sink()`, inspect the `Variable`, not just
  `store.resolve(name)`, and fail before invoking the handler if the type is not
  accepted.

Add tests:

- `/saynow` accepts `plain_text`.
- `/saynow` accepts `speech_text`.
- `/saynow` rejects `search_results`.
- rejected sink does not call the handler.
- trace contains a clear failed step/error.

### 4. Runtime workflow/traces are committed as repo files

Severity: Medium

Files:

- `workflow_store/wf-tokyo-weather-maid.json`
- `workflow_store/traces/**`
- `.gitignore` if runtime workflow store should not be tracked

Problem:

The commit contains live workflow store data and traces. Fresh checkouts will
show these in `/workflow list` and `/workflow traces`, and repository history will
carry local execution artifacts.

Expected:

Runtime state should not be committed unless it is an intentional deterministic
fixture. If fixtures are needed, move them under `tests/fixtures/` and load them
explicitly from tests.

Suggested fix:

- Remove committed `workflow_store/**` runtime artifacts from the repo.
- Add `workflow_store/` to `.gitignore`, unless this directory is intentionally
  user-managed and documented otherwise.
- If an E2E fixture is useful, create a deterministic fixture path under
  `tests/fixtures/workflows/`.

### 5. Test gap: Web workflow capture flow is not covered

Severity: Medium

File:

- `tests/test_command_bridge.py`

Problem:

Existing Web workflow tests mostly seed fake handlers/editors and verify wrapper
behavior. They do not cover the real `WorkflowEditor` capture sequence that is
required by the issue:

```text
/workflow new
plain text field capture
wfe:add
wfe:kind:<kind>
plain text field capture
wfe:save
```

Add tests for at least:

- Web blank workflow creation with captured id/goal.
- Web add `tool_call` step through captured fields.
- Web add `llm_transform` step through captured fields.
- Web add `command_sink` step through captured fields.
- Web save persists the workflow.

### 6. Natural-language coverage is incomplete outside the weather example

Severity: High

Files:

- `docs/TELEGRAM_TOOL_SPEC.md`
- `src/openclaw_adapter/telegram_bot.py`
- `src/openclaw_adapter/natural_language.py`
- `tests/test_natural_language.py`
- `tests/test_telegram_bot.py`

Problem:

The reviewed E2E only covered the morning weather/greeting/saynow workflow.
Additional local checks with natural-language examples showed:

- If the LLM router returns `create_workflow`, the processor correctly dispatches
  to `/workflow create <description>`.
- The deterministic fallback does not recognize workflow creation phrases such
  as `建立一個 workflow：每天早上問候我，然後放音樂`.
- The deterministic fallback does not recognize music phrases such as `放音樂`,
  `播放音樂`, or `放我最愛的音樂`.
- There is no fixed natural-language `music` intent in
  `TelegramNaturalLanguageIntent`; those phrases currently fall to the generic
  clarification menu and do not call `/music`.

Because music is in scope for #53, these are not optional polish issues. A user
must be able to describe a workflow containing music, then schedule it through
`/schedulehome`.

More generally, a user must be able to describe workflows containing any
schedulehome-safe home action, including light/power controls through `/ir`.

Observed local output for music phrases:

```text
MUSIC_TEXT: 放音樂
-> clarification menu
music_seen: []

MUSIC_TEXT: 播放音樂
-> clarification menu
music_seen: []

MUSIC_TEXT: 放我最愛的音樂
-> clarification menu
music_seen: []
```

Expected:

- Router/tool spec should teach workflow drafting examples that include
  command sinks beyond `/saynow`, especially `/music` and `/ir`.
- If direct natural-language music control is expected outside workflow
  authoring, add a `music` / `play_music` intent with safe mapping to
  `/music random`, `/music playbest`, or `/music <query>`.
- Add tests for natural-language workflow creation with descriptions like:

```text
建立一個 workflow：每天早上問候我，然後放音樂
幫我建立自動化流程：先說早安問候，再播放最愛音樂
建立一個 workflow：早上七點打開電燈，播放最愛音樂，然後跟我說早安
幫我建立自動化流程：打開 ceiling_light，放音樂，然後播報問候
```

Important distinction:

The processor path for `create_workflow` works when the router emits the intent:

```text
intent=create_workflow
workflow_description="每天早上問候我，然後放音樂"
-> dispatches /workflow create 每天早上問候我，然後放音樂
```

But live quality still depends on the LLM router producing that intent and on the
workflow draft generator knowing what command sink/tool should represent each
scheduled action (`/music`, `/ir`, etc.).

## What Already Passed

Earlier baseline pass before the latest unpushed changes:

```bash
.venv/bin/python -m pytest \
  tests/test_task_workspace.py \
  tests/test_workflow_command.py \
  tests/test_workflow_editor.py \
  tests/test_home_schedule.py \
  tests/test_command_bridge.py -q
```

Result:

```text
317 passed in 0.49s
```

Earlier full suite:

```bash
.venv/bin/python -m pytest -q
```

Result:

```text
2072 passed, 7 skipped, 215 warnings in 145.23s
```

Latest targeted and full-suite results are recorded in
`Latest Local Acceptance Pass` above.

Manual mock E2E passed:

- `tool_call -> llm_transform -> /saynow`
- two runs re-called the weather tool
- two runs produced different greeting text
- `/saynow` received the resolved `greeting` value, not the variable name
- traces were saved
- `/schedulehome` replay of `/workflow run <id>` used the scheduled chat id

Additional natural-language processor checks:

- Stubbed `create_workflow` intent dispatched to `/workflow create ...`.
- `放音樂`, `播放音樂`, and `放我最愛的音樂` did not dispatch to `/music`; they
  returned the generic clarification menu.

Now verified locally by manual script:

- direct workflow command sinks can reach `/music playbest`.
- direct workflow command sinks can reach `/ir send ceiling_light power`.
- scheduled `/workflow run <id>` can execute both sinks and preserve `chat_id`.

Still required before #53 closes:

- Natural-language workflow draft generation that actually emits command sinks
  for `/music` and `/ir send ceiling_light power`.
- Replacement of the static positive allowlist with a registry-derived
  schedule-safe policy plus explicit denylist.

## Local Repro Script For Command Policy Bug

Run from repo root:

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from openclaw_adapter.task_workspace import Workflow, WorkflowStep, WorkflowStore
from openclaw_adapter.workflow_command import _cmd_run

class FakeExecutor:
    client = None
    def run_tool_step(self, slug, explicit_params):
        return True, "unused"

def reg(name):
    def handler(remainder, chat_id):
        return f"{name} ok: {remainder}"
    return SimpleNamespace(handler=handler)

with TemporaryDirectory() as td:
    store = WorkflowStore(Path(td) / "workflow_store")
    wf = Workflow(
        id="wf-registry-only-command",
        goal="registered schedule command should be accepted unless denied",
        steps=[
            WorkflowStep(
                id="custom",
                kind="command_sink",
                command="/musiclistall",
                literal="",
                output="out",
            )
        ],
    )
    store.save(wf)
    registry = {"/musiclistall": reg("/musiclistall")}
    reply = _cmd_run(
        "wf-registry-only-command",
        "chat-direct",
        store,
        FakeExecutor(),
        saynow_raw=None,
        settings=SimpleNamespace(),
        command_registry=registry,
    )
    print(reply)
PY
```

Current output:

```text
❌ wf-registry-only-command 失敗
工作流定義有誤：
Step custom: command '/musiclistall' is not in the allowlist [...]
```

Expected after the denylist-policy fix:

```text
✅ wf-registry-only-command 完成
/musiclistall ok:
```

## Local Regression Script For Web Capture

Run from repo root:

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from openclaw_adapter.command_bridge import CommandBridge
from openclaw_adapter.workflow_command import build_workflow_handler
from openclaw_adapter.workflow_editor import WorkflowEditor
from openclaw_adapter.task_workspace import WorkflowStore

class Runner:
    def __init__(self, root):
        self.tools_dir = Path(root) / "generated_tools"
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        self.catalog = None
        self.client = None

    def run_tool_step(self, slug, explicit_params):
        return True, "unused"

settings = SimpleNamespace(openclaw_voice_enabled=False)

with TemporaryDirectory() as td:
    runner = Runner(td)
    store = WorkflowStore(Path(td) / "workflow_store")
    editor = WorkflowEditor(store)

    import openclaw_adapter.voice_command as vc
    orig = vc.build_saynow_handler
    vc.build_saynow_handler = lambda s: (lambda text, chat_id=None: "saynow-ok")
    try:
        handler = build_workflow_handler(settings, runner, workflow_editor=editor)
    finally:
        vc.build_saynow_handler = orig

    bridge = CommandBridge.__new__(CommandBridge)
    bridge.settings = settings
    bridge._workflow_handler = handler
    bridge._workflow_editor = editor
    bridge._workflow_lock = None

    print(bridge.run_workflow_command("new"))
    print("capturing after new:", editor.is_capturing("web-workflow"))
    print(bridge.run_workflow_command("wf-web / Web goal"))
    print("workflow id:", repr(editor._sessions["web-workflow"].workflow.id))

    print(bridge.run_workflow_action("wfe:add"))
    print(bridge.run_workflow_action("wfe:kind:tool_call"))
    print("capturing tool:", editor.is_capturing("web-workflow"))
    print(bridge.run_workflow_command("city_weather"))
    print("adding fields:", editor._sessions["web-workflow"].adding.fields)
PY
```

Current fixed output includes:

```text
capturing after new: True
workflow id: 'wf-web'
capturing tool: True
adding fields: {'tool': 'city_weather'}
```

Regression symptoms would look like:

```text
message: /workflow help text
workflow id: ''
adding fields: {}
```

## Verification Required After Fix

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_task_workspace.py \
  tests/test_workflow_command.py \
  tests/test_workflow_editor.py \
  tests/test_home_schedule.py \
  tests/test_command_bridge.py -q

.venv/bin/python -m pytest -q
```

For docs-only changes, also run:

```bash
.venv/bin/python scripts/check_docs_health.py
```

If runtime state files are removed or `.gitignore` changes, confirm:

```bash
git status --short
git ls-files workflow_store
```

Expected after cleanup:

```text
git ls-files workflow_store
# no output
```
