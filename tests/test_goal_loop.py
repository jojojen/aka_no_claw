"""Tests for goal_loop.py (#54 phase 3 core library)."""

from __future__ import annotations

from dataclasses import dataclass

from openclaw_adapter.continuation_policy import operation_key
from openclaw_adapter.goal_loop import (
    GoalLoop,
    GoalLoopContinuation,
    _command_dispatcher_for_chat,
)
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


def test_goal_loop_conservative_synthesis_on_replan_exhaustion():
    """When the replan budget is spent with the goal still unmet, an injected
    conservative synthesizer turns gathered evidence + the last judge reason
    into a best-effort answer instead of aborting with a raw failure string."""
    workflow = _workflow_with_tool("partial_tool", wf_id="wf-partial")
    planner = _FakePlanner(drafts=[(workflow, None, False)])
    executor = _FakeExecutor({"partial_tool": (True, "市價約 ¥16000")})

    def judge(goal: str, final_result: str):
        return False, "只給了市價，未計算獲利"

    captured: dict = {}

    def synth(goal: str, seeds: dict, last_reason: str) -> str:
        captured["goal"] = goal
        captured["seeds"] = dict(seeds)
        captured["last_reason"] = last_reason
        return "保守結論：市價約¥16000，但尚未計入鑑定費，無法確認淨利。"

    loop = GoalLoop(
        goal="這張卡送鑑定轉賣會賺嗎",
        planner=planner,
        executor=executor,
        command_registry={},
        replan_limit=0,
        result_judge=judge,
        seed_variables={"prior_research_result": "查到成交紀錄若干"},
        conservative_synthesizer=synth,
    )
    report = loop.run()
    assert report.done is True
    assert "保守結論" in report.final_result
    assert captured["goal"] == "這張卡送鑑定轉賣會賺嗎"
    assert captured["last_reason"] == "只給了市價，未計算獲利"
    assert "prior_research_result" in captured["seeds"]


def test_goal_loop_exhaustion_without_synthesizer_keeps_raw_abort():
    """Without a synthesizer, the terminal-exhaustion path is byte-for-byte the
    old behaviour: done=False with the raw failure result (no regression)."""
    workflow = _workflow_with_tool("partial_tool", wf_id="wf-partial")
    planner = _FakePlanner(drafts=[(workflow, None, False)])
    executor = _FakeExecutor({"partial_tool": (True, "市價約 ¥16000")})

    def judge(goal: str, final_result: str):
        return False, "只給了市價"

    loop = GoalLoop(
        goal="會賺嗎",
        planner=planner,
        executor=executor,
        command_registry={},
        replan_limit=0,
        result_judge=judge,
    )
    report = loop.run()
    assert report.done is False
    assert report.final_result == "市價約 ¥16000"


def _research_command_workflow(wf_id: str) -> Workflow:
    return Workflow(
        id=wf_id,
        goal="goal",
        steps=[
            WorkflowStep(
                id="s1", kind="command_sink", command="/research",
                input="q", output="r1",
            ),
        ],
    )


def test_command_dispatcher_dedupes_repeated_operation():
    """The run-scoped dedup memo makes an expensive command run at most once per
    (command, input): a second identical call returns the cached artifact
    without re-invoking the handler, while a genuinely different input still
    runs (issue #81 — deterministic 'at most one /research', not a prompt hope)."""
    calls: list[str] = []

    def research_handler(text: str, chat_id: str) -> str:
        calls.append(text)
        return f"研究結果:{text}(#{len(calls)})"

    memo: dict[str, str] = {}
    reuse: list[str] = []
    dispatcher = _command_dispatcher_for_chat(
        {"/research": _FakeRegistered(handler=research_handler)},
        chat_id="c",
        executed_operations=memo,
        narrate=reuse.append,
    )
    first = dispatcher["/research"]("mercari m123")
    second = dispatcher["/research"]("  Mercari   M123 ")  # same op key
    third = dispatcher["/research"]("別張卡 x999")

    assert calls == ["mercari m123", "別張卡 x999"]  # duplicate never re-runs
    assert first == second  # cached artifact reused verbatim
    assert "別張卡 x999" in third
    assert any("略過重複操作" in line for line in reuse)
    assert operation_key("/research", "mercari m123") in memo


def test_command_dispatcher_without_memo_is_plain_passthrough():
    """No memo → no dedup: byte-for-byte the old behaviour so any caller that
    doesn't opt in (e.g. other multi-step flows) is unchanged."""
    calls: list[str] = []

    def handler(text: str, chat_id: str) -> str:
        calls.append(text)
        return "ok"

    dispatcher = _command_dispatcher_for_chat(
        {"/x": _FakeRegistered(handler=handler)}, chat_id="c"
    )
    dispatcher["/x"]("same")
    dispatcher["/x"]("same")
    assert calls == ["same", "same"]  # runs both times


