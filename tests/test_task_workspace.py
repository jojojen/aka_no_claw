"""Tests for task_workspace.py (#53, Phase 1 + D + A persistence).

Coverage:
  Schema:        Workflow/WorkflowStep to_dict / from_dict roundtrip; validate_references.
  VariableStore: bind / resolve / missing key.
  WorkflowRunner — tool_call: ok/fail/explicit-params/$ref-arg.
  WorkflowRunner — command_sink: ok/not-allowlisted/no-handler/missing-input/exception.
  WorkflowRunner — llm_transform: no-invention prompt, output bound, no-client/missing-var/exception.
  WorkflowRunner — control flow: failure halting, invalid workflow short-circuit.
  WorkflowTrace:  to_dict / from_dict roundtrip.
  WorkflowStore:  save/get/list/delete, save_trace/list_traces roundtrip.
"""

import pytest

from openclaw_adapter.task_workspace import (
    COMMAND_SINK_ALLOWLIST,
    COMMAND_SINK_DENYLIST,
    COMMAND_SINK_INPUT_TYPES,
    VARIABLE_TYPE_COMMAND_RESULT,
    VARIABLE_TYPE_PLAIN_TEXT,
    VARIABLE_TYPE_SPEECH_TEXT,
    is_command_sink_allowed,
    LLMClient,
    StepTrace,
    ToolCallExecutor,
    Variable,
    VariableStore,
    Workflow,
    WorkflowRunner,
    WorkflowStore,
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
        WorkflowStep(id="s2", kind="command_sink", command="/restartall", input="data", output="out"),
    ])
    errors = wf.validate_references()
    assert any("/restartall" in e for e in errors)


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
    ex, runner = _make_runner(commands={"/restartall": lambda x: "boom"})
    step = WorkflowStep(id="s1", kind="command_sink", command="/restartall",
                        input="data", output="out")
    store = VariableStore()
    store.bind("data", "some value", "s0", "p0")
    step_trace, _ = runner._run_command_sink(step, store)
    assert step_trace.status == "failed"
    assert "denied" in (step_trace.error or "")


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
    store.bind("msg", "hello", "s0", "p0", type_=VARIABLE_TYPE_PLAIN_TEXT)
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

def test_llm_transform_no_client_stub_fails():
    ex, runner = _make_runner()  # no llm_client
    step = WorkflowStep(id="s1", kind="llm_transform",
                        inputs=["weather"], instructions="transform", output="greeting")
    store = VariableStore()
    store.bind("weather", "晴れ", "s0", "p0")
    step_trace, var = runner._run_llm_transform(step, store)
    assert step_trace.status == "failed"
    assert "llm_client" in (step_trace.error or "")
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
    assert not trace.ok                          # must be False despite empty step list
    assert trace.validation_error is not None
    assert "工作流定義有誤" in (trace.final_result or "")


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


# ── COMMAND_SINK_DENYLIST / is_command_sink_allowed sanity ───────────────────

def test_allowlist_contains_saynow():
    assert is_command_sink_allowed("/saynow")


def test_allowlist_does_not_contain_arbitrary_commands():
    for dangerous in ["/new", "/restartall", "/rm", "/exec", "/bash",
                      "/snsadd", "/snsdelete", "/backupclaw", "/workflow",
                      "/schedulehome"]:
        assert not is_command_sink_allowed(dangerous)


def test_allowlist_contains_safe_home_commands():
    for safe in ["/music", "/bluetooth", "/ir", "/translateja",
                 "/knowledge", "/research", "/snsbuzz"]:
        assert is_command_sink_allowed(safe)


# ── FakeLLMClient helper ──────────────────────────────────────────────────────

class FakeLLMClient:
    def __init__(self, response: str = "transformed output") -> None:
        self._response = response
        self.prompts: list[str] = []
        self.temperatures: list[float] = []
        self.raise_on_call: Exception | None = None

    def generate(self, prompt: str, *, temperature: float = 0.0) -> str:
        self.prompts.append(prompt)
        self.temperatures.append(temperature)
        if self.raise_on_call:
            raise self.raise_on_call
        return self._response


# ── WorkflowRunner — llm_transform ───────────────────────────────────────────

def test_llm_transform_binds_output_variable():
    llm = FakeLLMClient("おはようございます！今日は晴れです。")
    ex, runner = _make_runner()
    runner.llm_client = llm

    step = WorkflowStep(
        id="s1", kind="llm_transform",
        inputs=["weather"], instructions="用女僕口吻說早安",
        output="greeting",
    )
    store = VariableStore()
    store.bind("weather", "東京：晴れ、28℃", "s0", "p0")

    step_trace, var_name = runner._run_llm_transform(step, store)

    assert step_trace.status == "ok"
    assert var_name == "greeting"
    assert store.resolve("greeting") == "おはようございます！今日は晴れです。"
    assert step_trace.output_var == "greeting"
    assert "llm_transform" in (step_trace.provenance or "")


