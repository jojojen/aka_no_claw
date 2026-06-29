"""Tests for workflow_editor.py — card-based step editor (#53, Phase B+)."""

import json
import time
import pytest

from openclaw_adapter.task_workspace import Workflow, WorkflowStep, WorkflowStore
from openclaw_adapter.workflow_editor import (
    WorkflowEditor,
    _render_editor_card,
    _render_kind_picker,
    _SESSION_TTL,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_editor(tmp_path) -> WorkflowEditor:
    store = WorkflowStore(tmp_path / "workflows")
    return WorkflowEditor(store)


def _get_keyboard(markup: dict) -> list[list[dict]]:
    return markup.get("inline_keyboard", [])


def _callback_data(markup: dict) -> set[str]:
    return {btn["callback_data"] for row in _get_keyboard(markup) for btn in row}


# ── start_new ─────────────────────────────────────────────────────────────────

def test_start_new_returns_prompt(tmp_path):
    editor = _make_editor(tmp_path)
    text, _ = editor.start_new("chat-1")
    assert "ID" in text or "workflow" in text.lower() or "Workflow" in text


def test_start_new_creates_capturing_session(tmp_path):
    editor = _make_editor(tmp_path)
    editor.start_new("chat-1")
    assert editor.has_session("chat-1")
    assert editor.is_capturing("chat-1")


# ── goal collection ───────────────────────────────────────────────────────────

def test_collect_goal_with_slash_separator(tmp_path):
    editor = _make_editor(tmp_path)
    editor.start_new("chat-1")
    text, markup = editor.handle_text_capture("wf-test / テスト工作流", "chat-1")
    assert "wf-test" in text
    assert "テスト工作流" in text
    # After goal collected, no longer capturing
    assert not editor.is_capturing("chat-1")


def test_collect_goal_without_slash_uses_text_as_both(tmp_path):
    editor = _make_editor(tmp_path)
    editor.start_new("chat-1")
    editor.handle_text_capture("wf-simple", "chat-1")
    session = editor._sessions["chat-1"]
    assert session.workflow.id == "wf-simple"


def test_collect_goal_empty_id_prompts_retry(tmp_path):
    editor = _make_editor(tmp_path)
    editor.start_new("chat-1")
    text, _ = editor.handle_text_capture(" / テスト", "chat-1")
    assert "空" in text or "再" in text or "ID" in text
    # Still collecting
    assert editor.is_capturing("chat-1")


# ── editor card rendering ─────────────────────────────────────────────────────

def test_render_card_no_steps(tmp_path):
    editor = _make_editor(tmp_path)
    editor.start_new("chat-1")
    editor.handle_text_capture("wf-test / テスト", "chat-1")
    session = editor._sessions["chat-1"]
    text, markup = _render_editor_card(session)
    assert "wf-test" in text
    assert "テスト" in text
    assert "wfe:add" in _callback_data(markup)
    assert "wfe:save" in _callback_data(markup)
    assert "wfe:cancel" in _callback_data(markup)


def test_render_card_step_label_tool_call(tmp_path):
    editor = _make_editor(tmp_path)
    editor.start_new("chat-1")
    editor.handle_text_capture("wf-t / g", "chat-1")
    session = editor._sessions["chat-1"]
    session.workflow.steps.append(WorkflowStep(
        id="s1", kind="tool_call", tool="city-weather",
        args={"city": "東京"}, output="weather",
    ))
    text, markup = _render_editor_card(session)
    assert "city-weather" in text
    assert "weather" in text
    assert "wfe:del:0" in _callback_data(markup)


def test_render_card_step_label_command_sink(tmp_path):
    editor = _make_editor(tmp_path)
    editor.start_new("chat-1")
    editor.handle_text_capture("wf-t / g", "chat-1")
    session = editor._sessions["chat-1"]
    session.workflow.steps.append(WorkflowStep(
        id="s1", kind="command_sink", command="/saynow", input="greeting", output="out",
    ))
    text, _ = _render_editor_card(session)
    assert "/saynow" in text
    assert "greeting" in text


def test_render_card_step_label_llm_transform(tmp_path):
    editor = _make_editor(tmp_path)
    editor.start_new("chat-1")
    editor.handle_text_capture("wf-t / g", "chat-1")
    session = editor._sessions["chat-1"]
    session.workflow.steps.append(WorkflowStep(
        id="s1", kind="llm_transform", inputs=["weather"],
        instructions="greet", output="greeting",
    ))
    text, _ = _render_editor_card(session)
    assert "llm" in text
    assert "weather" in text
    assert "greeting" in text


# ── add step — tool_call sequence ─────────────────────────────────────────────

def _setup_session(editor, chat_id="chat-1"):
    editor.start_new(chat_id)
    editor.handle_text_capture("wf-test / テスト", chat_id)


def test_add_tool_call_full_sequence(tmp_path):
    editor = _make_editor(tmp_path)
    _setup_session(editor)

    # wfe:add → kind picker
    _, new_text, _ = editor._handle_callback("add", "", "chat-1")
    assert "tool" in new_text.lower() or "類型" in (new_text or "")

    # wfe:kind:tool_call → prompts for tool name
    _, prompt, _ = editor._handle_callback("kind:tool_call", "", "chat-1")
    assert "slug" in (prompt or "").lower() or "工具" in (prompt or "")
    assert editor.is_capturing("chat-1")

    # type tool name
    text, _ = editor.handle_text_capture("city-weather-abc", "chat-1")
    assert "args" in text.lower() or "Args" in text

    # type args
    text, _ = editor.handle_text_capture('{"city": "東京"}', "chat-1")
    assert "output" in text.lower() or "變數" in text

    # type output var
    text, markup = editor.handle_text_capture("weather", "chat-1")
    # Should show updated editor card now
    assert not editor.is_capturing("chat-1")
    session = editor._sessions["chat-1"]
    assert len(session.workflow.steps) == 1
    assert session.workflow.steps[0].kind == "tool_call"
    assert session.workflow.steps[0].tool == "city-weather-abc"
    assert session.workflow.steps[0].args == {"city": "東京"}
    assert session.workflow.steps[0].output == "weather"


def test_add_tool_call_args_empty_skipped(tmp_path):
    editor = _make_editor(tmp_path)
    _setup_session(editor)
    editor._handle_callback("kind:tool_call", "", "chat-1")
    editor.handle_text_capture("my-tool", "chat-1")   # tool name
    editor.handle_text_capture("", "chat-1")           # empty args → skip
    editor.handle_text_capture("out", "chat-1")        # output var
    session = editor._sessions["chat-1"]
    assert session.workflow.steps[0].args == {}


def test_add_tool_call_args_key_value_format(tmp_path):
    editor = _make_editor(tmp_path)
    _setup_session(editor)
    editor._handle_callback("kind:tool_call", "", "chat-1")
    editor.handle_text_capture("my-tool", "chat-1")
    editor.handle_text_capture("city=大阪, limit=5", "chat-1")
    session = editor._sessions["chat-1"]
    assert session.adding is not None
    assert session.adding.fields.get("args") == {"city": "大阪", "limit": "5"}


def test_add_tool_call_args_invalid_format_prompts_retry(tmp_path):
    editor = _make_editor(tmp_path)
    _setup_session(editor)
    editor._handle_callback("kind:tool_call", "", "chat-1")
    editor.handle_text_capture("my-tool", "chat-1")
    text, _ = editor.handle_text_capture("no equals sign here", "chat-1")
    assert "格式" in text or "JSON" in text
    assert editor.is_capturing("chat-1")


# ── add step — llm_transform sequence ────────────────────────────────────────

def test_add_llm_transform_full_sequence(tmp_path):
    editor = _make_editor(tmp_path)
    _setup_session(editor)
    editor._handle_callback("kind:llm_transform", "", "chat-1")
    assert editor.is_capturing("chat-1")

    editor.handle_text_capture("weather, city", "chat-1")    # inputs
    editor.handle_text_capture("用女僕口吻說早安", "chat-1")  # instructions
    editor.handle_text_capture("greeting", "chat-1")         # output var

    session = editor._sessions["chat-1"]
    step = session.workflow.steps[0]
    assert step.kind == "llm_transform"
    assert step.inputs == ["weather", "city"]
    assert step.instructions == "用女僕口吻說早安"
    assert step.output == "greeting"


# ── add step — command_sink sequence ─────────────────────────────────────────

def test_add_command_sink_full_sequence(tmp_path):
    editor = _make_editor(tmp_path)
    _setup_session(editor)

    # Choose kind → command_sink → shows command picker
    _, text, markup = editor._handle_callback("kind:command_sink", "", "chat-1")
    picker_buttons = {btn["callback_data"] for row in (markup or {}).get("inline_keyboard", []) for btn in row}
    assert "wfe:cmd:/saynow" in picker_buttons

    # Choose command
    _, prompt, _ = editor._handle_callback("cmd:/saynow", "", "chat-1")
    assert editor.is_capturing("chat-1")

    editor.handle_text_capture("greeting", "chat-1")    # input var
    editor.handle_text_capture("speech_out", "chat-1") # output var

    session = editor._sessions["chat-1"]
    step = session.workflow.steps[0]
    assert step.kind == "command_sink"
    assert step.command == "/saynow"
    assert step.input == "greeting"
    assert step.output == "speech_out"


def test_command_sink_non_allowlisted_rejected(tmp_path):
    editor = _make_editor(tmp_path)
    _setup_session(editor)
    toast, _, _ = editor._handle_callback("cmd:/rm-rf", "", "chat-1")
    assert "許可" in (toast or "") or "allowlist" in (toast or "").lower()


# ── delete step ───────────────────────────────────────────────────────────────

def test_del_step(tmp_path):
    editor = _make_editor(tmp_path)
    _setup_session(editor)
    session = editor._sessions["chat-1"]
    session.workflow.steps.append(WorkflowStep(id="s1", kind="tool_call", tool="t", output="a"))
    session.workflow.steps.append(WorkflowStep(id="s2", kind="tool_call", tool="u", output="b"))

    toast, _, _ = editor._handle_callback("del:0", "", "chat-1")
    assert len(session.workflow.steps) == 1
    assert session.workflow.steps[0].tool == "u"
    assert session.workflow.steps[0].id == "s1"  # re-numbered


def test_del_invalid_index(tmp_path):
    editor = _make_editor(tmp_path)
    _setup_session(editor)
    toast, _, _ = editor._handle_callback("del:99", "", "chat-1")
    assert "不存在" in (toast or "") or "步驟" in (toast or "")


# ── add_cancel ────────────────────────────────────────────────────────────────

def test_add_cancel_resets_adding(tmp_path):
    editor = _make_editor(tmp_path)
    _setup_session(editor)
    editor._handle_callback("kind:tool_call", "", "chat-1")
    assert editor.is_capturing("chat-1")
    editor._handle_callback("add_cancel", "", "chat-1")
    assert not editor.is_capturing("chat-1")
    assert editor._sessions["chat-1"].adding is None


# ── save ──────────────────────────────────────────────────────────────────────

def test_save_persists_workflow(tmp_path):
    store = WorkflowStore(tmp_path / "workflows")
    editor = WorkflowEditor(store)
    editor.start_new("chat-1")
    editor.handle_text_capture("wf-save-test / テスト保存", "chat-1")
    session = editor._sessions["chat-1"]
    session.workflow.steps.append(WorkflowStep(id="s1", kind="tool_call", tool="t", output="o"))

    toast, text, _ = editor._handle_callback("save", "", "chat-1")
    assert "✅" in (toast or "")
    assert store.get("wf-save-test") is not None
    assert "chat-1" not in editor._sessions  # session cleared


def test_save_empty_id_rejected(tmp_path):
    editor = _make_editor(tmp_path)
    editor.start_new("chat-1")
    # Don't collect goal → id remains ""
    session = editor._sessions["chat-1"]
    session.collecting = None  # clear collecting without setting an id
    toast, _, _ = editor._handle_callback("save", "", "chat-1")
    assert "ID" in (toast or "") or "⚠️" in (toast or "")
    assert "chat-1" in editor._sessions  # session NOT cleared


def test_save_invalid_refs_rejected(tmp_path):
    editor = _make_editor(tmp_path)
    _setup_session(editor)
    session = editor._sessions["chat-1"]
    # command_sink references "ghost" which no step produces
    session.workflow.steps.append(WorkflowStep(
        id="s1", kind="command_sink", command="/saynow", input="ghost", output="out",
    ))
    toast, _, _ = editor._handle_callback("save", "", "chat-1")
    assert "⚠️" in (toast or "") or "有誤" in (toast or "")
    assert "chat-1" in editor._sessions  # session NOT cleared


# ── cancel ────────────────────────────────────────────────────────────────────

def test_cancel_clears_session(tmp_path):
    editor = _make_editor(tmp_path)
    _setup_session(editor)
    editor._handle_callback("cancel", "", "chat-1")
    assert not editor.has_session("chat-1")


# ── expired session ───────────────────────────────────────────────────────────

def test_expired_session_cleared_on_gc(tmp_path):
    editor = _make_editor(tmp_path)
    _setup_session(editor)
    editor._sessions["chat-1"].created_at = time.time() - _SESSION_TTL - 1
    editor._gc()
    assert not editor.has_session("chat-1")


def test_expired_session_returns_error_on_callback(tmp_path):
    editor = _make_editor(tmp_path)
    _setup_session(editor)
    editor._sessions["chat-1"].created_at = time.time() - _SESSION_TTL - 1
    toast, _, _ = editor._handle_callback("save", "", "chat-1")
    assert "過期" in (toast or "") or "⚠️" in (toast or "")


# ── start_edit ────────────────────────────────────────────────────────────────

def test_start_edit_loads_existing_workflow(tmp_path):
    store = WorkflowStore(tmp_path / "workflows")
    wf = Workflow(id="wf-existing", goal="既存工作流", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="t", output="data"),
    ])
    store.save(wf)
    editor = WorkflowEditor(store)
    text, markup = editor.start_edit("chat-1", "wf-existing")
    assert "wf-existing" in text
    assert "wfe:save" in _callback_data(markup)


