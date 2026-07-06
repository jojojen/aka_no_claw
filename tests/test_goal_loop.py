"""Tests for goal_loop.py (#54 phase 3 core library)."""

from __future__ import annotations

from dataclasses import dataclass

from openclaw_adapter.goal_loop import GoalLoop, GoalLoopContinuation
from openclaw_adapter.task_loop import ContinuationState
from openclaw_adapter.task_workspace import (
    Workflow,
    WorkflowBudgetExhausted,
    WorkflowStep,
    WorkflowTrace,
)


@dataclass
class _FakeRegistered:
    handler: object


class _FakePlanner:
    def __init__(self, *, drafts, replans=None):
        self._drafts = list(drafts)
        self._replans = list(replans or [])
        self.draft_calls = []
        self.replan_calls = []
        self.draft_seeds = []
        self.replan_seeds = []

    def draft(self, goal: str, seed_variables=None):
        self.draft_calls.append(goal)
        self.draft_seeds.append(dict(seed_variables or {}))
        return self._drafts.pop(0)

    def replan(
        self,
        goal: str,
        previous_workflow: Workflow,
        trace: WorkflowTrace,
        seed_variables=None,
    ):
        self.replan_calls.append((goal, previous_workflow.id, trace.final_result))
        self.replan_seeds.append(dict(seed_variables or {}))
        return self._replans.pop(0)


class _FakeExecutor:
    def __init__(self, responses):
        self.responses = dict(responses)
        self.calls = []
        self.client = None
        self.last_budget_exhausted = None

    def run_tool_step(self, slug: str, explicit_params: dict):
        self.calls.append((slug, explicit_params))
        response = self.responses[slug]
        self.last_budget_exhausted = None
        if len(response) == 3:
            ok, text, budget = response
            self.last_budget_exhausted = budget
            return ok, text
        return response


def _workflow_with_tool(tool: str, *, wf_id: str = "wf-test") -> Workflow:
    return Workflow(
        id=wf_id,
        goal="goal",
        steps=[
            WorkflowStep(id="s1", kind="tool_call", tool=tool, args={}, output="r1"),
        ],
    )


def test_goal_loop_runs_draft_then_workflow_to_completion():
    workflow = _workflow_with_tool("real_tool", wf_id="wf-ok")
    planner = _FakePlanner(drafts=[(workflow, None, False)])
    executor = _FakeExecutor({"real_tool": (True, "done")})
    saved = []
    loop = GoalLoop(
        goal="完成任務",
        planner=planner,
        executor=executor,
        command_registry={},
        trace_saver=saved.append,
    )
    report = loop.run()
    assert report.done is True
    assert report.final_result == "done"
    assert report.workflow.id == "wf-ok"
    assert saved and saved[0].ok is True
    assert saved[0].narration[-1] == "工作流完成：done"
    assert "草稿完成" in "\n".join(report.narration)


def test_goal_loop_replans_after_failed_run():
    broken = _workflow_with_tool("broken_tool", wf_id="wf-broken")
    fixed = _workflow_with_tool("real_tool", wf_id="wf-fixed")
    planner = _FakePlanner(
        drafts=[(broken, None, False)],
        replans=[(fixed, None, False)],
    )
    executor = _FakeExecutor({
        "broken_tool": (False, "boom"),
        "real_tool": (True, "done"),
    })
    loop = GoalLoop(
        goal="修好任務",
        planner=planner,
        executor=executor,
        command_registry={},
        replan_limit=1,
    )
    report = loop.run()
    assert report.done is True
    assert report.replans_used == 1
    assert report.workflow.id == "wf-fixed"
    assert planner.replan_calls == [("修好任務", "wf-broken", "工作流在步驟 s1 失敗：boom")]


