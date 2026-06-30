"""Tests for workflow_command.py — /workflow subcommands (#53, Phase B)."""

import json
import pytest

from openclaw_adapter.task_workspace import Workflow, WorkflowStep, WorkflowStore
from openclaw_adapter.workflow_command import (
    _cmd_create,
    _cmd_delete,
    _cmd_list,
    _cmd_run,
    _cmd_show,
    _cmd_traces,
    _extract_json_object,
    _generate_workflow_from_nl,
    _help,
    build_workflow_handler,
)


# ── Fakes for NL-draft generation ─────────────────────────────────────────────

_MORNING_JSON = json.dumps({
    "id": "wf-morning-greeting",
    "goal": "每天早上查東京天氣，用女僕口吻說日文報天氣跟早安，然後念出來",
    "steps": [
        {"id": "s1", "kind": "tool_call", "tool": "city_weather",
         "args": {"city": "東京"}, "output": "weather"},
        {"id": "s2", "kind": "llm_transform", "inputs": ["weather"],
         "instructions": "用女僕口吻說日文報天氣跟早安", "output": "greeting"},
        {"id": "s3", "kind": "command_sink", "command": "/saynow",
         "input": "greeting", "output": "spoken"},
    ],
}, ensure_ascii=False)


class _FakeLLM:
    def __init__(self, response):
        self.response = response
        self.prompts = []

    def generate(self, prompt, *, temperature=0.0):
        self.prompts.append(prompt)
        return self.response


class _FakeEditorDraft:
    def __init__(self):
        self.drafts = []

    def start_from_draft(self, chat_id, workflow):
        self.drafts.append((chat_id, workflow))
        return f"draft-card:{workflow.id}", {"inline_keyboard": []}


# ── FakeExecutor ──────────────────────────────────────────────────────────────

class FakeExecutor:
    """Minimal ToolCallExecutor + tools_dir attribute."""

    def __init__(self, tmp_path, responses=None):
        self.tools_dir = tmp_path / "generated_tools"
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        self.client = None
        self._responses = responses or {}
        self.calls = []

    def run_tool_step(self, slug, explicit_params):
        self.calls.append((slug, explicit_params))
        return self._responses.get(slug, (False, f"no response for {slug!r}"))


def _make_store(tmp_path):
    return WorkflowStore(tmp_path / "workflow_store")


def _simple_wf(wf_id="wf-test"):
    return Workflow(
        id=wf_id,
        goal="テスト工作流",
        steps=[
            WorkflowStep(id="s1", kind="tool_call", tool="t",
                         args={"city": "東京"}, output="weather"),
        ],
    )


# ── /workflow list ────────────────────────────────────────────────────────────

def test_cmd_list_empty(tmp_path):
    reply = _cmd_list(_make_store(tmp_path))
    assert "尚無" in reply


def test_cmd_list_shows_ids_and_goals(tmp_path):
    store = _make_store(tmp_path)
    store.save(Workflow(id="wf-a", goal="A の目標"))
    store.save(Workflow(id="wf-b", goal="B の目標"))
    text, markup = _cmd_list(store)
    assert "wf-a" in text
    assert "wf-b" in text
    assert "A の目標" in text
    # Each workflow gets a "排程執行" action button with add_for_wf callback.
    cbs = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
    assert "add_for_wf wf-a" in cbs
    assert "add_for_wf wf-b" in cbs


def test_cmd_list_shows_step_count(tmp_path):
    store = _make_store(tmp_path)
    store.save(_simple_wf())
    text, _ = _cmd_list(store)
    assert "1 步驟" in text


# ── /workflow show ────────────────────────────────────────────────────────────

def test_cmd_show_known(tmp_path):
    store = _make_store(tmp_path)
    store.save(_simple_wf())
    reply = _cmd_show("wf-test", store)
    assert "wf-test" in reply
    assert "テスト工作流" in reply
    assert "tool" in reply
    assert "weather" in reply


def test_cmd_show_unknown(tmp_path):
    reply = _cmd_show("nonexistent", _make_store(tmp_path))
    assert "找不到" in reply
    assert "nonexistent" in reply


def test_cmd_show_missing_id(tmp_path):
    reply = _cmd_show("", _make_store(tmp_path))
    assert "用法" in reply


def test_cmd_show_command_sink_step(tmp_path):
    store = _make_store(tmp_path)
    wf = Workflow(id="wf-sink", goal="g", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="t", output="data"),
        WorkflowStep(id="s2", kind="command_sink", command="/saynow",
                     input="data", output="out"),
    ])
    store.save(wf)
    reply = _cmd_show("wf-sink", store)
    assert "/saynow" in reply
    assert "data" in reply


