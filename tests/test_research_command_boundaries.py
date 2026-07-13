"""R3.0 compatibility pins for the research-pipeline extraction."""

from __future__ import annotations

import inspect

from openclaw_adapter import research_command


def test_research_public_surface_is_available() -> None:
    for name in (
        "ResearchCommandService",
        "ResearchBudget",
        "ResearchTarget",
        "ResearchReport",
        "parse_research_target",
        "build_research_handler",
        "format_research_full_report",
    ):
        assert hasattr(research_command, name), name


def test_budgeted_search_and_normalization_remain_free_functions() -> None:
    assert inspect.isfunction(research_command.build_budgeted_search_fn)
    assert inspect.isfunction(research_command.parse_research_target)