def test_start_edit_unknown_id(tmp_path):
    editor = _make_editor(tmp_path)
    text, _ = editor.start_edit("chat-1", "nonexistent")
    assert "找不到" in text
    assert not editor.has_session("chat-1")


# ── start_from_draft (AI-generated draft → editable card) ─────────────────────

def _morning_draft() -> Workflow:
    return Workflow(id="wf-morning-greeting", goal="每天早上查東京天氣並念出來", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="city_weather",
                     args={"city": "東京"}, output="weather"),
        WorkflowStep(id="s2", kind="llm_transform", inputs=["weather"],
                     instructions="用女僕口吻說日文報天氣跟早安", output="greeting"),
        WorkflowStep(id="s3", kind="command_sink", command="/saynow",
                     input="greeting", output="spoken"),
    ])


def test_start_from_draft_lands_in_editable_card(tmp_path):
    editor = _make_editor(tmp_path)
    text, markup = editor.start_from_draft("chat-1", _morning_draft())
    assert "wf-morning-greeting" in text
    assert editor.has_session("chat-1")
    cb = _callback_data(markup)
    # editable card affordances present: per-step edit + add + save + cancel
    assert "wfe:edit:0" in cb
    assert "wfe:add" in cb
    assert "wfe:save" in cb
    assert "wfe:cancel" in cb


