"""PR2 (#51): bounded troubleshoot-reflect-continue loop.

Two acceptance fixtures:
  * budget-continuation — pause into a compact ContinuationState at the step
    budget, then resume *at the next action* without re-running completed steps.
  * distillation — after a multi-attempt success, an abstract transferable
    lesson is collected on the result.
"""

from __future__ import annotations

from openclaw_adapter.task_loop import (
    BoundedTaskLoop,
    ContinuationState,
    LoopContext,
    StepOutcome,
    resume_loop,
    sequential_decider,
)


def _counting_step(name: str, calls: dict[str, int], *, observation: str = "ok",
                   done: bool = False, failed: bool = False, reflection: str = "",
                   lesson: str = ""):
    def step(ctx: LoopContext) -> StepOutcome:
        calls[name] = calls.get(name, 0) + 1
        return StepOutcome(
            observation=observation, done=done, failed=failed,
            reflection=reflection, lesson=lesson,
        )
    return step


def test_pauses_at_budget_and_emits_continuation_state():
    calls: dict[str, int] = {}
    plan = ["inspect", "search", "match", "play"]
    steps = {
        "inspect": _counting_step("inspect", calls, observation="listed tracks"),
        "search": _counting_step("search", calls, observation="found candidate"),
        "match": _counting_step("match", calls, observation="matched"),
        "play": _counting_step("play", calls, observation="playing", done=True),
    }
    loop = BoundedTaskLoop(
        "play the right track", steps=steps,
        decider=sequential_decider(plan), max_steps=2,
        constraints="allowlist only",
        extra_budget={"search_used": 1, "search_limit": 4},
    )

    result = loop.run()

    assert result.done is False
    assert result.stop_condition == "step budget reached"
    assert calls == {"inspect": 1, "search": 1}  # only 2 of 4 ran
    state = result.state
    assert isinstance(state, ContinuationState)
    assert state.next_action == "match"  # next, NOT re-run inspect
    assert state.completed == ["inspect: listed tracks", "search: found candidate"]
    assert state.budget["steps_used"] == 2
    assert state.budget["steps_limit"] == 2
    assert state.budget["search_used"] == 1
    # anchored-summary text is renderable and carries the resume anchor
    rendered = state.render()
    assert "Goal: play the right track" in rendered
    assert "Next: match" in rendered
    assert "Stop if: step budget reached" in rendered


def test_resume_continues_at_next_action_without_redoing_work():
    calls: dict[str, int] = {}
    plan = ["inspect", "search", "match", "play"]

    def build_loop(max_steps: int) -> BoundedTaskLoop:
        steps = {
            "inspect": _counting_step("inspect", calls, observation="listed tracks"),
            "search": _counting_step("search", calls, observation="found candidate"),
            "match": _counting_step("match", calls, observation="matched"),
            "play": _counting_step("play", calls, observation="playing", done=True),
        }
        return BoundedTaskLoop(
            "play the right track", steps=steps,
            decider=sequential_decider(plan), max_steps=max_steps,
        )

    first = build_loop(max_steps=2).run()
    assert first.done is False
    state = first.state

    # Round-trip the continuation state through dict (as a real resume would).
    state = ContinuationState.from_dict(state.to_dict())

    resumed = resume_loop(build_loop(max_steps=2), state)

    assert resumed.done is True
    assert resumed.stop_condition == "goal satisfied"
    # inspect/search ran once (first leg); match/play ran once (resume leg).
    assert calls == {"inspect": 1, "search": 1, "match": 1, "play": 1}


def test_distillation_collects_abstract_lesson_after_recovered_failure():
    calls: dict[str, int] = {}
    plan = ["attempt", "repair", "verify"]
    steps = {
        "attempt": _counting_step(
            "attempt", calls, observation="contract violated",
            failed=True, reflection="missing answer block",
        ),
        "repair": _counting_step(
            "repair", calls, observation="repaired output",
            lesson="always emit the answer block before returning",
        ),
        "verify": _counting_step(
            "verify", calls, observation="verified", done=True,
        ),
    }
    loop = BoundedTaskLoop(
        "produce a valid answer", steps=steps,
        decider=sequential_decider(plan), max_steps=5,
    )

    result = loop.run()

    assert result.done is True
    assert result.lessons == ["always emit the answer block before returning"]


def test_non_allowlisted_action_pauses_instead_of_executing():
    def rogue_decider(ctx: LoopContext) -> str:
        return "rm_rf_everything"

    loop = BoundedTaskLoop(
        "stay safe", steps={"noop": _counting_step("noop", {})},
        decider=rogue_decider, max_steps=3,
    )

    result = loop.run()

    assert result.done is False
    assert "non-allowlisted action" in result.stop_condition
    assert result.state is not None
    assert result.state.next_action == ""


def test_plan_exhausted_without_success_reports_distinct_stop():
    plan = ["only"]
    steps = {"only": _counting_step("only", {}, observation="did a thing")}
    loop = BoundedTaskLoop(
        "unreachable goal", steps=steps,
        decider=sequential_decider(plan), max_steps=5,
    )

    result = loop.run()

    assert result.done is False
    assert "plan exhausted" in result.stop_condition