def test_goal_loop_replans_when_result_judged_not_achieving_goal():
    """A workflow whose steps all succeed but whose final result is e.g. a
    follow-up question must not count as done: the result judge rejects it
    and the loop replans (self-repair without user input)."""
    asking = _workflow_with_tool("asking_tool", wf_id="wf-ask")
    fixed = _workflow_with_tool("real_tool", wf_id="wf-fixed")
    planner = _FakePlanner(
        drafts=[(asking, None, False)],
        replans=[(fixed, None, False)],
    )
    executor = _FakeExecutor({
        "asking_tool": (True, "找到多個候選，請問您要哪一個？"),
        "real_tool": (True, "已完成播放"),
    })
    judge_calls = []

    def judge(goal: str, final_result: str):
        judge_calls.append((goal, final_result))
        achieved = len(judge_calls) > 1
        return achieved, "" if achieved else "結果在反問使用者，未達成目標"

    loop = GoalLoop(
        goal="播放歌曲",
        planner=planner,
        executor=executor,
        command_registry={},
        replan_limit=1,
        result_judge=judge,
    )
    report = loop.run()
    assert report.done is True
    assert report.replans_used == 1
    assert report.workflow.id == "wf-fixed"
    assert report.final_result == "已完成播放"
    assert judge_calls == [
        ("播放歌曲", "找到多個候選，請問您要哪一個？"),
        ("播放歌曲", "已完成播放"),
    ]
    joined = "\n".join(report.narration)
    assert "結果未達成目標：結果在反問使用者，未達成目標" in joined
    # the rejected trace (with the judge's reason in narration) reaches replan
    assert planner.replan_calls == [
        ("播放歌曲", "wf-ask", "找到多個候選，請問您要哪一個？"),
    ]


def test_goal_loop_result_judge_failure_does_not_sink_success():
    workflow = _workflow_with_tool("real_tool", wf_id="wf-ok")
    planner = _FakePlanner(drafts=[(workflow, None, False)])
    executor = _FakeExecutor({"real_tool": (True, "done")})

    def broken_judge(goal: str, final_result: str):
        raise RuntimeError("judge backend down")

    loop = GoalLoop(
        goal="完成任務",
        planner=planner,
        executor=executor,
        command_registry={},
        result_judge=broken_judge,
    )
    report = loop.run()
    assert report.done is True
    assert report.final_result == "done"


def test_goal_loop_returns_continuation_when_step_budget_hits():
    workflow = _workflow_with_tool("real_tool", wf_id="wf-ok")
    planner = _FakePlanner(drafts=[(workflow, None, False)])
    executor = _FakeExecutor({"real_tool": (True, "done")})
    loop = GoalLoop(
        goal="完成任務",
        planner=planner,
        executor=executor,
        command_registry={},
        max_steps=1,
    )
    report = loop.run()
    assert report.done is False
    assert report.continuation is not None
    assert report.continuation.state.next_action == "run_workflow"


def test_goal_loop_resume_uses_saved_workflow_without_redrafting():
    workflow = _workflow_with_tool("real_tool", wf_id="wf-ok")
    planner = _FakePlanner(drafts=[(workflow, None, False)])
    executor = _FakeExecutor({"real_tool": (True, "done")})
    loop = GoalLoop(
        goal="完成任務",
        planner=planner,
        executor=executor,
        command_registry={},
        max_steps=1,
    )
    paused = loop.run()
    assert paused.done is False
    assert paused.continuation is not None

    resumed = loop.run(resume=paused.continuation)
    assert resumed.done is True
    assert resumed.final_result == "done"
    assert planner.draft_calls == ["完成任務"]
    assert executor.calls == [("real_tool", {})]


