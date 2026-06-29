"""Tests for task_workspace.py — Phase 1 (#53).

Coverage:
  Schema:     Workflow/WorkflowStep to_dict / from_dict roundtrip; validate_references.
  VariableStore: bind / resolve / missing key.
  WorkflowRunner: tool_call ok/fail, command_sink ok/not-allowlisted/no-handler/
                  missing-input/$ref-arg, failure halting (skipped), llm_transform stub,
                  invalid workflow short-circuit, full 2-step pipeline trace.
"""

import pytest

from openclaw_adapter.task_workspace import (
    COMMAND_SINK_ALLOWLIST,
    StepTrace,
    ToolCallExecutor,
    Variable,
    VariableStore,
    Workflow,
    WorkflowRunner,
    WorkflowStep,
    WorkflowTrace,
)


# ── Fake executor ─────────────────────────────────────────────────────────────

class FakeExecutor:
    """Controllable stand-in for DynamicToolRunner.run_tool_step."""

    def __init__(self, responses: dict[str, tuple[bool, str]] | None = None) -> None:
        # slug -> (ok, text)
        self._responses: dict[str, tuple[bool, str]] = responses or {}
        self.calls: list[tuple[str, dict]] = []

    def set(self, slug: str, ok: bool, text: str) -> None:
        self._responses[slug] = (ok, text)

    def run_tool_step(self, slug: str, explicit_params: dict) -> tuple[bool, str]:
        self.calls.append((slug, explicit_params))
        return self._responses.get(slug, (False, f"no response configured for {slug!r}"))


def _make_runner(
    responses: dict[str, tuple[bool, str]] | None = None,
    commands: dict | None = None,
) -> tuple[FakeExecutor, WorkflowRunner]:
    ex = FakeExecutor(responses)
    runner = WorkflowRunner(executor=ex, command_dispatcher=commands)
    return ex, runner


# ── Schema roundtrip ──────────────────────────────────────────────────────────

def test_workflow_step_roundtrip_tool_call():
    step = WorkflowStep(
        id="s1", kind="tool_call", tool="weather-abc123",
        args={"city": "東京"}, output="weather",
    )
    assert WorkflowStep.from_dict(step.to_dict()) == step


def test_workflow_step_roundtrip_command_sink():
    step = WorkflowStep(
        id="s3", kind="command_sink", command="/saynow",
        input="greeting", output="speech_result",
    )
    assert WorkflowStep.from_dict(step.to_dict()) == step


def test_workflow_step_roundtrip_llm_transform():
    step = WorkflowStep(
        id="s2", kind="llm_transform",
        inputs=["weather"], instructions="用女僕口吻說早安", output="greeting",
    )
    assert WorkflowStep.from_dict(step.to_dict()) == step


def test_workflow_roundtrip():
    wf = Workflow(
        id="wf-morning",
        goal="早安工作流",
        steps=[
            WorkflowStep(id="s1", kind="tool_call", tool="w-abc", args={"city": "東京"}, output="weather"),
            WorkflowStep(id="s2", kind="command_sink", command="/saynow", input="weather", output="out"),
        ],
    )
    assert Workflow.from_dict(wf.to_dict()) == wf


# ── validate_references ───────────────────────────────────────────────────────

def test_validate_references_clean_workflow():
    wf = Workflow(id="wf", goal="g", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="t", args={"city": "東京"}, output="weather"),
        WorkflowStep(id="s2", kind="command_sink", command="/saynow", input="weather", output="out"),
    ])
    assert wf.validate_references() == []


def test_validate_references_missing_command_sink_input():
    wf = Workflow(id="wf", goal="g", steps=[
        # s1 produces "weather", but s2 references "greeting" which doesn't exist
        WorkflowStep(id="s1", kind="tool_call", tool="t", output="weather"),
        WorkflowStep(id="s2", kind="command_sink", command="/saynow", input="greeting", output="out"),
    ])
    errors = wf.validate_references()
    assert any("greeting" in e for e in errors)


def test_validate_references_forward_ref_in_args():
    # Step s1 tries to use $weather but weather is produced by s2
    wf = Workflow(id="wf", goal="g", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="t", args={"x": "$weather"}, output="result"),
        WorkflowStep(id="s2", kind="tool_call", tool="w", output="weather"),
    ])
    errors = wf.validate_references()
    assert any("weather" in e for e in errors)


