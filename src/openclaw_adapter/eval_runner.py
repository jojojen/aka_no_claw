"""Replayable eval harness for goal-loop behaviors (#54 phase 5)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .goal_loop import GoalLoop, GoalLoopContinuation, GoalLoopReport
from .task_workspace import (
    Workflow,
    WorkflowBudgetExhausted,
    WorkflowStep,
)


@dataclass
class EvalResult:
    case_id: str
    events: list[dict[str, Any]]
    runs: list[GoalLoopReport]
    counters: dict[str, int]


class _ScriptedPlanner:
    def __init__(self, case: dict[str, Any], counters: dict[str, int], events: list[dict[str, Any]]) -> None:
        self._drafts = [self._workflow_from_dict(item) for item in case.get("drafts", [])]
        self._replans = [self._workflow_from_dict(item) for item in case.get("replans", [])]
        self._counters = counters
        self._events = events

    @staticmethod
    def _workflow_from_dict(data: dict[str, Any]) -> tuple[Workflow | None, str | None, bool]:
        if data.get("workflow") is None:
            return None, str(data.get("error") or "no workflow"), False
        return Workflow.from_dict(data["workflow"]), data.get("error"), False

    def draft(self, goal: str):
        self._counters["draft_calls"] += 1
        self._events.append({"kind": "planner", "name": "draft", "goal": goal})
        return self._drafts.pop(0)

    def replan(self, goal: str, previous_workflow: Workflow, trace):
        self._counters["replan_calls"] += 1
        self._events.append(
            {
                "kind": "planner",
                "name": "replan",
                "goal": goal,
                "previous_workflow": previous_workflow.id,
                "error": trace.final_result,
            }
        )
        return self._replans.pop(0)


class _ScriptedExecutor:
    def __init__(self, case: dict[str, Any], counters: dict[str, int], events: list[dict[str, Any]]) -> None:
        self._responses = {
            item["slug"]: list(item.get("responses", []))
            for item in case.get("tool_responses", [])
        }
        self._events = events
        self._counters = counters
        self.client = _ScriptedLLM(case, counters, events)
        self.last_budget_exhausted = None

    def run_tool_step(self, slug: str, explicit_params: dict) -> tuple[bool, str]:
        self._counters["tool_calls"] += 1
        self._events.append({"kind": "tool_call", "name": slug, "args": dict(explicit_params)})
        responses = self._responses.get(slug)
        if not responses:
            raise AssertionError(f"missing scripted tool response for {slug}")
        current = responses.pop(0)
        self.last_budget_exhausted = None
        budget = current.get("budget_exhausted")
        if isinstance(budget, dict):
            self.last_budget_exhausted = WorkflowBudgetExhausted.from_dict(budget)
        return bool(current.get("ok", False)), str(current.get("text") or "")


class _ScriptedLLM:
    def __init__(self, case: dict[str, Any], counters: dict[str, int], events: list[dict[str, Any]]) -> None:
        self._outputs = list(case.get("llm_outputs", []))
        self._events = events
        self._counters = counters

    def generate(self, prompt: str, *, temperature: float = 0.0) -> str:
        self._counters["llm_calls"] += 1
        output = self._outputs.pop(0) if self._outputs else ""
        self._events.append({"kind": "llm_transform", "name": "generate", "output": output})
        return output


def _command_dispatcher(case: dict[str, Any], counters: dict[str, int], events: list[dict[str, Any]]):
    outputs = {
        item["command"]: list(item.get("responses", []))
        for item in case.get("command_responses", [])
    }
    dispatcher = {}
    for command in outputs:
        def _dispatch(text: str, *, _command=command) -> str:
            counters["command_calls"] += 1
            events.append({"kind": "command_sink", "name": _command, "input": text})
            queue = outputs[_command]
            if not queue:
                raise AssertionError(f"missing scripted command response for {_command}")
            return str(queue.pop(0))

        dispatcher[command] = _dispatch
    return dispatcher


def load_eval_case(path: str | Path) -> dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8")
    return json.loads(raw)


def run_eval_case(case: dict[str, Any]) -> EvalResult:
    events: list[dict[str, Any]] = []
    counters = {
        "draft_calls": 0,
        "replan_calls": 0,
        "tool_calls": 0,
        "command_calls": 0,
        "llm_calls": 0,
        "codegen_calls": int(case.get("codegen_calls") or 0),
    }
    planner = _ScriptedPlanner(case, counters, events)
    executor = _ScriptedExecutor(case, counters, events)
    loop = GoalLoop(
        goal=str(case["goal"]),
        planner=planner,
        executor=executor,
        command_registry={
            command: object() for command in {
                item["command"] for item in case.get("command_responses", [])
            }
        },
        llm_client=executor.client,
        max_steps=int(case["max_steps"]) if "max_steps" in case else 6,
        replan_limit=int(case["replan_limit"]) if "replan_limit" in case else 2,
    )
    dispatcher = _command_dispatcher(case, counters, events)
    loop.executor.client = executor.client
    runs: list[GoalLoopReport] = []

    # GoalLoop builds its own WorkflowRunner, so patch the registry shape it expects.
    loop.command_registry = {
        command: SimpleNamespace(handler=(lambda raw, _chat_id="", _fn=fn: _fn(raw)))
        for command, fn in dispatcher.items()
    }

    report = loop.run()
    runs.append(report)
    if case.get("resume_once") and report.continuation is not None:
        events.append({"kind": "continuation", "name": "resume", "next_action": report.continuation.state.next_action})
        report = loop.run(resume=report.continuation)
        runs.append(report)
    return EvalResult(case_id=str(case["id"]), events=events, runs=runs, counters=counters)


def assert_tool_called(result: EvalResult, slug: str) -> None:
    assert any(ev["kind"] == "tool_call" and ev["name"] == slug for ev in result.events)


def assert_tool_not_called(result: EvalResult, slug: str) -> None:
    assert not any(ev["kind"] == "tool_call" and ev["name"] == slug for ev in result.events)


def assert_tool_order(result: EvalResult, ordered: list[str]) -> None:
    actual = [ev["name"] for ev in result.events if ev["kind"] == "tool_call"]
    pos = 0
    for item in actual:
        if pos < len(ordered) and item == ordered[pos]:
            pos += 1
    assert pos == len(ordered), f"expected ordered subsequence {ordered!r}, got {actual!r}"


def assert_variable_exists(result: EvalResult, name: str) -> None:
    final = result.runs[-1]
    trace = final.trace
    assert trace is not None and name in trace.variables


def assert_final_state(result: EvalResult, expected: str) -> None:
    final = result.runs[-1]
    actual = "done" if final.done else "paused"
    assert actual == expected, f"expected final_state={expected!r}, got {actual!r}"


def assert_budget(result: EvalResult, key: str, expected: int) -> None:
    final = result.runs[-1]
    continuation = final.continuation
    assert continuation is not None
    assert int(continuation.state.budget.get(key) or 0) == expected


def assert_no_unsafe_command_execution(result: EvalResult, command: str) -> None:
    assert not any(ev["kind"] == "command_sink" and ev["name"] == command for ev in result.events)