def test_goal_loop_continuation_roundtrip_preserves_workflow_and_trace():
    workflow = _workflow_with_tool("real_tool", wf_id="wf-ok")
    trace = WorkflowTrace(workflow_id="wf-ok", goal="goal", final_result="partial")
    cont = GoalLoopContinuation(
        state=ContinuationState(
            goal="完成任務",
            completed=["draft: drafted wf-ok with 1 step(s)"],
            budget={"steps_used": 1, "steps_limit": 6},
            next_action="run_workflow",
            stop_condition="step budget reached",
        ),
        workflow=workflow,
        trace=trace,
        replans_used=1,
        narration=("a", "b"),
    )
    restored = GoalLoopContinuation.from_dict(cont.to_dict())
    assert restored.state.next_action == "run_workflow"
    assert restored.workflow is not None and restored.workflow.id == "wf-ok"
    assert restored.trace is not None and restored.trace.final_result == "partial"
    assert restored.replans_used == 1
    assert restored.narration == ("a", "b")


def test_goal_loop_pauses_on_search_budget_exhausted():
    workflow = _workflow_with_tool("real_tool", wf_id="wf-search")
    planner = _FakePlanner(drafts=[(workflow, None, False)])
    executor = _FakeExecutor(
        {
            "real_tool": (
                False,
                "web-search grounding budget exhausted (10/10)",
                WorkflowBudgetExhausted(
                    kind="search",
                    used=10,
                    limit=10,
                    hard_limit=20,
                    granted_extra=0,
                ),
            )
        }
    )
    loop = GoalLoop(
        goal="完成任務",
        planner=planner,
        executor=executor,
        command_registry={},
    )
    report = loop.run()

    assert report.done is False
    assert report.continuation is not None
    assert report.continuation.state.next_action == "run_workflow"
    assert report.continuation.state.stop_condition == "search soft cap reached (10/10)"
    assert report.continuation.state.budget["search_used"] == 10
    assert report.continuation.state.budget["search_limit"] == 10
    assert report.continuation.state.budget["search_hard_limit"] == 20


def test_goal_loop_passes_seed_variables_to_draft():
    workflow = _workflow_with_tool("real_tool", wf_id="wf-ok")
    planner = _FakePlanner(drafts=[(workflow, None, False)])
    executor = _FakeExecutor({"real_tool": (True, "done")})
    loop = GoalLoop(
        goal="統整結論",
        planner=planner,
        executor=executor,
        command_registry={},
        seed_variables={"image_observation": "卡面外觀：中上品相"},
    )
    report = loop.run()
    assert report.done is True
    assert planner.draft_seeds == [{"image_observation": "卡面外觀：中上品相"}]


def test_goal_loop_replan_receives_succeeded_step_outputs_as_seeds():
    """After a run whose result the judge rejects, the succeeded step outputs
    must reach the replanner as seed variables so the new plan can reuse them
    instead of re-executing the tools (the live rework bug)."""
    asking = _workflow_with_tool("asking_tool", wf_id="wf-ask")
    fixed = _workflow_with_tool("real_tool", wf_id="wf-fixed")
    planner = _FakePlanner(
        drafts=[(asking, None, False)],
        replans=[(fixed, None, False)],
    )
    executor = _FakeExecutor({
        "asking_tool": (True, "找到多個候選，請問您要哪一個？"),
        "real_tool": (True, "已完成播放"),
    })
    judge_calls = []

    def judge(goal: str, final_result: str):
        judge_calls.append(final_result)
        achieved = len(judge_calls) > 1
        return achieved, "" if achieved else "結果在反問使用者"

    loop = GoalLoop(
        goal="播放歌曲",
        planner=planner,
        executor=executor,
        command_registry={},
        replan_limit=1,
        result_judge=judge,
    )
    report = loop.run()
    assert report.done is True
    assert planner.replan_seeds[0].get("r1") == "找到多個候選，請問您要哪一個？"