def test_validate_references_non_allowlisted_command():
    wf = Workflow(id="wf", goal="g", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="t", output="data"),
        WorkflowStep(id="s2", kind="command_sink", command="/rm-rf", input="data", output="out"),
    ])
    errors = wf.validate_references()
    assert any("/rm-rf" in e for e in errors)


def test_validate_references_missing_tool():
    wf = Workflow(id="wf", goal="g", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool=None, output="data"),
    ])
    errors = wf.validate_references()
    assert any("missing 'tool'" in e for e in errors)


def test_validate_references_llm_transform_forward_ref():
    wf = Workflow(id="wf", goal="g", steps=[
        WorkflowStep(id="s1", kind="llm_transform", inputs=["weather"],
                     instructions="transform", output="greeting"),
        WorkflowStep(id="s2", kind="tool_call", tool="w", output="weather"),
    ])
    errors = wf.validate_references()
    assert any("weather" in e for e in errors)


# ── VariableStore ─────────────────────────────────────────────────────────────

def test_variable_store_bind_and_resolve():
    store = VariableStore()
    store.bind("weather", "東京：晴れ", source_step="s1", provenance="weather()")
    assert store.resolve("weather") == "東京：晴れ"


def test_variable_store_missing_raises():
    store = VariableStore()
    with pytest.raises(KeyError, match="weather"):
        store.resolve("weather")


def test_variable_store_get_returns_none_for_missing():
    store = VariableStore()
    assert store.get("x") is None


def test_variable_store_snapshot():
    store = VariableStore()
    store.bind("a", "v1", "s1", "p1")
    store.bind("b", "v2", "s2", "p2")
    snap = store.snapshot()
    assert set(snap.keys()) == {"a", "b"}
    assert snap["a"].value == "v1"


def test_variable_store_provenance_recorded():
    store = VariableStore()
    var = store.bind("weather", "晴れ", "s1", "city_weather({'city':'東京'})", type_="weather_summary")
    assert var.type == "weather_summary"
    assert var.provenance == "city_weather({'city':'東京'})"
    assert var.source_step == "s1"


# ── WorkflowRunner — tool_call ────────────────────────────────────────────────

def test_tool_call_ok_binds_variable():
    ex, runner = _make_runner({"weather-slug": (True, "東京：晴れ")})
    wf = Workflow(id="wf", goal="g", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="weather-slug",
                     args={"city": "東京"}, output="weather"),
    ])
    trace = runner.run(wf)
    assert trace.ok
    assert trace.variables["weather"].value == "東京：晴れ"
    assert trace.final_result == "東京：晴れ"
    assert trace.steps[0].status == "ok"
    assert trace.steps[0].output_var == "weather"


def test_tool_call_fail_marks_failed():
    ex, runner = _make_runner({"bad-slug": (False, "接続エラー")})
    wf = Workflow(id="wf", goal="g", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="bad-slug", output="data"),
    ])
    trace = runner.run(wf)
    assert not trace.ok
    assert trace.steps[0].status == "failed"
    assert "接続エラー" in (trace.steps[0].error or "")


def test_tool_call_passes_explicit_params():
    ex, runner = _make_runner({"t": (True, "ok")})
    wf = Workflow(id="wf", goal="g", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="t", args={"city": "大阪"}, output="out"),
    ])
    runner.run(wf)
    assert ex.calls[0] == ("t", {"city": "大阪"})


def test_tool_call_resolves_dollar_ref_in_args():
    ex = FakeExecutor()
    ex.set("tool-a", True, "result-A")
    ex.set("tool-b", True, "result-B")
    runner = WorkflowRunner(executor=ex)
    wf = Workflow(id="wf", goal="g", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="tool-a", args={}, output="data"),
        WorkflowStep(id="s2", kind="tool_call", tool="tool-b",
                     args={"input": "$data"}, output="final"),
    ])
    trace = runner.run(wf)
    assert trace.ok
    # s2 should have received the resolved value "result-A"
    assert ex.calls[1] == ("tool-b", {"input": "result-A"})


