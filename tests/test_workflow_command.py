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
    _help,
)


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
    reply = _cmd_list(store)
    assert "wf-a" in reply
    assert "wf-b" in reply
    assert "A の目標" in reply


def test_cmd_list_shows_step_count(tmp_path):
    store = _make_store(tmp_path)
    store.save(_simple_wf())
    reply = _cmd_list(store)
    assert "1 步驟" in reply


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


# ── /workflow help ────────────────────────────────────────────────────────────

def test_help_text():
    h = _help()
    for sub in ["list", "show", "run", "delete", "create"]:
        assert sub in h


# ── Helpers ───────────────────────────────────────────────────────────────────

def _noop_saynow(text, chat_id=None):
    return "🔊 noop"
