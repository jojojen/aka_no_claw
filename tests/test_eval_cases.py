from __future__ import annotations

import copy
from pathlib import Path

import pytest

from openclaw_adapter.eval_runner import (
    assert_budget,
    assert_final_state,
    assert_no_unsafe_command_execution,
    assert_tool_called,
    assert_tool_order,
    load_eval_case,
    run_eval_case,
)


CASES_DIR = Path(__file__).resolve().parent.parent / "eval_cases"


def _assert_expected(case: dict, result) -> None:
    expected = case.get("expected", {})
    if "tool_called" in expected:
        assert_tool_called(result, expected["tool_called"])
    if "tool_order" in expected:
        assert_tool_order(result, list(expected["tool_order"]))
    if "final_state" in expected:
        assert_final_state(result, expected["final_state"])
    if "unsafe_command" in expected:
        assert_no_unsafe_command_execution(result, expected["unsafe_command"])
    if "budget" in expected:
        for key, value in expected["budget"].items():
            assert_budget(result, key, int(value))
    if "command_input" in expected:
        command = expected["command_input"]["command"]
        value = expected["command_input"]["input"]
        assert any(
            ev["kind"] == "command_sink"
            and ev["name"] == command
            and ev["input"] == value
            for ev in result.events
        )
    if "replan_calls" in expected:
        assert result.counters["replan_calls"] == int(expected["replan_calls"])
    if "codegen_calls" in expected:
        assert result.counters["codegen_calls"] == int(expected["codegen_calls"])
    if expected.get("first_run_paused"):
        assert len(result.runs) >= 2
        assert result.runs[0].done is False
        assert result.runs[0].continuation is not None


@pytest.mark.parametrize(
    "case_path",
    sorted(CASES_DIR.glob("*.yaml")),
    ids=lambda path: path.stem,
)
def test_eval_case_replays_twice(case_path: Path) -> None:
    case = load_eval_case(case_path)
    for _ in range(2):
        result = run_eval_case(copy.deepcopy(case))
        _assert_expected(case, result)


def test_eval_case_mutation_spot_check_fails_exactly_that_case() -> None:
    case = load_eval_case(CASES_DIR / "001_goal_multistep_happy.yaml")
    mutated = copy.deepcopy(case)
    mutated["expected"]["command_input"]["input"] = "這是一個錯的播報稿"

    with pytest.raises(AssertionError):
        _assert_expected(mutated, run_eval_case(copy.deepcopy(mutated)))

    # The original case still passes unchanged.
    _assert_expected(case, run_eval_case(copy.deepcopy(case)))