def test_tool_call_dollar_ref_missing_fails():
    ex, runner = _make_runner()
    wf = Workflow(id="wf", goal="g", steps=[
        # validate_references won't catch this if the ref typo passes validation
        # We force it by building a Workflow without calling validate_references
        WorkflowStep(id="s1", kind="tool_call", tool="t",
                     args={"x": "$nonexistent"}, output="out"),
    ])
    # Bypass validate_references to test runtime resolution
    store = VariableStore()
    step = wf.steps[0]
    step_trace, var_name = runner._run_tool_call(step, store)
    assert step_trace.status == "failed"
    assert "nonexistent" in (step_trace.error or "")


# ── WorkflowRunner — command_sink ─────────────────────────────────────────────

def test_command_sink_ok():
    spoken: list[str] = []

    def saynow(text: str) -> str:
        spoken.append(text)
        return "🔊 OK"

    ex, runner = _make_runner(
        responses={"w": (True, "東京：晴れ")},
        commands={"/saynow": saynow},
    )
    wf = Workflow(id="wf", goal="g", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="w", output="weather"),
        WorkflowStep(id="s2", kind="command_sink", command="/saynow",
                     input="weather", output="speech"),
    ])
    trace = runner.run(wf)
    assert trace.ok
    assert spoken == ["東京：晴れ"]
    assert trace.variables["speech"].value == "🔊 OK"
    assert trace.final_result == "🔊 OK"


def test_command_sink_not_in_allowlist():
    ex, runner = _make_runner(commands={"/rm-rf": lambda x: "boom"})
    step = WorkflowStep(id="s1", kind="command_sink", command="/rm-rf",
                        input="data", output="out")
    store = VariableStore()
    store.bind("data", "some value", "s0", "p0")
    step_trace, _ = runner._run_command_sink(step, store)
    assert step_trace.status == "failed"
    assert "allowlist" in (step_trace.error or "")


def test_command_sink_no_handler_registered():
    ex, runner = _make_runner(commands={})  # /saynow in allowlist but no handler
    step = WorkflowStep(id="s1", kind="command_sink", command="/saynow",
                        input="data", output="out")
    store = VariableStore()
    store.bind("data", "hello", "s0", "p0")
    step_trace, _ = runner._run_command_sink(step, store)
    assert step_trace.status == "failed"
    assert "no handler" in (step_trace.error or "")


def test_command_sink_missing_input_variable():
    def saynow(x: str) -> str:
        return "ok"

    ex, runner = _make_runner(commands={"/saynow": saynow})
    step = WorkflowStep(id="s1", kind="command_sink", command="/saynow",
                        input="greeting", output="out")
    store = VariableStore()  # "greeting" never bound
    step_trace, _ = runner._run_command_sink(step, store)
    assert step_trace.status == "failed"
    assert "greeting" in (step_trace.error or "")


def test_command_sink_handler_exception_caught():
    def saynow(_: str) -> str:
        raise RuntimeError("音声合成エラー")

    ex, runner = _make_runner(commands={"/saynow": saynow})
    step = WorkflowStep(id="s1", kind="command_sink", command="/saynow",
                        input="msg", output="out")
    store = VariableStore()
    store.bind("msg", "hello", "s0", "p0")
    step_trace, _ = runner._run_command_sink(step, store)
    assert step_trace.status == "failed"
    assert "音声合成エラー" in (step_trace.error or "")


# ── WorkflowRunner — failure halting ─────────────────────────────────────────

def test_failure_halts_downstream_steps():
    spoken: list[str] = []

    def saynow(x: str) -> str:
        spoken.append(x)
        return "ok"

    ex, runner = _make_runner(
        responses={"t": (False, "ツール失敗")},
        commands={"/saynow": saynow},
    )
    wf = Workflow(id="wf", goal="g", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="t", output="data"),
        WorkflowStep(id="s2", kind="command_sink", command="/saynow",
                     input="data", output="out"),
    ])
    trace = runner.run(wf)
    assert trace.steps[0].status == "failed"
    assert trace.steps[1].status == "skipped"
    assert not trace.ok
    assert spoken == []  # saynow was never called
    assert "s1" in (trace.final_result or "")


def test_all_skipped_after_first_fail():
    ex, runner = _make_runner({"t1": (False, "fail"), "t2": (True, "ok"), "t3": (True, "ok")})
    wf = Workflow(id="wf", goal="g", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="t1", output="a"),
        WorkflowStep(id="s2", kind="tool_call", tool="t2", output="b"),
        WorkflowStep(id="s3", kind="tool_call", tool="t3", output="c"),
    ])
    trace = runner.run(wf)
    assert [st.status for st in trace.steps] == ["failed", "skipped", "skipped"]
    assert ex.calls == [("t1", {})]  # only s1 ran