def test_goal_loop_resume_carries_trace_variables_as_seeds():
    """A resumed run whose previous attempt failed mid-way must hand the
    already-bound variables to the replanner as seeds instead of losing them."""
    from openclaw_adapter.task_workspace import StepTrace, Variable

    broken = _workflow_with_tool("broken_tool", wf_id="wf-broken")
    fixed = _workflow_with_tool("real_tool", wf_id="wf-fixed")
    old_trace = WorkflowTrace(
        workflow_id="wf-broken",
        goal="完成任務",
        final_result="工作流在步驟 s2 失敗：boom",
    )
    old_trace.steps = [
        StepTrace(step_id="s1", kind="tool_call", status="ok"),
        StepTrace(step_id="s2", kind="tool_call", status="failed"),
    ]
    old_trace.variables = {
        "r1": Variable(name="r1", type="text", value="先前結果", source_step="s1", provenance="p"),
    }
    cont = GoalLoopContinuation(
        state=ContinuationState(
            goal="完成任務",
            completed=["draft: drafted wf-broken with 1 step(s)"],
            budget={"steps_used": 2, "steps_limit": 6},
            next_action="run_workflow",
            stop_condition="paused",
        ),
        workflow=broken,
        trace=old_trace,
        replans_used=0,
        narration=(),
    )
    planner = _FakePlanner(drafts=[], replans=[(fixed, None, False)])
    executor = _FakeExecutor({
        "broken_tool": (False, "boom"),
        "real_tool": (True, "done"),
    })
    loop = GoalLoop(
        goal="完成任務",
        planner=planner,
        executor=executor,
        command_registry={},
        replan_limit=1,
    )
    report = loop.run(resume=cont)
    assert report.done is True
    assert report.final_result == "done"
    assert planner.draft_calls == []  # no redraft on resume
    assert planner.replan_seeds and planner.replan_seeds[0].get("r1") == "先前結果"


def test_goal_loop_replan_seed_keeps_speech_text_type_for_saynow():
    """Regression for the live incident: a replan that carries a speech_text
    llm_transform output (e.g. a maid-voice report) forward as a seed must
    preserve that type, not silently degrade it to the generic default —
    otherwise the very next /saynow-style command_sink step rejects it with
    a type mismatch even though the value itself is perfectly valid speech
    text. Straight-line workflows (no replan) never exercised this path,
    which is why they always worked while replan-heavy goals broke."""
    from openclaw_adapter.task_workspace import StepTrace, Variable

    broken = _workflow_with_tool("broken_tool", wf_id="wf-broken")
    fixed = Workflow(
        id="wf-fixed",
        goal="goal",
        steps=[
            WorkflowStep(
                id="s1", kind="command_sink", command="/saynow",
                input="maid_report", output="r2",
            ),
        ],
    )
    old_trace = WorkflowTrace(
        workflow_id="wf-broken",
        goal="完成任務",
        final_result="工作流在步驟 s2 失敗：boom",
    )
    old_trace.steps = [
        StepTrace(step_id="s1", kind="llm_transform", status="ok"),
        StepTrace(step_id="s2", kind="tool_call", status="failed"),
    ]
    old_trace.variables = {
        "maid_report": Variable(
            name="maid_report", type="speech_text",
            value="おやすみなさいませ、ご主人様", source_step="s1", provenance="p",
        ),
    }
    cont = GoalLoopContinuation(
        state=ContinuationState(
            goal="完成任務",
            completed=["draft: drafted wf-broken with 2 step(s)"],
            budget={"steps_used": 2, "steps_limit": 6},
            next_action="run_workflow",
            stop_condition="paused",
        ),
        workflow=broken,
        trace=old_trace,
        replans_used=0,
        narration=(),
    )
    planner = _FakePlanner(drafts=[], replans=[(fixed, None, False)])
    executor = _FakeExecutor({"broken_tool": (False, "boom")})
    saynow_calls = []

    def saynow_handler(text: str, chat_id: str) -> str:
        saynow_calls.append(text)
        return "已朗讀"

    loop = GoalLoop(
        goal="完成任務",
        planner=planner,
        executor=executor,
        command_registry={"/saynow": _FakeRegistered(handler=saynow_handler)},
        replan_limit=1,
    )
    report = loop.run(resume=cont)
    assert report.done is True
    assert saynow_calls == ["おやすみなさいませ、ご主人様"]