def test_llm_transform_prompt_contains_no_invention_constraint():
    llm = FakeLLMClient("ok")
    ex, runner = _make_runner()
    runner.llm_client = llm

    step = WorkflowStep(
        id="s1", kind="llm_transform",
        inputs=["weather"], instructions="変換してください",
        output="greeting",
    )
    store = VariableStore()
    store.bind("weather", "東京：晴れ", "s0", "p0")
    runner._run_llm_transform(step, store)

    prompt = llm.prompts[0]
    assert "invent" in prompt.lower() or "NOT" in prompt
    assert "東京：晴れ" in prompt   # input value embedded
    assert "変換してください" in prompt


def test_llm_transform_prompt_embeds_all_input_variables():
    llm = FakeLLMClient("ok")
    ex, runner = _make_runner()
    runner.llm_client = llm

    step = WorkflowStep(
        id="s1", kind="llm_transform",
        inputs=["weather", "user_name"], instructions="greet",
        output="greeting",
    )
    store = VariableStore()
    store.bind("weather", "晴れ", "s0", "p0")
    store.bind("user_name", "ご主人様", "s0", "p0")
    runner._run_llm_transform(step, store)

    prompt = llm.prompts[0]
    assert "晴れ" in prompt
    assert "ご主人様" in prompt
    assert "[weather]" in prompt
    assert "[user_name]" in prompt


def test_llm_transform_no_client_fails():
    ex, runner = _make_runner()  # llm_client=None
    step = WorkflowStep(
        id="s1", kind="llm_transform",
        inputs=["weather"], output="greeting",
    )
    store = VariableStore()
    store.bind("weather", "晴れ", "s0", "p0")
    step_trace, var = runner._run_llm_transform(step, store)
    assert step_trace.status == "failed"
    assert "llm_client" in (step_trace.error or "")
    assert var is None


def test_llm_transform_missing_input_variable_fails():
    llm = FakeLLMClient("ok")
    ex, runner = _make_runner()
    runner.llm_client = llm

    step = WorkflowStep(
        id="s1", kind="llm_transform",
        inputs=["missing_var"], output="greeting",
    )
    store = VariableStore()  # missing_var never bound
    step_trace, var = runner._run_llm_transform(step, store)
    assert step_trace.status == "failed"
    assert "missing_var" in (step_trace.error or "")
    assert var is None
    assert llm.prompts == []  # LLM never called


def test_llm_transform_exception_caught():
    llm = FakeLLMClient()
    llm.raise_on_call = RuntimeError("timeout")
    ex, runner = _make_runner()
    runner.llm_client = llm

    step = WorkflowStep(
        id="s1", kind="llm_transform",
        inputs=["weather"], output="greeting",
    )
    store = VariableStore()
    store.bind("weather", "晴れ", "s0", "p0")
    step_trace, var = runner._run_llm_transform(step, store)
    assert step_trace.status == "failed"
    assert "timeout" in (step_trace.error or "")
    assert var is None


def test_full_three_step_pipeline():
    """tool_call -> llm_transform -> command_sink (wf-morning-greeting)."""
    spoken: list[str] = []

    def saynow(text: str) -> str:
        spoken.append(text)
        return "🔊 完了"

    llm = FakeLLMClient("おはようございます！東京は晴れです。")
    ex, runner = _make_runner(
        responses={"city-weather-xyz": (True, "東京：晴れ、28℃")},
        commands={"/saynow": saynow},
    )
    runner.llm_client = llm

    wf = Workflow(
        id="wf-morning",
        goal="早安工作流",
        steps=[
            WorkflowStep(id="s1", kind="tool_call", tool="city-weather-xyz",
                         args={"city": "東京"}, output="weather"),
            WorkflowStep(id="s2", kind="llm_transform",
                         inputs=["weather"], instructions="用女僕口吻說早安",
                         output="greeting"),
            WorkflowStep(id="s3", kind="command_sink", command="/saynow",
                         input="greeting", output="speech_result"),
        ],
    )
    trace = runner.run(wf)

    assert trace.ok
    assert [st.status for st in trace.steps] == ["ok", "ok", "ok"]
    assert spoken == ["おはようございます！東京は晴れです。"]
    assert trace.final_result == "🔊 完了"
    assert "weather" in trace.variables
    assert "greeting" in trace.variables
    assert "speech_result" in trace.variables