def test_cmd_show_llm_transform_step(tmp_path):
    store = _make_store(tmp_path)
    wf = Workflow(id="wf-llm", goal="g", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="t", output="data"),
        WorkflowStep(id="s2", kind="llm_transform", inputs=["data"],
                     instructions="用女僕口吻", output="greeting"),
    ])
    store.save(wf)
    reply = _cmd_show("wf-llm", store)
    assert "llm" in reply
    assert "用女僕口吻" in reply


# ── /workflow delete ──────────────────────────────────────────────────────────

def test_cmd_delete_known(tmp_path):
    store = _make_store(tmp_path)
    store.save(_simple_wf())
    reply = _cmd_delete("wf-test", store)
    assert "✅" in reply
    assert store.get("wf-test") is None


def test_cmd_delete_unknown(tmp_path):
    reply = _cmd_delete("nonexistent", _make_store(tmp_path))
    assert "找不到" in reply


def test_cmd_delete_missing_id(tmp_path):
    reply = _cmd_delete("", _make_store(tmp_path))
    assert "用法" in reply


# ── /workflow create ──────────────────────────────────────────────────────────

def test_cmd_create_valid_json(tmp_path):
    store = _make_store(tmp_path)
    data = {"id": "wf-new", "goal": "新工作流", "steps": []}
    reply = _cmd_create(json.dumps(data), store)
    assert "✅" in reply
    assert "wf-new" in reply
    assert store.get("wf-new") is not None


def test_cmd_create_invalid_json(tmp_path):
    reply = _cmd_create("{not valid json", _make_store(tmp_path))
    assert "JSON" in reply or "格式" in reply


def test_cmd_create_missing_id_key(tmp_path):
    reply = _cmd_create(json.dumps({"goal": "no id", "steps": []}), _make_store(tmp_path))
    assert "錯誤" in reply


def test_cmd_create_invalid_workflow_refs(tmp_path):
    # command_sink references a var not produced by any prior step
    data = {
        "id": "wf-bad",
        "goal": "bad",
        "steps": [
            {"id": "s1", "kind": "command_sink", "command": "/saynow",
             "input": "ghost_var", "output": "out"},
        ],
    }
    reply = _cmd_create(json.dumps(data), _make_store(tmp_path))
    assert "有誤" in reply or "ghost_var" in reply


def test_cmd_create_empty_arg(tmp_path):
    reply = _cmd_create("", _make_store(tmp_path))
    assert "用法" in reply


# ── /workflow create — natural language → editable draft ─────────────────────

def test_cmd_create_nl_generates_draft_and_lands_in_editor(tmp_path):
    store = _make_store(tmp_path)
    llm = _FakeLLM(_MORNING_JSON)
    editor = _FakeEditorDraft()
    reply = _cmd_create(
        "每天早上查東京天氣，用女僕口吻說日文報天氣跟早安，然後念出來",
        store, "chat-9", llm_client=llm, catalog=None, editor=editor,
    )
    # Routed to the editor as an editable draft, not saved directly.
    assert editor.drafts, "expected start_from_draft to be called"
    chat_id, wf = editor.drafts[0]
    assert chat_id == "chat-9"
    assert wf.id == "wf-morning-greeting"
    assert [s.kind for s in wf.steps] == ["tool_call", "llm_transform", "command_sink"]
    assert reply[0] == "draft-card:wf-morning-greeting"


def test_cmd_create_nl_handles_code_fenced_json(tmp_path):
    store = _make_store(tmp_path)
    fenced = "```json\n" + _MORNING_JSON + "\n```"
    llm = _FakeLLM(fenced)
    editor = _FakeEditorDraft()
    _cmd_create("造一個早安工作流", store, "c", llm_client=llm, catalog=None, editor=editor)
    assert editor.drafts and editor.drafts[0][1].id == "wf-morning-greeting"


def test_cmd_create_nl_without_editor_or_llm_explains(tmp_path):
    store = _make_store(tmp_path)
    reply = _cmd_create("做個早安工作流", store, "c", llm_client=None, catalog=None, editor=None)
    assert "自然語言" in reply or "未啟用" in reply


def test_cmd_create_nl_llm_bad_output_reports(tmp_path):
    store = _make_store(tmp_path)
    llm = _FakeLLM("抱歉我不知道怎麼做")   # no JSON
    editor = _FakeEditorDraft()
    reply = _cmd_create("xxx", store, "c", llm_client=llm, catalog=None, editor=editor)
    assert "❌" in reply
    assert not editor.drafts


def test_cmd_create_nl_surfaces_local_fallback_warning(tmp_path):
    store = _make_store(tmp_path)
    llm = _FakeLLM(_MORNING_JSON)
    editor = _FakeEditorDraft()
    warn = "⚠️ 雲端模型（big-pickle）目前無法使用，已改用本地模型生成草稿。\n\n"
    text, _ = _cmd_create(
        "早安工作流", store, "c",
        llm_client=llm, catalog=None, editor=editor, client_warning=warn,
    )
    assert text.startswith("⚠️")
    assert "本地模型" in text


