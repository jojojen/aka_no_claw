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

    def draft(self, goal: str):
        self.draft_calls.append(goal)
        return self._drafts.pop(0)

    def replan(self, goal: str, previous_workflow: Workflow, trace: WorkflowTrace):
        self.replan_calls.append((goal, previous_workflow.id, trace.final_result))
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