# ── WorkflowTrace.from_dict roundtrip ────────────────────────────────────────

def test_workflow_trace_from_dict_roundtrip():
    ex, runner = _make_runner({"t": (True, "東京：晴れ")})
    wf = Workflow(id="wf", goal="g", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="t",
                     args={"city": "東京"}, output="weather"),
    ])
    trace = runner.run(wf)
    restored = WorkflowTrace.from_dict(trace.to_dict())

    assert restored.workflow_id == trace.workflow_id
    assert restored.goal == trace.goal
    assert restored.final_result == trace.final_result
    assert restored.ok == trace.ok
    assert len(restored.steps) == len(trace.steps)
    assert restored.steps[0].status == trace.steps[0].status
    assert restored.variables["weather"].value == "東京：晴れ"
    assert restored.variables["weather"].source_step == "s1"


# ── WorkflowStore ─────────────────────────────────────────────────────────────

def test_store_save_and_get(tmp_path):
    store = WorkflowStore(tmp_path / "workflows")
    wf = Workflow(id="wf-test", goal="テスト", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="t",
                     args={"city": "東京"}, output="weather"),
    ])
    store.save(wf)
    loaded = store.get("wf-test")
    assert loaded is not None
    assert loaded == wf


def test_store_get_missing_returns_none(tmp_path):
    store = WorkflowStore(tmp_path / "workflows")
    assert store.get("nonexistent") is None


def test_store_list(tmp_path):
    store = WorkflowStore(tmp_path / "workflows")
    wf1 = Workflow(id="wf-a", goal="A")
    wf2 = Workflow(id="wf-b", goal="B")
    store.save(wf1)
    store.save(wf2)
    ids = {wf.id for wf in store.list()}
    assert ids == {"wf-a", "wf-b"}


def test_store_delete(tmp_path):
    store = WorkflowStore(tmp_path / "workflows")
    wf = Workflow(id="wf-del", goal="delete me")
    store.save(wf)
    assert store.delete("wf-del") is True
    assert store.get("wf-del") is None
    assert store.delete("wf-del") is False  # already gone


def test_store_save_and_list_traces(tmp_path):
    store = WorkflowStore(tmp_path / "workflows")
    ex, runner = _make_runner({"t": (True, "晴れ")})
    wf = Workflow(id="wf-trace", goal="g", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="t", output="out"),
    ])
    trace1 = runner.run(wf)
    trace2 = runner.run(wf)
    store.save_trace(trace1)
    store.save_trace(trace2)

    traces = store.list_traces("wf-trace")
    assert len(traces) == 2
    for t in traces:
        assert t.workflow_id == "wf-trace"
        assert t.ok


def test_store_list_traces_empty_for_unknown(tmp_path):
    store = WorkflowStore(tmp_path / "workflows")
    assert store.list_traces("wf-nonexistent") == []


def test_store_trace_roundtrip_preserves_variables(tmp_path):
    store = WorkflowStore(tmp_path / "workflows")
    ex, runner = _make_runner({"t": (True, "東京：晴れ、28℃")})
    wf = Workflow(id="wf-vars", goal="g", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="t",
                     args={"city": "東京"}, output="weather"),
    ])
    trace = runner.run(wf)
    store.save_trace(trace)
    [loaded] = store.list_traces("wf-vars")

    assert loaded.variables["weather"].value == "東京：晴れ、28℃"


# ── Type mismatch rejection ───────────────────────────────────────────────────

def test_command_sink_saynow_accepts_plain_text():
    spoken: list[str] = []

    def saynow(text: str) -> str:
        spoken.append(text)
        return "🔊 ok"

    ex, runner = _make_runner(commands={"/saynow": saynow})
    step = WorkflowStep(id="s1", kind="command_sink", command="/saynow",
                        input="msg", output="out")
    store = VariableStore()
    store.bind("msg", "hello", "s0", "p0", type_=VARIABLE_TYPE_PLAIN_TEXT)
    step_trace, _ = runner._run_command_sink(step, store)
    assert step_trace.status == "ok"
    assert spoken == ["hello"]


def test_command_sink_saynow_accepts_speech_text():
    spoken: list[str] = []

    def saynow(text: str) -> str:
        spoken.append(text)
        return "🔊 ok"

    ex, runner = _make_runner(commands={"/saynow": saynow})
    step = WorkflowStep(id="s1", kind="command_sink", command="/saynow",
                        input="msg", output="out")
    store = VariableStore()
    store.bind("msg", "おはよう", "s0", "p0", type_=VARIABLE_TYPE_SPEECH_TEXT)
    step_trace, _ = runner._run_command_sink(step, store)
    assert step_trace.status == "ok"
    assert spoken == ["おはよう"]