def test_start_from_draft_does_not_mutate_source(tmp_path):
    editor = _make_editor(tmp_path)
    draft = _morning_draft()
    editor.start_from_draft("chat-1", draft)
    editor._handle_callback("del:0", "", "chat-1")
    # source workflow untouched (editor clones)
    assert len(draft.steps) == 3


def test_draft_card_has_reorder_buttons(tmp_path):
    editor = _make_editor(tmp_path)
    _, markup = editor.start_from_draft("chat-1", _morning_draft())
    cb = _callback_data(markup)
    # first step can go down but not up; last can go up but not down
    assert "wfe:down:0" in cb
    assert "wfe:up:0" not in cb
    assert "wfe:up:2" in cb
    assert "wfe:down:2" not in cb


# ── per-step edit ─────────────────────────────────────────────────────────────

def test_edit_tool_call_replaces_in_place(tmp_path):
    editor = _make_editor(tmp_path)
    editor.start_from_draft("chat-1", _morning_draft())
    editor._handle_callback("edit:0", "", "chat-1")
    assert editor.is_capturing("chat-1")
    editor.handle_text_capture("city_weather_v2", "chat-1")   # new tool
    editor.handle_text_capture("city=大阪", "chat-1")          # new args
    editor.handle_text_capture("weather", "chat-1")            # output var
    session = editor._sessions["chat-1"]
    assert len(session.workflow.steps) == 3            # replaced, not appended
    assert session.workflow.steps[0].tool == "city_weather_v2"
    assert session.workflow.steps[0].args == {"city": "大阪"}
    assert session.workflow.steps[0].id == "s1"        # id preserved