# ── WorkflowRunner — llm_transform stub ──────────────────────────────────────

def test_llm_transform_returns_not_implemented():
    ex, runner = _make_runner()
    step = WorkflowStep(id="s1", kind="llm_transform",
                        inputs=["weather"], instructions="transform", output="greeting")
    store = VariableStore()
    store.bind("weather", "晴れ", "s0", "p0")
    step_trace, var = runner._run_llm_transform(step, store)
    assert step_trace.status == "failed"
    assert "Phase 4" in (step_trace.error or "")
    assert var is None


# ── WorkflowRunner — invalid workflow short-circuit ───────────────────────────

def test_invalid_workflow_returns_error_trace():
    ex, runner = _make_runner()
    wf = Workflow(id="wf", goal="g", steps=[
        # command_sink references "data" before any step produces it
        WorkflowStep(id="s1", kind="command_sink", command="/saynow",
                     input="data", output="out"),
    ])
    trace = runner.run(wf)
    # No steps were executed — the validate_references short-circuit fires
    assert trace.steps == []
    assert trace.final_result is not None
    assert "工作流定義有誤" in trace.final_result


# ── WorkflowTrace ─────────────────────────────────────────────────────────────

def test_trace_to_dict_structure():
    ex, runner = _make_runner({"t": (True, "東京：晴れ")})
    wf = Workflow(id="wf-morning", goal="早安", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="t",
                     args={"city": "東京"}, output="weather"),
    ])
    trace = runner.run(wf)
    d = trace.to_dict()
    assert d["workflow_id"] == "wf-morning"
    assert d["goal"] == "早安"
    assert d["final_result"] == "東京：晴れ"
    assert d["variables"]["weather"]["value"] == "東京：晴れ"
    assert d["variables"]["weather"]["source_step"] == "s1"
    assert d["steps"][0]["status"] == "ok"
    assert d["steps"][0]["output_var"] == "weather"


def test_trace_ok_property():
    ex, runner = _make_runner({"t": (True, "v")})
    wf = Workflow(id="wf", goal="g", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="t", output="out"),
    ])
    assert runner.run(wf).ok

    ex2, runner2 = _make_runner({"t": (False, "err")})
    wf2 = Workflow(id="wf", goal="g", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="t", output="out"),
    ])
    assert not runner2.run(wf2).ok


# ── Full pipeline (wf-morning-greeting without llm_transform) ─────────────────

def test_two_step_pipeline_tool_then_saynow():
    """tool_call -> command_sink full trace — mirrors the wf-morning-greeting
    E2E target but skips the llm_transform (Phase 4) step."""
    spoken: list[str] = []

    def saynow(text: str) -> str:
        spoken.append(text)
        return "🔊 唸出完成"

    ex, runner = _make_runner(
        responses={"city-weather-xyz": (True, "東京：晴れ、28℃")},
        commands={"/saynow": saynow},
    )
    wf = Workflow(
        id="wf-morning",
        goal="查東京天氣並唸出",
        steps=[
            WorkflowStep(id="s1", kind="tool_call", tool="city-weather-xyz",
                         args={"city": "東京"}, output="weather"),
            WorkflowStep(id="s2", kind="command_sink", command="/saynow",
                         input="weather", output="speech_result"),
        ],
    )
    trace = runner.run(wf)

    assert trace.ok
    assert [st.status for st in trace.steps] == ["ok", "ok"]
    assert spoken == ["東京：晴れ、28℃"]
    assert trace.final_result == "🔊 唸出完成"
    assert "weather" in trace.variables
    assert "speech_result" in trace.variables
    assert trace.variables["weather"].provenance.startswith("city-weather-xyz")
    assert trace.variables["speech_result"].provenance == "/saynow(input=weather)"


# ── COMMAND_SINK_ALLOWLIST sanity ─────────────────────────────────────────────

def test_allowlist_contains_saynow():
    assert "/saynow" in COMMAND_SINK_ALLOWLIST


def test_allowlist_does_not_contain_arbitrary_commands():
    for dangerous in ["/new", "/restartall", "/rm", "/exec", "/bash"]:
        assert dangerous not in COMMAND_SINK_ALLOWLIST