def test_command_sink_saynow_rejects_command_result():
    saynow_called: list[bool] = []

    def saynow(text: str) -> str:
        saynow_called.append(True)
        return "🔊 ok"

    ex, runner = _make_runner(commands={"/saynow": saynow})
    step = WorkflowStep(id="s1", kind="command_sink", command="/saynow",
                        input="raw_data", output="out")
    store = VariableStore()
    store.bind("raw_data", "JSON{}", "s0", "p0", type_=VARIABLE_TYPE_COMMAND_RESULT)
    step_trace, _ = runner._run_command_sink(step, store)
    assert step_trace.status == "failed"
    assert "type mismatch" in (step_trace.error or "")
    assert saynow_called == []


def test_command_sink_type_mismatch_trace_records_error():
    def saynow(text: str) -> str:
        return "ok"

    ex, runner = _make_runner(commands={"/saynow": saynow})
    step = WorkflowStep(id="s1", kind="command_sink", command="/saynow",
                        input="data", output="out")
    store = VariableStore()
    store.bind("data", "search result", "s0", "p0", type_=VARIABLE_TYPE_COMMAND_RESULT)
    step_trace, var = runner._run_command_sink(step, store)
    assert step_trace.status == "failed"
    assert var is None
    assert step_trace.error is not None
    assert "command_result" in (step_trace.error or "")


def test_command_sink_music_accepts_any_type():
    music_called: list[str] = []

    def music(text: str) -> str:
        music_called.append(text)
        return "🎵 playing"

    ex, runner = _make_runner(commands={"/music": music})
    step = WorkflowStep(id="s1", kind="command_sink", command="/music",
                        input="query", output="out")
    store = VariableStore()
    store.bind("query", "playbest", "s0", "p0", type_=VARIABLE_TYPE_COMMAND_RESULT)
    step_trace, _ = runner._run_command_sink(step, store)
    assert step_trace.status == "ok"
    assert music_called == ["playbest"]


def test_command_sink_literal_static_arg():
    music_called: list[str] = []

    def music(text: str) -> str:
        music_called.append(text)
        return "🎵 playing"

    ex, runner = _make_runner(commands={"/music": music})
    step = WorkflowStep(id="s1", kind="command_sink", command="/music",
                        literal="playbest", output="out")
    store = VariableStore()
    step_trace, _ = runner._run_command_sink(step, store)
    assert step_trace.status == "ok"
    assert music_called == ["playbest"]


def test_command_sink_ir_usage_help_is_failure():
    def ir(_: str) -> str:
        return "IR 指令：\n/ir discover\n/ir send <裝置名> <按鍵名>"

    ex, runner = _make_runner(commands={"/ir": ir})
    step = WorkflowStep(id="s1", kind="command_sink", command="/ir",
                        literal="send ceiling_light", output="out")
    store = VariableStore()
    step_trace, var_name = runner._run_command_sink(step, store)
    assert step_trace.status == "failed"
    assert var_name is None
    assert "usage help" in (step_trace.error or "")


def test_command_sink_ir_connection_error_halts_downstream_music():
    calls: list[str] = []

    def ir(_: str) -> str:
        calls.append("ir")
        return "找到 RM4 Mini 但無法連線：[Errno 65] No route to host"

    def music(_: str) -> str:
        calls.append("music")
        return "開始連續隨機播放最愛歌曲。用 /music stop 可停止。"

    ex, runner = _make_runner(commands={"/ir": ir, "/music": music})
    wf = Workflow(id="wf", goal="開燈播放音樂", steps=[
        WorkflowStep(id="s1", kind="command_sink", command="/ir",
                     literal="send ceiling_light power", output="r1"),
        WorkflowStep(id="s2", kind="command_sink", command="/music",
                     literal="playbest", output="r2"),
    ])
    trace = runner.run(wf)
    assert not trace.ok
    assert trace.steps[0].status == "failed"
    assert trace.steps[1].status == "skipped"
    assert calls == ["ir"]
    assert "No route to host" in (trace.final_result or "")


def test_command_sink_literal_roundtrip():
    step = WorkflowStep(id="s1", kind="command_sink", command="/music",
                        literal="playbest", output="out")
    d = step.to_dict()
    assert d["literal"] == "playbest"
    loaded = WorkflowStep.from_dict(d)
    assert loaded.literal == "playbest"
    assert loaded.input is None


def test_allowlist_contains_music():
    assert is_command_sink_allowed("/music")