def test_cmd_create_json_path_still_saves_directly(tmp_path):
    store = _make_store(tmp_path)
    editor = _FakeEditorDraft()
    data = {"id": "wf-json", "goal": "json路徑", "steps": []}
    reply = _cmd_create(json.dumps(data), store, "c",
                        llm_client=_FakeLLM("unused"), catalog=None, editor=editor)
    assert "✅" in reply
    assert store.get("wf-json") is not None
    assert not editor.drafts   # JSON path bypasses the draft editor


# ── helpers: JSON extraction + generation ────────────────────────────────────

def test_extract_json_object_plain():
    assert _extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_json_object_with_prose_and_fence():
    text = "好的，這是草稿：\n```json\n{\"id\": \"wf-x\"}\n```\n希望符合需求"
    assert _extract_json_object(text) == {"id": "wf-x"}


def test_extract_json_object_garbage_returns_none():
    assert _extract_json_object("no json here") is None
    assert _extract_json_object("") is None


def test_generate_workflow_from_nl_fills_missing_id_and_goal():
    llm = _FakeLLM(json.dumps({"goal": "", "steps": []}))   # no id, empty goal
    wf, err = _generate_workflow_from_nl("我的描述", llm, None)
    assert err is None
    assert wf.id          # backfilled
    assert wf.goal == "我的描述"


def test_generate_workflow_prompt_includes_catalog_slugs():
    class _Entry:
        def __init__(self, slug, desc):
            self.slug = slug
            self.description = desc

    class _Catalog:
        def entries(self):
            return [_Entry("city_weather_abc", "查城市天氣")]

    llm = _FakeLLM(_MORNING_JSON)
    _generate_workflow_from_nl("造工作流", llm, _Catalog())
    assert "city_weather_abc" in llm.prompts[0]


# ── /workflow run ─────────────────────────────────────────────────────────────

def test_cmd_run_ok(tmp_path):
    store = _make_store(tmp_path)
    store.save(_simple_wf())
    executor = FakeExecutor(tmp_path, {"t": (True, "東京：晴れ")})
    reply = _cmd_run("wf-test", "chat-1", store, executor, _noop_saynow, None)
    assert "✅" in reply
    assert "東京：晴れ" in reply


def test_cmd_run_tool_fails(tmp_path):
    store = _make_store(tmp_path)
    store.save(_simple_wf())
    executor = FakeExecutor(tmp_path, {"t": (False, "接続タイムアウト")})
    reply = _cmd_run("wf-test", "chat-1", store, executor, _noop_saynow, None)
    assert "❌" in reply


def test_cmd_run_unknown_workflow(tmp_path):
    store = _make_store(tmp_path)
    executor = FakeExecutor(tmp_path)
    reply = _cmd_run("nonexistent", "chat-1", store, executor, _noop_saynow, None)
    assert "找不到" in reply


def test_cmd_run_missing_id(tmp_path):
    store = _make_store(tmp_path)
    executor = FakeExecutor(tmp_path)
    reply = _cmd_run("", "chat-1", store, executor, _noop_saynow, None)
    assert "用法" in reply


def test_cmd_run_saves_trace(tmp_path):
    store = _make_store(tmp_path)
    store.save(_simple_wf())
    executor = FakeExecutor(tmp_path, {"t": (True, "晴れ")})
    _cmd_run("wf-test", "chat-1", store, executor, _noop_saynow, None)
    traces = store.list_traces("wf-test")
    assert len(traces) == 1
    assert traces[0].ok


def test_cmd_run_with_saynow_sink(tmp_path):
    """tool_call -> command_sink pipeline through _cmd_run."""
    spoken: list[str] = []

    def saynow_raw(text, chat_id=None):
        spoken.append(text)
        return "🔊 OK"

    store = _make_store(tmp_path)
    wf = Workflow(id="wf-speak", goal="speak", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="t", output="msg"),
        WorkflowStep(id="s2", kind="command_sink", command="/saynow",
                     input="msg", output="out"),
    ])
    store.save(wf)
    executor = FakeExecutor(tmp_path, {"t": (True, "おはようございます")})
    reply = _cmd_run("wf-speak", "chat-x", store, executor, saynow_raw, None)
    assert "✅" in reply
    assert spoken == ["おはようございます"]


def test_cmd_run_saynow_receives_chat_id(tmp_path):
    """Confirm the /saynow sink is called with the correct chat_id."""
    received_chat_id: list[str] = []

    def saynow_raw(text, chat_id=None):
        received_chat_id.append(chat_id)
        return "ok"

    store = _make_store(tmp_path)
    wf = Workflow(id="wf-cid", goal="g", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="t", output="msg"),
        WorkflowStep(id="s2", kind="command_sink", command="/saynow",
                     input="msg", output="out"),
    ])
    store.save(wf)
    executor = FakeExecutor(tmp_path, {"t": (True, "hello")})
    _cmd_run("wf-cid", "my-chat-42", store, executor, saynow_raw, None)
    assert received_chat_id == ["my-chat-42"]