def test_edit_command_sink_keeps_command(tmp_path):
    editor = _make_editor(tmp_path)
    editor.start_from_draft("chat-1", _morning_draft())
    editor._handle_callback("edit:2", "", "chat-1")
    editor.handle_text_capture("greeting", "chat-1")   # input var
    editor.handle_text_capture("spoken2", "chat-1")    # output var
    step = editor._sessions["chat-1"].workflow.steps[2]
    assert step.kind == "command_sink"
    assert step.command == "/saynow"
    assert step.output == "spoken2"


def test_edit_invalid_index(tmp_path):
    editor = _make_editor(tmp_path)
    editor.start_from_draft("chat-1", _morning_draft())
    toast, _, _ = editor._handle_callback("edit:99", "", "chat-1")
    assert "不存在" in (toast or "")


# ── reorder ───────────────────────────────────────────────────────────────────

def test_reorder_down_swaps_and_renumbers(tmp_path):
    editor = _make_editor(tmp_path)
    editor.start_from_draft("chat-1", _morning_draft())
    editor._handle_callback("down:0", "", "chat-1")
    steps = editor._sessions["chat-1"].workflow.steps
    assert steps[0].kind == "llm_transform"
    assert steps[1].kind == "tool_call"
    assert [s.id for s in steps] == ["s1", "s2", "s3"]   # renumbered