def test_goal_loop_runs_research_at_most_once_across_replan():
    """#81 regression guard (the mocked E2E the acceptance review asked for):
    a first /research that the judge rejects as partial escalates into a replan,
    but the loop must NOT spend a second identical /research — the dedup memo
    returns the first artifact, so the command is invoked exactly once for the
    whole run while a final answer is still produced."""
    research_calls: list[str] = []

    def research_handler(text: str, chat_id: str) -> str:
        research_calls.append(text)
        return "市價約¥16000（未計鑑定費）"

    planner = _FakePlanner(
        drafts=[(_research_command_workflow("wf-a"), None, False)],
        replans=[(_research_command_workflow("wf-b"), None, False)],
    )
    judge_calls: list[str] = []

    def judge(goal: str, final_result: str):
        judge_calls.append(final_result)
        achieved = len(judge_calls) > 1
        return achieved, "" if achieved else "只有市價，未算獲利"

    loop = GoalLoop(
        goal="這張卡送鑑定會賺嗎",
        planner=planner,
        executor=_FakeExecutor({}),  # no tool_call steps in these workflows
        command_registry={"/research": _FakeRegistered(handler=research_handler)},
        replan_limit=1,
        result_judge=judge,
        seed_variables={"q": "mercari m123"},
    )
    report = loop.run()
    assert report.done is True
    assert report.replans_used == 1
    # First round ran /research once; the replan re-drafted the same /research
    # but it was served from the memo — invocation count stays exactly one.
    assert research_calls == ["mercari m123"]


def test_goal_loop_seed_operation_blocks_reentrant_research():
    """The chat tool that escalated already ran /research; passing its operation
    key + answer as a seed means a goal-loop workflow re-drafting the same
    /research reuses that answer instead of paying for it a second time —
    closing the 'seeded operation dedup enforcement' gap in the review."""
    research_calls: list[str] = []

    def research_handler(text: str, chat_id: str) -> str:
        research_calls.append(text)
        return "重新研究（不應發生）"

    planner = _FakePlanner(drafts=[(_research_command_workflow("wf"), None, False)])
    loop = GoalLoop(
        goal="會賺嗎",
        planner=planner,
        executor=_FakeExecutor({}),
        command_registry={"/research": _FakeRegistered(handler=research_handler)},
        seed_variables={"q": "mercari m123"},
        seed_operations={
            operation_key("/research", "mercari m123"): "先前研究：市價¥16000",
        },
    )
    report = loop.run()
    assert research_calls == []  # the seeded op is never re-run
    assert report.done is True
    assert report.final_result == "先前研究：市價¥16000"


def test_goal_loop_cancel_preempts_all_work():
    """#81 cooperative cancel: an explicit cancel signal stops the loop at the
    next stage boundary — it never drafts, never runs the workflow, and reports a
    terminal non-resumable stop (done=False, '任務已取消。'). This is the backend
    half of the user's 停止 button (an explicit cancel, not a stream-drop)."""
    research_calls: list[str] = []

    def research_handler(text: str, chat_id: str) -> str:
        research_calls.append(text)
        return "不應執行"

    planner = _FakePlanner(drafts=[(_research_command_workflow("wf"), None, False)])
    loop = GoalLoop(
        goal="會賺嗎",
        planner=planner,
        executor=_FakeExecutor({}),
        command_registry={"/research": _FakeRegistered(handler=research_handler)},
        seed_variables={"q": "mercari m123"},
        cancel_check=lambda: True,  # cancelled from the outset
    )
    report = loop.run()
    assert report.done is False
    assert report.final_result == "任務已取消。"
    assert planner.draft_calls == []   # never drafted
    assert research_calls == []        # never ran the command
    assert report.continuation is None  # terminal, not resumable


def test_goal_loop_cancel_does_not_trigger_conservative_synthesis():
    """A cancel is a stop, not a request for a hedged answer: even when a
    conservative synthesizer is wired in, cancelling must NOT invoke it — the
    report stays the terminal '任務已取消。' rather than a best-effort conclusion."""
    synth_calls: list[str] = []

    def synth(goal: str, seeds: dict, last_reason: str) -> str:
        synth_calls.append(goal)
        return "保守結論（不應出現）"

    planner = _FakePlanner(drafts=[(_workflow_with_tool("t", wf_id="wf"), None, False)])
    loop = GoalLoop(
        goal="會賺嗎",
        planner=planner,
        executor=_FakeExecutor({"t": (True, "市價¥16000")}),
        command_registry={},
        conservative_synthesizer=synth,
        cancel_check=lambda: True,
    )
    report = loop.run()
    assert report.done is False
    assert report.final_result == "任務已取消。"
    assert synth_calls == []  # synthesizer never consulted on cancel