def test_command_sink_input_types_music_accepts_any():
    # Commands not in COMMAND_SINK_INPUT_TYPES have no type restriction.
    assert COMMAND_SINK_INPUT_TYPES.get("/music") is None


def test_command_sink_input_types_saynow_restricted():
    accepted = COMMAND_SINK_INPUT_TYPES["/saynow"]
    assert accepted is not None
    assert VARIABLE_TYPE_PLAIN_TEXT in accepted
    assert VARIABLE_TYPE_SPEECH_TEXT in accepted
    assert VARIABLE_TYPE_COMMAND_RESULT not in accepted


# ── Denylist policy: registry-only command accepted ───────────────────────────

def test_registry_command_not_in_denylist_accepted():
    """/musiclistall is not in COMMAND_SINK_DENYLIST → validate_references passes."""
    assert "/musiclistall" not in COMMAND_SINK_DENYLIST
    assert is_command_sink_allowed("/musiclistall")
    wf = Workflow(id="wf-test", goal="g", steps=[
        WorkflowStep(id="s1", kind="command_sink", command="/musiclistall",
                     literal="", output="out"),
    ])
    errors = wf.validate_references()
    assert errors == []


def test_registry_command_dispatched_through_registry():
    """/musiclistall injected via command_registry is executed by _run_command_sink."""
    from types import SimpleNamespace

    called: list[str] = []

    def musiclistall_handler(remainder, chat_id):
        called.append(remainder)
        return "list ok"

    registry = {"/musiclistall": SimpleNamespace(handler=musiclistall_handler)}

    ex = FakeExecutor()
    runner = WorkflowRunner(
        executor=ex,
        command_dispatcher={
            "/musiclistall": lambda text: musiclistall_handler(text, "chat-1"),
        },
    )
    step = WorkflowStep(id="s1", kind="command_sink", command="/musiclistall",
                        literal="", output="out")
    store = VariableStore()
    step_trace, var_name = runner._run_command_sink(step, store)
    assert step_trace.status == "ok"
    assert called == [""]
    assert var_name == "out"


# ── validate_references: known_commands check ─────────────────────────────────

def test_validate_references_known_commands_passes_registered():
    """/music is in known_commands → no error."""
    wf = Workflow(id="wf", goal="g", steps=[
        WorkflowStep(id="s1", kind="command_sink", command="/music",
                     literal="random", output="out"),
    ])
    errors = wf.validate_references(known_commands=frozenset({"/music", "/saynow"}))
    assert errors == []


def test_validate_references_known_commands_rejects_unregistered():
    """/ir is not in known_commands → error mentioning 'not registered'."""
    wf = Workflow(id="wf", goal="g", steps=[
        WorkflowStep(id="s1", kind="command_sink", command="/ir",
                     literal="send ceiling_light power", output="out"),
    ])
    errors = wf.validate_references(known_commands=frozenset({"/music", "/saynow"}))
    assert any("/ir" in e and "not registered" in e for e in errors)


def test_validate_references_no_known_commands_skips_registry_check():
    """Without known_commands the registry check is skipped (backward compat)."""
    wf = Workflow(id="wf", goal="g", steps=[
        WorkflowStep(id="s1", kind="command_sink", command="/ir",
                     literal="send ceiling_light power", output="out"),
    ])
    errors = wf.validate_references()  # no known_commands → only denylist check
    assert errors == []


def test_runner_run_catches_unregistered_command_at_validation():
    """WorkflowRunner.run() passes its dispatcher keys to validate_references,
    so an unregistered command (not in denylist, not in dispatcher) surfaces as
    a validation error instead of a runtime 'no handler' failure."""
    ex, runner = _make_runner(commands={"/saynow": lambda x: "ok"})
    wf = Workflow(id="wf", goal="g", steps=[
        WorkflowStep(id="s1", kind="command_sink", command="/ir",
                     literal="send ceiling_light power", output="out"),
    ])
    trace = runner.run(wf)
    assert not trace.ok
    assert trace.validation_error is not None
    assert "/ir" in (trace.validation_error or "")
    assert "not registered" in (trace.validation_error or "")
    assert trace.steps == []  # no steps ran — short-circuited by validation


def test_runner_run_empty_dispatcher_skips_registry_check():
    """When the dispatcher is empty, runner skips the registry check so
    workflows without any command_sink steps (tool_call only) still run."""
    ex, runner = _make_runner(responses={"t": (True, "ok")})
    wf = Workflow(id="wf", goal="g", steps=[
        WorkflowStep(id="s1", kind="tool_call", tool="t", output="out"),
    ])
    trace = runner.run(wf)
    assert trace.ok