def test_reorder_up_swaps(tmp_path):
    editor = _make_editor(tmp_path)
    editor.start_from_draft("chat-1", _morning_draft())
    editor._handle_callback("up:2", "", "chat-1")
    steps = editor._sessions["chat-1"].workflow.steps
    assert steps[1].kind == "command_sink"
    assert steps[2].kind == "llm_transform"


def test_reorder_out_of_bounds_noop(tmp_path):
    editor = _make_editor(tmp_path)
    editor.start_from_draft("chat-1", _morning_draft())
    toast, _, _ = editor._handle_callback("up:0", "", "chat-1")
    assert "無法" in (toast or "")
    assert len(editor._sessions["chat-1"].workflow.steps) == 3


# ── noop ─────────────────────────────────────────────────────────────────────

def test_noop_returns_none(tmp_path):
    editor = _make_editor(tmp_path)
    _setup_session(editor)
    toast, text, markup = editor._handle_callback("noop", "", "chat-1")
    assert toast is None
    assert text is None
    assert markup is None


# ── no session callbacks ──────────────────────────────────────────────────────

def test_callback_with_no_session_warns(tmp_path):
    editor = _make_editor(tmp_path)
    toast, _, _ = editor._handle_callback("save", "", "chat-unknown")
    assert "過期" in (toast or "") or "⚠️" in (toast or "")