# ── /workflow new and edit (card editor subcommands) ─────────────────────────

class _FakeSettings:
    """Minimal settings stub for build_workflow_handler (avoids voice imports)."""


class _FakeEditor:
    def __init__(self):
        self.new_calls: list[str] = []
        self.edit_calls: list[tuple] = []

    def start_new(self, chat_id):
        self.new_calls.append(chat_id)
        return "new-card", {"inline_keyboard": []}

    def start_edit(self, chat_id, workflow_id):
        self.edit_calls.append((chat_id, workflow_id))
        return f"edit-{workflow_id}", {"inline_keyboard": []}


def _make_handler(tmp_path, editor=None):
    executor = FakeExecutor(tmp_path)
    # Patch out voice import by giving settings a noop saynow
    import types
    settings = types.SimpleNamespace(
        openclaw_voice_enabled=False,
    )
    # build_workflow_handler imports voice lazily; provide a minimal stub
    import openclaw_adapter.workflow_command as wc
    original = None
    try:
        from openclaw_adapter import voice_command as _vc
        original = getattr(_vc, "build_saynow_handler", None)
        _vc.build_saynow_handler = lambda s: (lambda text, chat_id=None: "noop")
    except ImportError:
        pass
    h = build_workflow_handler(settings, executor, workflow_editor=editor)
    if original is not None:
        from openclaw_adapter import voice_command as _vc
        _vc.build_saynow_handler = original
    return h


def test_workflow_new_delegates_to_editor(tmp_path):
    editor = _FakeEditor()
    handler = _make_handler(tmp_path, editor=editor)
    result = handler("new", "chat-5")
    assert editor.new_calls == ["chat-5"]
    assert result[0] == "new-card"


def test_workflow_edit_delegates_to_editor(tmp_path):
    editor = _FakeEditor()
    handler = _make_handler(tmp_path, editor=editor)
    result = handler("edit wf-foo", "chat-5")
    assert editor.edit_calls == [("chat-5", "wf-foo")]
    assert "wf-foo" in result[0]


def test_workflow_edit_missing_id(tmp_path):
    editor = _FakeEditor()
    handler = _make_handler(tmp_path, editor=editor)
    result = handler("edit", "chat-5")
    assert "用法" in result


def test_workflow_new_no_editor(tmp_path):
    handler = _make_handler(tmp_path, editor=None)
    result = handler("new", "chat-5")
    assert "未啟用" in result


# ── /workflow help ────────────────────────────────────────────────────────────

def test_help_text():
    h = _help()
    for sub in ["new", "edit", "list", "show", "run", "delete", "create", "traces"]:
        assert sub in h


# ── /workflow traces ──────────────────────────────────────────────────────────

def test_cmd_traces_empty(tmp_path):
    store = _make_store(tmp_path)
    store.save(_simple_wf())
    reply = _cmd_traces("wf-test", store)
    assert "尚無" in reply


def test_cmd_traces_missing_id(tmp_path):
    reply = _cmd_traces("", _make_store(tmp_path))
    assert "用法" in reply


def test_cmd_traces_shows_ok_entries(tmp_path):
    store = _make_store(tmp_path)
    store.save(_simple_wf())
    executor = FakeExecutor(tmp_path, {"t": (True, "東京：晴れ")})
    _cmd_run("wf-test", "c", store, executor, _noop_saynow, None)
    _cmd_run("wf-test", "c", store, executor, _noop_saynow, None)
    reply = _cmd_traces("wf-test", store)
    assert "2 回" in reply
    assert "✅" in reply


def test_cmd_traces_shows_failed_entries(tmp_path):
    store = _make_store(tmp_path)
    store.save(_simple_wf())
    executor = FakeExecutor(tmp_path, {"t": (False, "タイムアウト")})
    _cmd_run("wf-test", "c", store, executor, _noop_saynow, None)
    reply = _cmd_traces("wf-test", store)
    assert "❌" in reply


def test_cmd_traces_limits_to_five(tmp_path):
    store = _make_store(tmp_path)
    store.save(_simple_wf())
    executor = FakeExecutor(tmp_path, {"t": (True, "ok")})
    for _ in range(7):
        _cmd_run("wf-test", "c", store, executor, _noop_saynow, None)
    reply = _cmd_traces("wf-test", store)
    assert "7 回" in reply           # total count shown
    assert reply.count("[") <= 5    # only 5 entries rendered


# ── Helpers ───────────────────────────────────────────────────────────────────

def _noop_saynow(text, chat_id=None):
    return "🔊 noop"
